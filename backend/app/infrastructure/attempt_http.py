"""HTTPX request hooks scoped to a single worker task; API calls use the default client."""

from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import cache
from ssl import SSLContext
from typing import Any
from uuid import UUID

import httpx
from httpx import (
    URL as URL,
)
from httpx import (
    ConnectError as ConnectError,
)
from httpx import (
    ConnectTimeout as ConnectTimeout,
)
from httpx import (
    HTTPError as HTTPError,
)
from httpx import (
    InvalidURL as InvalidURL,
)
from httpx import (
    TimeoutException as TimeoutException,
)


@dataclass
class Wire:
    before_send: Callable[[], Awaitable[None]]
    key: str
    attempt: UUID | None = None
    trace_token: str | None = field(default=None, repr=False)
    trace_id: UUID | None = None
    connect_failed: bool = False
    started: bool = False
    status: int | None = None
    quota_available_in: float | None = None
    retry_after: str | None = None
    request_id: str | None = None

    async def request(self, request: httpx.Request) -> None:
        await self.before_send()
        self.started = True
        request.headers["Idempotency-Key"] = self.key
        if self.trace_token:
            request.headers["X-Trace-Write"] = self.trace_token
            if self.trace_id:
                request.headers["X-Trace-Id"] = str(self.trace_id)

    async def response(self, response: httpx.Response) -> None:
        self.status = response.status_code
        self.retry_after = response.headers.get("retry-after")
        self.request_id = response.headers.get("x-request-id")


active: ContextVar[Wire | None] = ContextVar("attempt_wire", default=None)


@contextmanager
def observe(wire: Wire) -> Iterator[None]:
    token = active.set(wire)
    try:
        yield
    finally:
        active.reset(token)


@cache
def verified_tls_context() -> SSLContext:
    # All pinned clients disable ambient proxies and use the bundled CA list. Loading that
    # list for every short HTTP request can exhaust the request budget before connecting.
    return httpx.create_ssl_context(trust_env=False)


class AsyncClient(httpx.AsyncClient):
    def __init__(self, **options: Any) -> None:
        if options.get("trust_env") is False:
            options.setdefault("verify", verified_tls_context())
        wire = active.get()
        if wire is not None:
            options["event_hooks"] = {"request": [wire.request], "response": [wire.response]}
        super().__init__(**options)

    async def send(self, request: httpx.Request, **options: Any) -> httpx.Response:
        try:
            return await super().send(request, **options)
        except (httpx.ConnectError, httpx.ConnectTimeout):
            wire = active.get()
            if wire is not None:
                wire.connect_failed = True
            raise
