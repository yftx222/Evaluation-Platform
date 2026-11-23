"""A bounded stream of database snapshots. Redis only wakes an idle reader."""

import asyncio
import json
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import UUID, uuid4

from fastapi import APIRouter, Header, Request
from fastapi.responses import StreamingResponse
from redis.exceptions import RedisError
from sqlalchemy.exc import SQLAlchemyError

from app.api.access import Actor, Db, authenticate, authorize
from app.application.catalog.store import Fault, row
from app.application.runner.runs import RUN
from app.infrastructure.resources import Resources

router = APIRouter(prefix="/api/v1/evaluations", tags=["evaluations"])
COUNTS = ("pending", "running", "succeeded", "failed", "timed_out", "cancelled", "unknown_delivery")
TERMINAL = {"COMPLETED", "FAILED", "PARTIAL_FAILED", "CANCELLED"}


@router.get("/runs/{r}/progress")
async def progress(
    req: Request,
    db: Db,
    who: Actor,
    r: UUID,
    last_event_id: str | None = Header(None, max_length=160),
) -> StreamingResponse:
    project = (await row(db, "SELECT project_id FROM evaluation_runs WHERE id=:r", r=r))[
        "project_id"
    ]
    await authorize(db, who, project, "R", key_read=True, key_scope="results:read")
    resources: Resources = req.app.state.resources

    async def stream() -> AsyncIterator[str]:
        previous: dict[str, object] = {}
        completed = 0
        heartbeat = time.monotonic()
        subscribed = False
        async with resources.redis.pubsub(ignore_subscribe_messages=True) as sub:
            try:
                async with asyncio.timeout(0.3):
                    await sub.subscribe(f"evaluations:progress:{r}")
                    subscribed = True
            except (RedisError, OSError, TimeoutError):
                pass
            while not await req.is_disconnected():
                try:
                    async with resources.transaction() as session:
                        actor = await authenticate(req, session)
                        await authorize(
                            session, actor, project, "R", key_read=True, key_scope="results:read"
                        )
                        current = await row(session, RUN + " WHERE r.id=:r", r=r)
                except (Fault, SQLAlchemyError, OSError, TimeoutError):
                    return
                counts = {k: current[k + "_cases"] for k in COUNTS}
                status = current["status"]
                state = {"status": status, "total": current["total_cases"], **counts}
                finished = state["total"] - counts["pending"] - counts["running"]
                kinds = []
                if previous.get("status") != status:
                    kinds.append("run.status")
                if previous and finished > completed:
                    kinds.append("case.completed")
                if state != previous:
                    kinds.append("run.progress")
                if status in TERMINAL:
                    kinds.append("run.finished")
                for kind in kinds:
                    event_id = uuid4().hex
                    payload = {
                        "event_id": event_id,
                        "run_id": str(r),
                        "timestamp": datetime.now(UTC).isoformat(),
                        **state,
                        "resync": bool(last_event_id and not previous),
                    }
                    if kind == "case.completed":
                        payload["completed_delta"] = finished - completed
                    yield f"id: {event_id}\nevent: {kind}\ndata: {json.dumps(payload)}\n\n"
                if status in TERMINAL:
                    return
                previous, completed = state, finished
                if time.monotonic() - heartbeat >= 10:
                    yield ": heartbeat\n\n"
                    heartbeat = time.monotonic()
                if subscribed:
                    try:
                        await sub.get_message(timeout=1.5)
                        await asyncio.sleep(0.25)
                        continue
                    except (RedisError, OSError, TimeoutError):
                        subscribed = False
                await asyncio.sleep(1.5)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )
