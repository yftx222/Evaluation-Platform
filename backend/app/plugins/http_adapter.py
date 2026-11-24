import asyncio
import hashlib
import inspect
import ipaddress
import json
import re
import socket
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from types import ModuleType
from typing import Any
from urllib.parse import urlsplit

from pydantic import ValidationError

from app.application.access.identity import SecretResolver
from app.contracts.adapters import (
    AdapterError,
    BaseAdapter,
    ConnectionResult,
    Contract,
    UnifiedRequest,
    UnifiedResponse,
)
from app.contracts.http import AdapterConfig, TargetRule
from app.infrastructure import attempt_http as httpx
from app.plugins import dataagent, dataagent_profile, dataagent_trace
from app.plugins.adapter_protocols import (
    AgentMapping,
    AgentOptions,
    ChatMapping,
    ChatOptions,
    HTTPOptions,
    Mapping,
)


class DestinationPolicy:
    def __init__(self, rules: list[TargetRule], development: bool) -> None:
        self.rules = rules
        self.development = development

    async def approve(self, raw: str) -> tuple[httpx.URL, str, str]:
        denial = AdapterError("DESTINATION_DENIED", "not_sent")
        try:
            parts = urlsplit(raw)
            url = httpx.URL(raw)
            port = parts.port or (443 if parts.scheme == "https" else 80)
        except (ValueError, httpx.InvalidURL) as exc:
            raise denial from exc
        if (
            parts.scheme not in {"http", "https"}
            or (not self.development and parts.scheme != "https")
            or not parts.hostname
            or parts.username is not None
            or parts.fragment
            or parts.query
            or not re.fullmatch(r"[A-Za-z0-9/_~.\-]*", parts.path)
            or any(p in {".", ".."} for p in parts.path.split("/"))
            or "\\" in raw
        ):
            raise denial
        host = parts.hostname.lower()
        rule = next(
            (
                r
                for r in self.rules
                if (r.scheme, r.host.lower(), r.port) == (parts.scheme, host, port)
                and (
                    parts.path == r.prefix.rstrip("/")
                    or parts.path.startswith(r.prefix.rstrip("/") + "/")
                )
            ),
            None,
        )
        if not rule:
            raise denial
        try:
            answers = await asyncio.get_running_loop().getaddrinfo(
                host, port, type=socket.SOCK_STREAM
            )
        except OSError as exc:
            raise AdapterError("DNS_UNAVAILABLE", "not_sent", True) from exc
        approved = []
        for answer in answers:
            ip = ipaddress.ip_address(str(answer[4][0]))
            if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
                ip = ip.ipv4_mapped
            if (
                ip.is_link_local
                or ip.is_multicast
                or ip.is_unspecified
                or ip.is_reserved
                or (ip.is_loopback and not self.development)
                or not any(
                    ip in network and (ip.is_global or network.is_private)
                    for network in map(ipaddress.ip_network, rule.networks)
                )
            ):
                raise denial
            approved.append(str(ip))
        if not approved:
            raise denial
        return url.copy_with(host=approved[0]), host, url.netloc.decode()


class HTTPAdapter(BaseAdapter):
    def __init__(
        self,
        config: AdapterConfig,
        policy: DestinationPolicy,
        resolver: SecretResolver,
        project: str,
        mapping: Mapping | None = None,
        capabilities: tuple[str, ...] = ("execute",),
    ) -> None:
        self.mapping = mapping or Mapping(config, project)
        self.capabilities = capabilities
        self.config, self.policy = config, policy
        self.resolver, self.project = resolver, project

    async def _call(
        self,
        endpoint: str,
        method: str,
        body: dict[str, object],
        key: str | None = None,
        *,
        budget: float,
    ) -> tuple[bytes, str | None]:
        end = time.monotonic() + budget
        try:
            async with asyncio.timeout(budget):
                pinned, hostname, authority = await self.policy.approve(
                    self.config.address(endpoint)
                )
        except TimeoutError as exc:
            raise AdapterError("TARGET_TIMEOUT", "not_sent", True) from exc
        headers: dict[str, str] = {k: v for k, v in self.config.headers.items()}
        headers["Host"] = authority
        if self.config.secret_ref and self.config.secret_version:
            value = self.resolver.resolve(
                self.project, self.config.secret_ref, self.config.secret_version
            )
            if not value:
                raise AdapterError("SECRET_VERSION_UNAVAILABLE", "not_sent")
            headers["Authorization"] = "Bearer " + value.get_secret_value()
        if key is not None and (not key.isascii() or not key.isprintable() or not key.strip()):
            raise AdapterError("INVALID_REQUEST", "not_sent")
        if key:
            headers["Idempotency-Key"] = key
        params = {k: str(v) for k, v in body.items()} if method == "GET" else None
        content = None
        if method == "POST":
            try:
                content = json.dumps(
                    body, ensure_ascii=False, allow_nan=False, separators=(",", ":")
                ).encode()
            except (TypeError, ValueError, RecursionError) as exc:
                raise AdapterError("INVALID_REQUEST", "not_sent") from exc
            headers["Content-Type"] = "application/json"
        try:
            async with (
                asyncio.timeout(end - time.monotonic()),
                httpx.AsyncClient(
                    trust_env=False, follow_redirects=False, timeout=self.config.timeout_s
                ) as client,
            ):
                async with client.stream(
                    method,
                    pinned,
                    headers=headers,
                    params=params,
                    content=content,
                    extensions={"sni_hostname": hostname},
                ) as response:
                    if 300 <= response.status_code < 400:
                        raise AdapterError("REDIRECT_DENIED", "sent")
                    if response.status_code in (401, 403):
                        raise AdapterError("TARGET_AUTH_FAILED", "sent")
                    if response.status_code == 429:
                        raise AdapterError("TARGET_RATE_LIMIT", "sent", True)
                    if response.is_error:
                        raise AdapterError("TARGET_HTTP_ERROR", "sent", response.status_code >= 500)
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        data.extend(chunk)
                        if len(data) > 1048576:
                            raise AdapterError("RESPONSE_TOO_LARGE", "sent")
                    return bytes(data), response.headers.get("x-request-id")
        except httpx.ConnectTimeout as exc:
            raise AdapterError("TARGET_TIMEOUT", "not_sent", True) from exc
        except (httpx.TimeoutException, TimeoutError) as exc:
            raise AdapterError("TARGET_TIMEOUT", "unknown", True) from exc
        except httpx.ConnectError as exc:
            raise AdapterError("NETWORK_ERROR", "not_sent", True) from exc
        except httpx.HTTPError as exc:
            raise AdapterError("NETWORK_ERROR", "unknown", True) from exc

    async def validate_connection(self) -> ConnectionResult:
        start = time.monotonic()
        await self._call(self.config.probe_endpoint, "GET", {}, budget=self.config.timeout_s)
        return ConnectionResult(
            ok=True,
            latency_ms=int((time.monotonic() - start) * 1000),
            code="OK",
            message="连接成功",
            capabilities=list(self.capabilities),
        )

    async def execute(self, request: UnifiedRequest) -> UnifiedResponse:
        try:
            body = self.mapping.request_body(request)
        except (ValueError, KeyError, TypeError) as exc:
            raise AdapterError("INVALID_REQUEST", "not_sent") from exc
        budget = (request.deadline_at - datetime.now(UTC)).total_seconds()
        if budget <= 0:
            raise AdapterError("TARGET_TIMEOUT", "not_sent")
        start = time.monotonic()
        try:
            data, request_id = await self._call(
                self.config.endpoint,
                self.config.method,
                body,
                request.idempotency_key,
                budget=min(budget, self.config.timeout_s),
            )
            result = self.normalize_response(json.loads(data))
            if request_id:
                result.observed["request_id"] = request_id
        except (ValueError, RecursionError) as exc:
            raise AdapterError("INVALID_RESPONSE", "sent") from exc
        result.latency_ms = int((time.monotonic() - start) * 1000)
        return result

    def normalize_response(self, raw: object) -> UnifiedResponse:
        try:
            return self.mapping.normalize(raw)
        except (ValueError, KeyError, TypeError, IndexError, RecursionError) as exc:
            raise AdapterError("INVALID_RESPONSE", "sent") from exc

    async def collect_trace(self, context: dict[str, object]) -> list[dict[str, object]]:
        try:
            return self.mapping.trace(context)
        except (ValueError, KeyError, TypeError) as exc:
            raise AdapterError("INVALID_TRACE", "sent") from exc


@dataclass(frozen=True)
class Implementation:
    factory: type[Mapping]
    options: type[Contract]
    requirements: tuple[str, ...]
    capabilities: tuple[str, ...]
    example: dict[str, Any]
    dependencies: tuple[ModuleType, ...] = ()

    def fingerprint(self) -> str:
        # Include shared conversion helpers, wire contracts and the restricted transport.
        modules = {
            cls.__module__
            for cls in (Mapping, self.factory, self.options, AdapterConfig, UnifiedRequest)
        }
        modules.update(module.__name__ for module in self.dependencies)
        sources = [inspect.getsource(sys.modules[name]) for name in sorted(modules)]
        sources.extend(inspect.getsource(cls) for cls in (DestinationPolicy, HTTPAdapter))
        sources.append(inspect.getsource(AdapterRegistry.create))
        sources.extend(
            (
                self.factory.__qualname__,
                self.options.__qualname__,
                repr(self.requirements),
                repr(self.capabilities),
            )
        )
        return hashlib.sha256("\0".join(sources).encode("utf-8")).hexdigest()


class AdapterRegistry:
    def __init__(self) -> None:
        self._plugins: dict[tuple[str, str], Implementation] = {}
        self._digests: dict[tuple[str, str], str] = {}

    def register(
        self, kind: str, build: str, plugin: Implementation, *, release_digest: str | None = None
    ) -> None:
        if (kind, build) in self._plugins:
            raise ValueError("重复的 Adapter 注册")
        fingerprint = plugin.fingerprint()
        if (release_digest or build) != fingerprint:
            raise ValueError(f"Adapter {kind}/{build} 与发布摘要不符，请使用新 build")
        self._plugins[kind, build] = plugin
        self._digests[kind, build] = fingerprint

    def digest(self, kind: str, build: str) -> str:
        try:
            return self._digests[kind, build]
        except KeyError as exc:
            raise AdapterError("VERSION_UNAVAILABLE", "not_sent") from exc

    def capabilities(self, kind: str, build: str) -> tuple[str, ...]:
        self.digest(kind, build)
        return self._plugins[kind, build].capabilities

    def mapping_for(self, config: AdapterConfig, project: str) -> Mapping:
        self.digest(config.kind, config.build)
        return self._plugins[config.kind, config.build].factory(config, project)

    def describe(self) -> list[dict[str, Any]]:
        return [
            {
                "kind": k,
                "build": b,
                "implementation_digest": self.digest(k, b),
                "config_schema": p.options.model_json_schema(),
                "input_requirements": p.requirements,
                "capabilities": p.capabilities,
                "example": p.example,
            }
            for (k, b), p in self._plugins.items()
            if (k, b) != ("http", "1")
        ]

    def create(
        self,
        config: AdapterConfig,
        policy: DestinationPolicy,
        resolver: SecretResolver,
        project: str,
    ) -> BaseAdapter:
        plugin = self._plugins.get((config.kind, config.build))
        if plugin is None:
            raise AdapterError("VERSION_UNAVAILABLE", "not_sent")
        try:
            plugin.options.model_validate(config.options)
        except ValidationError as exc:
            raise AdapterError("INVALID_ADAPTER_CONFIG", "not_sent") from exc
        if config.kind != "http" and (
            config.method != "POST"
            or config.request_map != {"query": "input.query"}
            or config.answer_path != "answer"
            or config.structured_path is not None
        ):
            raise AdapterError("INVALID_ADAPTER_CONFIG", "not_sent")
        return HTTPAdapter(
            config, policy, resolver, project, plugin.factory(config, project), plugin.capabilities
        )


class PlatformAdapterRegistry(AdapterRegistry):
    """Multi-request protocols own their transport; existing HTTP builds stay frozen."""

    def create(
        self,
        config: AdapterConfig,
        policy: DestinationPolicy,
        resolver: SecretResolver,
        project: str,
    ) -> BaseAdapter:
        standard = super().create(config, policy, resolver, project)
        if config.kind == "dataagent":
            if config.build == "3":
                return dataagent_profile.ProfiledDataAgentAdapter(config, policy, resolver, project)
            if config.build == "2":
                return dataagent_trace.TracedDataAgentAdapter(config, policy, resolver, project)
            return dataagent.DataAgentEvaluationAdapter(config, policy, resolver, project)
        return standard


registry: AdapterRegistry = PlatformAdapterRegistry()
dataagent_plugin = Implementation(
    dataagent.DataMapping,
    dataagent.Options,
    ("turns",),
    ("execute", "multi_turn", "artifacts"),
    {"role": "dataagent_eval_d1", "fixture_version": "mfg-service-demo-v1"},
)
http_mapping = Implementation(Mapping, HTTPOptions, ("request_map",), ("execute",), {})
# Frozen baseline for snapshots published before implementation digests were stored.
LEGACY_RELEASES = {
    ("http", "1"): "53b6530eb65298b2be7f200739bfdf37962a9e5014ae92613a2adc32091b7a10",
    ("openai_compatible", "1"): "3cdfee7a08aa18b2a37ed02f55642928cdc10b2459db132aa3138dd508e83bf2",
    ("agent", "1"): "e370c9dedce90fd0a0f48d701cb7b31ea46e4703fd471580f201ab68b7097405",
}
registry.register("http", "2", http_mapping, release_digest=LEGACY_RELEASES["http", "1"])
registry.register("http", "1", http_mapping, release_digest=LEGACY_RELEASES["http", "1"])
registry.register(
    "openai_compatible",
    "1",
    Implementation(
        ChatMapping,
        ChatOptions,
        ("query | messages",),
        ("execute", "chat_completions"),
        {"model": "your-model-id"},
    ),
    release_digest=LEGACY_RELEASES["openai_compatible", "1"],
)
registry.register(
    "agent",
    "1",
    Implementation(
        AgentMapping, AgentOptions, ("query | messages",), ("execute", "embedded_events"), {}
    ),
    release_digest=LEGACY_RELEASES["agent", "1"],
)
registry.register(
    "dataagent",
    "1",
    dataagent_plugin,
    release_digest="e364fc5e1c3049532cbd042e096c9421874b672333df49224fa58a33baa31686",
)
traced_dataagent = Implementation(
    dataagent.DataMapping,
    dataagent.Options,
    ("turns",),
    (*dataagent_plugin.capabilities, "callback"),
    dataagent_plugin.example,
    (dataagent_trace,),
)
registry.register(
    "dataagent",
    "2",
    traced_dataagent,
    release_digest="8a8bf914622650a63100e1954d0d7e12515365b05d8fb75bfc374cda57813ee4",
)
profiled_dataagent = Implementation(
    dataagent.DataMapping,
    dataagent.Options,
    ("turns",),
    (*traced_dataagent.capabilities, "environment_snapshot"),
    dataagent_plugin.example,
    (dataagent_trace, dataagent_profile),
)
registry.register(
    "dataagent",
    "3",
    profiled_dataagent,
    release_digest="6910d333053ebb3fc5c2b8c76596607202a9864fd129d0d17c9ad0a24afca6d4",
)
