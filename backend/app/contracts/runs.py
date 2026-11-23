from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import Field, model_validator

from app.contracts.adapters import Contract
from app.contracts.catalog import Name


class ExecutionPolicy(Contract):
    max_parallel: int = Field(1, ge=1, le=1024)
    case_timeout_s: int = Field(120, ge=1, le=86400)
    run_timeout_s: int | None = Field(None, ge=1, le=604800)
    adapter_max_attempts: int = Field(1, ge=1, le=10)
    evaluator_max_attempts: int = Field(1, ge=1, le=10)
    initial_backoff_ms: int = Field(500, ge=1, le=60000)
    max_backoff_ms: int = Field(10000, ge=1, le=3600000)
    jitter: bool = True
    retry_after_cap_s: int = Field(60, ge=1, le=3600)
    retryable_error_codes: list[
        Literal[
            "DNS_UNAVAILABLE",
            "TARGET_TIMEOUT",
            "NETWORK_ERROR",
            "TARGET_RATE_LIMIT",
            "TARGET_HTTP_ERROR",
            "JUDGE_CONNECT_FAILED",
            "JUDGE_RATE_LIMIT",
            "TARGET_LIMIT",
            "JUDGE_LIMIT",
        ]
    ] = [
        "DNS_UNAVAILABLE",
        "TARGET_TIMEOUT",
        "NETWORK_ERROR",
        "TARGET_RATE_LIMIT",
        "TARGET_HTTP_ERROR",
        "JUDGE_CONNECT_FAILED",
        "JUDGE_RATE_LIMIT",
        "TARGET_LIMIT",
        "JUDGE_LIMIT",
    ]
    seed: int | None = None
    target_replay: Literal["not_sent_only", "verified_idempotent"] = "not_sent_only"
    approved_policy_ids: list[Name] = Field(default_factory=list, max_length=256)

    @model_validator(mode="after")
    def budgets(self) -> Self:
        if self.initial_backoff_ms > self.max_backoff_ms:
            raise ValueError("首次退避不能超过最大退避时间")
        return self


class TaskCreate(Contract):
    project_id: UUID | None = None
    project_version_id: UUID
    dataset_version_id: UUID
    evaluator_version_ids: list[UUID] = Field(min_length=1, max_length=64)
    name: Name
    description: Annotated[str, Field(max_length=4000)] | None = None
    policy: ExecutionPolicy = Field(default_factory=ExecutionPolicy)

    @model_validator(mode="after")
    def distinct_roots(self) -> Self:
        if len(set(self.evaluator_version_ids)) != len(self.evaluator_version_ids):
            raise ValueError("评分器版本不可重复")
        return self


class TaskPatch(Contract):
    name: Name | None = None
    description: Annotated[str, Field(max_length=4000)] | None = None

    @model_validator(mode="after")
    def nonempty(self) -> Self:
        if not self.model_fields_set or ("name" in self.model_fields_set and self.name is None):
            raise ValueError("请提供名称或描述")
        return self


class Empty(Contract):
    pass
