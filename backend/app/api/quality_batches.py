from typing import Any
from uuid import UUID

from fastapi import APIRouter, Request

from app.api.access import Db, User, authorize
from app.api.evaluations import Key
from app.api.production_traces import Limit
from app.api.projects import reply
from app.api.quality_access import Grant, require
from app.application.catalog.store import Fault, page, row
from app.application.runner import regression
from app.contracts.adapters import Contract
from app.contracts.quality import Name

router = APIRouter(prefix="/api/v1", tags=["regression-batches"])


class Trigger(Contract):
    task_id: UUID
    project_version_id: UUID
    environment: Name
    baseline_id: UUID | None = None


@router.post("/regression-batches")
async def trigger(req: Request, db: Db, g: Grant, body: Trigger, key: Key) -> dict[str, object]:
    require(g, "batch:start", body.environment)
    if body.project_version_id not in g.target_versions:
        raise Fault(403, "SCOPE_DENIED", "该环境未授权触发此候选版本")
    return reply(
        req,
        await regression.create_batch(
            db,
            g.project_id,
            body.environment,
            body.task_id,
            body.project_version_id,
            key,
            body.baseline_id,
        ),
    )


@router.post("/projects/{project}/regression-batches")
async def human_trigger(
    project: UUID, req: Request, db: Db, who: User, body: Trigger, key: Key
) -> dict[str, object]:
    await authorize(db, who, project, "W")
    return reply(
        req,
        await regression.create_batch(
            db,
            project,
            body.environment,
            body.task_id,
            body.project_version_id,
            key,
            body.baseline_id,
        ),
    )


async def batch(db: Db, g: Grant, b: UUID) -> dict[str, Any]:
    require(g, "batch:read")
    return await row(
        db,
        """SELECT b.*,t.dataset_version_id FROM regression_batches b
        JOIN evaluation_tasks t ON t.id=b.task_id WHERE b.id=:b AND b.project_id=:p AND
        b.environment=:e""",
        b=b,
        p=g.project_id,
        e=g.environment,
    )


@router.get("/regression-batches/{b}")
async def status(b: UUID, req: Request, db: Db, g: Grant) -> dict[str, object]:
    await batch(db, g, b)
    return reply(req, {"regression_batch_id": b, "trials": await regression.trials(db, b)})


@router.get("/regression-batches/{b}/report")
async def report(b: UUID, req: Request, db: Db, g: Grant) -> dict[str, object]:
    record = await batch(db, g, b)
    if record["report"] is None:
        raise Fault(409, "REPORT_PENDING", "三次试验尚未生成报告")
    return reply(req, record["report"])


@router.get("/regression-batches/{b}/case-results")
async def case_results(
    b: UUID, req: Request, db: Db, g: Grant, limit: Limit = 50, cursor: str | None = None
) -> dict[str, object]:
    record = await batch(db, g, b)
    if record["report"] is None:
        raise Fault(409, "REPORT_PENDING", "三次试验尚未生成报告")
    data = await page(
        db,
        "SELECT * FROM regression_results WHERE batch_id=:b",
        {"b": b},
        cursor,
        limit,
        g.subject,
        "created_at",
    )
    data["items"] = [
        {
            "evaluation_case_id": r["id"],
            "case_key": r["case_key"],
            "trial_results": r["result"]["trial_results"],
        }
        for r in data["items"]
    ]
    return reply(
        req, {"regression_batch_id": b, "dataset_version_id": record["dataset_version_id"], **data}
    )
