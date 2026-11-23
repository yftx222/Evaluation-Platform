"""Detach telemetry from predictions and translate the frozen Agent v1 wire format."""

from typing import Any

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.catalog.store import row
from app.application.trace.scope import TRACE
from app.application.trace.store import append, finalize
from app.contracts.adapters import UnifiedResponse
from app.contracts.traces import (
    MAX_BATCH_BYTES,
    MAX_EVENT_BYTES,
    MAX_EVENTS,
    TraceBatch,
    TraceEventInput,
    TraceFinalize,
    canonical,
    check_fields,
)
from trace_sdk.privacy import redact


def detach(prediction: UnifiedResponse, slug: str, kind: str) -> tuple[Any, Any]:
    private = prediction.extensions.get(slug)
    envelope = private.pop("trace", None) if isinstance(private, dict) else None
    legacy = prediction.extensions.pop("_agent_events", None) if kind == "agent" else None
    return envelope, legacy


def convert(value: Any) -> TraceEventInput:
    if not isinstance(value, dict):
        raise ValueError("invalid event")
    if "source_event_key" in value:

        def obj(v: Any) -> dict[str, Any]:
            return v if isinstance(v, dict) else {} if v is None else {"value": v}

        attrs = {**obj(value.get("extensions")), **obj(value.get("metadata"))}
        for k in ("input_tokens", "output_tokens"):
            if value.get(k) is not None:
                attrs[k] = value[k]
        value = {
            "event_key": value["source_event_key"],
            "sequence": value.get("sequence"),
            "parent_event_key": value.get("parent_key"),
            "event_type": value.get("event_type"),
            "name": attrs.get("name", value.get("event_type")),
            "status": attrs.get("status", "UNKNOWN"),
            "started_at": value.get("occurred_at"),
            "duration_ms": value.get("duration_ms"),
            "attributes": attrs,
            "input": obj(value.get("input")),
            "output": obj(value.get("output")),
        }
    value = redact(value)
    check_fields(value)
    if len(canonical(value)) > MAX_EVENT_BYTES:
        raise ValueError("oversize event")
    return TraceEventInput.model_validate(value)


async def persist(db: AsyncSession, trace_id: object, envelope: Any, legacy: Any) -> None:
    declaration = envelope if isinstance(envelope, dict) else {}
    events = declaration.get("events", legacy)
    if not isinstance(events, list):
        return
    trace = await row(db, TRACE + " FOR UPDATE OF t", id=trace_id)
    valid = []
    incomplete = len(events) > MAX_EVENTS
    size = 0
    for raw in events[:MAX_EVENTS]:
        try:
            converted = convert(raw)
            size += len(canonical(converted.model_dump(mode="json"))) + 1
            if size + 16 > MAX_BATCH_BYTES:
                incomplete = True
                break
            valid.append(converted)
        except (ValueError, TypeError, RecursionError, OverflowError):
            incomplete = True
    if not valid:
        return
    await append(db, trace, TraceBatch(events=valid))
    trace = await row(db, TRACE, id=trace_id)
    try:
        end = TraceFinalize.model_validate(declaration.get("finalize"))
    except ValidationError:
        end = TraceFinalize(outcome="PARTIAL", contiguous=False, last_sequence=0)
    if incomplete:
        end.outcome = "PARTIAL"
    await finalize(db, trace, end)
