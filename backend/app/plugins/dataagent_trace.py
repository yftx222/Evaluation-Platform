"""DataAgent build 2. Bind all turns to one attempt and seal after artifact verification."""

import asyncio
import json
from contextlib import suppress
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

import httpx

from app.contracts.adapters import UnifiedRequest, UnifiedResponse
from app.infrastructure.attempt_http import Wire, active
from app.plugins.dataagent import DataAgentEvaluationAdapter, Session


@dataclass
class Capture:
    wire: Wire
    session: Session | None = None
    conversation: UUID | None = None
    turns: int = 0

    async def headers(self, req: httpx.Request) -> None:
        if req.method != "POST" or req.url.path not in {
            "/api/v1/chat/stream",
            "/_eval/trace/finish",
        }:
            return
        if req.url.path.endswith("/stream"):
            self.conversation = UUID(json.loads(req.content)["conversation_id"])
            self.turns += 1
        req.headers["X-Trace-Id"] = str(self.wire.trace_id)
        req.headers["X-Trace-Write"] = self.wire.trace_token or ""
        req.headers["X-Eval-Turn"] = str(self.turns)


current: ContextVar[Capture | None] = ContextVar("dataagent_capture", default=None)


class TracedDataAgentAdapter(DataAgentEvaluationAdapter):
    async def keep_credential(self, wire: Wire) -> None:
        # TraceWrite credentials expire after five minutes. An active lease can rotate
        # them while the target is still working, before the heartbeat seals the trace.
        while wire.renew_trace:
            await asyncio.sleep(120)
            try:
                wire.trace_token = await wire.renew_trace()
            except Exception:
                return

    async def session(self, client: httpx.AsyncClient) -> Session:
        session = await super().session(client)
        capture = current.get()
        if capture:
            capture.session = session
            client.event_hooks["request"].append(capture.headers)
        return session

    async def execute(self, request: UnifiedRequest) -> UnifiedResponse:
        wire = active.get()
        if not wire or not wire.trace_id or not wire.trace_token:
            return await super().execute(request)
        capture = Capture(wire)
        binding = current.set(capture)
        keeper = asyncio.create_task(self.keep_credential(wire))
        complete = False
        try:
            reply = await super().execute(request)
            complete = True
            return reply
        finally:
            current.reset(binding)
            keeper.cancel()
            with suppress(asyncio.CancelledError):
                await keeper
            task = asyncio.current_task()
            budget = min(8.0, (request.deadline_at - datetime.now(UTC)).total_seconds() - 0.5)
            if (
                capture.conversation
                and capture.session
                and task
                and not task.cancelling()
                and budget > 0
            ):
                try:
                    async with (
                        asyncio.timeout(budget),
                        httpx.AsyncClient(
                            trust_env=False, event_hooks={"request": [capture.headers]}
                        ) as client,
                    ):
                        if wire.renew_trace:
                            wire.trace_token = await wire.renew_trace()
                        capture.session.client = client
                        await capture.session.call(
                            "POST",
                            "/_eval/trace/finish",
                            json={
                                "conversation_id": str(capture.conversation),
                                "complete": complete,
                            },
                        )
                except Exception:
                    # The worker closes an unconfirmed trace as PARTIAL/MISSING.
                    pass
