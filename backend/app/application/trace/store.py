import hashlib
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.catalog.store import Fault, row
from app.contracts.traces import TraceBatch, TraceCapabilities, TraceFinalize, canonical


class BatchFault(Fault):
    def __init__(self, status: int, code: str, count: int | None, index: int | None = None) -> None:
        super().__init__(status, code, "本批事件未写入")
        self.count = count
        self.index = index


async def append(db: AsyncSession, trace: dict[str, Any], batch: TraceBatch) -> dict[str, Any]:
    events = batch.events
    if trace["status"] != "OPEN":
        raise BatchFault(409, "TRACE_SEALED", len(events))
    capability = TraceCapabilities.model_validate(trace.get("capabilities") or {})
    for i, e in enumerate(events):
        if (
            capability.mode == "disabled"
            or e.event_type not in capability.event_types
            or (e.parent_event_key and not capability.parent_links)
        ):
            raise BatchFault(422, "UNDECLARED_EVENT", len(events), i)
    trace_id = trace["id"]
    existing = (
        await db.execute(
            text("""SELECT event_key,sequence,content_digest FROM trace_events
        WHERE trace_id=:t AND (event_key=ANY(CAST(:keys AS text[]))
        OR sequence=ANY(CAST(:seq AS bigint[])))"""),
            {
                "t": trace_id,
                "keys": [e.event_key for e in events],
                "seq": [e.sequence for e in events],
            },
        )
    ).mappings()
    contents = {}
    sequences = {}
    for old in existing:
        contents[old["event_key"]] = old["content_digest"]
        sequences[old["sequence"]] = old["event_key"]
    new = []
    for i, event in enumerate(events):
        entry = event.model_dump(mode="json")
        digest = hashlib.sha256(canonical(entry)).hexdigest()
        if event.event_key in contents:
            if contents[event.event_key] != digest:
                raise BatchFault(409, "EVENT_KEY_CONFLICT", len(events), i)
            continue
        if event.sequence in sequences:
            raise BatchFault(409, "SEQUENCE_CONFLICT", len(events), i)
        contents[event.event_key] = digest
        sequences[event.sequence] = event.event_key
        new.append({**entry, "content_digest": digest})
    if new:
        await db.execute(
            text("""INSERT INTO trace_events(trace_id,event_key,sequence,
            parent_event_key,event_type,name,status,started_at,ended_at,duration_ms,input,output,
            attributes,resource,schema_version,content_digest)
            SELECT :t,x.* FROM jsonb_to_recordset(CAST(:events AS jsonb)) AS x(
            event_key text,sequence bigint,parent_event_key text,event_type text,name text,
            status text,
            started_at timestamptz,ended_at timestamptz,duration_ms bigint,input jsonb,output jsonb,
            attributes jsonb,resource jsonb,schema_version text,content_digest text)"""),
            {"t": trace_id, "events": canonical(new).decode()},
        )
        params = {"t": trace_id, "keys": [e["event_key"] for e in new]}
        await db.execute(
            text("""UPDATE trace_events c SET parent_event_id=p.id FROM trace_events p
            WHERE c.trace_id=:t AND p.trace_id=:t AND c.parent_event_id IS NULL
            AND c.parent_event_key=p.event_key AND (c.event_key=ANY(CAST(:keys AS text[]))
            OR p.event_key=ANY(CAST(:keys AS text[])))"""),
            params,
        )
        # Every newly introduced cycle must pass through at least one newly inserted node.
        cycle = await db.scalar(
            text("""WITH RECURSIVE ancestors(origin,id) AS (
            SELECT id,parent_event_id FROM trace_events WHERE trace_id=:t
              AND event_key=ANY(CAST(:keys AS text[])) AND parent_event_id IS NOT NULL
            UNION SELECT a.origin,e.parent_event_id FROM ancestors a
              JOIN trace_events e ON e.id=a.id
              WHERE e.parent_event_id IS NOT NULL)
            SELECT e.event_key FROM ancestors a JOIN trace_events e ON e.id=a.origin
            WHERE a.origin=a.id LIMIT 1"""),
            params,
        )
        if cycle:
            raise BatchFault(
                409,
                "PARENT_CYCLE",
                len(events),
                next(i for i, e in enumerate(events) if e.event_key == cycle),
            )
        await db.execute(
            text("""UPDATE traces SET event_count=event_count+:n,
            last_event_at=clock_timestamp(),updated_at=clock_timestamp() WHERE id=:t"""),
            {"n": len(new), "t": trace_id},
        )
    return {
        "trace_id": trace_id,
        "accepted": len(new),
        "duplicate": len(events) - len(new),
        "rejected": 0,
    }


async def finalize(db: AsyncSession, trace: dict[str, Any], body: TraceFinalize) -> dict[str, Any]:
    if trace["status"] != "OPEN":
        return trace
    caps = TraceCapabilities.model_validate(trace.get("capabilities") or {})
    count = trace["event_count"]
    integrity = await row(
        db,
        """SELECT max(sequence) AS last,array_agg(DISTINCT event_type) AS types,
        count(*) FILTER (WHERE parent_event_key IS NOT NULL AND parent_event_id IS NULL) AS missing
        FROM trace_events WHERE trace_id=:t""",
        t=trace["id"],
    )
    if not count:
        status, reason = "MISSING", "NO_EVENTS"
    elif body.outcome == "PARTIAL" or (not body.contiguous and caps.contiguous):
        status, reason = "PARTIAL", "SOURCE_INCOMPLETE"
    elif not set(caps.required_events) <= set(integrity["types"]):
        status, reason = "PARTIAL", "REQUIRED_EVENT_MISSING"
    elif integrity["missing"]:
        status, reason = "PARTIAL", "PARENT_MISSING"
    elif (
        integrity["last"] != body.last_sequence
        or (body.contiguous and count != body.last_sequence)
        or (not body.contiguous and count != body.event_count)
        or (body.event_count is not None and count != body.event_count)
    ):
        status, reason = "PARTIAL", "SEQUENCE_GAP"
    else:
        status, reason = "COMPLETE", "SOURCE_FINALIZED"
    closed = await row(
        db,
        """UPDATE traces SET status=:s,close_reason=:why,ended_at=clock_timestamp(),
        updated_at=clock_timestamp() WHERE id=:t RETURNING *""",
        s=status,
        why=reason,
        t=trace["id"],
    )
    return {**closed, "capabilities": trace.get("capabilities")}
