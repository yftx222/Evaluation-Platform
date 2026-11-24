"""Request-scoped production telemetry. Configure the destination server-side."""

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Self
from uuid import UUID, uuid4

import httpx

from .client import TraceSession
from .privacy import check_fields


class ProductionTraceSession(TraceSession):
    write_path = "/api/v1/production-trace-write/"

    def __init__(
        self,
        base_url: str,
        credential: str,
        *,
        environment: str,
        agent_run_id: str,
        deployment_version: str | None,
        trace_id: UUID | None = None,
        conversation_id: str | None = None,
        message_id: str | None = None,
        session_id: str | None = None,
        experiment_id: str | None = None,
        variant_id: str | None = None,
        lineage: dict[str, Any] | None = None,
        timeout: float = 5,
        capacity: int = 128,
        batch_size: int = 15,
    ) -> None:
        identity = trace_id or uuid4()
        self.root: dict[str, Any] = dict(
            trace_id=str(identity),
            environment=environment,
            agent_run_id=agent_run_id,
            deployment_version=deployment_version,
            started_at=datetime.now(UTC).isoformat(),
        )
        for k, v in (
            ("conversation_id", conversation_id),
            ("message_id", message_id),
            ("session_id", session_id),
            ("experiment_id", experiment_id),
            ("variant_id", variant_id),
            ("lineage", lineage),
        ):
            if v is not None:
                check_fields(v)
                self.root[k] = v
        self._registered = False
        super().__init__(
            base_url,
            identity,
            credential,
            timeout=timeout,
            capacity=capacity,
            batch_size=batch_size,
        )

    @classmethod
    def from_headers(cls, base_url: str, headers: Mapping[str, str], **options: Any) -> Self | None:
        raise ValueError("Production trace requires a configured project/environment credential")

    async def _post(
        self, client: httpx.AsyncClient, path: str, data: bytes
    ) -> dict[str, Any] | None:
        if not self._registered:
            await self._start_session(client)
            if not self._registered:
                return None
        body = json.loads(data)
        if path == "events:batch":
            body.update(schema_version="1.0", trace=self.root)
        else:
            body.update(trace_id=str(self.trace_id), event_count=self._sequence)
        result = await super()._post(client, path, json.dumps(body, ensure_ascii=False).encode())
        if result is None:
            return None
        if path == "events:batch":
            return {"accepted": result["accepted_events"], "duplicate": result["duplicate_events"]}
        return {"status": result["completeness"]}

    async def _start_session(self, client: httpx.AsyncClient) -> None:
        registered = await super()._post(client, "register", json.dumps(self.root).encode())
        self._registered = bool(registered)
