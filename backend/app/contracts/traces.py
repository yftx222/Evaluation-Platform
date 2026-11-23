"""TraceWrite v1. Times supplied by the source are normalized before deduplication."""

import json
from datetime import UTC
from typing import Annotated, Any, Literal, Self, get_args
from uuid import UUID

from pydantic import AwareDatetime, Field, field_validator, model_validator

from app.contracts.adapters import Contract, TraceWriteScope
from trace_sdk.privacy import check_fields as check_fields


class BoundTraceWriteScope(TraceWriteScope):
    trace_id: UUID


MAX_BATCH_BYTES = 512 * 1024
MAX_EVENT_BYTES = 32 * 1024
MAX_EVENTS = 100
EventKey = Annotated[str, Field(pattern=r"^[A-Za-z0-9_.:-]{1,128}$")]
EventType = Literal[
    "AGENT_STEP",
    "MODEL_CALL",
    "TOOL_CALL",
    "RETRIEVAL",
    "RERANK",
    "GUARDRAIL",
    "FINAL_OUTPUT",
    "CUSTOM",
]


class TraceCapabilities(Contract):
    mode: Literal["callback", "embedded", "disabled"] = "callback"
    event_types: list[EventType] = Field(default_factory=lambda: list(get_args(EventType)))
    required_events: list[EventType] = Field(default_factory=list)
    contiguous: bool = True
    parent_links: bool = True
    model_usage: bool = True
    retrieval_refs: bool = True
    tool_details: bool = True

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if not set(self.required_events) <= set(self.event_types):
            raise ValueError("必需事件须属于支持类型")
        if len(set(self.event_types)) != len(self.event_types) or len(
            set(self.required_events)
        ) != len(self.required_events):
            raise ValueError("重复的事件类型")
        if self.mode == "disabled" and self.required_events:
            raise ValueError("关闭采集时不能要求事件")
        return self


Sequence = Annotated[int, Field(strict=True, ge=1, le=2**63 - 1)]


def canonical(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


class TraceEventInput(Contract):
    schema_version: Literal["1.0"] = "1.0"
    event_key: EventKey
    sequence: Sequence
    parent_event_key: EventKey | None = None
    event_type: EventType
    name: str = Field(min_length=1, max_length=160)
    status: Literal["STARTED", "SUCCEEDED", "FAILED", "CANCELLED", "UNKNOWN"]
    started_at: AwareDatetime
    ended_at: AwareDatetime | None = None
    duration_ms: int | None = Field(None, strict=True, ge=0, le=604800000)
    input: dict[str, Any] = Field(default_factory=dict)
    output: dict[str, Any] = Field(default_factory=dict)
    attributes: dict[str, Any] = Field(default_factory=dict)
    resource: dict[str, Any] = Field(default_factory=dict)

    @field_validator("started_at", "ended_at", mode="before")
    @classmethod
    def timestamp_string(cls, value: object) -> object:
        if value is not None and not isinstance(value, str):
            raise ValueError("ISO timestamp required")
        return value

    @model_validator(mode="after")
    def validate_span(self) -> Self:
        self.started_at = self.started_at.astimezone(UTC)
        if self.ended_at:
            self.ended_at = self.ended_at.astimezone(UTC)
            if self.ended_at < self.started_at:
                raise ValueError("INVALID_INTERVAL")
        if self.event_key == self.parent_event_key:
            raise ValueError("PARENT_CYCLE")
        return self


class TraceBatch(Contract):
    events: list[TraceEventInput] = Field(min_length=1, max_length=MAX_EVENTS)


class TraceFinalize(Contract):
    outcome: Literal["COMPLETE", "PARTIAL"]
    # Sparse sources can declare the expected number as well as their high-water mark.
    event_count: int | None = Field(None, strict=True, ge=0, le=2**63 - 1)
    contiguous: bool = Field(strict=True)
    last_sequence: int = Field(strict=True, ge=0, le=2**63 - 1)
