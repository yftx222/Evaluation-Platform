"""Versioned inputs and outputs for a single, read-only scoring operation."""

from dataclasses import dataclass
from typing import Annotated, Any, Literal, Protocol, Self
from uuid import UUID

from pydantic import Field, StringConstraints, model_validator

from app.contracts.adapters import Contract, UnifiedResponse
from app.contracts.datasets import Case as CaseView
from app.contracts.datasets import Key, Name
from app.contracts.http import TargetRule

Kind = Literal["rule", "code", "llm_judge", "business", "composite"]
Status = Literal["OK", "ERROR", "SKIPPED"]
Unit = Annotated[float, Field(ge=0, le=1, strict=True, allow_inf_nan=False)]
Weight = Annotated[float, Field(gt=0, le=10000, strict=True, allow_inf_nan=False)]
Slug = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_-]{0,63}$")]


class EvaluatorCreate(Contract):
    slug: Slug
    name: Name
    kind: Kind


class EvaluatorPatch(Contract):
    name: Name | None = None
    enabled: bool | None = None

    @model_validator(mode="after")
    def changes(self) -> Self:
        if not self.model_fields_set or any(
            getattr(self, k) is None for k in self.model_fields_set
        ):
            raise ValueError("请填写名称或启停状态")
        return self


class Member(Contract):
    child_version_id: UUID
    position: int = Field(ge=0)
    weight: Weight = 1


class Publish(Contract):
    version: Key
    impl_key: Key
    build: Key
    config: dict[str, Any]
    prompt_text: str | None = Field(None, min_length=1, max_length=8000)
    prompt_version: Key | None = None
    provider: Literal["openai_compatible"] | None = None
    model_id: Name | None = None
    model_revision: Name | None = None
    rubric_version: Key | None = None
    members: list[Member] = Field(default_factory=list, max_length=32)

    @model_validator(mode="after")
    def unique_children(self) -> Self:
        for key in ("position", "child_version_id"):
            if len({getattr(m, key) for m in self.members}) != len(self.members):
                raise ValueError("组合子版本及位置必须唯一")
        return self


class Trial(Contract):
    version_id: UUID
    case: CaseView
    prediction: UnifiedResponse
    materials: dict[str, Any] = Field(default_factory=dict)


class EvaluationResult(Contract):
    evaluator_version_id: UUID
    evaluator_name: str
    evaluator_version: str
    status: Status
    score: Unit | None = None
    passed: bool | None = Field(None, strict=True)
    reason: str = Field(min_length=1)
    details: dict[str, Any] = Field(default_factory=dict)
    evidence: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_status(self) -> Self:
        if self.status == "OK":
            if self.score is None or self.passed is None:
                raise ValueError("OK 需要有效得分和通过标志")
        elif self.score is not None or self.passed is not None:
            raise ValueError("未完成评分必须返回空分数和通过标志")
        return self


class JudgeAccount(Contract):
    endpoint: str
    destination: TargetRule
    models: list[Name] = Field(min_length=1)
    max_parallel: int = Field(ge=1, le=8)
    rpm: int = Field(ge=1, le=120)


class JudgeClient(Protocol):
    async def grade(
        self, case: CaseView, prediction: UnifiedResponse, ctx: "ScoringContext"
    ) -> EvaluationResult: ...


@dataclass(frozen=True)
class ScoringContext:
    version: dict[str, Any]
    deadline_at: float  # monotonic clock, shared by the entire DAG
    materials: dict[str, Any]
    judge: JudgeClient | None = None
    children: tuple[tuple[Member, EvaluationResult], ...] = ()

    def result(self, status: Status, reason: str, **values: Any) -> EvaluationResult:
        return EvaluationResult(
            evaluator_version_id=self.version["id"],
            evaluator_name=self.version["name"],
            evaluator_version=self.version["version"],
            status=status,
            reason=reason,
            **values,
        )


class BaseEvaluator(Protocol):
    async def evaluate(
        self, case: CaseView, prediction: UnifiedResponse, context: ScoringContext
    ) -> EvaluationResult: ...
