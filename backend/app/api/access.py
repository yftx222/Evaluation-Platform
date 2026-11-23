import hashlib
import secrets
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Annotated
from uuid import UUID

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.access.identity import IdentityProvider, Principal
from app.application.catalog.store import Fault, audit, row
from app.infrastructure.resources import Resources


async def session(request: Request) -> AsyncIterator[AsyncSession]:
    resources: Resources = request.app.state.resources
    async with resources.sessions() as db:
        yield db
        await db.commit()


Db = Annotated[AsyncSession, Depends(session, scope="function")]


@dataclass(frozen=True)
class ProjectKey:
    project: UUID
    scopes: list[str]


async def authenticate(request: Request, db: Db) -> Principal | ProjectKey:
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    denied = Fault(401, "INVALID_CREDENTIAL", "凭证无效或已到期")
    if scheme.lower() == "bearer":
        provider: IdentityProvider = request.app.state.identities
        principal = provider.authenticate(token)
        if principal:
            return principal
    if scheme.lower() == "apikey":
        prefix, _, secret = token.partition(".")
        try:
            info = await row(
                db,
                """SELECT k.project_id,k.scopes,k.verifier FROM api_keys k
                JOIN projects p ON p.id=k.project_id WHERE k.prefix=:prefix AND p.enabled
                AND k.revoked_at IS NULL AND k.expires_at>now()""",
                prefix=prefix,
            )
        except Fault:
            raise denied from None
        if secrets.compare_digest(hashlib.sha256(secret.encode()).hexdigest(), info["verifier"]):
            return ProjectKey(info["project_id"], info["scopes"])
    raise denied


Actor = Annotated[Principal | ProjectKey, Depends(authenticate)]


def user(actor: Actor) -> Principal:
    if not isinstance(actor, Principal):
        raise Fault(403, "FORBIDDEN", "需要组织用户身份")
    return actor


User = Annotated[Principal, Depends(user)]


async def authorize(
    db: AsyncSession,
    actor: Principal | ProjectKey,
    p: UUID,
    level: str,
    *,
    key_read: bool = False,
    key_scope: str = "project:read",
    lock: bool = False,
) -> str:
    await row(db, "SELECT id FROM projects WHERE id=:p" + (" FOR UPDATE" if lock else ""), p=p)
    if isinstance(actor, ProjectKey):
        if key_read and actor.project == p and key_scope in actor.scopes:
            return "R"
        raise Fault(403, "FORBIDDEN", "凭证范围不足")
    if "Org" in actor.org_roles:
        return "A"
    result = await row(
        db,
        """SELECT role FROM project_members WHERE project_id=:p
        AND issuer=:i AND subject=:s""",
        p=p,
        i=actor.issuer,
        s=actor.subject,
    )
    role = str(result["role"])
    if "RWA".index(role) < "RWA".index(level):
        raise Fault(403, "FORBIDDEN", "项目权限不足")
    return role


def maintainer(who: User) -> Principal:
    if "Cat" not in who.org_roles:
        raise Fault(403, "FORBIDDEN", "需要共享目录维护权限 Cat")
    return who


Cat = Annotated[Principal, Depends(maintainer)]


async def record(
    db: Db, req: Request, who: Principal, action: str, kind: str, id_: UUID, fields: list[str]
) -> None:
    await audit(
        db, None, f"{who.issuer}/{who.subject}", req.state.request_id, action, kind, id_, fields
    )
