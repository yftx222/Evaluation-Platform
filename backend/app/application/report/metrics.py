"""Terminal reports aggregate durable products in PostgreSQL; no event payloads leave SQL."""

from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.catalog.store import digest, row
from app.application.runner import runs
from app.contracts.reports import Dimension
from app.contracts.traces import TraceCapabilities

# At most one row per execution. Usage from a complete trace is a fallback only.
FACTS = """WITH scores AS (
 SELECT s.execution_id,count(*) FILTER(WHERE s.status='OK') ok,
 count(*) FILTER(WHERE s.status='ERROR') errors,
 count(*) FILTER(WHERE s.passed=false) failed_scores, min(s.score) min_score,
 bool_or((s.status='ERROR' OR s.passed=false) AND
 s.evaluator_version_id=ANY(CAST(:children AS uuid[]))) child_failed,
 count(*) FILTER(WHERE v.impl_key='chat_judge' AND s.status='OK') judge_ok,
 count(*) FILTER(WHERE v.impl_key='chat_judge' AND s.status='OK' AND
 (s.details->'usage'->>'total_tokens' IS NOT NULL OR
 (s.details->'usage'->>'prompt_tokens' IS NOT NULL AND
 s.details->'usage'->>'completion_tokens' IS NOT NULL))) judge_measured,
 sum(coalesce((s.details->'usage'->>'total_tokens')::numeric,
 (s.details->'usage'->>'prompt_tokens')::numeric+
 (s.details->'usage'->>'completion_tokens')::numeric))
 FILTER(WHERE v.impl_key='chat_judge' AND s.status='OK') judge_tokens
 FROM evaluation_scores s JOIN case_executions c ON c.id=s.execution_id
 JOIN evaluator_versions v ON v.id=s.evaluator_version_id WHERE c.run_id=:r GROUP BY s.execution_id
), selected AS (
 SELECT c.id,c.case_id,c.status,c.attempt_count,s.case_key,s.category,s.difficulty,s.tags,
 extract(epoch FROM c.finished_at-c.started_at)*1000 AS case_ms,c.latency_ms AS adapter_ms,
 t.id trace_id,coalesce(t.status,'NOT_COLLECTED') trace_status,coalesce(g.errors,0) errors,
 coalesce(g.ok,0) ok,coalesce(g.failed_scores,0) failed_scores,g.min_score,
 coalesce(g.child_failed,false) child_failed,
 CASE WHEN coalesce(g.judge_measured,0)=:judges THEN coalesce(g.judge_tokens,0) END judge_tokens,
 (c.prediction->'usage'->>'input_tokens')::numeric pred_in,
 (c.prediction->'usage'->>'output_tokens')::numeric pred_out
 FROM case_executions c JOIN evaluation_cases s ON s.id=c.case_id
 LEFT JOIN scores g ON g.execution_id=c.id
 LEFT JOIN LATERAL (SELECT id,status FROM traces t WHERE t.run_id=c.run_id AND t.execution_id=c.id
 ORDER BY (t.attempt_id=c.prediction_attempt_id) DESC NULLS LAST,t.created_at DESC,t.id LIMIT 1)t
 ON CAST(:collect AS boolean) WHERE c.run_id=:r
), model_events AS (
 SELECT t.trace_id, count(*) n,count(i) ni,count(o) no,sum(i) ins,sum(o) outs
 FROM selected t JOIN trace_events e ON e.trace_id=t.trace_id AND e.event_type='MODEL_CALL'
 CROSS JOIN LATERAL (SELECT CASE WHEN jsonb_typeof(e.attributes->'input_tokens')='number'
 AND e.attributes->>'input_tokens' ~ '^[0-9]{1,15}$'
 THEN (e.attributes->>'input_tokens')::numeric END i,
 CASE WHEN jsonb_typeof(e.attributes->'output_tokens')='number'
 AND e.attributes->>'output_tokens' ~ '^[0-9]{1,15}$'
 THEN (e.attributes->>'output_tokens')::numeric END o) usage
 WHERE t.trace_status='COMPLETE' AND t.pred_in IS NULL AND t.pred_out IS NULL
 GROUP BY t.trace_id
), usage AS (
 SELECT c.*,coalesce(pred_in,CASE WHEN n=ni THEN ins END) input_tokens,
 coalesce(pred_out,CASE WHEN n=no THEN outs END) output_tokens FROM selected c
 LEFT JOIN model_events m USING(trace_id)
), facts AS (SELECT u.*,input_tokens+output_tokens+judge_tokens total_tokens,
 (status IN ('FAILED','TIMED_OUT','UNKNOWN_DELIVERY') OR failed_scores>0 OR errors>0) bad
 FROM usage u)"""


def params(run_id: UUID, snap: dict[str, Any]) -> dict[str, Any]:
    plan = snap["evaluator_plan"]
    return {
        "r": run_id,
        "planned": len(plan),
        "collect": (snap["project_version"].get("trace_capabilities") or {}).get("mode", "callback")
        != "disabled",
        "judges": sum(n["impl_key"] == "chat_judge" for n in plan),
        "children": list({UUID(m["child_version_id"]) for n in plan for m in n["members"]}),
    }


async def rows(db: AsyncSession, sql: str, args: dict[str, Any]) -> list[dict[str, Any]]:
    result = await db.execute(text(sql), args)
    return [dict(record) for record in result.mappings()]


def distribution(column: str, alias: str) -> str:
    return f"""count({column}) AS {alias}_count,
    percentile_cont(0.5) WITHIN GROUP(ORDER BY {column}) AS {alias}_p50,
    percentile_cont(0.95) WITHIN GROUP(ORDER BY {column}) AS {alias}_p95"""


async def summary(db: AsyncSession, r: UUID, snap: dict[str, Any]) -> dict[str, Any]:
    detail = await runs.detail(db, r)
    totals = await row(
        db,
        FACTS
        + f""" SELECT {distribution("case_ms", "latency_ms")},
      {distribution("total_tokens", "tokens")},count(input_tokens) input_count,
      count(output_tokens) output_count,count(judge_tokens) judge_count,
      sum(input_tokens) input_known,sum(output_tokens) output_known,sum(judge_tokens) judge_known,
      CASE WHEN count(*)=count(total_tokens) THEN sum(total_tokens) END total_tokens,
      count(*) FILTER(WHERE trace_status='COMPLETE') complete,
      count(*) FILTER(WHERE trace_status='PARTIAL') partial,
      count(*) FILTER(WHERE trace_status='MISSING') missing,
      count(*) FILTER(WHERE trace_status='NOT_COLLECTED') not_collected
      FROM facts""",
        **params(r, snap),
    )
    total = detail["total_cases"]
    overview = {
        k: detail[k]
        for k in (
            "id",
            "project_id",
            "status",
            "total_cases",
            "succeeded_cases",
            "failed_cases",
            "timed_out_cases",
            "cancelled_cases",
            "unknown_delivery_cases",
            "metrics",
            "score_counts",
            "started_at",
            "finished_at",
        )
    }
    overview["duration_ms"] = (
        (detail["finished_at"] - detail["started_at"]).total_seconds() * 1000
        if detail["started_at"] and detail["finished_at"]
        else None
    )
    overview.update(totals)
    overview["trace_mode"] = (snap["project_version"].get("trace_capabilities") or {}).get(
        "mode", "callback"
    )
    overview["trace_complete_rate"] = runs.ratio(
        totals["complete"], total - totals["not_collected"]
    )
    versions = await rows(
        db,
        """SELECT evaluator_version_id id,avg(score) mean_score,
      count(*) FILTER(WHERE s.status='ERROR') errors,count(*) FILTER(WHERE s.status='OK') ok
      FROM evaluation_scores s JOIN case_executions c ON c.id=s.execution_id
      WHERE c.run_id=:r GROUP BY evaluator_version_id""",
        {"r": r},
    )
    by_version = {str(v["id"]): v for v in versions}
    overview["evaluators"] = [
        {
            **{k: n[k] for k in ("id", "name", "version", "members")},
            **by_version.get(n["id"], {"mean_score": None, "errors": 0, "ok": 0}),
            **detail["metrics"]["by_version"][n["id"]],
        }
        for n in snap["evaluator_plan"]
    ]
    return overview


async def process(db: AsyncSession, r: UUID, snap: dict[str, Any]) -> dict[str, Any]:
    attempts = await row(
        db,
        """SELECT count(*) attempts,
      count(*) FILTER(WHERE coalesce(error_code,'') NOT IN ('TARGET_LIMIT','JUDGE_LIMIT'))
      -count(DISTINCT(execution_id,logical_step_key)) FILTER(WHERE coalesce(error_code,'')
      NOT IN ('TARGET_LIMIT','JUDGE_LIMIT')) retries,
      count(*) FILTER(WHERE delivery_state='unknown') delivery_unknown,
      count(*) FILTER(WHERE error_code IN ('TARGET_LIMIT','JUDGE_LIMIT')) quota_waits
      FROM execution_attempts WHERE run_id=:r""",
        r=r,
    )
    timings = await rows(
        db,
        f"""SELECT stage,{distribution("latency_ms", "ms")} FROM (
      SELECT CASE WHEN a.stage='ADAPTER' THEN 'ADAPTER' ELSE 'JUDGE' END stage,a.latency_ms
      FROM execution_attempts a LEFT JOIN evaluator_versions v ON v.id=a.evaluator_version_id
      WHERE a.run_id=:r AND a.delivery_state<>'not_sent'
      AND (a.stage='ADAPTER' OR v.impl_key='chat_judge')
      UNION ALL SELECT e.event_type,e.duration_ms FROM trace_events e
      JOIN traces t ON t.id=e.trace_id WHERE t.run_id=:r AND
      e.event_type IN ('MODEL_CALL','TOOL_CALL','RETRIEVAL')) d GROUP BY stage""",
        {"r": r},
    )
    by_kind = {v["stage"]: v for v in timings}
    caps = snap["project_version"].get("trace_capabilities")
    for stage in ("ADAPTER", "JUDGE", "MODEL_CALL", "TOOL_CALL", "RETRIEVAL"):
        item = by_kind.setdefault(
            stage, {"stage": stage, "ms_count": 0, "ms_p50": None, "ms_p95": None}
        )
        supported = (
            True
            if stage == "ADAPTER"
            else any(n["impl_key"] == "chat_judge" for n in snap["evaluator_plan"])
            if stage == "JUDGE"
            else None
            if not caps
            else caps["mode"] != "disabled" and stage in caps["event_types"]
        )
        item["availability"] = (
            "OBSERVED"
            if item["ms_count"]
            else "NOT_APPLICABLE"
            if supported is False
            else "UNAVAILABLE"
        )
    return {**attempts, "timings": list(by_kind.values())}


DIMENSIONS = {"category": "s.category", "difficulty": "s.difficulty", "tag": "tag.value"}


async def groups(
    db: AsyncSession,
    r: UUID,
    snap: dict[str, Any],
    dimension: Dimension,
    offset: int,
    limit: int,
    version: UUID | None = None,
) -> dict[str, Any]:
    tag = (
        "LEFT JOIN LATERAL (SELECT DISTINCT unnest(s.tags) value) tag ON true"
        if dimension == "tag"
        else ""
    )
    sql = f"""WITH per_case AS (SELECT c.id,c.status,{DIMENSIONS[dimension]} label,
      count(g.id) FILTER(WHERE g.status='OK') ok,count(g.id) FILTER(WHERE g.status='ERROR') errors,
      count(g.id) FILTER(WHERE g.status='SKIPPED') skipped,
      count(g.id) FILTER(WHERE g.passed) passed,
      sum(g.score) score_sum FROM case_executions c JOIN evaluation_cases s ON s.id=c.case_id {tag}
      LEFT JOIN evaluation_scores g ON g.execution_id=c.id AND
      (CAST(:version AS uuid) IS NULL OR g.evaluator_version_id=:version)
      WHERE c.run_id=:r GROUP BY c.id,{DIMENSIONS[dimension]})
      SELECT label,count(*) cases,count(*) FILTER(WHERE status='SUCCEEDED') succeeded,
      sum(ok)::bigint ok,sum(errors)::bigint errors,sum(skipped)::bigint skipped,
      sum(passed)::bigint passed,sum(score_sum)/nullif(sum(ok),0) mean_score
      FROM per_case GROUP BY label ORDER BY label NULLS FIRST LIMIT :n OFFSET :offset"""
    data = await rows(db, sql, {"r": r, "version": version, "n": limit + 1, "offset": offset})
    for g in data[:limit]:
        g.update(
            runs.score_metrics(
                g["ok"],
                g["errors"],
                g["passed"],
                g["cases"] * (1 if version else len(snap["evaluator_plan"])),
            )
        )
        g["case_success_rate"] = runs.ratio(g["succeeded"], g["cases"])
    return {
        "dimension": dimension,
        "items": data[:limit],
        "next_offset": offset + limit if len(data) > limit else None,
    }


def conditions(snap: dict[str, Any]) -> dict[str, Any]:
    nodes = snap["evaluator_plan"]
    capability = TraceCapabilities.model_validate(
        snap["project_version"].get("trace_capabilities") or {}
    ).model_dump()
    for key in ("event_types", "required_events"):
        capability[key] = sorted(capability[key])
    return {
        "project_id": snap["project_id"],
        "project_version": snap["project_version"]["id"],
        "deployment": snap["project_version"]["deployment_ref"],
        "adapter_build": snap["adapter"]["build"],
        "adapter_fingerprint": snap["implementation_digest"],
        "dataset_version": snap["dataset_version"]["id"],
        "dataset_digest": snap["dataset_version"]["digest"],
        "evaluator_dag": digest(
            [sorted(snap["roots"]), sorted((n["id"], n["digest"]) for n in nodes)]
        ),
        "judges": [
            {
                k: n.get(k)
                for k in (
                    "id",
                    "provider",
                    "model_id",
                    "model_revision",
                    "prompt_version",
                    "rubric_version",
                    "revision_unverified",
                )
            }
            for n in sorted(nodes, key=lambda n: n["id"])
            if n["impl_key"] == "chat_judge"
        ],
        "target_model": snap["adapter"].get("options", {}).get("model"),
        "execution_policy": {
            **{k: v for k, v in snap["policy"].items() if k != "approved_policy_ids"},
            "approved_policy_digest": digest(snap["policy"].get("approved_policy_ids", [])),
        },
        "trace_capabilities": capability,
    }
