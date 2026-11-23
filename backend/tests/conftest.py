"""Integration checks against disposable databases on the local Compose PostgreSQL."""

import asyncio
import os
from collections.abc import Iterator
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from app.infrastructure.resources import Settings


@pytest.fixture
def services() -> Settings:
    if not os.getenv("EVAL_INTEGRATION"):
        pytest.skip("Set EVAL_INTEGRATION via the Compose integration test launcher")
    return Settings()


@pytest.fixture
def clean_db(services: Settings) -> Iterator[str]:
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
