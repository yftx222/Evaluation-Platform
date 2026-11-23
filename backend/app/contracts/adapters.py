from abc import ABC, abstractmethod
from datetime import datetime
from typing import Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class UnifiedRequest(Contract):
    schema_version: Literal[1] = 1
    run_id: UUID
    execution_id: UUID
    attempt_id: UUID
    case_key: str
    input: dict[str, object]
    parameters: dict[str, object] = Field(default_factory=dict)
    deadline_at: AwareDatetime
    idempotency_key: str
    extensions: dict[str, object] = Field(default_factory=dict)


class UnifiedResponse(Contract):
    schema_version: Literal[1] = 1
    answer: str | None = None
    structured_output: dict[str, object] | None = None
    usage: dict[str, int] = Field(default_factory=dict)
    latency_ms: int = 0
    observed: dict[str, str] = Field(default_factory=dict)
    extensions: dict[str, object] = Field(default_factory=dict)

    @model_validator(mode="after")
    def require_output(self) -> Self:
        if self.answer is None and self.structured_output is None:
            raise ValueError("缺少有效响应")
        return self


class ConnectionResult(Contract):
    ok: bool
    latency_ms: int
    code: str
    message: str
    capabilities: list[str]


class AdapterError(Exception):
    def __init__(
        self,
        code: str,
        delivery_state: Literal["not_sent", "sent", "unknown"],
        retryable: bool = False,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.delivery_state = delivery_state
        self.retryable = retryable


class BaseAdapter(ABC):
    @abstractmethod
    async def validate_connection(self) -> ConnectionResult: ...

    @abstractmethod
    async def execute(self, request: UnifiedRequest) -> UnifiedResponse: ...

    @abstractmethod
    def normalize_response(self, raw: object) -> UnifiedResponse: ...

    @abstractmethod
    async def collect_trace(self, context: dict[str, object]) -> list[dict[str, object]]: ...


class TraceWriteScope(Contract):
    project_id: UUID
    run_id: UUID
    execution_id: UUID
    attempt_id: UUID
    epoch: int


class TraceWriteBinding(TraceWriteScope):
    expires_at: AwareDatetime
    verifier: str


class TraceWriteIssuer(ABC):
    """Worker only. The attempt repository stores the verifier and clears it on revoke."""

    @abstractmethod
    async def issue(self, scope: TraceWriteScope, deadline: datetime) -> str:
        """Mint a random bearer, persist only its digest and the bounded expiry on the attempt."""
        ...

    @abstractmethod
    async def revoke(self, attempt_id: UUID, epoch: int) -> None: ...
