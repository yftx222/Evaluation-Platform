import asyncio
import ipaddress
import json
import re
import socket
import time
from collections.abc import Callable
from datetime import UTC, datetime
from urllib.parse import urlsplit

import httpx
from pydantic import ValidationError

from app.application.access.identity import SecretResolver
from app.contracts.adapters import (
    AdapterError,
    BaseAdapter,
    ConnectionResult,
    UnifiedRequest,
    UnifiedResponse,
)
from app.contracts.http import AdapterConfig, TargetRule


def field(value: object, path: str) -> object:
    for key in path.split("."):
        if not isinstance(value, dict) or key not in value:
            raise KeyError(key)
        value = value[key]
    return value


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
    ) -> None:
        self.config, self.policy = config, policy
        self.resolver, self.project = resolver, project

    async def _call(
        self, endpoint: str, method: str, body: dict[str, object], key: str | None = None
    ) -> bytes:
        pinned, hostname, authority = await self.policy.approve(endpoint)
        headers: dict[str, str] = {k: v for k, v in self.config.headers.items()}
        headers["Host"] = authority
        if self.config.secret_ref and self.config.secret_version:
            value = self.resolver.resolve(
                self.project, self.config.secret_ref, self.config.secret_version
            )
            if not value:
                raise AdapterError("SECRET_VERSION_UNAVAILABLE", "not_sent")
            headers["Authorization"] = "Bearer " + value.get_secret_value()
        if key:
            headers["Idempotency-Key"] = key
        params = {k: str(v) for k, v in body.items()} if method == "GET" else None
        try:
            async with httpx.AsyncClient(
                trust_env=False, follow_redirects=False, timeout=self.config.timeout_s
            ) as client:
                async with client.stream(
                    method,
                    pinned,
                    headers=headers,
                    params=params,
                    json=body if method == "POST" else None,
                    extensions={"sni_hostname": hostname},
                ) as response:
                    if 300 <= response.status_code < 400:
                        raise AdapterError("REDIRECT_DENIED", "sent")
                    if response.status_code in (401, 403):
                        raise AdapterError("TARGET_AUTH_FAILED", "sent")
                    if response.is_error:
                        raise AdapterError("TARGET_HTTP_ERROR", "sent", response.status_code >= 500)
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        data.extend(chunk)
                        if len(data) > 1048576:
                            raise AdapterError("RESPONSE_TOO_LARGE", "sent")
                    return bytes(data)
        except httpx.TimeoutException as exc:
            raise AdapterError("TARGET_TIMEOUT", "unknown", True) from exc
        except httpx.ConnectError as exc:
            raise AdapterError("NETWORK_ERROR", "not_sent", True) from exc
        except httpx.HTTPError as exc:
            raise AdapterError("NETWORK_ERROR", "unknown", True) from exc

    async def validate_connection(self) -> ConnectionResult:
        start = time.monotonic()
        try:
            async with asyncio.timeout(self.config.timeout_s):
                await self._call(self.config.probe_endpoint, "GET", {})
        except TimeoutError as exc:
            raise AdapterError("TARGET_TIMEOUT", "unknown", True) from exc
        return ConnectionResult(
            ok=True,
            latency_ms=int((time.monotonic() - start) * 1000),
            code="OK",
            message="连接成功",
            capabilities=["execute"],
        )

    async def execute(self, request: UnifiedRequest) -> UnifiedResponse:
        try:
            body = {
                target: field(request.model_dump(mode="json"), path)
                for target, path in self.config.request_map.items()
            }
        except KeyError as exc:
            raise AdapterError("INVALID_REQUEST", "not_sent") from exc
        budget = (request.deadline_at - datetime.now(UTC)).total_seconds()
        if budget <= 0:
            raise AdapterError("TARGET_TIMEOUT", "not_sent")
        start = time.monotonic()
        try:
            async with asyncio.timeout(min(budget, self.config.timeout_s)):
                data = await self._call(
                    self.config.endpoint, self.config.method, body, request.idempotency_key
                )
            result = self.normalize_response(json.loads(data))
        except TimeoutError as exc:
            raise AdapterError("TARGET_TIMEOUT", "unknown", True) from exc
        except (ValueError, UnicodeError) as exc:
            raise AdapterError("INVALID_RESPONSE", "sent") from exc
        result.latency_ms = int((time.monotonic() - start) * 1000)
        return result

    def normalize_response(self, raw: object) -> UnifiedResponse:
        try:
            return UnifiedResponse.model_validate(
                {
                    "answer": field(raw, self.config.answer_path)
                    if self.config.answer_path
                    else None,
                    "structured_output": field(raw, self.config.structured_path)
                    if self.config.structured_path
                    else None,
                }
            )
        except (ValidationError, KeyError) as exc:
            raise AdapterError("INVALID_RESPONSE", "sent") from exc

    async def collect_trace(self, context: dict[str, object]) -> list[dict[str, object]]:
        events = context.get("events", [])
        if not isinstance(events, list) or not all(isinstance(e, dict) for e in events):
            raise AdapterError("INVALID_TRACE", "sent")
        return events


type Factory = Callable[[AdapterConfig, DestinationPolicy, SecretResolver, str], BaseAdapter]


class AdapterRegistry:
    def __init__(self) -> None:
        self.factories: dict[tuple[str, str], Factory] = {}

    def register(self, kind: str, build: str, factory: Factory) -> None:
        if (kind, build) in self.factories:
            raise ValueError("重复的 Adapter 注册")
        self.factories[kind, build] = factory

    def create(
        self,
        config: AdapterConfig,
        policy: DestinationPolicy,
        resolver: SecretResolver,
        project: str,
    ) -> BaseAdapter:
        factory = self.factories.get((config.kind, config.build))
        if factory is None:
            raise AdapterError("VERSION_UNAVAILABLE", "not_sent")
        return factory(config, policy, resolver, project)


registry = AdapterRegistry()
registry.register("http", "1", HTTPAdapter)
