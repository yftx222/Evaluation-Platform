from typing import Any, Literal
from uuid import UUID

from pydantic import Field

from app.contracts.adapters import Contract

Dimension = Literal["category", "difficulty", "tag"]
Signal = Literal[
    "any",
    "all",
    "case_failed",
    "timed_out",
    "score_error",
    "low_score",
    "child_failed",
    "trace_partial",
    "trace_missing",
    "token_limit",
    "latency_limit",
    "new_failure",
    "fixed",
    "regressed",
]


class Rate(Contract):
    provider: str = Field(min_length=1, max_length=100)
    model: str = Field(min_length=1, max_length=160)
    revision: str = Field(min_length=1, max_length=160)
    input_per_million: float = Field(ge=0, allow_inf_nan=False)
    output_per_million: float = Field(ge=0, allow_inf_nan=False)


class ReportPolicy(Contract):
    currency: str = Field(default="USD", pattern=r"^[A-Z]{3}$")
    target: Rate | None = None
    judges: dict[UUID, Rate] = Field(default_factory=dict)
    max_case_tokens: int | None = Field(None, ge=1)
    max_case_ms: int | None = Field(None, ge=1)


class Ratio(Contract):
    numerator: int
    denominator: int
    value: float | None


class VersionMetrics(Contract):
    id: UUID
    name: str
    version: str
    members: list[dict[str, Any]]
    mean_score: float | None
    ok: int
    errors: int
    skipped: int
    missing: int
    pass_rate: Ratio
    score_coverage: Ratio
    score_execution_success_rate: Ratio


class Summary(Contract):
    id: UUID
    project_id: UUID
    status: str
    started_at: str | None
    finished_at: str | None
    duration_ms: float | None
    total_cases: int
    succeeded_cases: int
    failed_cases: int
    timed_out_cases: int
    cancelled_cases: int
    unknown_delivery_cases: int
    metrics: dict[str, Any]
    score_counts: dict[str, int]
    evaluators: list[VersionMetrics]
    latency_ms_count: int
    latency_ms_p50: float | None
    latency_ms_p95: float | None
    tokens_count: int
    tokens_p50: float | None
    tokens_p95: float | None
    input_count: int
    output_count: int
    judge_count: int
    input_known: int | None
    output_known: int | None
    judge_known: int | None
    total_tokens: int | None
    complete: int
    partial: int
    missing: int
    not_collected: int
    trace_mode: Literal["disabled", "callback", "embedded"]
    trace_complete_rate: Ratio


class Timing(Contract):
    stage: str
    ms_count: int
    ms_p50: float | None
    ms_p95: float | None
    availability: Literal["OBSERVED", "NOT_APPLICABLE", "UNAVAILABLE"]


class Process(Contract):
    attempts: int
    retries: int
    delivery_unknown: int
    quota_waits: int
    timings: list[Timing]


class Usage(Contract):
    currency: str | None
    estimated_cost: float | None
    cost_basis: str
    models: list[dict[str, Any]]
    truncated: bool


class RunReport(Contract):
    summary: Summary
    process: Process
    usage: Usage
    conditions: dict[str, Any]
    limits: dict[str, int | None]


class Group(Contract):
    label: str | None
    cases: int
    succeeded: int
    ok: int
    errors: int
    skipped: int
    passed: int
    mean_score: float | None
    case_success_rate: Ratio
    pass_rate: Ratio
    score_coverage: Ratio
    score_execution_success_rate: Ratio


class GroupPage(Contract):
    dimension: Dimension
    items: list[Group]
    next_offset: int | None


class CaseRow(Contract):
    execution_id: UUID
    case_key: str
    status: str
    trace_id: UUID | None
    trace_status: str | None
    case_ms: float | None
    input_tokens: int | None
    output_tokens: int | None
    judge_tokens: int | None
    total_tokens: int | None
    min_score: float | None
    errors: int
    failed_scores: int
    attempt_count: int
    reasons: list[str]
    baseline_execution: UUID | None
    evidence_url: str


class CasePage(Contract):
    items: list[CaseRow]
    next_offset: int | None


class RunComparison(Contract):
    baseline: UUID
    reports: list[RunReport]
    groups: list[GroupPage]
    comparisons: list[dict[str, Any]]


class ReportResponse[T](Contract):
    data: T
    request_id: str
