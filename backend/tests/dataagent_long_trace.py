"""Explicit wall-clock check for the five-minute credential expiry seen in D2 acceptance."""

import asyncio
import time
from contextlib import suppress
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from test_trace_write import Window, event
from test_trace_write import window as window

from app.application.access.identity import DevelopmentSecretResolver
from app.contracts.http import AdapterConfig
from app.contracts.traces import BoundTraceWriteScope
from app.infrastructure.attempt_http import Wire
from app.infrastructure.resources import Resources
from app.plugins.dataagent_trace import TracedDataAgentAdapter
from app.plugins.http_adapter import DestinationPolicy
from app.worker.scheduler import renew
from app.worker.trace import AttemptTraceIssuer


def test_open_trace_outlives_original_credential(
    window: Window, request: pytest.FixtureRequest
) -> None:
    async def exercise() -> None:
        lease = window.lease
        assert lease.attempt
        scope = BoundTraceWriteScope(
            project_id=lease.project,
            run_id=lease.run,
            execution_id=lease.case,
            attempt_id=lease.attempt,
            epoch=lease.epoch,
            trace_id=window.trace,
        )
        resources = Resources(window.settings)
        issuer = AttemptTraceIssuer(resources)
        deadline = datetime.now(UTC) + timedelta(minutes=7)

        async def before() -> None:
            pass

        wire = Wire(before, "long-trace", trace_id=window.trace)
        count = 0

        async def rotate() -> str:
            nonlocal count
            token = await issuer.issue(scope, deadline, previous=wire.trace_token)
            count += 1
            return token

        adapter = TracedDataAgentAdapter(
            AdapterConfig(
                kind="dataagent",
                build="2",
                endpoint="http://127.0.0.1/api/v1/chat/stream",
                probe_endpoint="http://127.0.0.1/_eval/status",
                secret_ref="unused",
                secret_version="v1",
                options={"role": "demo", "fixture_version": "test"},
            ),
            DestinationPolicy([], True),
            DevelopmentSecretResolver({}),
            "test",
        )
        started = time.monotonic()
        try:
            wire.trace_token = original = await issuer.issue(scope, deadline, previous=window.token)
            wire.renew_trace = rotate
            async with resources.transaction() as db:
                expires = await db.scalar(
                    text("SELECT trace_expires_at FROM execution_attempts WHERE id=:a"),
                    {"a": lease.attempt},
                )
            assert expires is not None
            keeper = asyncio.create_task(adapter.keep_credential(wire))
            try:
                async with asyncio.timeout(320):
                    while True:
                        assert await renew(resources, lease, 60)
                        async with resources.transaction() as db:
                            left = await db.scalar(
                                text(
                                    "SELECT extract(epoch FROM "
                                    "CAST(:expiry AS timestamptz)-clock_timestamp())"
                                ),
                                {"expiry": expires},
                            )
                        if float(left) <= 0:
                            break
                        await asyncio.sleep(min(float(left), 15))
                assert count >= 2 and wire.trace_token and wire.trace_token != original
                assert not await issuer.valid(scope, original)
                assert await issuer.valid(scope, wire.trace_token)
                window.token = wire.trace_token
                assert window.write(event(1)).json()["data"]["accepted"] == 1
                assert window.close(1).json()["data"]["status"] == "COMPLETE"
                request.node.user_properties.extend(
                    [
                        ("rotations", count),
                        ("elapsed_seconds", round(time.monotonic() - started, 2)),
                    ]
                )
            finally:
                keeper.cancel()
                with suppress(asyncio.CancelledError):
                    await keeper
        finally:
            await resources.close()

    asyncio.run(exercise())
