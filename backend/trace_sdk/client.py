"""Bounded asynchronous TraceWrite client; each session belongs to one target request."""

import asyncio
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from types import TracebackType
from typing import Any, Self
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import httpx

from .privacy import check_fields, redact


class TraceSession:
    def __init__(
        self,
        base_url: str,
        trace_id: UUID,
        token: str,
        *,
        timeout: float = 5,
        capacity: int = 128,
        batch_size: int = 15,
    ) -> None:
        dest = urlsplit(base_url)
        if (
            (
                dest.scheme != "https"
                and not (
                    dest.scheme == "http" and dest.hostname in {"localhost", "127.0.0.1", "::1"}
                )
            )
            or not dest.hostname
            or dest.username
            or dest.password
            or dest.fragment
            or dest.query
        ):
            raise ValueError("TraceWrite URL requires HTTPS or local loopback HTTP")
        if not 1 <= capacity <= 1024 or not 1 <= batch_size <= 15 or not 0 < timeout <= 300:
            raise ValueError("invalid trace buffer or deadline")
        self.trace_id = trace_id
        self._token = token
        self._url = base_url.rstrip("/") + "/api/v1/trace-write/"
        self._deadline = asyncio.get_running_loop().time() + timeout
        self._queue: asyncio.Queue[bytes] = asyncio.Queue(capacity)
        self._wake = asyncio.Event()
        self._batch_size = batch_size
        self._sequence = 0
        self._closing = False
        self._partial = False
        self.status: str | None = None
        self.confirmed_status: str | None = None
        self._sender = asyncio.create_task(self._send())

    @classmethod
    def from_headers(cls, base_url: str, headers: Mapping[str, str], **options: Any) -> Self | None:
        # Accept both ASGI header objects and plain case-sensitive dictionaries.
        context = {
            k.lower(): v for k, v in headers.items() if k.lower() in {"x-trace-write", "x-trace-id"}
        }
        if not context.get("x-trace-write") or not context.get("x-trace-id"):
            return None
        try:
            identity = UUID(context["x-trace-id"])
            return cls(base_url, identity, context["x-trace-write"], **options)
        except ValueError:
            return None

    def add(self, event_type: str, name: str, **fields: Any) -> str | None:
        if self._closing or self._sender.done():
            return None
        self._sequence += 1
        # Short hex groups keep generated IDs distinct from sensitive phone-like text.
        raw_key = uuid4().hex
        key = "-".join(raw_key[i : i + 8] for i in range(0, len(raw_key), 8))
        if self._queue.full():
            self._partial = True
            return None
        try:
            entry = {
                "started_at": datetime.now(UTC).isoformat(),
                "status": "SUCCEEDED",
                **fields,
                "name": name,
                "event_type": event_type,
                "event_key": key,
                "sequence": self._sequence,
            }
            entry = redact(entry)
            check_fields(entry)
            encoded = json.dumps(
                entry, ensure_ascii=False, separators=(",", ":"), allow_nan=False
            ).encode()
            if len(encoded) > 32768:
                raise ValueError("event too large")
            self._queue.put_nowait(encoded)
        except (ValueError, TypeError, RecursionError):
            self._partial = True
            return None
        self._wake.set()
        return key

    async def _post(
        self, client: httpx.AsyncClient, path: str, data: bytes
    ) -> dict[str, Any] | None:
        for attempt in range(3):
            try:
                response = await client.post(self._url + path, content=data)
                if response.is_success:
                    parsed = response.json()
                    if isinstance(parsed, dict) and isinstance(parsed.get("data"), dict):
                        return dict(parsed["data"])
                    return None
                if response.status_code != 429 and response.status_code < 500:
                    return None
            except httpx.TransportError:
                pass
            except ValueError:
                return None
            if attempt < 2:
                await asyncio.sleep(0.05 * 2**attempt)
        return None

    async def _send(self) -> None:
        try:
            async with (
                asyncio.timeout_at(self._deadline),
                httpx.AsyncClient(
                    headers={
                        "Authorization": "Bearer " + self._token,
                        "Content-Type": "application/json",
                    },
                    follow_redirects=False,
                    trust_env=False,
                    timeout=httpx.Timeout(2, connect=1),
                ) as client,
            ):
                while True:
                    await self._wake.wait()
                    self._wake.clear()
                    while not self._queue.empty():
                        # Even 15 maximum-sized events fit inside the server's 512 KiB limit.
                        entries = [
                            self._queue.get_nowait()
                            for _ in range(min(self._queue.qsize(), self._batch_size))
                        ]
                        try:
                            reply = await self._post(
                                client, "events:batch", b'{"events":[' + b",".join(entries) + b"]}"
                            )
                            if reply is None or reply.get("accepted", 0) + reply.get(
                                "duplicate", 0
                            ) != len(entries):
                                self._partial = True
                        finally:
                            for _ in entries:
                                self._queue.task_done()
                    if self._closing:
                        break
                result = await self._post(
                    client,
                    "finalize",
                    json.dumps(
                        {
                            "outcome": "PARTIAL" if self._partial else "COMPLETE",
                            "contiguous": True,
                            "last_sequence": self._sequence,
                        }
                    ).encode(),
                )
                if result and result.get("status") in {"COMPLETE", "PARTIAL", "MISSING"}:
                    self.confirmed_status = result["status"]
                    self.status = "PARTIAL" if self._partial else result["status"]
        except Exception:
            # Telemetry failures must not replace the application's response or exception.
            self._partial = True
        finally:
            if self.status is None:
                self.status = "PARTIAL"
                self._partial = True
            self._closing = True
            self._token = ""
            while not self._queue.empty():
                self._queue.get_nowait()
                self._queue.task_done()

    @property
    def finalize_confirmed(self) -> bool:
        return self.confirmed_status is not None

    async def flush(self) -> bool:
        await self._queue.join()
        return not self._partial

    def finish(self, *, complete: bool = True) -> asyncio.Future[None]:
        """Signal completion without waiting for HTTP in the business request."""
        if not self._sender.done():
            self._partial |= not complete
            self._closing = True
            self._wake.set()
        # Cancelling a waiter must not cancel the shared sender or other waiters.
        return asyncio.shield(self._sender)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        sender = self.finish(complete=exc_type is None)
        try:
            await asyncio.shield(sender)
        except asyncio.CancelledError:
            self._partial = True
            # Flush within the sender's original deadline. Preserve caller cancellation.
            await asyncio.shield(sender)
            self.status = "PARTIAL"
            raise
