from typing import Any
from uuid import UUID

from fastapi import APIRouter, Request, Response

from app.api.access import Cat, Db, User, record
from app.api.projects import ListArgs, reply
from app.application.catalog import evaluators as service
from app.application.catalog.store import page, row
from app.contracts.evaluators import EvaluatorCreate, EvaluatorPatch, Kind, Publish, Trial
from app.plugins.evaluators import registry
from app.plugins.judge import CompatibleJudge

router = APIRouter(prefix="/api/v1/evaluators", tags=["evaluators"])


@router.get("/implementations")
async def implementations(req: Request, who: User) -> dict[str, object]:
    return reply(
        req,
        [
            {
                "impl_key": p.impl_key,
                "build": p.build,
                "kind": p.kind,
                "config_schema": p.schema.model_json_schema(),
                "requirements": p.requirements,
                "example": p.example,
                "capabilities": ["single_sample"],
                "resource_limits": {"cpu_seconds": 2, "memory_mib": 128, "wall_seconds": 3}
                if p.kind == "code"
                else {},
                "evidence_types": ["prediction_pointer"],
                "output_schema": "EvaluationResult",
            }
            for p in registry.plugins.values()
        ],
    )


@router.get("")
async def listing(
    req: Request,
    db: Db,
    who: User,
    args: ListArgs,
    kind: Kind | None = None,
    enabled: bool | None = None,
) -> dict[str, object]:
    sql = service.CATALOG + " WHERE (e.name ILIKE :q OR e.slug ILIKE :q)"
    params: dict[str, Any] = {"q": "%" + args.q + "%"}
    for key, value in (("kind", kind), ("enabled", enabled)):
        if value is not None:
            sql += " AND e." + key + "=:" + key
            params[key] = value
    return reply(req, await page(db, sql, params, args.cursor, args.limit, str(who), args.sort))


@router.post("", status_code=201)
async def create(req: Request, db: Db, who: Cat, body: EvaluatorCreate) -> dict[str, object]:
    item = await row(
        db,
        "INSERT INTO evaluators(slug,name,kind) VALUES (:slug,:name,:kind) RETURNING *",
        **body.model_dump(),
    )
    await record(db, req, who, "create", "evaluator", item["id"], list(body.model_fields_set))
    return reply(req, item)


@router.get("/{e}")
async def detail(req: Request, db: Db, who: User, e: UUID) -> dict[str, object]:
    item = await row(db, service.CATALOG + " WHERE e.id=:e", e=e)
    item["version_count"] = (
        await row(db, "SELECT count(*) AS n FROM evaluator_versions WHERE evaluator_id=:e", e=e)
    )["n"]
    return reply(req, item)


@router.patch("/{e}")
async def update(
    req: Request, db: Db, who: Cat, e: UUID, body: EvaluatorPatch
) -> dict[str, object]:
    changes = body.model_dump(exclude_unset=True)
    assignment = ", ".join(f"{k}=:{k}" for k in changes)
    item = await row(
        db, f"UPDATE evaluators SET {assignment} WHERE id=:e RETURNING *", e=e, **changes
    )
    await record(db, req, who, "update", "evaluator", e, list(changes))
    return reply(req, item)


@router.get("/{e}/versions")
async def versions(req: Request, db: Db, who: User, e: UUID, args: ListArgs) -> dict[str, object]:
    await row(db, "SELECT id FROM evaluators WHERE id=:e", e=e)
    return reply(
        req,
        await page(
            db,
            service.VERSIONS + " WHERE v.evaluator_id=:e",
            {"e": e},
            args.cursor,
            args.limit,
            str(who),
            args.sort,
        ),
    )


@router.post("/{e}/versions", status_code=201)
async def publish(req: Request, db: Db, who: Cat, e: UUID, body: Publish) -> dict[str, object]:
    item = await service.publish(db, e, body, req.app.state.settings.judge_accounts)
    await record(
        db, req, who, "publish", "evaluator_version", item["id"], list(body.model_fields_set)
    )
    return reply(req, item)


@router.post("/{e}/test")
async def test_version(
    req: Request, response: Response, db: Db, who: Cat, e: UUID, body: Trial
) -> dict[str, object]:
    service.check_size(body.model_dump(mode="json"), 98304)
    await row(
        db,
        "SELECT id FROM evaluator_versions WHERE id=:v AND evaluator_id=:e",
        v=body.version_id,
        e=e,
    )
    nodes = await service.graph(db, [body.version_id])
    await record(db, req, who, "test", "evaluator_version", body.version_id, [])
    await db.commit()  # release the request connection before the external call
    client = CompatibleJudge(req.app.state.settings, req.app.state.secrets, req.app.state.resources)
    result = await service.trial(nodes, body, client)
    output = reply(req, result)
    if result.reason in {"JUDGE_TIMEOUT", "TRIAL_TIMEOUT"}:
        response.status_code = 504
        output["error"] = {"code": result.reason, "message": "试评超过时限", "retryable": False}
    return output
