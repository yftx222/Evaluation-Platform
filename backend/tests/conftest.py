"""Integration checks against disposable databases on the local Compose PostgreSQL."""

import asyncio
import hashlib
import os
import secrets
import subprocess
import sys
from collections.abc import Iterator
from uuid import uuid4

import pytest
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from app.application.access.identity import DevelopmentIdentity
from app.infrastructure.resources import Settings


@pytest.fixture
def services() -> Settings:
    if not os.getenv("EVAL_INTEGRATION"):
        pytest.skip("Set EVAL_INTEGRATION via the Compose integration test launcher")
    return Settings()


@pytest.fixture
def clean_db(services: Settings) -> Iterator[str]:
    yield from disposable_db(services)


def disposable_db(services: Settings) -> Iterator[str]:
    url = make_url(services.database_url.get_secret_value())
    assert url.host == "127.0.0.1"
    name = "acceptance_" + uuid4().hex
    db_url = url.set(database=name).render_as_string(hide_password=False)

    async def admin(statement: str) -> None:
        engine = create_async_engine(url, isolation_level="AUTOCOMMIT")
        try:
            async with engine.connect() as conn:
                await conn.execute(text(statement))
        finally:
            await engine.dispose()

    asyncio.run(admin(f"CREATE DATABASE {name}"))
    try:
        yield db_url
    finally:
        asyncio.run(admin(f"DROP DATABASE {name}"))


@pytest.fixture
def catalog(
    clean_db: str, services: Settings, request: pytest.FixtureRequest
) -> tuple[Settings, dict[str, str]]:
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", getattr(request, "param", "head")],
        env={**os.environ, "EVAL_DATABASE_URL": clean_db},
        check=True,
    )
    tokens = {n: secrets.token_urlsafe() for n in ["curator", "shop", "rag"]}
    identities = [
        DevelopmentIdentity(
            subject=k,
            org_roles=["Org", "Cat"] if k == "curator" else [],
            verifier=hashlib.sha256(v.encode()).hexdigest(),
        )
        for k, v in tokens.items()
    ]
    return services.model_copy(
        update={"database_url": SecretStr(clean_db), "identities": identities}
    ), tokens
