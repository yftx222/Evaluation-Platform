import json
from datetime import UTC, datetime
from typing import Any, Literal, Self
from uuid import UUID, uuid4

from fastapi import APIRouter, Request
from pydantic import AwareDatetime, Field, model_validator
from sqlalchemy import text

from app.api.access import Db, User, authorize
from app.api.production_traces import visible
from app.api.projects import reply
from app.api.quality_access import Grant, require
from app.application.catalog import datasets
from app.application.catalog.store import Fault, audit, digest, row
from app.application.runner import plans, runs
from app.application.runner.inspections import approved_task
from app.application.runner.regression import enqueue
from app.contracts.adapters import Contract
from app.contracts.datasets import Case, DatasetCreate, VersionAction
from app.contracts.quality import Name
from app.contracts.runs import ExecutionPolicy, TaskCreate
from app.contracts.traces import check_fields

router = APIRouter(prefix="/api/v1", tags=["online-quality"])


class Inspection(Contract):
    task_id: UUID
    environment: Name
    starts_at: AwareDatetime
    period_s: int = Field(ge=60, le=31536000)
    threshold: float = Field(ge=0, le=1)


class Shadow(Contract):
    task_id: UUID
    environment: Name
    sample_rate: float = Field(ge=0, le=1)
    max_parallel: int = Field(ge=1, le=128)
    timeout_s: int = Field(ge=1, le=3600)


class Sample(Contract):
    trace_id: UUID
    input: dict[str, Any]
    expected: dict[str, Any]


class Decision(Contract):
    decision: Literal["MATCH", "DIFFERENCE"]
    review_ref: Name


class Experiment(Contract):
    environment: Name
    control_id: UUID
    candidate_id: UUID
    ratio: float = Field(gt=0, lt=1)
    starts_at: AwareDatetime
    ends_at: AwareDatetime
    max_sessions: int = Field(ge=1, le=10000000)

    @model_validator(mode="after")
    def window(self) -> Self:
        if self.control_id == self.candidate_id or self.starts_at >= self.ends_at:
            raise ValueError("请选择不同版本和有效时间窗口")
        return self


class Assignment(Contract):
    conversation_id: Name


class Observation(Assignment):
    trace_id: UUID
    variant_id: Literal["control", "candidate"]
    deployment_version: Name
    outcome: Name
    guardrail_ref: Name | None = None


def bucket(plan: UUID, salt: str, key: str) -> float:
    return int(digest([plan, salt, key])[:16], 16) / 2**64


@router.post("/projects/{p}/inspection-plans", status_code=201)
async def inspect_plan(
    p: UUID, req: Request, db: Db, who: User, body: Inspection
) -> dict[str, object]:
    await authorize(db, who, p, "W")
    await approved_task(db, req.app.state.settings, p, body.task_id, body.environment)
    plan = await row(
        db,
        """INSERT INTO inspection_plans(project_id,environment,task_id,period_s,threshold,next_at)
        VALUES(:p,:environment,:task_id,:period_s,:threshold,:starts_at) RETURNING *""",
        p=p,
        **body.model_dump(),
    )
    return reply(req, plan)


@router.post("/projects/{p}/shadow-plans", status_code=201)
async def shadow_plan(p: UUID, req: Request, db: Db, who: User, body: Shadow) -> dict[str, object]:
    await authorize(db, who, p, "W")
    await approved_task(db, req.app.state.settings, p, body.task_id, body.environment)
    return reply(
        req,
        await row(
            db,
            """INSERT INTO shadow_plans(project_id,environment,task_id,sample_rate,max_parallel,
                timeout_s)
        VALUES(:p,:environment,:task_id,:sample_rate,:max_parallel,:timeout_s) RETURNING *""",
            p=p,
            **body.model_dump(),
        ),
    )


@router.post("/shadow-plans/{plan}/samples")
async def shadow_sample(
    plan: UUID, req: Request, db: Db, g: Grant, body: Sample
) -> dict[str, object]:
    require(g, "shadow:submit")
    cfg = req.app.state.settings
    spec = await row(
        db,
        "SELECT * FROM shadow_plans WHERE id=:id AND project_id=:p AND environment=:e FOR UPDATE",
        id=plan,
        p=g.project_id,
        e=g.environment,
    )
    trace = await visible(db, g, body.trace_id)
    raw = body.model_dump(mode="json")
    fingerprint = digest(raw)
    prior = (
        (
            await db.execute(
                text("SELECT run_id,digest FROM shadow_pairs WHERE plan_id=:p AND trace_id=:t"),
                dict(p=plan, t=body.trace_id),
            )
        )
        .mappings()
        .first()
    )
    if prior:
        if prior["digest"] != fingerprint:
            raise Fault(409, "SAMPLE_CONFLICT", "相同轨迹已绑定其他输入")
        return reply(req, {"run_id": prior["run_id"], "trace_id": body.trace_id})
    if not spec["enabled"] or bucket(plan, "shadow", str(body.trace_id)) >= spec["sample_rate"]:
        return reply(req, {"sampled": False})
    template = await approved_task(db, cfg, g.project_id, spec["task_id"], g.environment)
    if trace["completeness"] != "COMPLETE" or trace["availability"] != "AVAILABLE":
        raise Fault(409, "EVIDENCE_REQUIRED", "请先完成可查阅的生产轨迹")
    active = await db.scalar(
        text("""SELECT count(*) FROM shadow_pairs s JOIN evaluation_runs r ON r.id=s.run_id
        WHERE s.plan_id=:p AND r.status NOT IN ('COMPLETED','PARTIAL_FAILED','FAILED',
        'CANCELLED')"""),
        {"p": plan},
    )
    if active >= spec["max_parallel"]:
        raise Fault(429, "SHADOW_LIMIT", "影子计划已达并发上限")
    try:
        check_fields(body.input)
        check_fields(body.expected)
        case = Case(
            case_key="shadow",
            input={
                **body.input,
                "context": {
                    **body.input.get("context", {}),
                    "execution_mode": "shadow",
                    "session_id": str(uuid4()),
                },
            },
            expected=body.expected,
        )
    except (TypeError, ValueError) as exc:
        raise Fault(422, "INVALID_SAMPLE", "需要脱敏且格式正确的影子输入") from exc
    ds = await datasets.create(
        db, DatasetCreate(slug="shadow-" + uuid4().hex, name="影子样本 " + str(body.trace_id))
    )
    await db.execute(
        text("UPDATE datasets SET shadow_project_id=:p WHERE id=:d"),
        {"p": g.project_id, "d": ds["id"]},
    )
    await datasets.add(db, ds["id"], ds["draft_id"], case)
    dataset = await datasets.version_action(
        db,
        ds["id"],
        VersionAction(
            action="publish",
            draft_id=ds["draft_id"],
            reviewed_digest=await datasets.checksum(db, ds["draft_id"]),
        ),
    )
    snap = template["config_snapshot"]
    task = await plans.create_task(
        db,
        g.project_id,
        TaskCreate(
            name="Shadow " + str(body.trace_id),
            project_version_id=template["project_version_id"],
            dataset_version_id=dataset["id"],
            evaluator_version_ids=snap["roots"],
            policy=ExecutionPolicy.model_validate(
                {
                    **snap["policy"],
                    "case_timeout_s": spec["timeout_s"],
                    "run_timeout_s": spec["timeout_s"],
                    "max_parallel": 1,
                }
            ),
        ),
        g.subject,
    )
    run, _ = await runs.start(db, task["id"], "shadow:" + str(body.trace_id))
    await db.execute(
        text("INSERT INTO shadow_pairs(plan_id,trace_id,run_id,digest) VALUES(:p,:t,:r,:h)"),
        dict(p=plan, t=body.trace_id, r=run["id"], h=fingerprint),
    )
    return reply(req, {"run_id": run["id"], "trace_id": body.trace_id})


@router.post("/projects/{p}/shadow-plans/{plan}/pairs/{tid}/review")
async def review(
    p: UUID, plan: UUID, tid: UUID, body: Decision, req: Request, db: Db, who: User
) -> dict[str, object]:
    await authorize(db, who, p, "W")
    pair = await row(
        db,
        """SELECT s.*,q.environment,r.status,r.config_snapshot FROM shadow_pairs s
        JOIN shadow_plans q ON q.id=s.plan_id JOIN evaluation_runs r ON r.id=s.run_id WHERE
        q.project_id=:p
        AND q.id=:plan AND s.trace_id=:t FOR UPDATE OF s""",
        p=p,
        plan=plan,
        t=tid,
    )
    if pair["decision"] is not None:
        if pair["decision"] != body.decision or pair["review_ref"] != body.review_ref:
            raise Fault(409, "REVIEW_CONFLICT", "已有另一项审核结论")
    elif pair["status"] != "COMPLETED":
        raise Fault(409, "RESULT_REQUIRED", "候选运行尚无完整结果")
    else:
        await db.execute(
            text(
                """UPDATE shadow_pairs SET decision=:decision,review_ref=:review_ref WHERE
                    plan_id=:q AND trace_id=:t"""
            ),
            dict(**body.model_dump(), q=plan, t=tid),
        )
        if body.decision == "DIFFERENCE":
            pv = pair["config_snapshot"]["project_version"]
            await enqueue(
                db,
                p,
                pv["slug"],
                pair["environment"],
                pv["deployment_ref"],
                "SHADOW_DIFFERENCE",
                {"run_id": pair["run_id"], "review_ref": body.review_ref},
                {"trace_id": tid},
            )
        await audit(
            db, p, who.subject, req.state.request_id, "review", "shadow_pair", tid, ["decision"]
        )
    return reply(req, {"decision": body.decision, "run_id": pair["run_id"], "trace_id": tid})


@router.post("/projects/{p}/experiments", status_code=201)
async def experiment(
    p: UUID, body: Experiment, req: Request, db: Db, who: User
) -> dict[str, object]:
    await authorize(db, who, p, "A")
    for v in (body.control_id, body.candidate_id):
        await row(db, "SELECT id FROM project_versions WHERE project_id=:p AND id=:v", p=p, v=v)
    exp = await row(
        db,
        """INSERT INTO quality_experiments(project_id,environment,control_id,candidate_id,ratio,
            starts_at,ends_at,max_sessions)
        VALUES(:p,:environment,:control_id,:candidate_id,:ratio,:starts_at,:ends_at,
        :max_sessions) RETURNING *""",
        p=p,
        **body.model_dump(),
    )
    return reply(req, exp)


@router.get("/projects/{p}/online/{kind}/{identity}")
async def online_detail(
    p: UUID,
    kind: Literal["inspection", "shadow", "experiment"],
    identity: UUID,
    req: Request,
    db: Db,
    who: User,
) -> dict[str, object]:
    await authorize(db, who, p, "R")
    table, details = {
        "inspection": (
            "inspection_plans",
            """SELECT slot,run_id,result FROM inspection_windows
            WHERE plan_id=:id ORDER BY slot DESC""",
        ),
        "shadow": (
            "shadow_plans",
            """SELECT s.trace_id,s.run_id,s.decision,s.review_ref,c.status,
            c.prediction,
            a.trace_id AS candidate_trace_id FROM shadow_pairs s JOIN case_executions c ON
            c.run_id=s.run_id
            LEFT JOIN execution_attempts a ON a.id=c.prediction_attempt_id WHERE plan_id=:id
            ORDER BY s.created_at DESC""",
        ),
        "experiment": (
            "quality_experiments",
            """SELECT variant,deployment,count(DISTINCT s.conversation_id) AS sessions,
            count(o.trace_id) AS observed FROM experiment_sessions s LEFT JOIN
            experiment_observations o USING(experiment_id,conversation_id)
            WHERE experiment_id=:id GROUP BY variant,deployment ORDER
            BY variant""",
        ),
    }[kind]
    plan = await row(db, f"SELECT * FROM {table} WHERE id=:id AND project_id=:p", id=identity, p=p)
    plan.pop("salt", None)
    records = [
        dict(r)
        for r in (
            await db.execute(
                text(details + " LIMIT 100"),
                {"id": identity},
            )
        ).mappings()
    ]
    return reply(req, {"plan": plan, "records": records})


class Switch(Contract):
    enabled: bool


@router.patch("/projects/{p}/online/{kind}/{identity}")
async def switch(
    p: UUID,
    kind: Literal["inspection", "shadow", "experiment"],
    identity: UUID,
    body: Switch,
    req: Request,
    db: Db,
    who: User,
) -> dict[str, object]:
    await authorize(db, who, p, "A" if kind == "experiment" else "W")
    table = {
        "inspection": "inspection_plans",
        "shadow": "shadow_plans",
        "experiment": "quality_experiments",
    }[kind]
    updated = await row(
        db,
        f"UPDATE {table} SET enabled=:v WHERE id=:id AND project_id=:p RETURNING id,enabled",
        p=p,
        id=identity,
        v=body.enabled,
    )
    await audit(db, p, who.subject, req.state.request_id, "switch", kind, identity, ["enabled"])
    return reply(req, updated)


@router.post("/experiments/{id}/assign")
async def assign(id: UUID, body: Assignment, req: Request, db: Db, g: Grant) -> dict[str, object]:
    require(g, "experiment:observe")
    exp = await row(
        db,
        """SELECT * FROM quality_experiments WHERE id=:id AND project_id=:p AND environment=:e
            FOR UPDATE""",
        id=id,
        p=g.project_id,
        e=g.environment,
    )
    if not exp["enabled"] or not exp["starts_at"] <= datetime.now(UTC) < exp["ends_at"]:
        return reply(req, {"assigned": False})
    params = dict(id=id, c=body.conversation_id)
    previous = (
        (
            await db.execute(
                text(
                    """SELECT variant,deployment FROM experiment_sessions WHERE
                        experiment_id=:id AND conversation_id=:c"""
                ),
                params,
            )
        )
        .mappings()
        .first()
    )
    if previous:
        return reply(
            req,
            {
                "assigned": True,
                "variant_id": previous["variant"],
                "deployment_version": previous["deployment"],
            },
        )
    if exp["assigned"] >= exp["max_sessions"]:
        return reply(req, {"assigned": False})
    variant = (
        "candidate"
        if bucket(id, str(exp["salt"]), body.conversation_id) < exp["ratio"]
        else "control"
    )
    pv = await row(
        db, "SELECT deployment_ref FROM project_versions WHERE id=:v", v=exp[variant + "_id"]
    )
    await db.execute(
        text(
            """INSERT INTO experiment_sessions(experiment_id,conversation_id,variant,
                deployment) VALUES(:id,:c,:v,:dep)"""
        ),
        {**params, "v": variant, "dep": pv["deployment_ref"]},
    )
    await db.execute(
        text("UPDATE quality_experiments SET assigned=assigned+1 WHERE id=:id"), {"id": id}
    )
    return reply(
        req, {"assigned": True, "variant_id": variant, "deployment_version": pv["deployment_ref"]}
    )


@router.post("/experiments/{id}/observe")
async def observe(id: UUID, body: Observation, req: Request, db: Db, g: Grant) -> dict[str, object]:
    require(g, "experiment:observe")
    trace = await visible(db, g, body.trace_id)
    session = await row(
        db,
        """SELECT s.*,p.slug FROM experiment_sessions s JOIN quality_experiments q ON
            q.id=s.experiment_id
        JOIN projects p ON p.id=q.project_id WHERE q.id=:id AND q.project_id=:p AND q.environment=:e
        AND conversation_id=:c FOR UPDATE OF s""",
        id=id,
        p=g.project_id,
        e=g.environment,
        c=body.conversation_id,
    )
    expected = {
        "experiment_id": str(id),
        "variant_id": body.variant_id,
        "conversation_id": body.conversation_id,
        "deployment_version": body.deployment_version,
    }
    if (
        session["variant"] != body.variant_id
        or session["deployment"] != body.deployment_version
        or any(trace["root"].get(k) != v for k, v in expected.items())
    ):
        raise Fault(409, "ASSIGNMENT_MISMATCH", "回报版本或轨迹与实际分组不一致")
    result = body.model_dump(mode="json")
    prior = await db.scalar(
        text("SELECT result FROM experiment_observations WHERE trace_id=:t"), {"t": body.trace_id}
    )
    if prior and prior != result:
        raise Fault(409, "OBSERVATION_CONFLICT", "已有不同的业务结果")
    if not prior:
        await db.execute(
            text(
                """INSERT INTO experiment_observations
                    (trace_id,experiment_id,conversation_id,result)
                    VALUES(:t,:id,:c,CAST(:r AS jsonb))"""
            ),
            dict(r=json.dumps(result), t=body.trace_id, id=id, c=body.conversation_id),
        )
        if body.guardrail_ref:
            await enqueue(
                db,
                g.project_id,
                session["slug"],
                g.environment,
                body.deployment_version,
                "AB_GUARDRAIL",
                {"guardrail_ref": body.guardrail_ref, "outcome": body.outcome},
                {
                    "experiment_id": id,
                    "variant_id": body.variant_id,
                    "conversation_id": body.conversation_id,
                    "trace_id": body.trace_id,
                },
            )
    return reply(req, {"recorded": True})
