import asyncio
import logging
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Header, Request, Response
from redis.exceptions import RedisError
from sqlalchemy import text

from app.api.access import Actor, Db, User, authorize
from app.api.projects import ListArgs, record, reply
from app.application.catalog.dataset_files import sensitive
from app.application.catalog.store import Fault, page, row
from app.application.runner import plans, runs
from app.contracts.runs import Empty, TaskCreate, TaskPatch
from trace_sdk.privacy import PRIVATE_VALUE

router = APIRouter(prefix="/api/v1/evaluations", tags=["evaluations"])
Key = Annotated[
    str, Header(alias="Idempotency-Key", min_length=1, max_length=160, pattern=r"^[!-~]+$")
]


async def project_for(
    db: Db, table: Literal["project_versions", "evaluation_tasks", "evaluation_runs"], id_: UUID
) -> UUID:
    found = await row(db, f"SELECT project_id FROM {table} WHERE id=:id", id=id_)
    return UUID(str(found["project_id"]))


async def wake(req: Request, r: UUID) -> None:
    try:
        async with asyncio.timeout(1):
            await req.app.state.resources.redis.publish("evaluations:wake", str(r))
    except (RedisError, TimeoutError, OSError):
        logging.getLogger(__name__).warning("wake unavailable; durable run_id=%s", r)


@router.post("/tasks", status_code=201)
async def create(req: Request, db: Db, who: User, body: TaskCreate) -> dict[str, object]:
    project = await project_for(db, "project_versions", body.project_version_id)
    await authorize(db, who, project, "W", lock=True)
    if body.project_id is not None and body.project_id != project:
        raise Fault(422, "PROJECT_MISMATCH", "项目版本不属于指定项目")
    task = await plans.create_task(
        db,
        project,
        body,
        f"{who.issuer}/{who.subject}",
        req.app.state.settings.report_policies.get(body.project_version_id),
    )
    await record(req, db, who, project, "create", "task", task["id"], ["config_digest"])
    return reply(req, task)


@router.get("/tasks")
async def tasks(
    req: Request, db: Db, who: User, project_id: UUID, args: ListArgs, archived: bool | None = None
) -> dict[str, object]:
    await authorize(db, who, project_id, "R")
    return reply(
        req,
        await page(
            db,
            plans.TASK
            + " WHERE project_id=:p AND name ILIKE :q"
            + (
                ""
                if archived is None
                else " AND archived_at IS " + ("NOT NULL" if archived else "NULL")
            ),
            {"p": project_id, "q": "%" + args.q + "%"},
            args.cursor,
            args.limit,
            str(who),
            args.sort,
        ),
    )


@router.post("/tasks/preview")
async def preview(req: Request, db: Db, who: User, body: TaskCreate) -> dict[str, object]:
    p = await project_for(db, "project_versions", body.project_version_id)
    await authorize(db, who, p, "W")
    if body.project_id is not None and body.project_id != p:
        raise Fault(422, "PROJECT_MISMATCH", "项目版本不属于指定项目")
    snap = await plans.snapshot(
        db,
        p,
        body,
        validate_inputs=True,
        report_policy=req.app.state.settings.report_policies.get(body.project_version_id),
    )
    return reply(req, plans.public_snapshot(snap))


@router.get("/tasks/{t}")
async def task_detail(req: Request, db: Db, who: User, t: UUID) -> dict[str, object]:
    p = await project_for(db, "evaluation_tasks", t)
    await authorize(db, who, p, "R")
    task = await row(db, plans.TASK + " WHERE id=:t", t=t)
    saved = await row(db, "SELECT config_snapshot FROM evaluation_tasks WHERE id=:t", t=t)
    task["snapshot"] = plans.public_snapshot(saved["config_snapshot"])
    return reply(req, task)


@router.patch("/tasks/{t}")
async def edit(req: Request, db: Db, who: User, t: UUID, body: TaskPatch) -> dict[str, object]:
    p = await project_for(db, "evaluation_tasks", t)
    await authorize(db, who, p, "W", lock=True)
    if (await row(db, "SELECT archived_at FROM evaluation_tasks WHERE id=:t FOR UPDATE", t=t))[
        "archived_at"
    ]:
        raise Fault(409, "TASK_ARCHIVED", "任务已归档")
    values = body.model_dump(exclude_unset=True)
    fields = ",".join(k + "=:" + k for k in values)
    await db.execute(
        text(f"UPDATE evaluation_tasks SET {fields} WHERE id=:id"), {**values, "id": t}
    )
    await record(req, db, who, p, "edit", "task", t, list(values))
    return reply(req, await row(db, plans.TASK + " WHERE id=:t", t=t))


@router.delete("/tasks/{t}", status_code=204)
async def archive(req: Request, db: Db, who: User, t: UUID) -> Response:
    p = await project_for(db, "evaluation_tasks", t)
    await authorize(db, who, p, "W", lock=True)
    changed = await db.scalar(
        text("""UPDATE evaluation_tasks SET archived_at=clock_timestamp()
        WHERE id=:t AND archived_at IS NULL RETURNING id"""),
        {"t": t},
    )
    if changed:
        await record(req, db, who, p, "archive", "task", t, ["archived_at"])
    return Response(status_code=204)


@router.post("/tasks/{t}/runs", status_code=202)
async def start(
    req: Request, db: Db, who: User, t: UUID, key: Key, body: Empty | None = None
) -> dict[str, object]:
    p = await project_for(db, "evaluation_tasks", t)
    await authorize(db, who, p, "W", lock=True)
    run, created = await runs.start(db, t, key)
    if created:
        await record(req, db, who, p, "start", "run", run["id"], ["status", "snapshot_digest"])
        await db.commit()
        await wake(req, run["id"])
    return reply(req, run)


@router.get("/tasks/{t}/runs")
async def list_runs(
    req: Request, db: Db, who: Actor, t: UUID, args: ListArgs, status: str | None = None
) -> dict[str, object]:
    p = await project_for(db, "evaluation_tasks", t)
    await authorize(db, who, p, "R", key_read=True, key_scope="results:read")
    sql = runs.RUN + " WHERE r.task_id=:t"
    params: dict[str, Any] = {"t": t}
    if status is not None:
        sql += " AND r.status=:status"
        params["status"] = status
    return reply(req, await page(db, sql, params, args.cursor, args.limit, str(who), args.sort))


@router.get("/runs/{r}")
async def detail(req: Request, db: Db, who: Actor, r: UUID) -> dict[str, object]:
    await authorize(
        db,
        who,
        await project_for(db, "evaluation_runs", r),
        "R",
        key_read=True,
        key_scope="results:read",
    )
    return reply(req, await runs.detail(db, r))


@router.get("/runs/{r}/results")
async def results(
    req: Request,
    db: Db,
    who: Actor,
    r: UUID,
    args: ListArgs,
    status: str | None = None,
    case_key: str | None = None,
    execution_id: UUID | None = None,
    evaluator_version_id: UUID | None = None,
) -> dict[str, object]:
    await authorize(
        db,
        who,
        await project_for(db, "evaluation_runs", r),
        "R",
        key_read=True,
        key_scope="results:read",
    )
    sql = runs.RESULT
    params: dict[str, Any] = {"r": r}
    if status is not None:
        sql += " AND c.status=:status"
        params["status"] = status
    if execution_id is not None:
        sql += " AND c.id=:execution"
        params["execution"] = execution_id
    if case_key is not None:
        sql += " AND s.case_key=:key"
        params["key"] = case_key
    if evaluator_version_id is not None:
        sql += """ AND EXISTS(SELECT 1 FROM evaluation_scores g
            WHERE g.execution_id=c.id AND g.evaluator_version_id=:v)"""
        params["v"] = evaluator_version_id
    result = await page(db, sql, params, args.cursor, args.limit, str(who), args.sort)
    for item in result["items"]:
        item["input"] = sensitive(item["input"])
        attempts = [a for a in item["attempts"] if a["trace_id"]]
        selected = next(
            (a for a in attempts if a["id"] == str(item["prediction_attempt_id"])),
            attempts[-1] if attempts else None,
        )
        item["trace_status"] = selected["trace_status"] if selected else "NOT_COLLECTED"
        item["prediction"] = sensitive(item["prediction"])
        for score in item["scores"]:
            usage = score.get("details", {}).get("usage")
            safe = sensitive(score)
            if "evidence" in safe:
                safe["evidence"] = [
                    PRIVATE_VALUE.sub("[REDACTED]", ref) for ref in safe["evidence"]
                ]
            if isinstance(usage, dict):
                safe["details"]["usage"] = {
                    k: v
                    for k, v in usage.items()
                    if k in {"prompt_tokens", "completion_tokens", "total_tokens"}
                    and type(v) is int
                }
            score.update(safe)
    return reply(req, result)


async def change(
    req: Request, db: Db, who: User, r: UUID, action: Literal["pause", "resume", "cancel"]
) -> dict[str, object]:
    p = await project_for(db, "evaluation_runs", r)
    await authorize(db, who, p, "W", lock=True)
    run, changed = await runs.control(db, r, action)
    if changed:
        await record(req, db, who, p, action, "run", r, ["status"])
        if action == "resume":
            await db.commit()
            await wake(req, r)
    return reply(req, run)


@router.post("/runs/{r}/pause", status_code=202)
async def pause(
    req: Request, db: Db, who: User, r: UUID, body: Empty | None = None
) -> dict[str, object]:
    return await change(req, db, who, r, "pause")


@router.post("/runs/{r}/resume")
async def resume(
    req: Request, db: Db, who: User, r: UUID, body: Empty | None = None
) -> dict[str, object]:
    return await change(req, db, who, r, "resume")


@router.post("/runs/{r}/cancel", status_code=202)
async def cancel(
    req: Request, db: Db, who: User, r: UUID, body: Empty | None = None
) -> dict[str, object]:
    return await change(req, db, who, r, "cancel")
