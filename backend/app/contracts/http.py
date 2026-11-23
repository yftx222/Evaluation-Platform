import ipaddress
import re
from typing import Any, Literal, Self
from urllib.parse import urlsplit

from pydantic import Field, model_validator

from app.contracts.adapters import Contract


class TargetRule(Contract):
    scheme: Literal["http", "https"] = "https"
    host: str
    port: int = Field(default=443, ge=1, le=65535)
    prefix: str = "/"
    networks: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def check_networks(self) -> Self:
        for network in self.networks:
            ipaddress.ip_network(network)
        return self


class AdapterConfig(Contract):
    kind: str = "http"
    build: str = "1"
    base_url: str | None = Field(None, exclude_if=lambda value: value is None)
    options: dict[str, Any] = Field(default_factory=dict, exclude_if=lambda value: not value)
    endpoint: str
    probe_endpoint: str
    method: Literal["GET", "POST"] = "POST"
    request_map: dict[str, str] = Field(default_factory=lambda: {"query": "input.query"})
    answer_path: str | None = "answer"
    structured_path: str | None = None
    headers: dict[
        Literal["Accept", "Accept-Language"], Literal["application/json", "zh-CN", "en"]
    ] = {}
    secret_ref: str | None = None
    secret_version: str | None = None
    timeout_s: float = Field(default=5, gt=0, le=20)

    @model_validator(mode="after")
    def validate_mapping(self) -> Self:
        if self.base_url is not None:
            base = urlsplit(self.base_url)
            if (
                not base.hostname
                or base.scheme not in {"http", "https"}
                or base.query
                or base.fragment
                or base.username is not None
            ):
                raise ValueError("base_url 需要明确的 HTTP(S) 地址")
            for value in (self.endpoint, self.probe_endpoint):
                if not value or urlsplit(value).scheme or value.startswith("//"):
                    raise ValueError("设置 base_url 时，业务和探测地址应填写相对路径")
        if (self.secret_ref is None) != (self.secret_version is None):
            raise ValueError("密钥引用和版本必须同时设置")
        if self.secret_version is not None and (
            not self.secret_ref
            or not self.secret_version.strip()
            or self.secret_version.strip().lower() == "latest"
        ):
            raise ValueError("需要固定的密钥版本")
        if not self.answer_path and not self.structured_path:
            raise ValueError("至少配置一条响应映射")
        for path in [*self.request_map.values(), self.answer_path, self.structured_path]:
            if path is not None and not re.fullmatch(r"[\w-]+(?:\.[\w-]+)*", path):
                raise ValueError("字段路径无效")
        return self

    def address(self, path: str) -> str:
        if self.base_url is None:
            return path
        return self.base_url.rstrip("/") + "/" + path.lstrip("/")
