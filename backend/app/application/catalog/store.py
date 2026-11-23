import base64
import hashlib
import json
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.contracts.adapters import AdapterError
from app.contracts.http import AdapterConfig
from app.plugins.http_adapter import LEGACY_RELEASES, registry


class Fault(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def digest(value: object) -> str:
    raw = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


async def row(db: AsyncSession, sql: str, **params: object) -> dict[str, Any]:
    result = (await db.execute(text(sql), params)).mappings().one_or_none()
    if result is None:
        raise Fault(404, "NOT_FOUND", "资源不可见或不存在")
    return dict(result)


async def page(
    db: AsyncSession,
    sql: str,
    params: dict[str, Any],
    cursor: str | None,
    limit: int,
    scope: str,
    sort: str,
) -> dict[str, Any]:
    if sort not in ("-created_at", "created_at"):
        raise Fault(422, "INVALID_SORT", "仅支持按创建时间排序")
    signature = digest([scope, sql, params, sort])
    direction = "DESC" if sort.startswith("-") else "ASC"
    query = f"SELECT * FROM ({sql}) AS visible"
    if cursor:
        try:
            saved, instant, last_id = json.loads(base64.urlsafe_b64decode(cursor))
            if saved != signature:
                raise ValueError
            params = {
                **params,
                "instant": datetime.fromisoformat(instant),
                "last_id": UUID(last_id),
            }
        except (ValueError, TypeError, UnicodeError) as exc:
            raise Fault(422, "INVALID_CURSOR", "游标无效") from exc
        operator = "<" if direction == "DESC" else ">"
        query += f" WHERE (created_at,id) {operator} (:instant,:last_id)"
    query += f" ORDER BY created_at {direction},id {direction} LIMIT :limit"
    rows = [
        dict(r) for r in (await db.execute(text(query), {**params, "limit": limit + 1})).mappings()
    ]
    more = len(rows) > limit
    items = rows[:limit]
    token = None
    if more:
        last = items[-1]
        raw = json.dumps([signature, last["created_at"].isoformat(), str(last["id"])])
        token = base64.urlsafe_b64encode(raw.encode()).decode()
    return {"items": items, "next_cursor": token}


async def audit(
    db: AsyncSession,
    project: UUID | None,
    actor: str,
    request_id: str,
    action: str,
    resource: str,
    resource_id: UUID,
    fields: list[str],
) -> None:
    await db.execute(
        text("""INSERT INTO audit_logs
        (project_id,actor,action,resource_type,resource_id,request_id,changes)
        VALUES (:p,:actor,:action,:resource,:id,:request,CAST(:changes AS jsonb))"""),
        dict(
            p=project,
            actor=actor,
            action=action,
            resource=resource,
            id=resource_id,
            request=request_id,
            changes=json.dumps({"fields": fields}),
        ),
    )


async def version_adapter_config(db: AsyncSession, project: UUID, version: UUID) -> AdapterConfig:
    stored = await row(
        db,
        """SELECT a.* FROM project_versions v JOIN project_adapters a
         ON v.adapter_id=a.id WHERE v.id=:v AND v.project_id=:p""",
        v=version,
        p=project,
    )
    config = dict(stored["config"])
    pinned = config.pop("_implementation_digest", None)
    kind, build = stored["kind"], stored["build"]
    deployed = registry.digest(kind, build)
    if pinned is None:
        # Both phases wrote http/1. Only Phase 2 understood base_url/options.
        if (kind, build) != ("http", "1") or config.get("base_url") or config.get("options"):
            pinned = LEGACY_RELEASES.get((kind, build))
    if pinned != deployed:
        raise AdapterError("VERSION_UNAVAILABLE", "not_sent")
    return AdapterConfig.model_validate(
        {
            **config,
            **{k: stored[k] for k in ("kind", "build", "secret_ref", "secret_version")},
        }
    )
