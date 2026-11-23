"""Loopback-only acceptance entrypoint. Time the actual PostgreSQL TraceEvent INSERT."""

import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from sqlalchemy import event

from app.api.main import create_app

api = create_app()
base = api.router.lifespan_context
writes: list[float] = []


@asynccontextmanager
async def instrument(app: FastAPI) -> Any:
    async with base(app):
        engine = app.state.resources.engine.sync_engine

        @event.listens_for(engine, "before_cursor_execute")
        def before(conn: Any, cursor: Any, stmt: str, args: Any, context: Any, many: bool) -> None:
            if stmt.startswith("INSERT INTO trace_events("):
                context.bench_started = time.perf_counter()

        @event.listens_for(engine, "after_cursor_execute")
        def after(conn: Any, cursor: Any, stmt: str, args: Any, context: Any, many: bool) -> None:
            if hasattr(context, "bench_started"):
                writes.append((time.perf_counter() - context.bench_started) * 1000)

        yield


api.router.lifespan_context = instrument


@api.get("/_acceptance/insert-timings")
def timings() -> list[float]:
    return writes.copy()
