"""Resolve score citations only inside the frozen execution that produced the score."""

import json
import re
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.catalog.store import row
from trace_sdk.privacy import PRIVATE_KEY, PRIVATE_VALUE, redact

SCOPE = """SELECT c.id,c.prediction,c.prediction_attempt_id,c.case_id,c.dataset_version_id,
    s.input,s.expected,r.project_id,r.config_snapshot->'policy'->'approved_policy_ids' policies,
    a.trace_id FROM case_executions c JOIN evaluation_runs r ON r.id=c.run_id
    JOIN evaluation_cases s ON s.id=c.case_id AND s.dataset_version_id=r.dataset_version_id
    LEFT JOIN execution_attempts a ON a.id=c.prediction_attempt_id AND a.execution_id=c.id
    WHERE c.id=:id"""
TYPES = {
    "prediction": "PREDICTION",
    "case_input": "CASE_INPUT",
    "expected": "EXPECTED",
    "external": "EXTERNAL_REFERENCE",
    "trace_event": "TRACE_EVENT",
    "output_event": "TRACE_EVENT",
}
REASONS = {
    "INVALID_REFERENCE",
    "SOURCE_MISSING",
    "INVALID_POINTER",
    "UNTRUSTED_SOURCE",
    "SOURCE_ARCHIVED",
}


def locate(doc: Any, pointer: str) -> tuple[Any, bool]:
    if (
        len(pointer) > 512
        or (pointer and not pointer.startswith("/"))
        or re.search(r"~(?![01])", pointer)
    ):
        raise ValueError
    hidden = False
    for part in pointer.split("/")[1:]:
        key = part.replace("~1", "/").replace("~0", "~")
        hidden |= bool(
            PRIVATE_KEY.search(re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", key.lower()))
            or PRIVATE_VALUE.search(key)
        )
        if isinstance(doc, dict) and key in doc:
            doc = doc[key]
        elif (
            isinstance(doc, list)
            and re.fullmatch(r"0|[1-9][0-9]{0,8}", key)
            and int(key) < len(doc)
        ):
            doc = doc[int(key)]
        else:
            raise ValueError
    return doc, hidden


def unavailable(kind: str, reason: str) -> dict[str, Any]:
    return dict(
        source_type=TYPES.get(kind),
        status="EVIDENCE_UNAVAILABLE",
        reason=reason,
        source_id=None,
        attempt_id=None,
        trace_id=None,
        event_id=None,
        json_pointer=None,
        excerpt=None,
        metadata={},
    )


async def resolve(db: AsyncSession, scope: dict[str, Any], ref: str) -> dict[str, Any]:
    kind, separator, target = ref.partition(":")
    item = unavailable(kind, "INVALID_REFERENCE")
    if kind == "redacted" and target in TYPES:
        item = unavailable(target, "SENSITIVE_CONTENT")
        item["status"] = "REDACTED"
        return item
    if kind == "unavailable":
        original, _, why = target.partition(":")
        return unavailable(original, why if why in REASONS else "INVALID_REFERENCE")
    if not separator or kind not in TYPES or len(target) > 600:
        return item
    doc: Any
    pointer = target
    if kind in {"trace_event", "output_event"}:
        raw, _, pointer = target.partition("#")
        try:
            event_id = UUID(raw)
        except ValueError:
            return item
        event = (
            (
                await db.execute(
                    text("""SELECT ar.trace_id IS NOT NULL archived,
            CASE WHEN ar.trace_id IS NULL THEN to_jsonb(e) - 'content_digest' END AS document
            FROM trace_events e
            JOIN traces t ON t.id=e.trace_id
            LEFT JOIN trace_archives ar ON ar.trace_id=t.id WHERE t.project_id=:project_id
            AND t.execution_id=:id AND t.attempt_id=:prediction_attempt_id AND t.status<>'OPEN'
            AND e.trace_id=:trace_id AND e.id=:event"""),
                    {**scope, "event": event_id},
                )
            )
            .mappings()
            .one_or_none()
        )
        if not event:
            return unavailable(kind, "SOURCE_MISSING")
        item.update(
            source_id=event_id,
            event_id=event_id,
            trace_id=scope["trace_id"],
            attempt_id=scope["prediction_attempt_id"],
        )
        if kind == "output_event":
            item["metadata"]["relation"] = "prediction_provenance"
        if event["archived"]:
            item["reason"] = "SOURCE_ARCHIVED"
            return item
        doc = event["document"]
    elif kind == "prediction":
        if not scope["prediction_attempt_id"]:
            return unavailable(kind, "SOURCE_MISSING")
        doc = scope["prediction"]
        item.update(
            source_id=scope["id"],
            attempt_id=scope["prediction_attempt_id"],
            trace_id=scope["trace_id"],
        )
    elif kind in {"case_input", "expected"}:
        field = "input" if kind == "case_input" else "expected"
        doc = scope[field]
        if doc is None:
            return unavailable(kind, "SOURCE_MISSING")
        item.update(
            source_id=scope["case_id"], metadata={"dataset_version_id": scope["dataset_version_id"]}
        )
    else:
        registry, _, key = target.partition("/")
        if (
            registry != "policy"
            or not re.fullmatch(r"[a-zA-Z0-9_.-]{1,100}", key)
            or key not in (scope["policies"] or [])
            or PRIVATE_VALUE.search(key)
        ):
            return unavailable(kind, "UNTRUSTED_SOURCE")
        item.update(source_id=target, metadata={"registry": "policy"})
        pointer, doc = "", key

    try:
        value, hidden = locate(doc, pointer)
    except ValueError:
        return unavailable(kind, "INVALID_POINTER")
    item.update(status="AVAILABLE", reason=None, json_pointer=None if hidden else pointer)
    try:
        safe = redact(value) if not hidden else None
        hidden |= safe != value or safe == "[REDACTED]"
    except ValueError:
        hidden = True
        safe = None
    if hidden:
        item.update(status="REDACTED", reason="SENSITIVE_CONTENT")
    else:
        excerpt = safe if isinstance(safe, str) else json.dumps(safe, ensure_ascii=False)
        item["excerpt"] = excerpt if len(excerpt) <= 240 else excerpt[:239] + "…"
    return item


async def bind(db: AsyncSession, execution: UUID, references: list[str]) -> list[str]:
    """Freeze validated claims and explicit FINAL_OUTPUT provenance with the official score."""
    if not references:
        return []
    scope = await row(db, SCOPE, id=execution)
    bound = []
    predictions: set[str] = set()
    for ref in references:
        result = await resolve(db, scope, ref)
        kind = ref.partition(":")[0]
        if (
            result["status"] == "EVIDENCE_UNAVAILABLE"
            or kind not in TYPES
            or kind == "output_event"
        ):
            why = result["reason"] if result["reason"] in REASONS else "INVALID_REFERENCE"
            bound.append(f"unavailable:{kind if kind in TYPES else 'unknown'}:{why}")
        elif PRIVATE_VALUE.search(ref):
            # A pointer segment can itself contain a private value. Keep only its type.
            bound.append("redacted:" + kind)
        else:
            bound.append(ref)
            if kind == "prediction" and result["status"] == "AVAILABLE":
                pointer = result["json_pointer"]
                predictions.add(ref)
                if re.fullmatch(r"(/[A-Za-z0-9_]+)+", pointer):
                    predictions.add("prediction" + pointer.replace("/", "."))
    if predictions and scope["trace_id"]:
        events = await db.execute(
            text("""SELECT min(e.id::text) AS id
            FROM trace_events e JOIN traces t ON t.id=e.trace_id
            WHERE t.id=:trace_id AND t.execution_id=:id AND t.attempt_id=:prediction_attempt_id
            AND t.status IN ('PARTIAL','COMPLETE') AND e.event_type='FINAL_OUTPUT'
            AND e.status='SUCCEEDED' AND e.output->>'output_ref'=ANY(CAST(:refs AS text[]))
            AND NOT EXISTS (SELECT 1 FROM trace_archives ar WHERE ar.trace_id=t.id)
            GROUP BY e.output->>'output_ref' HAVING count(*)=1 ORDER BY min(e.sequence)"""),
            {**scope, "refs": list(predictions)},
        )
        bound.extend(f"output_event:{event.id}#/output/output_ref" for event in events)
    return bound
