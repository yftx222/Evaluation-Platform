from typing import Annotated, Literal, Self

from pydantic import AwareDatetime, Field, StringConstraints, model_validator

from app.contracts.adapters import Contract
from app.contracts.http import AdapterConfig
from app.contracts.traces import TraceCapabilities

type Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=160)]
type Kind = Literal["agent", "rag", "dataagent", "llm", "custom"]
type Role = Literal["R", "W", "A"]
type Scope = Literal["project:read", "results:read"]
type Limit = Annotated[int, Field(gt=0, le=1000000)]


class ProjectCreate(Contract):
    slug: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_-]{0,63}$")]
    name: Name
    kind: Kind
    max_parallel: Limit
    target_max_parallel: Limit
    rpm: Limit


class ProjectPatch(Contract):
    name: Name | None = None
    enabled: bool | None = None
    max_parallel: Limit | None = None
    target_max_parallel: Limit | None = None
    rpm: Limit | None = None

    @model_validator(mode="after")
    def require_values(self) -> Self:
        if not self.model_fields_set or any(
            getattr(self, k) is None for k in self.model_fields_set
        ):
            raise ValueError("至少提供一项非空变更")
        return self


class MemberRole(Contract):
    role: Role


class MemberCreate(MemberRole):
    issuer: Name
    subject: Name


class KeyExpiry(Contract):
    expires_at: AwareDatetime


class KeyCreate(KeyExpiry):
    name: Name
    scopes: list[Scope] = Field(min_length=1, max_length=2)


class Publish(Contract):
    label: Name
    deployment_ref: Name
    adapter: AdapterConfig
    trace_capabilities: TraceCapabilities = Field(default_factory=TraceCapabilities)

    @model_validator(mode="after")
    def protocol_defaults(self) -> Self:
        if self.adapter.kind == "dataagent" and self.adapter.build in {"2", "3"}:
            if self.trace_capabilities.mode == "embedded":
                raise ValueError("此 DataAgent 版本支持 callback 或 disabled")
            if "trace_capabilities" not in self.model_fields_set:
                self.trace_capabilities = TraceCapabilities(
                    event_types=[
                        "AGENT_STEP",
                        "MODEL_CALL",
                        "TOOL_CALL",
                        "RETRIEVAL",
                        "FINAL_OUTPUT",
                        "CUSTOM",
                    ],
                    required_events=["FINAL_OUTPUT"],
                    retrieval_refs=False,
                )
        if self.adapter.kind == "dataagent" and self.adapter.build == "1":
            if (
                "trace_capabilities" in self.model_fields_set
                and self.trace_capabilities.mode != "disabled"
            ):
                raise ValueError("DataAgent D1 仅支持关闭轨迹采集")
            self.trace_capabilities = TraceCapabilities(mode="disabled", event_types=[])
        if (
            "trace_capabilities" not in self.model_fields_set
            and self.adapter.kind == "openai_compatible"
        ):
            self.trace_capabilities = TraceCapabilities(
                event_types=["MODEL_CALL", "FINAL_OUTPUT", "CUSTOM"],
                parent_links=False,
                retrieval_refs=False,
                tool_details=False,
            )
        return self
