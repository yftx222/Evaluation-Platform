import json
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Request
from sqlalchemy import text

from app.api.access import Db, User, authorize
from app.api.evaluations import Key
from app.api.production_traces import Limit
from app.api.projects import reply
from app.application.catalog import evaluators
from app.application.catalog.store import Fault, page, row
from app.application.runner.regression import trials
from app.application.trace.evidence import bind
from app.contracts.adapters import Contract
from app.contracts.evaluators import Trial
from app.plugins.judge import CompatibleJudge

router = APIRouter(prefix="/api/v1/projects/{p}", tags=["quality-reports"])


class Rescore(Contract):
    evaluator_version_id: UUID


@router.get("/case-executions/{execution}/rescores")
async def history(p: UUID, execution: UUID, req: Request, db: Db, who: User) -> dict[str, object]:
    await authorize(db, who, p, "R")
    await row(
        db,
        """SELECT c.id FROM case_executions c JOIN evaluation_runs r ON c.run_id=r.id
        WHERE c.id=:id AND r.project_id=:p""",
        id=execution,
        p=p,
    )
    original = [
        dict(r)
        for r in (
            await db.execute(
                text("""SELECT id,evaluator_version_id,status,
        score,passed,reason,evidence FROM evaluation_scores WHERE execution_id=:id
        ORDER BY created_at,id"""),
                {"id": execution},
            )
        ).mappings()
    ]
    revised = [
        dict(r)
        for r in (
            await db.execute(
                text("""SELECT id,version_id,result,created_at
        FROM quality_rescores WHERE execution_id=:id ORDER BY created_at,id"""),
                {"id": execution},
            )
        ).mappings()
    ]
    return reply(req, {"original": original, "rescores": revised})


@router.post("/case-executions/{execution}/rescores")
async def rescore(
    p: UUID, execution: UUID, body: Rescore, key: Key, req: Request, db: Db, who: User
) -> dict[str, object]:
    await authorize(db, who, p, "W")
    frozen = await row(
        db,
        """SELECT c.prediction,to_jsonb(s)-ARRAY['id','created_at','dataset_version_id'] sample,
        r.config_snapshot->'policy' policy FROM case_executions c JOIN evaluation_cases s ON
        s.id=c.case_id
        JOIN evaluation_runs r ON r.id=c.run_id WHERE c.id=:id AND r.project_id=:p""",
        id=execution,
        p=p,
    )
    if frozen["prediction"] is None:
        raise Fault(409, "NO_PREDICTION", "没有可复用的历史回答")
    vid = body.evaluator_version_id
    nodes = await evaluators.graph(db, [vid])
    trial = Trial.model_validate(
        {
            "version_id": vid,
            "case": frozen["sample"],
            "prediction": frozen["prediction"],
            "materials": {"approved_policy_ids": frozen["policy"].get("approved_policy_ids", [])},
        }
    )
    params = dict(x=execution, v=vid, k=key)
    sid = await db.scalar(
        text("""INSERT INTO quality_rescores(execution_id,version_id,request_key,result)
        VALUES(:x,:v,:k,'{"state":"STARTED"}') ON CONFLICT DO NOTHING RETURNING id"""),
        params,
    )
    if not sid:
        prior = await row(
            db, "SELECT * FROM quality_rescores WHERE execution_id=:x AND request_key=:k", **params
        )
        if prior["version_id"] != vid:
            raise Fault(409, "RESCORE_CONFLICT", "该请求键已绑定另一版本")
        return reply(req, prior)
    await db.commit()
    judge = CompatibleJudge(req.app.state.settings, req.app.state.secrets, req.app.state.resources)
    result = await evaluators.trial(nodes, trial, judge)
    product = result.model_dump(mode="json")
    product["evidence"] = await bind(db, execution, result.evidence)
    saved = await row(
        db,
        "UPDATE quality_rescores SET result=CAST(:r AS jsonb) WHERE id=:id RETURNING *",
        id=sid,
        r=json.dumps({"state": "FINISHED", "score": product}),
    )
    return reply(req, saved)


@router.get("/quality-overview")
async def overview(p: UUID, req: Request, db: Db, who: User, environment: str) -> dict[str, object]:
    await authorize(db, who, p, "R")
    queries = {
        "batches": "SELECT id,task_id,created_at,report->>'gate' AS gate FROM regression_batches",
        "traces": """SELECT id,agent_run_id,completeness,availability,created_at FROM
            production_traces""",
        "inspections": "SELECT id,enabled,period_s,next_at FROM inspection_plans",
        "shadows": "SELECT id,enabled,sample_rate,max_parallel FROM shadow_plans",
        "experiments": "SELECT id,enabled,assigned,max_sessions,ends_at FROM quality_experiments",
    }
    data: dict[str, Any] = {}
    for name, q in queries.items():
        order = "created_at DESC,id" if name in {"batches", "traces"} else "id"
        data[name] = [
            dict(x)
            for x in (
                await db.execute(
                    text(
                        q
                        + " WHERE project_id=:p AND environment=:e ORDER BY "
                        + order
                        + " LIMIT 100"
                    ),
                    dict(p=p, e=environment),
                )
            ).mappings()
        ]
    return reply(req, data)


@router.get("/regression-batches/{batch}")
async def report(
    p: UUID,
    batch: UUID,
    req: Request,
    db: Db,
    who: User,
    limit: Limit = 25,
    cursor: str | None = None,
) -> dict[str, object]:
    await authorize(db, who, p, "R")
    stored = await row(
        db, "SELECT id,report FROM regression_batches WHERE project_id=:p AND id=:b", p=p, b=batch
    )
    stored["trials"] = await trials(db, batch)
    stored["cases"] = await page(
        db,
        "SELECT id,case_key,result,created_at FROM regression_results WHERE batch_id=:b",
        {"b": batch},
        cursor,
        limit,
        who.subject,
        "created_at",
    )
    return reply(req, stored)


@router.get("/production-traces/{trace}/timeline")
async def timeline(
    p: UUID, trace: UUID, req: Request, db: Db, who: User, after: int = 0, limit: Limit = 50
) -> dict[str, object]:
    from app.api.production_traces import summary
    from app.application.catalog.store import audit

    await authorize(db, who, p, "R")
    info = await row(
        db, "SELECT * FROM production_traces WHERE id=:id AND project_id=:p", id=trace, p=p
    )
    items = []
    if info["availability"] == "AVAILABLE":
        items = list(
            (
                await db.execute(
                    text("""SELECT metadata FROM production_trace_events WHERE trace_id=:t
            AND sequence>:after ORDER BY sequence LIMIT :limit"""),
                    dict(t=trace, after=after, limit=limit + 1),
                )
            ).scalars()
        )
    await audit(
        db,
        p,
        who.subject,
        req.state.request_id,
        "read",
        "production_trace_events",
        trace,
        ["metadata"],
    )
    return reply(
        req,
        {
            "trace": summary(info),
            "events": items[:limit],
            "next_after": items[limit - 1]["sequence"] if len(items) > limit else None,
        },
    )


@router.get("/quality-deliveries")
async def delivery_log(
    p: UUID,
    req: Request,
    db: Db,
    who: User,
    batch_id: UUID | None = None,
    limit: Limit = 50,
    cursor: str | None = None,
) -> dict[str, object]:
    await authorize(db, who, p, "R")
    sql = """SELECT id,created_at,state,attempts,last_error,receipt,event->>'event_type' event_type
        FROM quality_outbox WHERE project_id=:p"""
    params: dict[str, Any] = {"p": p}
    if batch_id is not None:
        sql += " AND event#>>'{payload,regression_batch_id}'=:b"
        params["b"] = str(batch_id)
    return reply(req, await page(db, sql, params, cursor, limit, who.subject, "-created_at"))


@router.post("/quality-deliveries/{event}/replay")
async def replay(p: UUID, event: UUID, req: Request, db: Db, who: User) -> dict[str, object]:
    await authorize(db, who, p, "A")
    record = await row(
        db,
        "SELECT state FROM quality_outbox WHERE id=:id AND project_id=:p FOR UPDATE",
        id=event,
        p=p,
    )
    if record["state"] != "FAILED":
        raise Fault(409, "INVALID_TRANSITION", "只能重放投递失败的事件")
    await db.execute(
        text("UPDATE quality_outbox SET state='PENDING',attempts=0,next_at=now() WHERE id=:id"),
        {"id": event},
    )
    return reply(req, {"event_id": event, "state": "PENDING"})
