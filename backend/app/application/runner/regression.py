"""Three independent executions of a single frozen task."""

import json
from datetime import UTC, datetime
from statistics import mean
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.catalog.store import Fault, digest, row
from app.application.report.analysis import model_usage
from app.application.runner import runs
from app.application.trace.evidence import SCOPE, resolve

TERMINAL = {"COMPLETED", "PARTIAL_FAILED", "FAILED", "CANCELLED"}


async def create_batch(
    db: AsyncSession,
    project: UUID,
    environment: str,
    task: UUID,
    version: UUID,
    key: str,
    baseline: UUID | None,
) -> dict[str, Any]:
    frozen = await row(
        db,
        "SELECT * FROM evaluation_tasks WHERE id=:t AND project_id=:p FOR UPDATE",
        t=task,
        p=project,
    )
    if version != frozen["project_version_id"]:
        raise Fault(422, "VERSION_MISMATCH", "候选版本必须和任务的冻结版本一致")
    fingerprint = digest([task, version, baseline])
    if baseline:
        old = await row(
            db,
            """SELECT b.report,t.dataset_version_id FROM regression_batches b
            JOIN evaluation_tasks t ON t.id=b.task_id WHERE b.id=:b AND b.project_id=:p AND
            b.environment=:e""",
            b=baseline,
            p=project,
            e=environment,
        )
        if old["report"] is None or old["dataset_version_id"] != frozen["dataset_version_id"]:
            raise Fault(409, "BASELINE_INCOMPATIBLE", "基线须已完成且使用同一版数据集")
    params = dict(p=project, e=environment, t=task, k=key, h=fingerprint, b=baseline)
    batch = await db.scalar(
        text("""INSERT INTO regression_batches(project_id,environment,task_id,trigger_key,
            request_digest,baseline_id)
        VALUES(:p,:e,:t,:k,:h,:b) ON CONFLICT DO NOTHING RETURNING id"""),
        params,
    )
    if batch:
        for trial in (1, 2, 3):
            run, _ = await runs.start(db, task, f"quality:{batch}:{trial}")
            await db.execute(
                text("INSERT INTO regression_trials(batch_id,trial,run_id) VALUES(:b,:n,:r)"),
                dict(b=batch, n=trial, r=run["id"]),
            )
    else:
        saved = await row(
            db,
            """SELECT id,request_digest FROM regression_batches WHERE project_id=:p
            AND environment=:e AND trigger_key=:k""",
            **params,
        )
        if saved["request_digest"] != fingerprint:
            raise Fault(409, "TRIGGER_CONFLICT", "该触发键已用于另一组版本")
        batch = saved["id"]
    return {"regression_batch_id": batch, "trials": await trials(db, batch)}


async def trials(db: AsyncSession, batch: UUID) -> list[dict[str, Any]]:
    return [
        dict(x)
        for x in (
            await db.execute(
                text("""SELECT t.trial,t.run_id,r.status
        FROM regression_trials t JOIN evaluation_runs r ON r.id=t.run_id WHERE t.batch_id=:b
        ORDER BY t.trial"""),
                {"b": batch},
            )
        ).mappings()
    ]


def verdict(values: list[str]) -> str:
    if "FAILED" in values:
        return "FAILED"
    return "PASSED" if values and all(v == "PASSED" for v in values) else "BLOCKED"


def evaluate_case(slots: list[dict[str, Any]], snap: dict[str, Any]) -> dict[str, Any]:
    outcomes: list[list[str]] = [[], [], []]
    rules = []
    decisions = []
    for node in snap["evaluator_plan"]:
        rubrics = node.get("rubrics") or (
            [
                dict(
                    rubric_key="passed",
                    gate_class="BLOCKING",
                    evaluator_ref="self",
                    evidence_required=False,
                    minimum=0,
                    threshold=1,
                    weight=1,
                )
            ]
            if node["id"] in snap["roots"]
            else []
        )
        for rubric in rubrics:
            ref = node["id"] if rubric["evaluator_ref"] == "self" else rubric["evaluator_ref"]
            values = []
            judgments = []
            evidence = []
            for i, slot in enumerate(slots):
                score = slot["scores"].get(ref, {})
                available = (
                    slot["status"] == "SUCCEEDED"
                    and score.get("status") == "OK"
                    and (not rubric["evidence_required"] or score.get("verified_evidence", False))
                )
                value = float(score["score"]) if available else None
                values.append(value)
                evidence.append(
                    {
                        "score_id": score.get("id"),
                        "reason": score.get("reason", slot["status"]),
                        "refs": score.get("evidence", []),
                    }
                )
                outcome = (
                    "BLOCKED"
                    if value is None
                    else "PASSED"
                    if (
                        score["passed"]
                        if rubric["gate_class"] == "BLOCKING"
                        else value >= rubric["minimum"]
                    )
                    else "FAILED"
                )
                judgments.append(outcome)
                if rubric["gate_class"] in ("BLOCKING", "THRESHOLD"):
                    outcomes[i].append(outcome)
            numbers = [v for v in values if v is not None]
            average = mean(numbers) if numbers else None
            decision = verdict(judgments)
            if (
                rubric["gate_class"] == "THRESHOLD"
                and len(numbers) == 3
                and average is not None
                and average < rubric["threshold"]
            ):
                decision = "FAILED"
            if rubric["gate_class"] in ("BLOCKING", "THRESHOLD"):
                decisions.append(decision)
            rules.append(
                {
                    "rule": rubric,
                    "version_id": ref,
                    "scores": values,
                    "valid_count": len(numbers),
                    "mean": average,
                    "minimum": min(numbers, default=None),
                    "decision": decision,
                    "evidence": evidence,
                }
            )
    statuses = [verdict(v) for v in outcomes]
    bonus = sum(
        r["rule"].get("weight", 1) * r["mean"]
        for r in rules
        if r["rule"]["gate_class"] == "BONUS" and r["mean"] is not None
    )
    return {
        "bonus_points": bonus,
        "trial_results": statuses,
        "gate": verdict(decisions),
        "valid_trials": 3 - statuses.count("BLOCKED"),
        "pass_fraction": statuses.count("PASSED") / 3,
        "any_passed": "PASSED" in statuses,
        "all_passed": statuses == ["PASSED"] * 3,
        "rubrics": rules,
    }


async def enqueue(
    db: AsyncSession,
    project: UUID,
    slug: str,
    environment: str,
    deployment: str | None,
    kind: str,
    payload: dict[str, Any],
    correlation: dict[str, Any] | None = None,
) -> UUID:
    eid = uuid4()
    event = dict(
        schema_version="1.0",
        event_id=eid,
        event_type=kind,
        occurred_at=datetime.now(UTC).isoformat(),
        source_system="evaluation-platform",
        project=dict(project_id=project, project_slug=slug, environment=environment),
        deployment_version=deployment,
        correlation=correlation or {},
        payload=payload,
    )
    await db.execute(
        text("INSERT INTO quality_outbox(id,project_id,event) VALUES(:id,:p,CAST(:ev AS jsonb))"),
        dict(id=eid, p=project, ev=json.dumps(event, default=str)),
    )
    return eid


async def finish_batch(db: AsyncSession, b: dict[str, Any]) -> None:
    batch = b["id"]
    trial_rows = await trials(db, batch)
    if len(trial_rows) != 3 or any(t["status"] not in TERMINAL for t in trial_rows):
        return
    task = await row(db, "SELECT * FROM evaluation_tasks WHERE id=:t", t=b["task_id"])
    snap = task["config_snapshot"]
    slots: dict[UUID, list[dict[str, Any]]] = {}
    # All three executions exist at creation, even if a run was later cancelled.
    executions = (
        await db.execute(
            text("""SELECT c.id,c.case_id,s.case_key,c.status,c.latency_ms,t.trial,
        COALESCE((SELECT jsonb_object_agg(g.evaluator_version_id,jsonb_build_object(
          'id',g.id,'passed',g.passed,'score',g.score,'status',g.status,'evidence',g.evidence,
          'reason',g.reason)) FROM
        evaluation_scores g
         WHERE g.execution_id=c.id),'{}') AS scores FROM regression_trials t
        JOIN case_executions c ON c.run_id=t.run_id JOIN evaluation_cases s ON s.id=c.case_id
        WHERE t.batch_id=:b ORDER BY s.case_key,t.trial"""),
            {"b": batch},
        )
    ).mappings()
    required_refs = {
        n["id"] if r["evaluator_ref"] == "self" else r["evaluator_ref"]
        for n in snap["evaluator_plan"]
        for r in (n.get("rubrics") or [])
        if r["evidence_required"]
    }
    for item in executions:
        for ref in required_refs & item["scores"].keys():
            score = item["scores"][ref]
            scope = await row(db, SCOPE, id=item["id"])
            statuses = [(await resolve(db, scope, r))["status"] for r in score["evidence"]]
            score["verified_evidence"] = bool(statuses) and all(s == "AVAILABLE" for s in statuses)
        slots.setdefault(item["case_id"], []).append(dict(item))
    failures: list[str] = []
    blocked: list[str] = []
    passed: list[str] = []
    results = []
    for case, values in slots.items():
        result = evaluate_case(values, snap)
        key = values[0]["case_key"]
        {"PASSED": passed, "FAILED": failures, "BLOCKED": blocked}[result["gate"]].append(key)
        results.append(dict(b=batch, id=case, k=key, r=json.dumps(result, default=str)))
    if results:
        await db.execute(
            text("""INSERT INTO regression_results(batch_id,id,case_key,result)
            VALUES(:b,:id,:k,CAST(:r AS jsonb))"""),
            results,
        )
    gate = "FAILED" if failures else "BLOCKED" if blocked or not slots else "PASSED"
    ref = f"/api/v1/regression-batches/{batch}"
    payload = dict(
        regression_batch_id=batch,
        task_id=b["task_id"],
        project_version_id=task["project_version_id"],
        dataset_version_id=task["dataset_version_id"],
        evaluator_version_ids=[n["id"] for n in snap["evaluator_plan"]],
        trials=trial_rows,
        blocking_failures=failures,
        gate=gate,
        report_ref=ref + "/report",
        case_results_ref=ref + "/case-results",
    )
    usage = [await model_usage(db, t["run_id"], snap) for t in trial_rows]
    report = {
        **payload,
        "blocked_cases": blocked,
        "case_count": len(slots),
        "core_pass_rate": len(passed) / len(slots) if slots else None,
        "usage_by_trial": usage,
        "currency": snap.get("report_policy", {}).get("currency"),
        "passed_cases": len(passed),
        "release_allowed": gate == "PASSED",
        "snapshot_digest": task["config_digest"],
        "mean_latency_ms": mean(times)
        if (
            times := [
                x["latency_ms"] for s in slots.values() for x in s if x["latency_ms"] is not None
            ]
        )
        else None,
    }
    if b["baseline_id"]:
        previous = await row(
            db, "SELECT report FROM regression_batches WHERE id=:b", b=b["baseline_id"]
        )
        report["baseline"] = {
            "id": b["baseline_id"],
            "passed_cases": previous["report"]["passed_cases"],
            "mean_latency_ms": previous["report"]["mean_latency_ms"],
            "usage_by_trial": previous["report"]["usage_by_trial"],
            "same_rubrics": previous["report"]["evaluator_version_ids"]
            == payload["evaluator_version_ids"],
            "recurring_failures": sorted(
                set(failures) & set(previous["report"]["blocking_failures"])
            ),
        }
    await db.execute(
        text("UPDATE regression_batches SET report=CAST(:r AS jsonb) WHERE id=:b"),
        dict(b=batch, r=json.dumps(report, default=str)),
    )
    await enqueue(
        db,
        b["project_id"],
        snap["project_version"]["slug"],
        b["environment"],
        snap["project_version"]["deployment_ref"],
        "REGRESSION_BATCH_COMPLETED",
        payload,
    )
