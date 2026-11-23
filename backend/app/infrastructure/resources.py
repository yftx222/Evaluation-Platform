from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Literal
from uuid import UUID

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.application.access.identity import DevelopmentIdentity
from app.application.catalog.store import Fault, row
from app.contracts.evaluators import JudgeAccount
from app.contracts.http import TargetRule
from app.contracts.reports import ReportPolicy
from app.infrastructure.attempt_http import active


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", extra="ignore", env_prefix="EVAL_", env_parse_none_str="null"
    )
    database_url: SecretStr = SecretStr("postgresql+asyncpg://eval@127.0.0.1:5432/eval")
    redis_url: SecretStr = SecretStr("redis://127.0.0.1:6379/0")
    mode: Literal["development", "production"] = "development"
    identities: list[DevelopmentIdentity] = Field(default_factory=list)
    targets: dict[str, list[TargetRule]] = Field(default_factory=dict)
    secrets: dict[str, dict[str, dict[str, SecretStr]]] = Field(default_factory=dict)
    judge_accounts: dict[str, JudgeAccount] = Field(default_factory=dict)
    report_policies: dict[UUID, ReportPolicy] = Field(default_factory=dict)
    trace_retention_days: int | None = Field(None, ge=1, le=36500)
    trace_target_versions: set[UUID] = Field(default_factory=set)
    verified_idempotent_versions: set[UUID] = Field(default_factory=set)
    pool_size: int = Field(5, ge=1, le=100)


class Resources:
    def __init__(self, config: Settings) -> None:
        self.engine = create_async_engine(
            config.database_url.get_secret_value(),
            pool_size=config.pool_size,
            max_overflow=0,
            pool_timeout=3,
            pool_pre_ping=True,
            hide_parameters=True,
            connect_args={"timeout": 3, "command_timeout": 5},
        )
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.redis = Redis.from_url(
            config.redis_url.get_secret_value(), socket_connect_timeout=2, socket_timeout=2
        )

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[AsyncSession]:
        async with self.sessions.begin() as session:
            yield session

    async def reserve_call(self, db: AsyncSession, quota: str, timeout: float) -> dict[str, object]:
        lease = await row(
            db,
            """INSERT INTO call_leases(quota_key,expires_at)
            VALUES (:q,now()+make_interval(secs=>:ttl)) RETURNING id""",
            q=quota,
            ttl=timeout + 2,
        )
        wire = active.get()
        if wire and wire.attempt:
            await db.execute(
                text("UPDATE call_leases SET attempt_id=:a WHERE id=:id"),
                {"a": wire.attempt, "id": lease["id"]},
            )
        return lease

    @asynccontextmanager
    async def target_permit(self, project: UUID, timeout: float) -> AsyncIterator[None]:
        quota = f"target:{project}"
        async with self.transaction() as db:
            limits = await row(
                db,
                """SELECT target_max_parallel,rpm,enabled,
                extract(epoch FROM next_call_at-clock_timestamp()) AS wait_s
                FROM projects WHERE id=:p FOR UPDATE""",
                p=project,
            )
            if not limits["enabled"]:
                raise Fault(409, "PROJECT_DISABLED", "项目已停用")
            await db.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:q,0))"), {"q": quota}
            )
            await db.execute(
                text("DELETE FROM call_leases WHERE quota_key=:q AND expires_at<=now()"),
                {"q": quota},
            )
            count = await db.scalar(
                text("SELECT count(*) FROM call_leases WHERE quota_key=:q"), {"q": quota}
            )
            rpm_wait = float(limits["wait_s"] or 0)
            if rpm_wait > 0 or count >= limits["target_max_parallel"]:
                wire = active.get()
                if wire and rpm_wait > 0:
                    wire.quota_available_in = rpm_wait
                raise Fault(429, "TARGET_LIMIT", "项目调用达到限额")
            lease = await self.reserve_call(db, quota, timeout)
            await db.execute(
                text("""UPDATE projects SET next_call_at=now()+
                make_interval(secs=>60.0/rpm) WHERE id=:p"""),
                {"p": project},
            )
        try:
            yield
        finally:
            async with self.transaction() as db:
                await db.execute(text("DELETE FROM call_leases WHERE id=:id"), {"id": lease["id"]})

    @asynccontextmanager
    async def judge_permit(
        self, account_id: str, account: JudgeAccount, timeout: float
    ) -> AsyncIterator[None]:
        quota = "judge:" + account_id
        window = "judge-rpm:" + account_id
        async with self.transaction() as db:
            await db.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:q,0))"), {"q": quota}
            )
            await db.execute(
                text("DELETE FROM call_leases WHERE quota_key IN (:q,:w) AND expires_at<=now()"),
                {"q": quota, "w": window},
            )
            counts = await row(
                db,
                """SELECT count(*) FILTER(WHERE quota_key=:q) AS active,
                count(*) FILTER(WHERE quota_key=:w) AS recent,
                ceil(extract(epoch FROM min(expires_at) FILTER(WHERE quota_key=:w)
                    -clock_timestamp())) AS rpm_wait FROM call_leases
                WHERE quota_key IN (:q,:w)""",
                q=quota,
                w=window,
            )
            if counts["active"] >= account.max_parallel or counts["recent"] >= account.rpm:
                wire = active.get()
                if wire and counts["recent"] >= account.rpm:
                    wire.quota_available_in = max(1, int(counts["rpm_wait"]))
                raise Fault(429, "JUDGE_LIMIT", "裁判账户额度已用完")
            lease = await self.reserve_call(db, quota, timeout)
            await db.execute(
                text(
                    "INSERT INTO call_leases(quota_key,expires_at) "
                    "VALUES (:w,now()+interval '60 seconds')"
                ),
                {"w": window},
            )
        try:
            yield
        finally:
            async with self.transaction() as db:
                await db.execute(text("DELETE FROM call_leases WHERE id=:id"), {"id": lease["id"]})

    async def close(self) -> None:
        try:
            await self.redis.aclose()
        finally:
            await self.engine.dispose()
