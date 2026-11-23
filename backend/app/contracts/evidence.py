from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field

from app.contracts.adapters import Contract


class Evidence(Contract):
    evidence_id: str
    score_id: UUID
    evaluator_version_id: UUID
    created_at: AwareDatetime
    source_type: (
        Literal["TRACE_EVENT", "PREDICTION", "CASE_INPUT", "EXPECTED", "EXTERNAL_REFERENCE"] | None
    )
    source_id: UUID | str | None
    attempt_id: UUID | None
    trace_id: UUID | None
    event_id: UUID | None
    json_pointer: str | None
    excerpt: str | None = Field(max_length=240)
    status: Literal["AVAILABLE", "REDACTED", "EVIDENCE_UNAVAILABLE"]
    reason: str | None
    metadata: dict[str, UUID | str]


class ChildScore(Contract):
    score_id: UUID | None
    evaluator_version_id: UUID
    position: int
    weight: float


class ScoreEvidence(Contract):
    score_id: UUID
    evaluator_version_id: UUID
    items: list[Evidence]
    children: list[ChildScore]


class CaseEvidence(Contract):
    scores: list[ScoreEvidence]


class EvidenceResponse[T](Contract):
    data: T
    request_id: str
