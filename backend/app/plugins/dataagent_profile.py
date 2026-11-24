"""DataAgent build 3 binds observed deployment inputs to a published project version."""

import asyncio
import hashlib
import json
import time
from typing import Annotated, Any, Literal

import httpx
from pydantic import Field

from app.contracts.adapters import (
    AdapterError,
    ConnectionResult,
    Contract,
    UnifiedRequest,
    UnifiedResponse,
)
from app.plugins.dataagent import Session
from app.plugins.dataagent_trace import TracedDataAgentAdapter

Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Label = Annotated[str, Field(min_length=1, max_length=160, pattern=r"^[\w./:@+-]+$")]
Count = Annotated[int, Field(strict=True, ge=0)]


class ModelRoute(Contract):
    route: Label
    provider: Label
    model: Label


class Embedding(Contract):
    model: Label
    dimensions: Count
    weights: Digest | None
    probe_digest: Digest


class Fixture(Contract):
    database: Label
    version: Label
    tables: Count
    rows: Count
    digest: Digest


class Identity(Contract):
    user: Label
    role: Label
    grants_digest: Digest


class Sandbox(Contract):
    image: Label
    memory: Label
    nano_cpus: Count
    pids: Count
    max_file_bytes: Count


class Index(Contract):
    uuid: Label
    docs: Count
    shards_digest: Digest


class Manifest(Contract):
    schema_version: Literal[1]
    profile: Label
    git: Annotated[str, Field(pattern=r"^[a-f0-9]{40}$")]
    source: Digest
    evaluation_code: Digest
    dependency_lock: Digest
    config: Digest
    prompts: Digest
    skills: Digest
    ontology: Digest
    metadata_yaml: Digest
    metadata_catalog: Digest
    indices: dict[Label, Index] = Field(min_length=1, max_length=32)
    models: dict[Literal["planner", "explorer", "analyst", "reviewer"], ModelRoute] = Field(
        min_length=4
    )
    embedding: Embedding
    fixture: Fixture
    identity: Identity
    sandbox: Sandbox
    experience_writes: Literal[False]


class Environment(Contract):
    digest: Digest
    manifest: Manifest


class ProfiledDataAgentAdapter(TracedDataAgentAdapter):
    pinned_digest: str | None = None
    snapshot_timeout = 60

    async def read_profile(self, session: Session, *, sent: bool = False) -> Environment:
        delivery: Literal["sent", "not_sent"] = "sent" if sent else "not_sent"
        try:
            response = Environment.model_validate(
                await session.call("GET", "/_eval/environment"), strict=True
            )
            m = response.manifest
            canonical = json.dumps(
                m.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode()
            if hashlib.sha256(canonical).hexdigest() != response.digest:
                raise ValueError("digest mismatch")
        except (AdapterError, httpx.HTTPError, ValueError, TypeError, KeyError):
            raise AdapterError("DATAAGENT_ENVIRONMENT_UNAVAILABLE", delivery) from None
        if (
            m.profile != self.opts.profile
            or m.identity.role != self.opts.role
            or m.fixture.version != self.opts.fixture_version
            or (self.pinned_digest and self.pinned_digest != response.digest)
        ):
            raise AdapterError("DATAAGENT_ENVIRONMENT_CHANGED", delivery)
        return response

    async def capture(self) -> Environment:
        try:
            async with (
                asyncio.timeout(self.snapshot_timeout),
                httpx.AsyncClient(
                    timeout=httpx.Timeout(self.snapshot_timeout, connect=self.cfg.timeout_s),
                    trust_env=False,
                ) as client,
            ):
                return await self.read_profile(await super().session(client))
        except (TimeoutError, httpx.HTTPError, ValueError, KeyError, TypeError):
            raise AdapterError("DATAAGENT_ENVIRONMENT_UNAVAILABLE", "not_sent") from None

    async def validate_connection(self) -> ConnectionResult:
        started = time.monotonic()
        await self.capture()
        return ConnectionResult(
            ok=True,
            latency_ms=int((time.monotonic() - started) * 1000),
            code="OK",
            message="已核对评测身份、只读环境和版本快照",
            capabilities=["execute", "multi_turn", "artifacts", "callback", "environment_snapshot"],
        )

    async def session(self, client: httpx.AsyncClient) -> Session:
        session = await super().session(client)
        await self.read_profile(session)
        return session

    async def artifacts(
        self, s: Session, cid: str, history: list[dict[str, Any]], attachments: set[tuple[int, str]]
    ) -> list[dict[str, Any]]:
        result = await super().artifacts(s, cid, history, attachments)
        await self.read_profile(s, sent=True)
        return result

    async def execute(self, request: UnifiedRequest) -> UnifiedResponse:
        if not self.pinned_digest:
            raise AdapterError("DATAAGENT_ENVIRONMENT_UNPINNED", "not_sent")
        result = await super().execute(request)
        result.observed["environment_digest"] = self.pinned_digest
        return result
