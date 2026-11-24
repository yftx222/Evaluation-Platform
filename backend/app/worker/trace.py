import hashlib
import secrets
from datetime import datetime
from uuid import UUID

from sqlalchemy import text

from app.application.trace.scope import LIVE_SCOPE
from app.contracts.adapters import TraceWriteIssuer, TraceWriteScope
from app.contracts.traces import BoundTraceWriteScope
from app.infrastructure.resources import Resources

SCOPE = f"""a.id=:attempt_id AND a.execution_id=:execution_id AND a.run_id=:run_id
 AND a.epoch=:epoch AND r.project_id=:project_id AND {LIVE_SCOPE}"""


class AttemptTraceIssuer(TraceWriteIssuer):
    def __init__(self, resources: Resources) -> None:
        self.resources = resources

    async def issue(
        self, scope: TraceWriteScope, deadline: datetime, *, previous: str | None = None
    ) -> str:
        bound = " AND a.trace_id=:trace_id" if isinstance(scope, BoundTraceWriteScope) else ""
        rotation = (
            " AND a.trace_verifier=:previous AND a.trace_expires_at>clock_timestamp() "
            "AND EXISTS (SELECT 1 FROM traces t WHERE t.id=a.trace_id AND t.status='OPEN')"
            if previous is not None
            else ""
        )
        token = f"{scope.attempt_id}.{secrets.token_urlsafe(32)}"
        async with self.resources.transaction() as db:
            issued = await db.scalar(
                text(f"""UPDATE execution_attempts a SET trace_verifier=:hash,
                trace_expires_at=least(:deadline,clock_timestamp()+interval '5 minutes')
                FROM case_executions c,evaluation_runs r WHERE {SCOPE}{bound}{rotation}
                AND :deadline>clock_timestamp() RETURNING a.id"""),
                {
                    **scope.model_dump(),
                    "hash": hashlib.sha256(token.encode()).hexdigest(),
                    "deadline": deadline,
                    "previous": hashlib.sha256((previous or "").encode()).hexdigest(),
                },
            )
            if issued is None:
                raise ValueError("trace scope expired")
        return token

    async def valid(self, scope: TraceWriteScope, token: str) -> bool:
        bound = " AND a.trace_id=:trace_id" if isinstance(scope, BoundTraceWriteScope) else ""
        if len(token) > 128 or not token.startswith(str(scope.attempt_id) + "."):
            return False
        async with self.resources.transaction() as db:
            stored = await db.scalar(
                text(f"""SELECT a.trace_verifier FROM execution_attempts a,
                case_executions c,evaluation_runs r WHERE {SCOPE}{bound}
                AND a.trace_expires_at>clock_timestamp()"""),
                scope.model_dump(),
            )
        return bool(
            stored and secrets.compare_digest(stored, hashlib.sha256(token.encode()).hexdigest())
        )

    async def revoke(self, attempt_id: UUID, epoch: int) -> None:
        async with self.resources.transaction() as db:
            await db.execute(
                text("""UPDATE execution_attempts a SET trace_verifier=NULL,
                trace_expires_at=NULL FROM case_executions c WHERE a.id=:a AND a.epoch=:epoch
                AND a.trace_verifier IS NOT NULL AND c.id=a.execution_id AND c.lease_epoch=a.epoch
                AND c.lease_expires_at>clock_timestamp()"""),
                {"a": attempt_id, "epoch": epoch},
            )
