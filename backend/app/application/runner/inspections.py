import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.catalog.store import Fault, row
from app.application.runner import runs
from app.application.runner.regression import enqueue
from app.infrastructure.resources import Resources, Settings


async def approved_task(
    db: AsyncSession, cfg: Settings, project: UUID, task: UUID, env: str
) -> dict[str, Any]:
    task_row = await row(
        db, "SELECT * FROM evaluation_tasks WHERE id=:t AND project_id=:p", t=task, p=project
    )
    if task_row["project_version_id"] not in cfg.isolated_quality_versions.get(env, set()):
        raise Fault(403, "ISOLATION_REQUIRED", "请配置该环境已验证的只读或隔离目标版本")
    return task_row


async def inspections(resources: Resources, cfg: Settings) -> None:
    async with resources.transaction() as db:
        plan = (
            (
                await db.execute(
                    text("""SELECT p.*,t.project_version_id FROM inspection_plans p
            JOIN evaluation_tasks t ON t.id=p.task_id JOIN projects j ON j.id=p.project_id
            WHERE p.enabled AND p.next_at<=now() AND j.enabled ORDER BY p.next_at LIMIT 1
            FOR UPDATE OF p SKIP LOCKED""")
                )
            )
            .mappings()
            .first()
        )
        if plan:
            try:
                async with db.begin_nested():
                    await approved_task(
                        db, cfg, plan["project_id"], plan["task_id"], plan["environment"]
                    )
                    # Coalesce missed windows onto the latest scheduled slot.
                    interval = plan["period_s"]
                    slot = plan["next_at"].timestamp()
                    slot += max(0, int(datetime.now(UTC).timestamp() - slot) // interval) * interval
                    instant = datetime.fromtimestamp(slot, UTC)
                    run, _ = await runs.start(
                        db, plan["task_id"], f"inspection:{plan['id']}:{instant.isoformat()}"
                    )
                    await db.execute(
                        text(
                            """INSERT INTO inspection_windows(plan_id,slot,run_id) VALUES(:p,
                                :slot,:r)"""
                        ),
                        dict(p=plan["id"], slot=instant, r=run["id"]),
                    )
                    await db.execute(
                        text(
                            """UPDATE inspection_plans SET
                                next_at=CAST(:slot AS timestamptz)+make_interval(secs=>:period)
                                WHERE id=:p"""
                        ),
                        dict(slot=instant, period=interval, p=plan["id"]),
                    )
            except Fault:
                await db.execute(
                    text("UPDATE inspection_plans SET enabled=false WHERE id=:p"), {"p": plan["id"]}
                )
    async with resources.transaction() as db:
        window = (
            (
                await db.execute(
                    text("""SELECT w.*,p.threshold,p.environment,p.project_id,r.status,
                        r.config_snapshot
            FROM inspection_windows w JOIN inspection_plans p ON p.id=w.plan_id JOIN
            evaluation_runs r ON r.id=w.run_id
            WHERE w.result IS NULL AND r.status IN ('COMPLETED','PARTIAL_FAILED','FAILED',
            'CANCELLED')
            ORDER BY w.slot LIMIT 1 FOR UPDATE OF w SKIP LOCKED""")
                )
            )
            .mappings()
            .first()
        )
        if window:
            snap = window["config_snapshot"]
            cases = (
                (
                    await db.execute(
                        text("""SELECT s.case_key,c.status='SUCCEEDED' AND
                (SELECT count(*) FROM evaluation_scores g WHERE g.execution_id=c.id AND
                g.status='OK' AND g.passed
                AND g.evaluator_version_id::text=ANY(:roots))=:n AS passed FROM case_executions c
                JOIN evaluation_cases s ON s.id=c.case_id WHERE c.run_id=:r ORDER BY s.case_key"""),
                        dict(r=window["run_id"], roots=snap["roots"], n=len(snap["roots"])),
                    )
                )
                .mappings()
                .all()
            )
            failed = [c["case_key"] for c in cases if not c["passed"]]
            rate = (len(cases) - len(failed)) / len(cases) if cases else None
            result = dict(
                run_id=window["run_id"],
                plan_id=window["plan_id"],
                case_count=len(cases),
                pass_rate=rate,
                failures=failed,
                threshold=float(window["threshold"]),
                report_ref=f"/api/v1/reports/runs/{window['run_id']}",
            )
            if rate is None or rate < window["threshold"]:
                await enqueue(
                    db,
                    window["project_id"],
                    snap["project_version"]["slug"],
                    window["environment"],
                    snap["project_version"]["deployment_ref"],
                    "INSPECTION_ANOMALY",
                    {k: v for k, v in result.items() if k != "failures"},
                )
            await db.execute(
                text("UPDATE inspection_windows SET result=CAST(:r AS jsonb) WHERE run_id=:id"),
                dict(r=json.dumps(result, default=str), id=window["run_id"]),
            )
