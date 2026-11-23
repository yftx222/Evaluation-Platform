import asyncio
import json
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy import text

from app.api.access import Actor, Db, User, authorize
from app.application.access.identity import Principal
from app.application.catalog.store import Fault, audit, digest, page, row, version_adapter_config
from app.contracts.adapters import AdapterError, Contract
from app.contracts.catalog import Kind, ProjectCreate, ProjectPatch, Publish
from app.contracts.http import AdapterConfig
from app.plugins.http_adapter import DestinationPolicy, registry

router = APIRouter(prefix="/api/v1")
PROJECT = """SELECT p.id,p.slug,p.name,p.kind,p.enabled,p.max_parallel,p.target_max_parallel,
    p.rpm,p.created_at,p.updated_at,coalesce((SELECT a.secret_ref IS NOT NULL
    FROM project_adapters a WHERE a.project_id=p.id ORDER BY a.revision DESC LIMIT 1),
    false) AS has_credential FROM projects p"""


def reply(request: Request, data: object) -> dict[str, object]:
    return {"data": data, "request_id": request.state.request_id}


class Listing:
    def __init__(
        self,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        cursor: str | None = None,
        sort: str = "-created_at",
        q: str = "",
    ) -> None:
        self.limit, self.cursor, self.sort, self.q = limit, cursor, sort, q


ListArgs = Annotated[Listing, Depends()]


async def record(
    request: Request,
    db: Db,
    who: Principal,
    p: UUID,
    action: str,
    resource: str,
    resource_id: UUID,
    fields: list[str],
) -> None:
    await audit(
        db,
        p,
        f"{who.issuer}/{who.subject}",
        request.state.request_id,
        action,
        resource,
        resource_id,
        fields,
    )


@router.get("/auth/me")
async def me(request: Request, who: User) -> dict[str, object]:
    return reply(request, who)


@router.get("/adapters/implementations")
async def implementations(request: Request, who: User) -> dict[str, object]:
    return reply(request, registry.describe())


@router.get("/projects")
async def projects(
    request: Request,
    db: Db,
    who: User,
    listing: ListArgs,
    kind: Kind | None = None,
    enabled: bool | None = None,
) -> dict[str, object]:
    sql = PROJECT + " WHERE p.name ILIKE :q"
    params: dict[str, Any] = {"q": "%" + listing.q + "%"}
    if "Org" not in who.org_roles:
        sql += (
            " AND EXISTS (SELECT 1 FROM project_members m WHERE m.project_id=p.id"
            " AND m.issuer=:issuer AND m.subject=:subject)"
        )
        params.update(issuer=who.issuer, subject=who.subject)
    if kind is not None:
        sql += " AND p.kind=:kind"
        params["kind"] = kind
    if enabled is not None:
        sql += " AND p.enabled=:enabled"
        params["enabled"] = enabled
    return reply(
        request, await page(db, sql, params, listing.cursor, listing.limit, str(who), listing.sort)
    )


@router.post("/projects", status_code=201)
async def create(request: Request, db: Db, who: User, body: ProjectCreate) -> dict[str, object]:
    if "Org" not in who.org_roles:
        raise Fault(403, "FORBIDDEN", "需要组织管理员")
    project = await row(
        db,
        """INSERT INTO projects(slug,name,kind,max_parallel,target_max_parallel,rpm)
        VALUES (:slug,:name,:kind,:max_parallel,:target_max_parallel,:rpm) RETURNING id""",
        **body.model_dump(),
    )
    p = project["id"]
    await db.execute(
        text("""INSERT INTO project_members(project_id,issuer,subject,role)
        VALUES (:p,:i,:s,'A')"""),
        dict(p=p, i=who.issuer, s=who.subject),
    )
    await record(request, db, who, p, "create", "project", p, list(body.model_fields_set))
    return reply(request, await row(db, PROJECT + " WHERE p.id=:p", p=p))


@router.get("/projects/{p}")
async def detail(request: Request, p: UUID, db: Db, actor: Actor) -> dict[str, object]:
    await authorize(db, actor, p, "R", key_read=True)
    return reply(request, await row(db, PROJECT + " WHERE p.id=:p", p=p))


@router.patch("/projects/{p}")
async def edit(
    request: Request, p: UUID, db: Db, who: User, body: ProjectPatch
) -> dict[str, object]:
    await authorize(db, who, p, "A", lock=True)
    fields = body.model_dump(exclude_unset=True)
    assignments = ", ".join(f"{key}=:{key}" for key in fields)
    await db.execute(
        text(f"UPDATE projects SET {assignments},updated_at=now() WHERE id=:p"), dict(p=p, **fields)
    )
    await record(request, db, who, p, "edit", "project", p, list(fields))
    return reply(request, await row(db, PROJECT + " WHERE p.id=:p", p=p))


@router.delete("/projects/{p}", status_code=204)
async def disable(request: Request, p: UUID, db: Db, who: User) -> Response:
    await edit(request, p, db, who, ProjectPatch(enabled=False))
    return Response(status_code=204)


@router.get("/projects/{p}/permissions")
async def permissions(request: Request, p: UUID, db: Db, who: User) -> dict[str, object]:
    role = await authorize(db, who, p, "R")
    actions = ["project:read", "tasks:read", "results:read"]
    if role in ("A", "W"):
        actions.extend(["tasks:write", "runs:control"])
    if role == "A":
        actions.extend(
            ["project:edit", "members:manage", "keys:manage", "versions:publish", "connection:test"]
        )
    return reply(request, {"project_id": p, "role": role, "actions": actions})


VERSION = """SELECT v.id,v.project_id,v.label,v.deployment_ref,v.digest,v.created_at,
    v.trace_capabilities,a.kind,a.build,a.digest AS adapter_digest FROM project_versions v
    JOIN project_adapters a ON a.id=v.adapter_id WHERE v.project_id=:p"""


@router.get("/projects/{p}/versions")
async def versions(
    request: Request, p: UUID, db: Db, actor: Actor, listing: ListArgs
) -> dict[str, object]:
    await authorize(db, actor, p, "R", key_read=True)
    return reply(
        request,
        await page(
            db,
            VERSION + " AND v.label ILIKE :q",
            dict(p=p, q="%" + listing.q + "%"),
            listing.cursor,
            listing.limit,
            str(actor),
            listing.sort,
        ),
    )


def policy_for(request: Request, slug: str) -> DestinationPolicy:
    settings = request.app.state.settings
    return DestinationPolicy(settings.targets.get(slug, []), settings.mode == "development")


@router.post("/projects/{p}/versions", status_code=201)
async def publish(request: Request, p: UUID, db: Db, who: User, body: Publish) -> dict[str, object]:
    await authorize(db, who, p, "A")
    project = await row(db, "SELECT slug FROM projects WHERE id=:p", p=p)
    # DNS validation occurs outside a DB transaction; reauthorize after reacquiring the lock.
    await db.commit()
    policy = policy_for(request, project["slug"])
    cfg = body.adapter
    if (cfg.kind, cfg.build) == ("http", "1"):
        raise Fault(422, "BUILD_RETIRED", "新 HTTP 版本请使用 build 2")
    registry.create(cfg, policy, request.app.state.secrets, project["slug"])
    fingerprint = registry.digest(cfg.kind, cfg.build)
    async with asyncio.timeout(cfg.timeout_s):
        await policy.approve(cfg.address(cfg.endpoint))
        await policy.approve(cfg.address(cfg.probe_endpoint))
    async with db.begin():
        await authorize(db, who, p, "A", lock=True)
        data = cfg.model_dump(exclude={"kind", "build", "secret_ref", "secret_version"})
        data["_implementation_digest"] = fingerprint
        a = await row(
            db,
            """INSERT INTO project_adapters(project_id,revision,kind,build,config,
            secret_ref,secret_version,digest) SELECT :p,COALESCE(max(revision),0)+1,:kind,:build,
            CAST(:cfg AS jsonb),:ref,:ver,:digest FROM project_adapters WHERE project_id=:p
            RETURNING id""",
            p=p,
            kind=cfg.kind,
            build=cfg.build,
            cfg=json.dumps(data),
            ref=cfg.secret_ref,
            ver=cfg.secret_version,
            digest=digest([cfg.model_dump(), fingerprint]),
        )
        v = await row(
            db,
            """INSERT INTO project_versions(project_id,adapter_id,label,deployment_ref,
            digest,trace_capabilities)
            VALUES (:p,:a,:label,:ref,:digest,CAST(:trace AS jsonb)) RETURNING id""",
            p=p,
            a=a["id"],
            trace=body.trace_capabilities.model_dump_json(),
            label=body.label,
            ref=body.deployment_ref,
            digest=digest([body.model_dump(), fingerprint]),
        )
        await record(
            request,
            db,
            who,
            p,
            "publish",
            "project_version",
            v["id"],
            ["label", "deployment_ref", "adapter"],
        )
        return reply(request, await row(db, VERSION + " AND v.id=:id", p=p, id=v["id"]))


class Probe(Contract):
    version_id: UUID | None = None
    adapter: AdapterConfig | None = None


@router.post("/projects/{p}/test-connection")
async def probe(request: Request, p: UUID, db: Db, who: User, body: Probe) -> dict[str, object]:
    await authorize(db, who, p, "A", lock=True)
    project = await row(db, "SELECT slug,enabled FROM projects WHERE id=:p", p=p)
    if not project["enabled"]:
        raise Fault(409, "PROJECT_DISABLED", "项目已停用")
    if (body.version_id is None) == (body.adapter is None):
        raise Fault(422, "INVALID_INPUT", "请选择一个版本或候选配置")
    cfg = body.adapter
    if cfg is None:
        assert body.version_id is not None
        cfg = await version_adapter_config(db, p, body.version_id)
    slug = project["slug"]
    adapter = registry.create(cfg, policy_for(request, slug), request.app.state.secrets, slug)
    await db.commit()
    async with request.app.state.resources.target_permit(p, cfg.timeout_s):
        try:
            result = await adapter.validate_connection()
        except AdapterError as exc:
            if exc.code == "TARGET_TIMEOUT":
                raise Fault(504, exc.code, "探测超时") from exc
            if exc.code == "DESTINATION_DENIED":
                raise Fault(422, exc.code, "目标未获准") from exc
            return reply(
                request,
                {
                    "ok": False,
                    "code": exc.code,
                    "message": "探测失败",
                    "latency_ms": None,
                    "capabilities": [],
                },
            )
    return reply(request, result)
