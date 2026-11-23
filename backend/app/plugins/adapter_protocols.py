"""Pure wire-format mappings. Credentials, URLs and the HTTP client stay in the transport."""

from typing import Annotated, Any, Literal, Self

from pydantic import AwareDatetime, Field, model_validator

from app.contracts.adapters import Contract, UnifiedRequest, UnifiedResponse
from app.contracts.http import AdapterConfig

Path = Annotated[str, Field(pattern=r"^[\w-]+(?:\.[\w-]+)*$", max_length=256)]


def field(value: object, path: str) -> Any:
    for key in path.split("."):
        if not isinstance(value, dict) or key not in value:
            raise KeyError(key)
        value = value[key]
    return value


class HTTPOptions(Contract):
    usage_path: Path | None = None
    observed_path: Path | None = None
    extensions_path: Path | None = None


class ChatOptions(Contract):
    model: str = Field(min_length=1, max_length=160)
    parsed_path: Path = "parsed"
    revision_path: Path = "model_revision"


class AgentOptions(Contract):
    answer_field: Path = "final_answer"
    structured_field: Path = "structured_output"


class Event(Contract):
    source_event_key: str = Field(min_length=1, max_length=160)
    sequence: int = Field(ge=1, strict=True)
    parent_key: str | None = None
    event_type: Literal[
        "AGENT_STEP", "MODEL_CALL", "TOOL_CALL", "RETRIEVAL", "FINAL_OUTPUT", "CUSTOM"
    ]
    occurred_at: AwareDatetime
    duration_ms: int | None = Field(None, ge=0, strict=True)
    input: Any = None
    output: Any = None
    metadata: dict[str, Any] = {}
    extensions: dict[str, Any] = {}
    input_tokens: int | None = Field(None, ge=0, strict=True)
    output_tokens: int | None = Field(None, ge=0, strict=True)


class Events(Contract):
    events: list[Event] = Field(max_length=256)

    @model_validator(mode="after")
    def unique_keys(self) -> Self:
        if len({e.source_event_key for e in self.events}) != len(self.events) or len(
            {e.sequence for e in self.events}
        ) != len(self.events):
            raise ValueError("事件键与序号必须分别唯一")
        return self


def optional(data: object, path: str) -> Any:
    try:
        return field(data, path)
    except KeyError:
        return None


def obj(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("需要 JSON 对象")
    return value


def scoped(value: object, project: str) -> dict[str, object]:
    if value is None:
        return {}
    return {project: obj(value)}


def response(
    answer: object,
    structured: object,
    usage: object,
    observed: object,
    extensions: dict[str, object],
) -> UnifiedResponse:
    counters = {k: v for k, v in obj(usage).items() if k in {"input_tokens", "output_tokens"}}
    if any(type(v) is not int or v < 0 for v in counters.values()):
        raise ValueError("token 数必须为非负整数")
    metadata = {
        k: v
        for k, v in obj(observed).items()
        if k in {"deployment_ref", "model_id", "model_revision", "request_id"}
    }
    return UnifiedResponse.model_validate(
        dict(
            answer=answer,
            structured_output=structured,
            usage=counters,
            observed=metadata,
            extensions=extensions,
        ),
        strict=True,
    )


class Mapping:
    """Trusted mapping hooks accept data only. HTTP field mapping is the default."""

    def __init__(self, config: AdapterConfig, project: str) -> None:
        self.config, self.project = config, project

    def request_body(self, request: UnifiedRequest) -> dict[str, object]:
        data = request.model_dump(mode="json")
        return {target: field(data, path) for target, path in self.config.request_map.items()}

    def normalize(self, raw: object) -> UnifiedResponse:
        c = self.config
        options = HTTPOptions.model_validate(c.options)
        return response(
            field(raw, c.answer_path) if c.answer_path else None,
            field(raw, c.structured_path) if c.structured_path else None,
            field(raw, options.usage_path) if options.usage_path else {},
            field(raw, options.observed_path) if options.observed_path else {},
            scoped(field(raw, options.extensions_path), self.project)
            if options.extensions_path
            else {},
        )

    def trace(self, context: dict[str, object]) -> list[dict[str, object]]:
        events = context.get("events", [])
        if not isinstance(events, list) or not all(isinstance(e, dict) for e in events):
            raise ValueError("无效的事件数组")
        return events


class ChatParameters(Contract):
    temperature: float | None = Field(None, ge=0, le=2)
    top_p: float | None = Field(None, ge=0, le=1)
    max_tokens: int | None = Field(None, gt=0)
    max_completion_tokens: int | None = Field(None, gt=0)
    seed: int | None = None
    stop: str | list[str] | None = None
    response_format: dict[str, Any] | None = None


def messages(data: dict[str, object]) -> list[dict[str, Any]]:
    supplied = data.get("messages")
    if supplied is None or supplied == []:
        query = data.get("query")
        if not isinstance(query, str) or not query.strip():
            raise ValueError("需要 query 或 messages")
        return [{"role": "user", "content": query}]
    if not isinstance(supplied, list) or not all(
        isinstance(m, dict)
        and isinstance(m.get("role"), str)
        and ("content" in m or "tool_calls" in m)
        for m in supplied
    ):
        raise ValueError("messages 需要有效消息数组")
    return supplied


class ChatMapping(Mapping):
    def request_body(self, request: UnifiedRequest) -> dict[str, object]:
        options = ChatOptions.model_validate(self.config.options)
        params = ChatParameters.model_validate(request.parameters, strict=True)
        return {
            "model": options.model,
            "messages": messages(request.input),
            **params.model_dump(exclude_none=True),
        }

    def normalize(self, raw: object) -> UnifiedResponse:
        data = obj(raw)
        options = ChatOptions.model_validate(self.config.options)
        choices = data["choices"]
        if not isinstance(choices, list) or not choices:
            raise ValueError("缺少 choices")
        message = obj(obj(choices[0])["message"])
        usage = obj(data["usage"]) if data.get("usage") is not None else {}
        counts = {
            k: usage[src]
            for k, src in (
                ("input_tokens", "prompt_tokens"),
                ("output_tokens", "completion_tokens"),
            )
            if usage.get(src) is not None
        }
        observed = {
            k: v
            for k, v in (
                ("model_id", data.get("model")),
                ("model_revision", optional(data, options.revision_path)),
                ("request_id", data.get("request_id")),
            )
            if v is not None
        }
        extra = scoped(data.get("extensions"), self.project)
        if isinstance(data.get("id"), str):
            extra["_chat_completion_id"] = data["id"]
        return response(
            message.get("content"),
            optional(message, options.parsed_path),
            counts,
            observed,
            extra,
        )


class AgentMapping(Mapping):
    def request_body(self, request: UnifiedRequest) -> dict[str, object]:
        messages(request.input)
        body = {
            k: request.input[k]
            for k in ("query", "messages", "context", "attachments")
            if k in request.input
        }
        return {
            **body,
            "parameters": request.parameters,
            "extensions": scoped(request.extensions.get(self.project), self.project),
        }

    def normalize(self, raw: object) -> UnifiedResponse:
        data = obj(raw)
        options = AgentOptions.model_validate(self.config.options)
        extras = scoped(data.get("extensions"), self.project)
        if "events" in data:
            # Keep capture per response. Trace parsing cannot discard a valid prediction.
            extras["_agent_events"] = data["events"]
        return response(
            optional(data, options.answer_field),
            optional(data, options.structured_field),
            data.get("usage", {}),
            data.get("observed", {}),
            extras,
        )

    def trace(self, context: dict[str, object]) -> list[dict[str, object]]:
        source = obj(context.get("extensions", {}))
        events = Events.model_validate(
            {"events": source.get("_agent_events", context.get("events", []))}
        )
        return [
            event.model_dump(mode="json", exclude_none=True)
            for event in sorted(events.events, key=lambda e: e.sequence)
        ]
