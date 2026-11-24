"""Drive a private DataAgent conversation through the public evaluation deployment."""

import asyncio
import base64
import hashlib
import json
import time
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from pydantic import Field

from app.application.access.identity import SecretResolver
from app.contracts.adapters import (
    AdapterError,
    BaseAdapter,
    ConnectionResult,
    Contract,
    UnifiedRequest,
    UnifiedResponse,
)
from app.contracts.http import AdapterConfig
from app.infrastructure.attempt_http import active
from app.plugins.adapter_protocols import Mapping

if TYPE_CHECKING:
    from app.plugins.http_adapter import DestinationPolicy


class Options(Contract):
    role: str = Field(min_length=1)
    fixture_version: str = Field(min_length=1)
    profile: str = "dataagent_eval_readonly"


class Turn(Contract):
    message: str = Field(min_length=1, max_length=32000)


class Attachment(Contract):
    filename: str = Field(pattern=r"^[\w-][\w.-]{0,119}$")
    content_base64: str = Field(max_length=1048576)


class Case(Contract):
    turns: list[Turn] = Field(min_length=1, max_length=32)
    attachments: list[Attachment] = Field(default_factory=list, max_length=8)
    evaluation_profile: str = "dataagent_eval_readonly"
    expected_data_scope: dict[str, object] = Field(default_factory=dict)


class DataMapping(Mapping):
    def request_body(self, request: UnifiedRequest) -> dict[str, object]:
        case = Case.model_validate(request.input)
        if case.evaluation_profile != Options.model_validate(self.config.options).profile:
            raise ValueError("评测环境不匹配")
        for file in case.attachments:
            base64.b64decode(file.content_base64, validate=True)
        return case.model_dump()


class Session:
    def __init__(
        self,
        client: httpx.AsyncClient,
        policy: "DestinationPolicy",
        origin: str,
        login: dict[str, str],
        role: str,
    ) -> None:
        self.client, self.policy, self.origin = client, policy, origin
        self.login, self.role = login, role
        self.access = self.refresh = ""
        self.expires = 0.0

    async def call(
        self, method: str, path: str, *, auth: bool = True, stream: bool = False, **kwargs: Any
    ) -> Any:
        if auth and time.monotonic() >= self.expires:
            await self.authenticate()
        pinned, host, authority = await self.policy.approve(self.origin + path)
        headers = {"Host": authority}
        if auth:
            headers["Authorization"] = "Bearer " + self.access
        async with self.client.stream(
            method, pinned, headers=headers, extensions={"sni_hostname": host}, **kwargs
        ) as res:
            if not res.is_success:
                raise AdapterError("DATAAGENT_HTTP_" + str(res.status_code), "sent")
            if stream:
                buf = b""
                async for chunk in res.aiter_bytes(chunk_size=8192):
                    buf += chunk
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        if not line.startswith(b"data:"):
                            continue
                        evt = json.loads(line[5:])
                        if evt["type"] == "done":
                            return
                        if evt["type"] == "error":
                            if (
                                evt.get("content")
                                == "事件流消费速度过慢，请重新连接以恢复最新状态。"
                            ):
                                return
                            raise AdapterError("DATAAGENT_TURN_FAILED", "sent")
                    if len(buf) > 1048576:
                        raise AdapterError("RESPONSE_TOO_LARGE", "sent")
                return
            size = 0
            file = path.endswith("/attachment/get")
            digest = hashlib.sha256()
            data = bytearray()
            async for chunk in res.aiter_bytes(chunk_size=65536):
                size += len(chunk)
                if size > (33554432 if file else 4194304):
                    raise AdapterError("RESPONSE_TOO_LARGE", "sent")
                if file:
                    digest.update(chunk)
                else:
                    data.extend(chunk)
            if file:
                return {
                    "sha256": digest.hexdigest(),
                    "size": size,
                    "media_type": res.headers.get("content-type"),
                    "http_status": res.status_code,
                }
            return json.loads(data)

    async def authenticate(self) -> None:
        refresh = bool(self.refresh)
        data = await self.call(
            "POST",
            "/api/v1/auth/" + ("refresh" if refresh else "login"),
            auth=False,
            json={"refresh_token": self.refresh} if refresh else self.login,
        )
        user = data["user"]
        if (
            user["is_admin"] is not False
            or user["is_active"] is not True
            or user["doris_role"] != self.role
        ):
            raise AdapterError("DATAAGENT_PROFILE_DENIED", "not_sent")
        self.access, self.refresh = data["access_token"], data["refresh_token"]
        self.expires = time.monotonic() + max(1, int(data["expires_in"]) - 30)


class DataAgentEvaluationAdapter(BaseAdapter):
    def __init__(
        self,
        config: AdapterConfig,
        policy: "DestinationPolicy",
        resolver: SecretResolver,
        project: str,
    ) -> None:
        self.cfg, self.policy, self.resolver, self.project = config, policy, resolver, project
        self.opts = Options.model_validate(config.options)
        dest = urlsplit(config.address(config.endpoint))
        self.origin = f"{dest.scheme}://{dest.netloc}"
        if (
            config.address(config.endpoint) != self.origin + "/api/v1/chat/stream"
            or config.address(config.probe_endpoint) != self.origin + "/_eval/status"
            or not config.secret_ref
            or not config.secret_version
        ):
            raise AdapterError("INVALID_ADAPTER_CONFIG", "not_sent")

    async def session(self, client: httpx.AsyncClient) -> Session:
        secret = self.resolver.resolve(
            self.project, self.cfg.secret_ref or "", self.cfg.secret_version or ""
        )
        if not secret:
            raise AdapterError("SECRET_VERSION_UNAVAILABLE", "not_sent")
        try:
            login = json.loads(secret.get_secret_value())
            if set(login) != {"identifier", "password"} or not all(
                isinstance(v, str) and v for v in login.values()
            ):
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise AdapterError("INVALID_DATAAGENT_CREDENTIAL", "not_sent") from None
        s = Session(client, self.policy, self.origin, login, self.opts.role)
        if (await s.call("GET", "/_eval/status", auth=False)).get("experience_writes") is not False:
            raise AdapterError("DATAAGENT_EXPERIENCE_WRITES_ENABLED", "not_sent")
        await s.authenticate()
        return s

    async def validate_connection(self) -> ConnectionResult:
        start = time.monotonic()
        try:
            async with (
                asyncio.timeout(self.cfg.timeout_s),
                httpx.AsyncClient(trust_env=False) as client,
            ):
                await self.session(client)
        except (ValueError, KeyError, TypeError, httpx.HTTPError, TimeoutError):
            raise AdapterError("DATAAGENT_PROBE_FAILED", "not_sent") from None
        return ConnectionResult(
            ok=True,
            latency_ms=int(1000 * (time.monotonic() - start)),
            code="OK",
            message="已验证专用评测身份与经验写入开关",
            capabilities=["execute", "multi_turn", "artifacts"],
        )

    async def execute(self, request: UnifiedRequest) -> UnifiedResponse:
        start = time.monotonic()
        try:
            case = Case.model_validate(DataMapping(self.cfg, self.project).request_body(request))
        except (TypeError, ValueError):
            raise AdapterError("INVALID_REQUEST", "not_sent") from None
        sent = False
        try:
            async with (
                asyncio.timeout((request.deadline_at - datetime.now(UTC)).total_seconds()),
                httpx.AsyncClient(
                    timeout=httpx.Timeout(self.cfg.timeout_s, read=None),
                    trust_env=False,
                    follow_redirects=False,
                ) as client,
            ):
                s = await self.session(client)
                wire = active.get()
                if wire:
                    await wire.before_send()
                    wire.started = True
                sent = True
                created = await s.call(
                    "POST",
                    "/api/v1/chat/create",
                    json={"is_draft": True, "initial_message": "Evaluation"},
                )
                cid = str(UUID(created["conversation_id"]))
                chat = "/api/v1/chat"
                refs = []
                for file in case.attachments:
                    uploaded = await s.call(
                        "POST",
                        chat + "/attachment/upload",
                        data={"conversation_id": cid},
                        files={
                            "file": (
                                file.filename,
                                base64.b64decode(file.content_base64, validate=True),
                            )
                        },
                    )
                    refs.append({"f_path": uploaded["attachment"]["f_path"]})
                final_attachments: set[tuple[int, str]] = set()
                for i, turn in enumerate(case.turns):
                    try:
                        await s.call(
                            "POST",
                            chat + "/stream",
                            stream=True,
                            json={
                                "conversation_id": cid,
                                "message": {
                                    "parts": [{"type": "text", "text": turn.message}],
                                    "attachments": refs if i == 0 else [],
                                },
                            },
                        )
                    except httpx.TransportError:
                        pass
                    while True:
                        running = (await s.call("GET", f"{chat}/{cid}/run"))["running"]
                        if running is False:
                            break
                        if running is not True:
                            raise AdapterError("INVALID_RESPONSE", "unknown")
                        try:
                            await s.call("GET", f"{chat}/{cid}/events", stream=True)
                        except httpx.TransportError:
                            pass
                        await asyncio.sleep(0.2)
                    history = (await s.call("GET", f"{chat}/ls/{cid}"))["messages"]
                    users = [n for n, m in enumerate(history) if m["role"] == "user"]
                    submitted = [
                        "\n".join(p["text"] for p in history[n]["parts"] if p["type"] == "text")
                        for n in users
                    ]
                    if submitted != [t.message for t in case.turns[: i + 1]]:
                        raise AdapterError("DATAAGENT_SUBMISSION_UNCONFIRMED", "unknown")
                    latest = history[users[-1] + 1 :]
                    if not latest:
                        raise AdapterError("DATAAGENT_NO_FINAL_ANSWER", "sent")
                    final = latest[-1]
                    if (
                        final["role"] != "assistant"
                        or final.get("finish_reason") not in (None, "stop")
                        or any(p["type"] == "tool_call" for p in final["parts"])
                    ):
                        raise AdapterError("DATAAGENT_NO_FINAL_ANSWER", "sent")
                    answer = "\n".join(p["text"] for p in final["parts"] if p["type"] == "text")
                    if not answer.strip():
                        raise AdapterError("DATAAGENT_NO_FINAL_ANSWER", "sent")
                    final_attachments.update(
                        (i + 1, a["f_path"])
                        for a in final.get("attachments") or []
                        if a.get("delivery_status") == "verified"
                    )
                artifacts = await self.artifacts(s, cid, history, final_attachments)
                result = self.normalize_response(
                    {
                        "answer": answer,
                        "conversation_id": cid,
                        "turns": len(users),
                        "artifacts": artifacts,
                    }
                )
                result.latency_ms = int(1000 * (time.monotonic() - start))
                return result
        except AdapterError:
            raise
        except (httpx.HTTPError, TimeoutError) as exc:
            raise AdapterError(
                "DATAAGENT_DELIVERY_UNKNOWN" if sent else "NETWORK_ERROR",
                "unknown" if sent else "not_sent",
                not sent,
            ) from exc
        except (KeyError, ValueError, IndexError, TypeError, AttributeError):
            raise AdapterError("INVALID_RESPONSE", "unknown" if sent else "not_sent") from None

    async def artifacts(
        self, s: Session, cid: str, history: list[dict[str, Any]], attachments: set[tuple[int, str]]
    ) -> list[dict[str, Any]]:
        records = []
        turn = 0
        receipts = []
        for m in history:
            turn += int(m["role"] == "user")
            for p in m["parts"]:
                if p["type"] == "tool_result" and p.get("name") == "publish_artifact":
                    receipt = json.loads(p["content"])
                    if m["role"] == "tool" and receipt.get("status") == "verified":
                        receipts.append((turn, receipt))
        for n, r in receipts:
            path = r["f_path"]
            meta = await s.call(
                "GET",
                "/api/v1/chat/attachment/get",
                params={"conversation_id": cid, "f_path": path},
            )
            expected = PurePosixPath(path).stem.rsplit("_", 1)[-1]
            attached = (n, path) in attachments
            if meta["sha256"] != expected:
                raise AdapterError("DATAAGENT_ARTIFACT_DIGEST_MISMATCH", "sent")
            records.append(
                {
                    **meta,
                    "receipt_id": r["receipt_id"],
                    "ref": path,
                    "turn": n,
                    "attached": attached,
                    "verified": True,
                }
            )
        return records

    def normalize_response(self, raw: object) -> UnifiedResponse:
        if not isinstance(raw, dict):
            raise AdapterError("INVALID_RESPONSE", "sent")
        return UnifiedResponse(
            answer=raw["answer"],
            structured_output={"role": "assistant", "turns": raw["turns"], "status": "finished"},
            extensions={
                self.project: {
                    "conversation_id": raw["conversation_id"],
                    "artifacts": raw["artifacts"],
                    "execution": {"running": False},
                    "fixture_version": self.opts.fixture_version,
                }
            },
        )

    async def collect_trace(self, context: dict[str, object]) -> list[dict[str, object]]:
        return []
