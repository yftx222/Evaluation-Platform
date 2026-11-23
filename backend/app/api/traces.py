import json
from datetime import timedelta
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import AwareDatetime, ValidationError
from sqlalchemy import text

from app.api.access import Actor, Db, authorize
from app.api.projects import reply
from app.application.catalog.store import Fault, row
from app.application.trace import store
from app.application.trace.scope import TRACE, invalid_token, verify, write_window
from app.application.trace.store import BatchFault
from app.contracts.traces import (
    MAX_BATCH_BYTES,
    MAX_EVENT_BYTES,
    MAX_EVENTS,
    EventType,
    TraceBatch,
    TraceFinalize,
    canonical,
    check_fields,
)

router = APIRouter(prefix="/api/v1", tags=["traces"])
bearer = HTTPBearer(scheme_name="AttemptTraceWrite", auto_error=False)


async def token(
    req: Request, auth: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]
) -> str:
    if (
        not auth
        or len(auth.credentials) > 128
        or req.app.state.settings.mode == "production"
        and req.url.scheme != "https"
    ):
        raise invalid_token()
    async with req.app.state.resources.transaction() as session:
        await verify(session, auth.credentials)
    return auth.credentials


Token = Annotated[str, Depends(token)]


def unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value = dict(pairs)
    if len(value) != len(pairs):
        raise ValueError("duplicate JSON key")
    return value


async def read(req: Request) -> Any:
    raw = bytearray()
    async for chunk in req.stream():
        if len(raw) + len(chunk) > MAX_BATCH_BYTES:
            raise BatchFault(413, "BATCH_TOO_LARGE", None)
        raw.extend(chunk)
    try:
        return json.loads(
            raw.decode("utf-8"), object_pairs_hook=unique_keys, parse_constant=reject_constant
        )
    except (ValueError, RecursionError):
        raise BatchFault(422, "INVALID_JSON", None) from None


def reject_constant(value: str) -> None:
    raise ValueError("invalid JSON number")


def request_schema(model: type[TraceBatch] | type[TraceFinalize]) -> dict[str, Any]:
    schema = model.model_json_schema()
    # Inline local definitions before embedding the requestBody in the OpenAPI document.
    definitions = schema.pop("$defs", {})

    def inline(value: Any) -> Any:
        if isinstance(value, list):
            return [inline(v) for v in value]
        if isinstance(value, dict):
            if "$ref" in value:
                return inline(definitions[value["$ref"].split("/")[-1]])
            return {k: inline(v) for k, v in value.items()}
        return value

    return {
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": inline(schema)}},
        }
    }


@router.post("/trace-write/events:batch", openapi_extra=request_schema(TraceBatch))
async def batch(req: Request, db: Db, credential: Token) -> dict[str, object]:
    payload = await read(req)
    events = payload.get("events") if isinstance(payload, dict) else None
    count = len(events) if isinstance(events, list) else None
    if isinstance(events, list) and not events:
        raise BatchFault(422, "EMPTY_BATCH", 0)
    if count is not None and count > MAX_EVENTS:
        raise BatchFault(413, "BATCH_TOO_LARGE", count)
    if isinstance(events, list):
        for i, event in enumerate(events):
            try:
                check_fields(event)
                if len(canonical(event)) > MAX_EVENT_BYTES:
                    raise BatchFault(413, "EVENT_TOO_LARGE", count, i)
            except (ValueError, RecursionError) as exc:
                code = (
                    str(exc)
                    if str(exc) in {"SENSITIVE_FIELD", "UNSAFE_VALUE", "FIELD_DEPTH"}
                    else "INVALID_EVENT"
                )
                raise BatchFault(422, code, count, i) from None
    try:
        body = TraceBatch.model_validate(payload)
    except ValidationError as exc:
        loc = exc.errors(include_input=False)[0]["loc"]
        index = loc[1] if len(loc) > 1 and isinstance(loc[1], int) else None
        raise BatchFault(422, "INVALID_EVENT", count, index) from None
    async with write_window(db, credential) as trace:
        result = await store.append(db, trace, body)
    req.state.trace_counts = {k: result[k] for k in ("accepted", "duplicate", "rejected")}
    return reply(req, result)


@router.post("/trace-write/finalize", openapi_extra=request_schema(TraceFinalize))
async def finalize(req: Request, db: Db, credential: Token) -> dict[str, object]:
    try:
        body = TraceFinalize.model_validate(await read(req))
    except ValidationError:
        raise Fault(422, "INVALID_FINALIZE", "请填写结束状态、连续序号声明和最后序号") from None
    async with write_window(db, credential) as trace:
        result = await store.finalize(db, trace, body)
    return reply(req, result)


async def readable(db: Db, who: Actor, t: UUID) -> dict[str, Any]:
    header = await row(db, TRACE, id=t)
    await authorize(db, who, header["project_id"], "R", key_read=True, key_scope="results:read")
    if await db.scalar(text("SELECT 1 FROM trace_archives WHERE trace_id=:id"), {"id": t}):
        raise Fault(410, "TRACE_ARCHIVED", "轨迹已归档")
    return header


@router.get("/traces/{t}")
async def summary(req: Request, db: Db, who: Actor, t: UUID) -> dict[str, object]:
    header = await readable(db, who, t)
    days = req.app.state.settings.trace_retention_days
    header["archive_eligible_at"] = (
        header["ended_at"] + timedelta(days=days) if header["ended_at"] and days else None
    )
    return reply(req, header)


@router.get("/traces/{t}/events")
async def events(
    req: Request,
    db: Db,
    who: Actor,
    t: UUID,
    after_sequence: Annotated[int, Query(ge=0, le=2**63 - 1)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    event_type: EventType | None = None,
    parent_id: UUID | None = None,
    roots: bool = False,
    started_from: AwareDatetime | None = None,
    started_to: AwareDatetime | None = None,
    summary_only: bool = False,
) -> dict[str, object]:
    await readable(db, who, t)
    if started_from and started_to and started_from > started_to:
        raise Fault(422, "INVALID_INTERVAL", "起点必须早于终点")
    if roots and parent_id:
        raise Fault(422, "INVALID_PARENT", "根节点与指定父节点不能同时筛选")
    columns = """e.id,e.trace_id,e.event_key,e.sequence,e.parent_event_key,e.parent_event_id,
        e.event_type,e.name,e.status,e.started_at,e.ended_at,e.duration_ms,e.received_at,
        e.schema_version, EXISTS(SELECT 1 FROM trace_events child WHERE child.trace_id=e.trace_id
         AND child.parent_event_id=e.id) AS has_children,
        CASE WHEN jsonb_typeof(e.attributes->'input_tokens')='number'
          AND e.attributes->>'input_tokens' ~ '^[0-9]{1,15}$'
          THEN (e.attributes->>'input_tokens')::bigint END AS input_tokens,
        CASE WHEN jsonb_typeof(e.attributes->'output_tokens')='number'
          AND e.attributes->>'output_tokens' ~ '^[0-9]{1,15}$'
          THEN (e.attributes->>'output_tokens')::bigint END AS output_tokens"""
    if not summary_only:
        columns += ",e.input,e.output,e.attributes,e.resource"
    sql = f"SELECT {columns} FROM trace_events e WHERE e.trace_id=:t AND e.sequence>:after"
    params: dict[str, Any] = {"t": t, "after": after_sequence, "n": limit + 1}
    for clause, key, value in [
        ("e.event_type=:type", "type", event_type),
        ("e.parent_event_id=:p", "p", parent_id),
        ("e.started_at>=:start", "start", started_from),
        ("e.started_at<=:end", "end", started_to),
    ]:
        if value is not None:
            sql += " AND " + clause
            params[key] = value
    if roots:
        sql += " AND e.parent_event_id IS NULL"
    rows = (await db.execute(text(sql + " ORDER BY e.sequence LIMIT :n"), params)).mappings().all()
    return reply(
        req,
        {
            "items": [dict(r) for r in rows[:limit]],
            "next_sequence": rows[limit - 1]["sequence"] if len(rows) > limit else None,
        },
    )


@router.get("/traces/{t}/events/{event_id}")
async def event_detail(
    req: Request, db: Db, who: Actor, t: UUID, event_id: UUID
) -> dict[str, object]:
    await readable(db, who, t)
    detail = await row(
        db, "SELECT * FROM trace_events WHERE trace_id=:t AND id=:e", t=t, e=event_id
    )
    detail.pop("content_digest")
    return reply(req, detail)
