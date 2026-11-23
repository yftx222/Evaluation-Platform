from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.application.catalog.store import Fault, row
from app.application.report.metrics import FACTS, conditions, params, rows
from app.contracts.reports import Signal

TERMINAL = {"COMPLETED", "FAILED", "PARTIAL_FAILED", "CANCELLED"}


async def model_usage(db: AsyncSession, r: UUID, snap: dict[str, Any]) -> dict[str, Any]:
    # Only official products carry authoritative model metadata. A retry can incur additional spend.
    data = await rows(
        db,
        """WITH products AS (
      SELECT 'target' role,NULL::uuid version,c.prediction->'observed'->>'model_id' model,
      c.prediction->'observed'->>'model_revision' revision,
      (c.prediction->'usage'->>'input_tokens')::numeric i,
      (c.prediction->'usage'->>'output_tokens')::numeric o
      FROM case_executions c WHERE c.run_id=:r AND c.prediction IS NOT NULL
      UNION ALL SELECT 'judge',g.evaluator_version_id,g.details->>'model',g.details->>'revision',
      (g.details->'usage'->>'prompt_tokens')::numeric,(g.details->'usage'->>'completion_tokens')::numeric
      FROM evaluation_scores g JOIN case_executions c ON c.id=g.execution_id
      JOIN evaluator_versions v ON v.id=g.evaluator_version_id WHERE c.run_id=:r
      AND v.impl_key='chat_judge' AND g.status='OK')
      SELECT role,version,model,revision,count(*) products,count(i+o) measured,sum(i) inputs,
      sum(o) outputs FROM products GROUP BY role,version,model,revision
      ORDER BY role,version,model,revision LIMIT 101""",
        {"r": r},
    )
    cfg = snap.get("report_policy", {})
    judges = {n["id"]: n for n in snap["evaluator_plan"] if n["impl_key"] == "chat_judge"}
    estimate = 0.0
    available = bool(data) and len(data) <= 100
    for m in data[:100]:
        key = str(m["version"])
        rate = cfg.get("target") if m["role"] == "target" else cfg.get("judges", {}).get(key)
        matched = bool(rate and (m["model"], m["revision"]) == (rate["model"], rate["revision"]))
        m["provider"] = rate["provider"] if matched else None
        m["protocol"] = (
            snap["adapter"]["kind"] if m["role"] == "target" else judges[key]["provider"]
        )
        m["estimate"] = None
        if matched and m["products"] == m["measured"]:
            m["estimate"] = (
                float(m["inputs"]) * rate["input_per_million"]
                + float(m["outputs"]) * rate["output_per_million"]
            ) / 1e6
            estimate += m["estimate"]
        else:
            available = False
    return {
        "models": data[:100],
        "truncated": len(data) > 100,
        "currency": cfg.get("currency"),
        "estimated_cost": estimate if available else None,
        "cost_basis": "official_products",
    }


def comparison_conditions(snap: dict[str, Any], usage: dict[str, Any]) -> dict[str, Any]:
    values = conditions(snap)
    values["observed_models"] = [
        {k: m[k] for k in ("role", "version", "provider", "model", "revision")}
        for m in usage["models"]
    ]
    return values


def differences(a: dict[str, Any], b: dict[str, Any], truncated: bool) -> list[str]:
    mismatches = [k for k, v in a.items() if b.get(k) != v]
    for c in (a, b):
        judges = {str(j["id"]): j for j in c["judges"]}
        if any(j["revision_unverified"] or not j["model_revision"] for j in judges.values()):
            mismatches.append("judge_revision_unverified")
        seen_judges = {str(m["version"]) for m in c["observed_models"] if m["role"] == "judge"}
        if judges.keys() - seen_judges:
            mismatches.append("judge_revision_unverified")
        if not any(m["role"] == "target" for m in c["observed_models"]):
            mismatches.append("target_revision_unverified")
        for m in c["observed_models"]:
            if m["role"] == "judge":
                expected = judges[str(m["version"])]
                if (m["model"], m["revision"]) != (
                    expected["model_id"],
                    expected["model_revision"],
                ):
                    mismatches.append("judge_revision_unverified")
            elif (
                not m["model"]
                or not m["revision"]
                or (c["target_model"] and m["model"] != c["target_model"])
            ):
                mismatches.append("target_revision_unverified")
    if truncated:
        mismatches.append("model_summary_truncated")
    return sorted(set(mismatches))


# Match immutable sample identities, and compare the same evaluator version on both sides.
PAIRS = """baseline AS (
 SELECT c.id,c.case_id,c.status IN ('FAILED','TIMED_OUT','UNKNOWN_DELIVERY') OR EXISTS(
 SELECT 1 FROM evaluation_scores s WHERE s.execution_id=c.id AND (s.status='ERROR' OR NOT s.passed)
 ) bad,c.status,(SELECT count(*) FROM evaluation_scores g WHERE g.execution_id=c.id
 AND g.status='OK') ok FROM case_executions c WHERE c.run_id=:base
), classified AS (
 SELECT f.*,b.id baseline_execution,
 (b.status='SUCCEEDED' AND b.ok=:planned AND NOT b.bad AND f.bad) new_failure,
 (b.bad AND f.status='SUCCEEDED' AND f.ok=:planned AND NOT f.bad) fixed,
 (b.id IS NOT NULL AND EXISTS(SELECT 1 FROM evaluation_scores s JOIN evaluation_scores old
 ON old.execution_id=b.id AND old.evaluator_version_id=s.evaluator_version_id
 WHERE s.execution_id=f.id AND s.score<old.score)) regressed FROM facts f
 LEFT JOIN baseline b USING(case_id))"""
SIGNALS = {
    "case_failed": "status='FAILED'",
    "timed_out": "status='TIMED_OUT'",
    "score_error": "errors>0",
    "low_score": "min_score<:threshold",
    "child_failed": "child_failed",
    "trace_partial": "trace_status='PARTIAL'",
    "trace_missing": "CAST(:collect AS boolean) AND trace_status='MISSING'",
    "token_limit": "total_tokens>:tokens",
    "latency_limit": "case_ms>:ms",
    "new_failure": "new_failure",
    "fixed": "fixed",
    "regressed": "regressed",
}


def case_query(
    r: UUID, snap: dict[str, Any], signal: Signal, threshold: float, baseline: UUID | None
) -> tuple[str, dict[str, Any]]:
    cfg = snap.get("report_policy", {})
    args = {
        **params(r, snap),
        "threshold": threshold,
        "collect": (snap["project_version"].get("trace_capabilities") or {}).get("mode")
        != "disabled",
        "base": baseline,
        "tokens": cfg.get("max_case_tokens"),
        "ms": cfg.get("max_case_ms"),
    }
    if signal in {"new_failure", "fixed", "regressed"} and not baseline:
        raise Fault(422, "BASELINE_REQUIRED", "请先选择可比较的基线运行")
    with_pairs = (
        FACTS + "," + PAIRS
        if baseline
        else FACTS
        + """, classified AS
      (SELECT f.*,NULL::uuid baseline_execution,false new_failure,false fixed,false regressed
       FROM facts f)"""
    )
    flags = ", ".join(
        f"CASE WHEN {condition} THEN '{name}' END" for name, condition in SIGNALS.items()
    )
    if signal == "all":
        where = "true"
    elif signal == "any":
        where = "bad OR cardinality(array_remove(reasons,'fixed'))>0"
    else:
        where = ":signal=ANY(reasons)"
        args["signal"] = signal
    return (
        with_pairs
        + f""", flagged AS (SELECT c.*,array_remove(ARRAY[{flags}],NULL) reasons
      FROM classified c) SELECT id execution_id,case_key,status,trace_id,trace_status,
      case_ms,input_tokens,output_tokens,judge_tokens,total_tokens,min_score,errors,failed_scores,
      attempt_count,reasons,baseline_execution FROM flagged WHERE {where} ORDER BY id""",
        args,
    )


async def changes(
    db: AsyncSession, r: UUID, snap: dict[str, Any], baseline: UUID
) -> dict[str, Any]:
    return await row(
        db,
        FACTS
        + ","
        + PAIRS
        + """ SELECT count(baseline_execution) matched,
        count(*) FILTER(WHERE new_failure) new_failure,count(*) FILTER(WHERE fixed) fixed,
        count(*) FILTER(WHERE regressed) regressed FROM classified""",
        **params(r, snap),
        base=baseline,
    )
