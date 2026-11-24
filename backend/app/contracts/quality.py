from typing import Annotated, Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import AwareDatetime, Field, SecretStr, StringConstraints, field_validator

from app.contracts.adapters import Contract
from app.contracts.datasets import Key
from app.contracts.evaluators import Publish, Unit, Weight
from app.contracts.traces import TraceEventInput, TraceFinalize

Name = Annotated[str, StringConstraints(min_length=1, max_length=256)]


class Rubric(Contract):
    rubric_key: Key
    metric_key: Key
    description: Name
    evaluator_ref: UUID | Literal["self"] = "self"
    gate_class: Literal["BLOCKING", "THRESHOLD", "BONUS", "OBSERVE"]
    weight: Weight = 1
    threshold: Unit = 1
    minimum: Unit = 0
    evidence_required: bool = False


class RubricPublish(Publish):
    rubrics: list[Rubric] = Field(default_factory=list, max_length=32)

    @field_validator("rubrics")
    @classmethod
    def unique_rubrics(cls, value: list[Rubric]) -> list[Rubric]:
        if len({r.rubric_key for r in value}) != len(value):
            raise ValueError("细则标识须唯一")
        return value


class QualityReceiver(Contract):
    endpoint: str
    credential: SecretStr
    max_attempts: int = Field(default=8, ge=1, le=100)

    @field_validator("endpoint")
    @classmethod
    def check_endpoint(cls, value: str) -> str:
        dest = urlsplit(value)
        if (
            not dest.hostname
            or dest.username
            or dest.password
            or dest.fragment
            or dest.query
            or not (
                dest.scheme == "https"
                or dest.scheme == "http"
                and dest.hostname in {"127.0.0.1", "::1", "localhost"}
            )
        ):
            raise ValueError("质量事件地址需要 HTTPS 或本机回环地址")
        return value


class ServiceGrant(Contract):
    subject: Name
    verifier: str = Field(pattern="^[0-9a-f]{64}$")
    project_id: UUID
    environment: Name
    scopes: set[
        Literal[
            "trace:write",
            "trace:read",
            "payload:read",
            "proposal:write",
            "batch:read",
            "batch:start",
            "experiment:observe",
            "shadow:submit",
        ]
    ]
    datasets: set[UUID] = Field(default_factory=set)
    target_versions: set[UUID] = Field(default_factory=set)
    expires_at: AwareDatetime


class ProductionRoot(Contract):
    trace_id: UUID
    environment: Name
    deployment_version: Name | None
    agent_run_id: Name
    session_id: Name | None = None
    conversation_id: Name | None = None
    message_id: Name | None = None
    experiment_id: Name | None = None
    variant_id: Name | None = None
    started_at: AwareDatetime
    lineage: dict[
        Literal[
            "project_version_id",
            "model_provider",
            "model_id",
            "model_revision",
            "prompt_version",
            "prompt_digest",
            "tool_version",
            "skill_version",
            "knowledge_version",
            "index_version",
            "embedding_version",
            "agent_config_digest",
            "fixture_version",
        ],
        Name | None,
    ] = Field(default_factory=dict)

    @field_validator("lineage")
    @classmethod
    def version_identity(cls, value: dict[str, str | None]) -> dict[str, str | None]:
        if version := value.get("project_version_id"):
            UUID(version)
        return value


class ProductionBatch(Contract):
    schema_version: Literal["1.0"]
    trace: ProductionRoot
    events: list[TraceEventInput] = Field(min_length=1)


class ProductionFinal(TraceFinalize):
    trace_id: UUID
    event_count: int = Field(ge=0, le=2**63 - 1, strict=True)


class TracePolicy(Contract):
    max_trace_events: int = Field(default=10000, ge=1, le=1000000)
    idle_seconds: int = Field(default=900, gt=0)
    retention_days: int | None = Field(default=None, gt=0)
    max_event_bytes: int = Field(default=32768, gt=0)
    max_batch_bytes: int = Field(default=524288, gt=0)
    max_batch_events: int = Field(default=100, gt=0)
