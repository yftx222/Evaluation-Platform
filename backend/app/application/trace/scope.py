import hashlib
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.catalog.store import Fault, row

LIVE_SCOPE = """a.stage='ADAPTER' AND a.status IN ('CREATED','RUNNING')
    AND c.id=a.execution_id AND r.id=a.run_id AND c.run_id=r.id
    AND c.status='RUNNING' AND c.stage='ADAPTER' AND r.status='RUNNING'
    AND c.lease_epoch=a.epoch AND c.lease_owner=a.lease_owner
    AND c.lease_expires_at>clock_timestamp()"""


TRACE = """SELECT t.*, r.config_snapshot->'project_version'->'trace_capabilities' AS capabilities
    FROM traces t JOIN evaluation_runs r ON r.id=t.run_id WHERE t.id=:id"""


def invalid_token() -> Fault:
    return Fault(401, "INVALID_TRACE_CREDENTIAL", "上报凭证无效或采集窗口已关闭")


async def verify(db: AsyncSession, token: str) -> dict[str, Any]:
    try:
        attempt = UUID(token.partition(".")[0])
    except ValueError:
        raise invalid_token() from None
    # Authenticate before taking locks. Re-read the live scope after acquiring locks
    # in worker order so a revocation or epoch change cannot race the accepted write.
    digest = hashlib.sha256(token.encode()).hexdigest()
    lookup = f"""SELECT a.id,a.trace_verifier,a.trace_id,a.execution_id,a.run_id,
        least(a.trace_expires_at,c.lease_expires_at) AS deadline FROM execution_attempts a
        JOIN case_executions c ON c.id=a.execution_id AND c.run_id=a.run_id
        JOIN evaluation_runs r ON r.id=a.run_id
        WHERE a.id=:id AND {LIVE_SCOPE} AND a.trace_expires_at>clock_timestamp()"""

    found = (await db.execute(text(lookup), {"id": attempt})).mappings().one_or_none()
    if (
        not found
        or not found["trace_id"]
        or not secrets.compare_digest(found["trace_verifier"] or "", digest)
    ):
        raise invalid_token()
    return dict(found)


@asynccontextmanager
async def write_window(db: AsyncSession, token: str) -> AsyncIterator[dict[str, Any]]:
    found = await verify(db, token)
    for query, identity in [
        ("SELECT id FROM evaluation_runs WHERE id=:id FOR SHARE", found["run_id"]),
        ("SELECT id FROM case_executions WHERE id=:id FOR UPDATE", found["execution_id"]),
        ("SELECT id FROM execution_attempts WHERE id=:id FOR UPDATE", found["id"]),
    ]:
        await db.execute(text(query), {"id": identity})
    found = await verify(db, token)
    yield await row(
        db,
        TRACE + " AND t.attempt_id=:a FOR UPDATE OF t",
        id=found["trace_id"],
        a=found["id"],
    )
    if not await db.scalar(
        text("SELECT clock_timestamp()<:deadline"), {"deadline": found["deadline"]}
    ):
        raise invalid_token()
