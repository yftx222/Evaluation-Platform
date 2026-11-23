"""Integration checks against disposable databases on the local Compose PostgreSQL."""

import asyncio
import logging
import os
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from app.api.main import create_app
from app.infrastructure.resources import Resources, Settings
from app.worker.scheduler import initialize
from app.worker.settings import WorkerSettings


def test_migration_cycle_and_constraints(clean_db: str) -> None:
    for command, revision in [("upgrade", "head"), ("downgrade", "base"), ("upgrade", "head")]:
        subprocess.run(
            [sys.executable, "-m", "alembic", command, revision],
            env={**os.environ, "EVAL_DATABASE_URL": clean_db},
            check=True,
        )

    async def check() -> None:
        engine = create_async_engine(clean_db)
        try:
            async with engine.begin() as conn:
                insert = text("""INSERT INTO projects
                    (slug,name,kind,max_parallel,target_max_parallel,rpm)
                    VALUES (:slug,'Test','custom',1,1,10) RETURNING id""")
                first = await conn.scalar(insert, {"slug": "first"})
                second = await conn.scalar(insert, {"slug": "second"})
                with pytest.raises(IntegrityError):
                    async with conn.begin_nested():
                        await conn.execute(insert, {"slug": "first"})
                adapter = await conn.scalar(
                    text("""INSERT INTO project_adapters
                    (project_id,revision,kind,build,config,digest)
                    VALUES (:p,1,'http','1','{}','a') RETURNING id"""),
                    {"p": first},
                )
                version = text("""INSERT INTO project_versions(project_id,adapter_id,label,
                    deployment_ref,digest) VALUES (:p,:a,'1','test','v')""")
                with pytest.raises(IntegrityError):
                    async with conn.begin_nested():
                        await conn.execute(version, {"p": second, "a": adapter})
                await conn.execute(version, {"p": first, "a": adapter})
                with pytest.raises(IntegrityError):
                    async with conn.begin_nested():
                        await conn.execute(text("UPDATE project_versions SET label='edited'"))
        finally:
            await engine.dispose()

    asyncio.run(check())


def test_readiness_and_logs(services: Settings, caplog: pytest.LogCaptureFixture) -> None:
    configs = [
        (services, 200, ""),
        (
            services.model_copy(
                update={"database_url": SecretStr("postgresql+asyncpg://eval@127.0.0.1:1/eval")}
            ),
            503,
            "postgresql",
        ),
        (services.model_copy(update={"redis_url": SecretStr("redis://127.0.0.1:1")}), 503, "redis"),
    ]
    caplog.set_level(logging.INFO, logger="uvicorn.error")
    for config, status, failed in configs:
        with TestClient(create_app(config)) as client:
            assert client.get("/health").status_code == 200
            result = client.get("/ready", headers={"Authorization": "Bearer private-sentinel"})
            assert result.status_code == status
            assert result.headers["x-request-id"] == result.json()["request_id"]
            if failed:
                assert result.json()["error"]["message"] == failed
    assert "private-sentinel" not in caplog.text


@pytest.mark.parametrize("catalog", ["0006_worker_scheduler", "0007_task_archive"], indirect=True)
def test_worker_schema_compatibility(catalog: tuple[Settings, dict[str, str]]) -> None:
    settings, _ = catalog

    async def check() -> None:
        resources = Resources(settings)
        try:
            async with resources.transaction() as db:
                current = await db.scalar(text("SELECT version_num FROM alembic_version"))
            if current == "0006_worker_scheduler":
                with pytest.raises(RuntimeError, match="0007_task_archive.*0006_worker_scheduler"):
                    await initialize(resources, WorkerSettings())
                return
            await initialize(resources, WorkerSettings())
            async with resources.transaction() as db:
                await db.execute(text("ALTER TABLE worker_capacity ADD COLUMN note text"))
                await db.execute(text("UPDATE alembic_version SET version_num='0008_add_note'"))
            await initialize(resources, WorkerSettings())
            async with resources.transaction() as db:
                await db.execute(
                    text("UPDATE alembic_version SET version_num='v2_0001_new_contract'")
                )
            with pytest.raises(RuntimeError, match="v2_0001"):
                await initialize(resources, WorkerSettings())
        finally:
            await resources.close()

    asyncio.run(check())
