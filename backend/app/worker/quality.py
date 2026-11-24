"""Run quality housekeeping independently of case leases and target requests."""

import asyncio
import json
import logging
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import httpx
from sqlalchemy import text

from app.application.runner.inspections import inspections
from app.application.runner.regression import finish_batch
from app.infrastructure.resources import Resources, Settings
from app.worker.retry import retry_after

log = logging.getLogger(__name__)


async def sweep(resources: Resources) -> None:
    async with resources.transaction() as db:
        await db.execute(
            text("""WITH due AS (SELECT id FROM production_traces WHERE completeness='OPEN'
            AND deadline<=now() ORDER BY deadline LIMIT 100 FOR UPDATE SKIP LOCKED)
            UPDATE production_traces t SET completeness=CASE WHEN event_count=0 THEN 'MISSING'
            ELSE 'PARTIAL' END,
            updated_at=now() FROM due WHERE t.id=due.id""")
        )
        await db.execute(
            text("""WITH due AS (SELECT id FROM production_traces WHERE NOT pinned
            AND retained_until<=now() AND completeness<>'OPEN' AND availability IN ('AVAILABLE',
            'ARCHIVED')
            ORDER BY retained_until LIMIT 100 FOR UPDATE SKIP LOCKED), expired AS (
            UPDATE production_traces t SET availability='EXPIRED' FROM due WHERE t.id=due.id
            RETURNING t.id),
            cold AS (DELETE FROM production_trace_cold_events e USING expired WHERE
            e.trace_id=expired.id)
            DELETE FROM production_trace_events e USING expired WHERE e.trace_id=expired.id""")
        )
    async with resources.transaction() as db:
        item = (
            (
                await db.execute(
                    text("""SELECT b.* FROM regression_batches b WHERE report IS NULL AND
            NOT EXISTS(SELECT 1 FROM regression_trials t JOIN evaluation_runs r ON r.id=t.run_id
            WHERE t.batch_id=b.id AND r.status NOT IN ('COMPLETED','PARTIAL_FAILED','FAILED',
            'CANCELLED'))
            ORDER BY created_at LIMIT 1 FOR UPDATE OF b SKIP LOCKED""")
                )
            )
            .mappings()
            .first()
        )
        if item:
            await finish_batch(db, dict(item))


async def deliver(resources: Resources, settings: Settings) -> bool:
    if not settings.quality_receivers:
        return False
    lease = uuid4()
    async with resources.transaction() as db:
        delivery = (
            (
                await db.execute(
                    text("""WITH due AS (SELECT id FROM quality_outbox
            WHERE state IN ('PENDING','SENDING') AND next_at<=now()
            AND (CAST(:scopes AS jsonb)->project_id::text) ? (event#>>'{project,environment}')
            ORDER BY next_at LIMIT 1 FOR UPDATE SKIP LOCKED)
            UPDATE quality_outbox q SET lease=:lease,state='SENDING',attempts=attempts+1,
            next_at=now()+interval '30 seconds' FROM due WHERE q.id=due.id RETURNING q.*"""),
                    {
                        "scopes": json.dumps(
                            {str(p): list(envs) for p, envs in settings.quality_receivers.items()}
                        ),
                        "lease": lease,
                    },
                )
            )
            .mappings()
            .first()
        )
    if delivery is None:
        return False
    config = settings.quality_receivers[delivery["project_id"]][
        delivery["event"]["project"]["environment"]
    ]
    state = "PENDING"
    receipt: dict[str, Any] | None = None
    error = None
    delay = min(300, 2 ** min(delivery["attempts"], 8))
    try:
        async with (
            asyncio.timeout(8),
            httpx.AsyncClient(follow_redirects=False, trust_env=False, timeout=5) as client,
        ):
            res = await client.post(
                config.endpoint,
                headers={"Authorization": "Bearer " + config.credential.get_secret_value()},
                json=delivery["event"],
            )
            if res.status_code == 202:
                data = res.json()["data"]
                if data["event_id"] != str(delivery["id"]) or data["status"] != "RECEIVED":
                    raise ValueError("invalid receipt")
                datetime.fromisoformat(data["received_at"])
                receipt = {k: data[k] for k in ("event_id", "status", "received_at")}
                state = "DELIVERED"
            else:
                error = f"HTTP_{res.status_code}"
                hint = retry_after(res.headers.get("Retry-After"), datetime.now(UTC))
                if hint is not None:
                    delay = max(delay, int(hint) + 1)
                if (res.status_code != 429 and res.status_code < 500) or delay > 86400:
                    state = "FAILED"
    except (httpx.HTTPError, TimeoutError, ValueError, KeyError, TypeError) as exc:
        error = type(exc).__name__
    if state == "PENDING" and delivery["attempts"] >= config.max_attempts:
        state = "FAILED"
    async with resources.transaction() as db:
        await db.execute(
            text("""UPDATE quality_outbox SET state=:s,next_at=now()+make_interval(secs=>:delay),
            receipt=CAST(:receipt AS jsonb),last_error=:err,lease=NULL WHERE id=:id AND
            lease=:lease"""),
            dict(
                s=state,
                delay=min(delay, 86400),
                receipt=json.dumps(receipt) if receipt else None,
                err=error,
                id=delivery["id"],
                lease=lease,
            ),
        )
    return True


async def maintain_quality(resources: Resources, settings: Settings, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            async with resources.transaction() as db:
                ready = await db.scalar(text("SELECT to_regclass('quality_outbox') IS NOT NULL"))
            if ready:
                await sweep(resources)
                await deliver(resources, settings)
                async with resources.transaction() as db:
                    online = await db.scalar(
                        text("SELECT to_regclass('inspection_plans') IS NOT NULL")
                    )
                if online:
                    await inspections(resources, settings)
        except Exception as exc:
            log.warning("quality maintenance interrupted: %s", type(exc).__name__)
        try:
            await asyncio.wait_for(stop.wait(), 2)
        except TimeoutError:
            pass
