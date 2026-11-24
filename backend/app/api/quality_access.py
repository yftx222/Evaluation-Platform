import hashlib
import secrets
from datetime import UTC, datetime
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy import text

from app.api.access import Db
from app.application.catalog.store import Fault
from app.contracts.quality import ServiceGrant
from app.infrastructure.resources import Settings


async def service(req: Request, db: Db) -> ServiceGrant:
    scheme, _, value = req.headers.get("authorization", "").partition(" ")
    cfg: Settings = req.app.state.settings
    denied = Fault(401, "INVALID_CREDENTIAL", "需要有效的服务凭据")
    if (
        scheme.lower() != "bearer"
        or len(req.headers.getlist("authorization")) != 1
        or not value
        or len(value) > 512
        or (cfg.mode == "production" and req.url.scheme != "https")
    ):
        raise denied
    verifier = hashlib.sha256(value.encode()).hexdigest()
    matches = [g for g in cfg.quality_grants if secrets.compare_digest(g.verifier, verifier)]
    if len(matches) != 1 or matches[0].expires_at <= datetime.now(UTC):
        raise denied
    grant = matches[0]
    for name, expected in (
        ("project_id", str(grant.project_id)),
        ("environment", grant.environment),
    ):
        if any(value != expected for value in req.query_params.getlist(name)):
            raise Fault(403, "SCOPE_DENIED", "查询范围与服务身份不一致")
    if not await db.scalar(
        text("SELECT enabled FROM projects WHERE id=:p"), {"p": grant.project_id}
    ):
        raise denied
    return grant


Grant = Annotated[ServiceGrant, Depends(service)]


def require(grant: ServiceGrant, scope: str, environment: str | None = None) -> None:
    if scope not in grant.scopes or environment not in (None, grant.environment):
        raise Fault(403, "SCOPE_DENIED", "该服务身份没有此范围的权限")
