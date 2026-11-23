from typing import Any, Literal
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.catalog.store import Fault, digest, row
from app.application.runner.plans import public_snapshot, snapshot
from app.contracts.runs import TaskCreate

RUN = """SELECT r.id,r.project_id,r.task_id,r.status,r.total_cases,r.total_cases AS total_count,
    r.config_digest AS snapshot_digest,r.started_at,r.finished_at,r.created_at,
    r.cancel_requested_at,r.last_error_code,c.*
    FROM evaluation_runs r CROSS JOIN LATERAL (
    SELECT count(*) FILTER (WHERE status IN ('PENDING','READY','RETRY_WAIT')) AS pending_cases,
      count(*) FILTER (WHERE status='RUNNING') AS running_cases,
      count(*) FILTER (WHERE status='SUCCEEDED') AS succeeded_cases,
      count(*) FILTER (WHERE status='FAILED') AS failed_cases,
      count(*) FILTER (WHERE status='CANCELLED') AS cancelled_cases,
      count(*) FILTER (WHERE status='TIMED_OUT') AS timed_out_cases,
      count(*) FILTER (WHERE status='UNKNOWN_DELIVERY') AS unknown_delivery_cases
    FROM case_executions WHERE run_id=r.id) c"""
RESULT = """SELECT c.id,c.id AS execution_id,c.created_at,c.run_id,c.case_id,s.case_key,
    s.input,c.status,
    c.stage,c.attempt_count,c.latency_ms,c.prediction,c.prediction_attempt_id,c.adapter_error_code,
    COALESCE((SELECT jsonb_agg(jsonb_build_object('id',a.id,'stage',a.stage,'status',a.status,
    'trace_id',a.trace_id,'trace_status',t.status,'trace_event_count',t.event_count,
    'attempt_no',a.attempt_no,'epoch',a.epoch,'delivery_state',a.delivery_state,
    'evaluator_version_id',a.evaluator_version_id,'error_code',a.error_code) ORDER BY a.attempt_no)
    FROM execution_attempts a LEFT JOIN traces t ON t.id=a.trace_id
    WHERE a.execution_id=c.id),'[]') AS attempts,
    COALESCE((SELECT jsonb_agg(to_jsonb(g) ORDER BY g.evaluator_version_id)
    FROM evaluation_scores g WHERE g.execution_id=c.id),'[]') AS scores,
    COALESCE((SELECT jsonb_agg(a.trace_id ORDER BY a.attempt_no) FROM execution_attempts a
    WHERE a.execution_id=c.id AND a.trace_id IS NOT NULL),'[]') AS trace_ids
    FROM case_executions c JOIN evaluation_cases s ON s.id=c.case_id WHERE c.run_id=:r"""


def ratio(numerator: int, denominator: int) -> dict[str, int | float | None]:
    return {
        "numerator": numerator,
        "denominator": denominator,
        "value": numerator / denominator if denominator else None,
    }


def score_metrics(ok: int, errors: int, passed: int, planned: int) -> dict[str, Any]:
    return {
        "score_execution_success_rate": ratio(ok, ok + errors),
        "score_coverage": ratio(ok, planned),
        "pass_rate": ratio(passed, ok),
    }


async def detail(db: AsyncSession, r: UUID) -> dict[str, Any]:
    result = await row(db, RUN + " WHERE r.id=:r", r=r)
    stored = await row(db, "SELECT config_snapshot FROM evaluation_runs WHERE id=:r", r=r)
    result["snapshot"] = public_snapshot(stored["config_snapshot"])
    plan = stored["config_snapshot"]["evaluator_plan"]
    counts = (
        await db.execute(
            text("""SELECT s.evaluator_version_id,
        count(*) FILTER(WHERE s.status='OK') ok,count(*) FILTER(WHERE s.status='ERROR') errors,
        count(*) FILTER(WHERE s.status='SKIPPED') skipped,count(*) FILTER(WHERE s.passed) passed
        FROM evaluation_scores s JOIN case_executions c ON c.id=s.execution_id
        WHERE c.run_id=:r GROUP BY s.evaluator_version_id"""),
            {"r": r},
        )
    ).mappings()
    by_id: dict[str, dict[str, Any]] = {str(c["evaluator_version_id"]): dict(c) for c in counts}
    totals = [0, 0, 0]
    total = result["total_cases"]
    versions = {}
    for node in plan:
        values = by_id.get(node["id"], {})
        ok, errors, passed = (values.get(k, 0) for k in ("ok", "errors", "passed"))
        skipped = values.get("skipped", 0)
        versions[node["id"]] = {
            **score_metrics(ok, errors, passed, total),
            "skipped": skipped,
            "missing": total - ok - errors - skipped,
        }
        totals = [a + b for a, b in zip(totals, (ok, errors, passed), strict=True)]
    result["metrics"] = {
        **score_metrics(totals[0], totals[1], totals[2], total * len(plan)),
        "case_success_rate": ratio(result["succeeded_cases"], total),
        "by_version": versions,
    }
    result["score_counts"] = {
        "OK": totals[0],
        "ERROR": totals[1],
        "SKIPPED": sum(v["skipped"] for v in versions.values()),
        "passed": totals[2],
        "failed": totals[0] - totals[2],
    }
    result["failure_reasons"] = [
        dict(item)
        for item in (
            await db.execute(
                text("""
        SELECT code,count(*) AS cases FROM (SELECT DISTINCT c.id,coalesce(a.error_code,
        c.adapter_error_code,c.status) AS code FROM case_executions c
        LEFT JOIN execution_attempts a ON a.execution_id=c.id AND a.error_code IS NOT NULL
        AND NOT EXISTS(SELECT 1 FROM execution_attempts later WHERE later.execution_id=c.id
        AND later.logical_step_key=a.logical_step_key AND later.attempt_no>a.attempt_no)
        WHERE c.run_id=:r AND c.status IN ('FAILED','TIMED_OUT','UNKNOWN_DELIVERY')) failures
        GROUP BY code ORDER BY cases DESC,code"""),
                {"r": r},
            )
        ).mappings()
    ]
    return result


async def start(db: AsyncSession, t: UUID, key: str) -> tuple[dict[str, Any], bool]:
    task = await row(db, "SELECT * FROM evaluation_tasks WHERE id=:t FOR UPDATE", t=t)
    previous = await db.scalar(
        text("SELECT id FROM evaluation_runs WHERE task_id=:t AND request_key=:k"),
        {"t": t, "k": key},
    )
    if previous:
        return await detail(db, previous), False
    if task["archived_at"] is not None:
        raise Fault(409, "TASK_ARCHIVED", "任务已归档，历史运行仍可查看")
    snap = task["config_snapshot"]
    selection = TaskCreate(
        name=task["name"],
        project_version_id=task["project_version_id"],
        dataset_version_id=task["dataset_version_id"],
        evaluator_version_ids=snap["roots"],
        policy=snap["policy"],
    )
    verified = await snapshot(db, task["project_id"], selection, validate_inputs=False)
    if "report_policy" in snap:
        verified["report_policy"] = snap["report_policy"]
    if "required_adapter_capabilities" not in snap:
        verified.pop("required_adapter_capabilities", None)
    for added in ("retryable_error_codes", "run_timeout_s"):
        if added not in snap["policy"]:
            verified["policy"].pop(added, None)
    if digest(verified) != task["config_digest"] or digest(snap) != task["config_digest"]:
        raise Fault(409, "SNAPSHOT_CHANGED", "冻结版本与部署不一致")
    run = await row(
        db,
        """INSERT INTO evaluation_runs(task_id,project_id,dataset_version_id,
        request_key,config_snapshot,config_digest,total_cases)
        SELECT id,project_id,dataset_version_id,:k,config_snapshot,config_digest,
        (SELECT count(*) FROM evaluation_cases WHERE dataset_version_id=t.dataset_version_id)
        FROM evaluation_tasks t WHERE id=:t RETURNING id""",
        k=key,
        t=t,
    )
    await db.execute(
        text("""INSERT INTO case_executions(run_id,dataset_version_id,case_id)
        SELECT :r,dataset_version_id,id FROM evaluation_cases WHERE dataset_version_id=:v"""),
        {"r": run["id"], "v": task["dataset_version_id"]},
    )
    await db.execute(
        text("UPDATE case_executions SET status='READY' WHERE run_id=:r"), {"r": run["id"]}
    )
    await db.execute(
        text("UPDATE evaluation_runs SET status='QUEUED' WHERE id=:r"), {"r": run["id"]}
    )
    return await detail(db, run["id"]), True


async def control(
    db: AsyncSession, r: UUID, action: Literal["pause", "resume", "cancel"]
) -> tuple[dict[str, Any], bool]:
    current = await row(db, "SELECT status FROM evaluation_runs WHERE id=:r FOR UPDATE", r=r)
    state = current["status"]
    params = {"r": r}
    if action == "pause" and state in ("QUEUED", "RUNNING"):
        await db.execute(
            text("UPDATE evaluation_runs SET status='PAUSE_REQUESTED' WHERE id=:r"), params
        )
        await db.execute(
            text("""UPDATE evaluation_runs SET status='PAUSED' WHERE id=:r AND NOT EXISTS
            (SELECT 1 FROM case_executions WHERE run_id=:r AND status='RUNNING')"""),
            params,
        )
    elif action == "resume" and state == "PAUSED":
        await db.execute(text("UPDATE evaluation_runs SET status='QUEUED' WHERE id=:r"), params)
    elif action == "cancel" and state in (
        "PENDING",
        "QUEUED",
        "RUNNING",
        "PAUSED",
        "PAUSE_REQUESTED",
    ):
        await db.execute(
            text("""UPDATE evaluation_runs SET status='CANCEL_REQUESTED',
            cancel_requested_at=now() WHERE id=:r"""),
            params,
        )
        await db.execute(
            text("""UPDATE case_executions SET status='CANCELLED',finished_at=now()
            WHERE run_id=:r AND status IN ('PENDING','READY','RETRY_WAIT')"""),
            params,
        )
        await db.execute(
            text("""UPDATE evaluation_runs SET status='CANCELLED',finished_at=now()
            WHERE id=:r AND NOT EXISTS(SELECT 1 FROM case_executions
            WHERE run_id=:r AND status='RUNNING')"""),
            params,
        )
    elif (
        state
        in {
            "pause": ("PAUSED", "PAUSE_REQUESTED"),
            "resume": ("QUEUED",),
            "cancel": ("CANCELLED", "CANCEL_REQUESTED"),
        }[action]
    ):
        return await detail(db, r), False
    else:
        raise Fault(409, "INVALID_TRANSITION", "当前运行状态不允许此操作")
    return await detail(db, r), True
