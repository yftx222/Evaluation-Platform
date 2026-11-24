import base64
import json
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Query, Request
from pydantic import AwareDatetime, ValidationError
from sqlalchemy import text

from app.api.access import Db, User, authorize
from app.api.projects import reply
from app.api.quality_access import Grant, require
from app.api.traces import reject_constant, unique_keys
from app.application.catalog.store import Fault, audit, digest, page, row
from app.contracts.quality import ProductionBatch, ProductionFinal, ProductionRoot, TracePolicy
from app.contracts.traces import EventType, canonical, check_fields
from app.infrastructure.resources import Settings

router = APIRouter(prefix="/api/v1", tags=["production-traces"])
Limit = Annotated[int, Query(ge=1, le=100)]


def policy(req: Request, grant: Grant) -> TracePolicy:
    cfg: Settings = req.app.state.settings
    return cfg.production_trace_policies.get(grant.project_id, {}).get(
        grant.environment, TracePolicy()
    )


async def bounded_json(req: Request, limit: int) -> Any:
    raw = bytearray()
    async for part in req.stream():
        if len(raw) + len(part) > limit:
            raise Fault(413, "BATCH_TOO_LARGE", "批次超过配置上限")
        raw.extend(part)
    try:
        return json.loads(
            raw.decode(), object_pairs_hook=unique_keys, parse_constant=reject_constant
        )
    except (ValueError, RecursionError) as exc:
        raise Fault(422, "INVALID_JSON", "无效 JSON") from exc


async def visible(db: Db, g: Grant, tid: UUID, *, lock: bool = False) -> dict[str, Any]:
    return await row(
        db,
        "SELECT * FROM production_traces WHERE id=:t AND project_id=:p AND environment=:e"
        + (" FOR UPDATE" if lock else ""),
        t=tid,
        p=g.project_id,
        e=g.environment,
    )


async def register(db: Db, g: Grant, root: ProductionRoot, limits: TracePolicy) -> dict[str, Any]:
    require(g, "trace:write", root.environment)
    data = root.model_dump(mode="json", exclude_unset=True)
    # session_id is an opaque business reference here. Still check its value.
    for value in data.values():
        check_fields(value)
    await db.execute(
        text("""INSERT INTO production_traces(id,project_id,environment,agent_run_id,root,
            deadline,retained_until)
        VALUES (:id,:p,:env,:run,CAST(:root AS jsonb),:due,:until) ON CONFLICT DO NOTHING"""),
        {
            "id": root.trace_id,
            "p": g.project_id,
            "env": root.environment,
            "run": root.agent_run_id,
            "root": json.dumps(data),
            "due": datetime.now(UTC) + timedelta(seconds=limits.idle_seconds),
            "until": datetime.now(UTC) + timedelta(days=limits.retention_days)
            if limits.retention_days
            else None,
        },
    )
    trace = await row(
        db,
        """SELECT * FROM production_traces WHERE project_id=:p AND environment=:e
        AND (agent_run_id=:r OR id=:id) ORDER BY (id=:id) DESC LIMIT 1 FOR UPDATE""",
        p=g.project_id,
        e=g.environment,
        r=root.agent_run_id,
        id=root.trace_id,
    )
    if trace["root"] != data:
        raise Fault(409, "TRACE_ID_CONFLICT", "该轨迹已绑定其他执行信息")
    return trace


@router.post("/production-trace-write/register")
async def start(req: Request, db: Db, g: Grant, body: ProductionRoot) -> dict[str, object]:
    try:
        trace = await register(db, g, body, policy(req, g))
    except ValueError as exc:
        raise Fault(422, "SENSITIVE_FIELD", "运行元数据需要脱敏") from exc
    return reply(req, {"trace_id": trace["id"], "completeness": trace["completeness"]})


@router.post("/production-trace-write/events:batch")
async def append(req: Request, db: Db, g: Grant) -> dict[str, object]:
    require(g, "trace:write")
    limits = policy(req, g)
    try:
        body = ProductionBatch.model_validate(await bounded_json(req, limits.max_batch_bytes))
        if len(body.events) > limits.max_batch_events:
            raise Fault(413, "BATCH_TOO_LARGE", "事件数量超过上限")
        trace = await register(db, g, body.trace, limits)
    except (ValueError, RecursionError) as exc:
        raise Fault(422, "INVALID_EVENT", "运行或事件格式有误") from exc
    cfg: Settings = req.app.state.settings
    version = cfg.production_trace_key_version
    key = cfg.production_trace_keys.get(version or "")
    if not version or not key or len(key.get_secret_value()) < 32:
        raise Fault(503, "PAYLOAD_KEY_UNAVAILABLE", "证据加密密钥不可用")
    accepted = 0
    for event in body.events:
        data = event.model_dump(mode="json", exclude_none=True)
        try:
            check_fields(data)
        except (ValueError, RecursionError) as exc:
            raise Fault(422, "SENSITIVE_FIELD", "事件含有敏感字段") from exc
        if len(canonical(data)) > limits.max_event_bytes:
            raise Fault(413, "EVENT_TOO_LARGE", "事件超过大小限制")
        checksum = digest(data)
        params = {"t": trace["id"], "k": event.event_key, "seq": event.sequence}
        prior = (
            (
                await db.execute(
                    text("""SELECT digest FROM production_trace_events
            WHERE trace_id=:t AND (event_key=:k OR sequence=:seq)"""),
                    params,
                )
            )
            .scalars()
            .all()
        )
        if prior:
            if prior != [checksum]:
                raise Fault(409, "EVENT_ID_CONFLICT", "事件编号或序号已绑定其他内容")
            continue
        if trace["completeness"] != "OPEN" or trace["availability"] != "AVAILABLE":
            raise Fault(409, "TRACE_CLOSED", "轨迹已封口")
        if trace["deadline"] <= datetime.now(UTC):
            raise Fault(409, "TRACE_CLOSED", "轨迹上报时间已过")
        if trace["event_count"] + accepted >= limits.max_trace_events:
            raise Fault(413, "TRACE_TOO_LARGE", "轨迹事件数超过配置上限")
        payload = {k: data.pop(k) for k in ("input", "output", "resource", "attributes")}
        if payload["attributes"].get("call_kind") == "skill":
            data["attributes"] = {"call_kind": "skill"}
        await db.execute(
            text("""INSERT INTO production_trace_events
            (trace_id,event_key,sequence,parent_event_key,event_type,name,status,digest,metadata,payload,key_version)
            VALUES(:t,:k,:seq,:parent,:kind,:name,:state,:hash,CAST(:meta AS jsonb),
                pgp_sym_encrypt(:payload,:key,'cipher-algo=aes256'),:version)"""),
            {
                **params,
                "parent": event.parent_event_key,
                "kind": event.event_type,
                "name": event.name,
                "state": event.status,
                "hash": checksum,
                "meta": json.dumps(data),
                "payload": json.dumps(payload),
                "key": key.get_secret_value(),
                "version": version,
            },
        )
        accepted += 1
    if accepted:
        await db.execute(
            text("""UPDATE production_traces SET event_count=event_count+:count,
            last_sequence=greatest(last_sequence,:seq),updated_at=now(),deadline=:due WHERE
            id=:t"""),
            {
                "t": trace["id"],
                "count": accepted,
                "seq": max(e.sequence for e in body.events),
                "due": datetime.now(UTC) + timedelta(seconds=limits.idle_seconds),
            },
        )
    return reply(
        req,
        {
            "trace_id": trace["id"],
            "accepted_events": accepted,
            "duplicate_events": len(body.events) - accepted,
        },
    )


def complete_tree(parents: dict[str, str | None]) -> bool:
    checked: set[str] = set()
    for key, parent in parents.items():
        if parent is not None and parent not in checked:
            return False
        checked.add(key)
    return True


@router.post("/production-trace-write/finalize")
async def finish(req: Request, db: Db, g: Grant) -> dict[str, object]:
    require(g, "trace:write")
    try:
        body = ProductionFinal.model_validate(
            await bounded_json(req, policy(req, g).max_batch_bytes)
        )
    except ValidationError as exc:
        raise Fault(422, "INVALID_FINALIZE", "封口字段不符合契约") from exc
    trace = await visible(db, g, body.trace_id, lock=True)
    checksum = digest(body.model_dump(mode="json"))
    status = trace["completeness"]
    if status == "OPEN":
        count = trace["event_count"]
        status = "PARTIAL" if count else "MISSING"
        if (
            count
            and trace["deadline"] > datetime.now(UTC)
            and body.outcome == "COMPLETE"
            and body.contiguous
            and count == body.event_count == body.last_sequence == trace["last_sequence"]
        ):
            links = await db.execute(
                text(
                    """SELECT event_key,parent_event_key FROM production_trace_events WHERE
                        trace_id=:t ORDER BY sequence"""
                ),
                {"t": body.trace_id},
            )
            if complete_tree({k: p for k, p in links}):
                status = "COMPLETE"
        await db.execute(
            text("""UPDATE production_traces SET completeness=:s,finalize_digest=:h,
                        updated_at=now() WHERE id=:t"""),
            {"t": body.trace_id, "s": status, "h": checksum},
        )
    elif trace["finalize_digest"] != checksum:
        raise Fault(409, "TRACE_CLOSED", "轨迹已按另一终态封口")
    return reply(
        req,
        {"trace_id": body.trace_id, "completeness": status, "event_count": trace["event_count"]},
    )


def summary(trace: dict[str, Any]) -> dict[str, Any]:
    return {
        **trace["root"],
        **{
            k: trace[k]
            for k in (
                "project_id",
                "completeness",
                "availability",
                "event_count",
                "last_sequence",
                "retained_until",
            )
        },
        "collection_level": "full_redacted",
    }


@router.get("/production-traces")
async def listing(
    req: Request,
    db: Db,
    g: Grant,
    project_id: UUID | None = None,
    environment: str | None = None,
    agent_run_id: str | None = None,
    conversation_id: str | None = None,
    deployment_version: str | None = None,
    started_from: AwareDatetime | None = None,
    started_to: AwareDatetime | None = None,
    limit: Limit = 50,
    cursor: str | None = None,
) -> dict[str, object]:
    require(g, "trace:read", environment)
    if project_id and project_id != g.project_id:
        raise Fault(403, "SCOPE_DENIED", "项目范围不符")
    params: dict[str, Any] = {"p": g.project_id, "e": g.environment}
    sql = "SELECT * FROM production_traces WHERE project_id=:p AND environment=:e"
    for name, value in (
        ("agent_run_id", agent_run_id),
        ("conversation_id", conversation_id),
        ("deployment_version", deployment_version),
    ):
        if value is not None:
            sql += f" AND root->>'{name}'=:{name}"
            params[name] = value
    for name, op, instant in (("since", ">=", started_from), ("until", "<", started_to)):
        if instant is not None:
            sql += f" AND CAST(root->>'started_at' AS timestamptz){op}:{name}"
            params[name] = instant
    data = await page(db, sql, params, cursor, limit, g.subject, "-created_at")
    data["items"] = [summary(x) for x in data["items"]]
    await audit(
        db,
        g.project_id,
        g.subject,
        req.state.request_id,
        "list",
        "production_trace",
        g.project_id,
        [g.environment],
    )
    return reply(req, data)


@router.get("/production-traces/{tid}")
async def detail(tid: UUID, req: Request, db: Db, g: Grant) -> dict[str, object]:
    require(g, "trace:read")
    trace = await visible(db, g, tid)
    await audit(
        db,
        g.project_id,
        g.subject,
        req.state.request_id,
        "read",
        "production_trace",
        tid,
        [g.environment],
    )
    return reply(req, summary(trace))


@router.get("/production-traces/{tid}/events")
async def events(
    tid: UUID,
    req: Request,
    db: Db,
    g: Grant,
    limit: Limit = 50,
    cursor: str | None = None,
    event_type: EventType | None = None,
    name: str | None = None,
    status: str | None = None,
    call_kind: Literal["skill"] | None = None,
) -> dict[str, object]:
    require(g, "trace:read")
    trace = await visible(db, g, tid)
    if trace["availability"] != "AVAILABLE":
        raise Fault(410, "TRACE_" + trace["availability"], "事件正文当前不可用")
    full = "payload:read" in g.scopes
    sig = digest(
        [tid, g.subject, g.project_id, g.environment, full, event_type, name, status, call_kind]
    )
    after = 0
    if cursor:
        try:
            scope, after = json.loads(base64.urlsafe_b64decode(cursor))
            if scope != sig or type(after) is not int or not 0 <= after < 2**63:
                raise ValueError
        except (TypeError, ValueError) as exc:
            raise Fault(422, "INVALID_CURSOR", "无效的轨迹游标") from exc
    params: dict[str, Any] = {"t": tid, "after": after, "limit": limit + 1}
    where = "trace_id=:t AND sequence>:after"
    if call_kind:
        where += " AND metadata #>> '{attributes,call_kind}'=:kind"
        params["kind"] = call_kind
    for field, value in (("event_type", event_type), ("name", name), ("status", status)):
        if value is not None:
            where += f" AND {field}=:{field}"
            params[field] = value
    payload_sql = ""
    if full:
        cfg: Settings = req.app.state.settings
        payload_sql = ",pgp_sym_decrypt(payload,CAST(:keys AS jsonb)->>key_version) AS body"
        params["keys"] = json.dumps(
            {k: v.get_secret_value() for k, v in cfg.production_trace_keys.items()}
        )
    rows = (
        (
            await db.execute(
                text(
                    "SELECT metadata"
                    + payload_sql
                    + " FROM production_trace_events WHERE "
                    + where
                    + " ORDER BY sequence LIMIT :limit"
                ),
                params,
            )
        )
        .mappings()
        .all()
    )
    items = []
    for result in rows[:limit]:
        entry = dict(result["metadata"])
        if full:
            if not result["body"]:
                raise Fault(503, "PAYLOAD_KEY_UNAVAILABLE", "历史密钥不可用")
            entry.update(json.loads(result["body"]))
        items.append(entry)
    token = (
        base64.urlsafe_b64encode(json.dumps([sig, items[-1]["sequence"]]).encode()).decode()
        if len(rows) > limit
        else None
    )
    await audit(
        db,
        g.project_id,
        g.subject,
        req.state.request_id,
        "read",
        "production_trace_events",
        tid,
        [g.environment, "payload" if full else "metadata"],
    )
    return reply(req, {"items": items, "next_cursor": token})


@router.post("/projects/{project}/production-traces/{tid}/{action}")
async def retention(
    project: UUID,
    tid: UUID,
    action: Literal["pin", "unpin", "archive", "restore"],
    req: Request,
    db: Db,
    who: User,
) -> dict[str, object]:
    await authorize(db, who, project, "A")
    trace = await row(
        db,
        "SELECT * FROM production_traces WHERE id=:t AND project_id=:p FOR UPDATE",
        t=tid,
        p=project,
    )
    if trace["availability"] in {"EXPIRED", "DELETED"} or trace["completeness"] == "OPEN":
        raise Fault(409, "INVALID_TRANSITION", "当前状态不支持此操作")
    if action in {"pin", "unpin"}:
        await db.execute(
            text("UPDATE production_traces SET pinned=:v WHERE id=:t"),
            {"t": tid, "v": action == "pin"},
        )
    else:
        source, destination = (
            ("production_trace_events", "production_trace_cold_events")
            if action == "archive"
            else ("production_trace_cold_events", "production_trace_events")
        )
        required = "AVAILABLE" if action == "archive" else "ARCHIVED"
        if trace["availability"] == required:
            moved = await db.scalar(
                text(f"""WITH moved AS (DELETE FROM {source} WHERE
                trace_id=:t RETURNING *),
                stored AS (INSERT INTO {destination} SELECT * FROM moved RETURNING sequence)
                SELECT count(*) FROM stored"""),
                {"t": tid},
            )
            if moved != trace["event_count"]:
                raise Fault(409, "ARCHIVE_INCOMPLETE", "归档数量与轨迹记录不符")
        await db.execute(
            text("UPDATE production_traces SET availability=:v WHERE id=:t"),
            {"t": tid, "v": "ARCHIVED" if action == "archive" else "AVAILABLE"},
        )
    await audit(db, project, who.subject, req.state.request_id, action, "production_trace", tid, [])
    return reply(req, {"trace_id": tid, "action": action})
