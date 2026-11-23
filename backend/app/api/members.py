import hashlib
import secrets
from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Request, Response
from sqlalchemy import text

from app.api.access import Db, User, authorize
from app.api.projects import ListArgs, record, reply
from app.application.catalog.store import Fault, page, row
from app.contracts.catalog import KeyCreate, KeyExpiry, MemberCreate, MemberRole, Role

router = APIRouter(prefix="/api/v1/projects/{p}")
MEMBERS = "SELECT id,issuer,subject,role,created_at FROM project_members WHERE project_id=:p"
KEY_META = "id,project_id,name,prefix,scopes,expires_at,revoked_at,created_at"


@router.get("/members")
async def members(
    request: Request, p: UUID, db: Db, who: User, listing: ListArgs, role: Role | None = None
) -> dict[str, object]:
    await authorize(db, who, p, "A")
    sql = MEMBERS + " AND subject ILIKE :q"
    params: dict[str, object] = {"p": p, "q": "%" + listing.q + "%"}
    if role:
        sql += " AND role=:role"
        params["role"] = role
    return reply(
        request, await page(db, sql, params, listing.cursor, listing.limit, str(who), listing.sort)
    )


@router.post("/members", status_code=201)
async def add(
    request: Request, p: UUID, db: Db, who: User, body: MemberCreate
) -> dict[str, object]:
    await authorize(db, who, p, "A", lock=True)
    if not request.app.state.identities.resolve_member(body.issuer, body.subject):
        raise Fault(422, "UNKNOWN_MEMBER", "未知组织成员")
    member = await row(
        db,
        """INSERT INTO project_members(project_id,issuer,subject,role)
        VALUES (:p,:issuer,:subject,:role) RETURNING id,issuer,subject,role,created_at""",
        p=p,
        **body.model_dump(),
    )
    await record(request, db, who, p, "add", "member", member["id"], ["issuer", "subject", "role"])
    return reply(request, member)


async def protect_admin(db: Db, p: UUID, m: UUID, new_role: Role | None) -> None:
    current = await row(db, MEMBERS + " AND id=:m", p=p, m=m)
    if current["role"] == "A" and new_role != "A":
        count = await db.scalar(
            text("SELECT count(*) FROM project_members WHERE project_id=:p AND role='A'"), {"p": p}
        )
        if count == 1:
            raise Fault(409, "LAST_ADMIN", "项目需要至少一名管理员")


@router.patch("/members/{m}")
async def edit(
    request: Request, p: UUID, m: UUID, db: Db, who: User, body: MemberRole
) -> dict[str, object]:
    await authorize(db, who, p, "A", lock=True)
    await protect_admin(db, p, m, body.role)
    await db.execute(
        text("UPDATE project_members SET role=:role WHERE id=:m AND project_id=:p"),
        dict(role=body.role, m=m, p=p),
    )
    await record(request, db, who, p, "edit", "member", m, ["role"])
    return reply(request, await row(db, MEMBERS + " AND id=:m", p=p, m=m))


@router.delete("/members/{m}", status_code=204)
async def remove(request: Request, p: UUID, m: UUID, db: Db, who: User) -> Response:
    await authorize(db, who, p, "A", lock=True)
    await protect_admin(db, p, m, None)
    await db.execute(
        text("DELETE FROM project_members WHERE id=:m AND project_id=:p"), dict(m=m, p=p)
    )
    await record(request, db, who, p, "remove", "member", m, [])
    return Response(status_code=204)


@router.get("/api-keys")
async def keys(
    request: Request, p: UUID, db: Db, who: User, listing: ListArgs, state: str | None = None
) -> dict[str, object]:
    await authorize(db, who, p, "A")
    filters = {
        "active": "revoked_at IS NULL AND expires_at>now()",
        "revoked": "revoked_at IS NOT NULL",
        "expired": "revoked_at IS NULL AND expires_at<=now()",
    }
    if state is not None and state not in filters:
        raise Fault(422, "INVALID_STATE", "无效状态")
    sql = f"SELECT {KEY_META} FROM api_keys WHERE project_id=:p AND name ILIKE :q"
    if state:
        sql += " AND " + filters[state]
    return reply(
        request,
        await page(
            db,
            sql,
            dict(p=p, q="%" + listing.q + "%"),
            listing.cursor,
            listing.limit,
            str(who),
            listing.sort,
        ),
    )


async def issue(
    request: Request, response: Response, p: UUID, db: Db, who: User, body: KeyCreate
) -> dict[str, object]:
    if body.expires_at <= datetime.now(UTC) or len(set(body.scopes)) != len(body.scopes):
        raise Fault(422, "INVALID_KEY", "有效期或范围不合法")
    prefix = secrets.token_hex(12)
    secret = secrets.token_urlsafe(32)
    key = await row(
        db,
        f"""INSERT INTO api_keys(project_id,name,prefix,verifier,created_by,
        scopes,expires_at) VALUES (:p,:name,:prefix,:v,:actor,:scopes,:expires_at)
        RETURNING {KEY_META}""",
        p=p,
        prefix=prefix,
        v=hashlib.sha256(secret.encode()).hexdigest(),
        actor=f"{who.issuer}/{who.subject}",
        **body.model_dump(),
    )
    await record(request, db, who, p, "issue", "api_key", key["id"], ["scopes", "expires_at"])
    response.headers["Cache-Control"] = "no-store"
    return reply(request, {"key": key, "secret": prefix + "." + secret})


@router.post("/api-keys", status_code=201)
async def create_key(
    request: Request, response: Response, p: UUID, db: Db, who: User, body: KeyCreate
) -> dict[str, object]:
    await authorize(db, who, p, "A", lock=True)
    return await issue(request, response, p, db, who, body)


async def revoke(request: Request, p: UUID, k: UUID, db: Db, who: User) -> None:
    await db.execute(
        text("UPDATE api_keys SET revoked_at=now() WHERE project_id=:p AND id=:k"), dict(p=p, k=k)
    )
    await record(request, db, who, p, "revoke", "api_key", k, ["revoked_at"])


@router.post("/api-keys/{k}/rotate", status_code=201)
async def rotate(
    request: Request, response: Response, p: UUID, k: UUID, db: Db, who: User, body: KeyExpiry
) -> dict[str, object]:
    await authorize(db, who, p, "A", lock=True)
    key = await row(db, f"SELECT {KEY_META} FROM api_keys WHERE project_id=:p AND id=:k", p=p, k=k)
    if key["revoked_at"]:
        raise Fault(409, "KEY_REVOKED", "凭证已吊销")
    await revoke(request, p, k, db, who)
    return await issue(
        request,
        response,
        p,
        db,
        who,
        KeyCreate(name=key["name"], scopes=key["scopes"], expires_at=body.expires_at),
    )


@router.delete("/api-keys/{k}", status_code=204)
async def delete_key(request: Request, p: UUID, k: UUID, db: Db, who: User) -> Response:
    await authorize(db, who, p, "A", lock=True)
    key = await row(db, "SELECT revoked_at FROM api_keys WHERE project_id=:p AND id=:k", p=p, k=k)
    if key["revoked_at"] is None:
        await revoke(request, p, k, db, who)
    return Response(status_code=204)
