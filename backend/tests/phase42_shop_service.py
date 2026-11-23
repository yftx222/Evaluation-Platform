"""Opt-in HTTP facade around the ecommerce project's actual isolated Agent executor."""

import os
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, Request

from trace_sdk.client import TraceSession


def application() -> FastAPI:
    # The project's own interpreter supplies these packages in the opt-in smoke run.
    from ai_service.common.config import get_settings  # type: ignore[import-not-found]
    from ecommerce_eval.adapter import AgentAdapter  # type: ignore[import-not-found]
    from ecommerce_eval.data import Dataset  # type: ignore[import-not-found]
    from ecommerce_eval.trace import AttemptTrace, active_attempt  # type: ignore[import-not-found]

    api = FastAPI()
    settings = get_settings()
    agent = AgentAdapter(settings)

    @api.get("/ready")
    def ready() -> dict[str, bool]:
        return {"ready": True}

    @api.post("/ask")
    async def ask(req: Request) -> dict[str, Any]:
        session = TraceSession.from_headers(
            os.environ["TRACE_ACCEPTANCE_URL"], req.headers, timeout=85
        )
        assert session
        query = (await req.json())["query"]
        rid = uuid4().hex
        audit = AttemptTrace(rid, "phase42-synthetic", "isolated-test", {})
        mark = active_attempt.set(audit)
        root = session.add("AGENT_STEP", "客服处理公开问候", status="STARTED")
        try:
            request = Dataset.make_request({"userInput": query, "history": []}, rid)
            with agent.install():
                answer = await agent.execute(request, audit)
            for call in audit.calls:
                kind = {"MODEL": "MODEL_CALL", "TOOL": "TOOL_CALL"}.get(
                    call.record_type, "AGENT_STEP"
                )
                session.add(
                    kind,
                    call.tool_name,
                    parent_event_key=root,
                    started_at=call.created_at.isoformat(),
                    duration_ms=round(call.latency_ms) if call.latency_ms is not None else None,
                    status="UNKNOWN"
                    if call.success is None
                    else "SUCCEEDED"
                    if call.success
                    else "FAILED",
                    attributes={"model": settings.llm_model}
                    if kind == "MODEL_CALL"
                    else {"tool_name": call.tool_name},
                )
            session.add(
                "FINAL_OUTPUT",
                "返回答案",
                parent_event_key=root,
                status="SUCCEEDED",
                output={"output_ref": "prediction.answer"},
            )
            await session.finish()
            return {
                "final_answer": answer.reply_content,
                "trace_status": session.status,
                "model_calls": sum(c.record_type == "MODEL" for c in audit.calls),
            }
        finally:
            active_attempt.reset(mark)
            session.finish(complete=False)

    return api
