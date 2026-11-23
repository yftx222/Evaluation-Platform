"""Wire contract used by deployed adapters and a trusted custom mapping."""

import asyncio
import json
import logging
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.api.main import create_app
from app.application.access.identity import DevelopmentSecretResolver
from app.application.catalog.store import version_adapter_config
from app.contracts.adapters import AdapterError, Contract, UnifiedRequest, UnifiedResponse
from app.contracts.datasets import Case
from app.contracts.http import AdapterConfig, TargetRule
from app.infrastructure.resources import Resources, Settings
from app.plugins.adapter_protocols import Mapping
from app.plugins.http_adapter import AdapterRegistry, DestinationPolicy, Implementation, registry

EVENT = {
    "source_event_key": "step-1",
    "sequence": 1,
    "event_type": "RETRIEVAL",
    "occurred_at": "2026-01-01T00:00:00Z",
    "metadata": {"document_ids": ["doc-1"]},
}


class EchoMapping(Mapping):
    def request_body(self, request: UnifiedRequest) -> dict[str, object]:
        return {"ask": request.input["query"]}

    def normalize(self, raw: object) -> UnifiedResponse:
        if not isinstance(raw, dict):
            raise ValueError("object expected")
        return UnifiedResponse(
            answer=raw["echo"],
            extensions={self.project: raw["extensions"]},
            usage=raw["usage"],
            observed=raw["observed"],
        )


@pytest.fixture
def wire() -> Iterator[tuple[str, TargetRule, list[dict[str, Any]]]]:
    calls: list[dict[str, Any]] = []

    class Endpoint(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            pass

        def do_GET(self) -> None:
            self.send_response(200)
            self.end_headers()

        def do_POST(self) -> None:
            data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append(
                {
                    "path": self.path,
                    "body": data,
                    "key": self.headers.get("Idempotency-Key"),
                    "authenticated": self.headers.get("Authorization")
                    == "Bearer contract-test-sentinel",
                }
            )
            if self.path.endswith("/slow"):
                time.sleep(0.25)
            status = {
                "/wire/busy": 429,
                "/wire/down": 503,
                "/wire/auth": 403,
                "/wire/redirect": 302,
            }.get(self.path, 200)
            self.send_response(status)
            self.send_header("x-request-id", "request-header")
            self.end_headers()
            usage = {"input_tokens": 4, "output_tokens": 2}
            observed = {"model_id": "actual", "model_revision": "r7"}
            misc = {"extensions": {"color": "red"}, "unknown_private": "drop"}
            if self.path.endswith("/http"):
                result = {"answer": data["prompt"], "usage": usage, "observed": observed, **misc}
            elif self.path.endswith("/openai_compatible"):
                result = {
                    "choices": [
                        {
                            "message": {
                                "content": data["messages"][-1]["content"],
                                "parsed": {"ok": True},
                            }
                        }
                    ],
                    "model": "actual",
                    "model_revision": "r7",
                    "id": "provider-id",
                    "usage": {"prompt_tokens": 4, "completion_tokens": 2},
                    **misc,
                }
            elif self.path.endswith("/agent"):
                result = {
                    "final_answer": data["query"],
                    "structured_output": {"ok": True},
                    "usage": usage,
                    "events": [EVENT],
                    "observed": observed,
                    **misc,
                }
            elif self.path.endswith("/echo"):
                result = {"echo": data["ask"], "usage": usage, "observed": observed, **misc}
            else:
                result = {}
            try:
                self.wfile.write(
                    b"not-json" if self.path.endswith("/invalid") else json.dumps(result).encode()
                )
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Endpoint)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield (
            f"http://127.0.0.1:{server.server_port}/wire",
            TargetRule(
                scheme="http",
                host="127.0.0.1",
                port=server.server_port,
                prefix="/wire",
                networks=["127.0.0.1/32"],
            ),
            calls,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def config(url: str, kind: str) -> AdapterConfig:
    data: dict[str, Any] = dict(
        kind=kind,
        base_url=url,
        endpoint=kind,
        probe_endpoint="ready",
        secret_ref="service",
        secret_version="v1",
    )
    if kind == "http":
        data.update(
            request_map={"prompt": "input.query"},
            options=dict(
                usage_path="usage", observed_path="observed", extensions_path="extensions"
            ),
        )
    if kind == "openai_compatible":
        data["options"] = {"model": "fixed-model"}
    return AdapterConfig.model_validate(data)


def sample() -> UnifiedRequest:
    return UnifiedRequest(
        run_id=uuid4(),
        execution_id=uuid4(),
        attempt_id=uuid4(),
        case_key="sample",
        input={"query": "two", "context": {"order": 8}},
        parameters={"temperature": 0.2},
        deadline_at=datetime.now(UTC) + timedelta(seconds=5),
        idempotency_key="sample-attempt",
        extensions={"shop": {"locale": "en"}, "unrelated": {"private": "unshared"}},
    )


def secrets() -> DevelopmentSecretResolver:
    return DevelopmentSecretResolver(
        {"shop": {"service": {"v1": SecretStr("contract-test-sentinel")}}}
    )


@pytest.mark.parametrize("kind", ["http", "openai_compatible", "agent", "echo"])
def test_wire_contract(
    kind: str, wire: tuple[str, TargetRule, list[dict[str, Any]]], caplog: pytest.LogCaptureFixture
) -> None:
    url, rule, calls = wire
    plugins = registry
    if kind == "echo":
        plugins = AdapterRegistry()
        impl = Implementation(EchoMapping, Contract, ("query",), ("execute",), {})
        plugins.register("echo", "1", impl)
        with pytest.raises(ValueError):
            plugins.register("echo", "1", impl)
    cfg = config(url, kind)
    policy = DestinationPolicy([rule], True)
    adapter = plugins.create(cfg, policy, secrets(), "shop")
    caplog.set_level(logging.INFO)
    request = sample()
    assert asyncio.run(adapter.validate_connection()).ok
    result = asyncio.run(adapter.execute(request))
    assert result.answer == "two" and result.usage == {"input_tokens": 4, "output_tokens": 2}
    assert result.observed == {
        "model_id": "actual",
        "model_revision": "r7",
        "request_id": "request-header",
    }
    assert result.extensions["shop"] == {"color": "red"}
    assert (
        len(calls) == 1 and calls[0]["authenticated"] and calls[0]["key"] == request.idempotency_key
    )
    assert (
        "unshared" not in json.dumps(calls[0]["body"])
        and "unknown_private" not in result.model_dump_json()
    )
    if kind == "openai_compatible":
        assert calls[0]["body"] == dict(
            model="fixed-model", temperature=0.2, messages=[dict(role="user", content="two")]
        )
        assert result.extensions["_chat_completion_id"] == "provider-id"
        assert result.structured_output == {"ok": True}
    if kind == "agent":
        assert calls[0]["body"]["extensions"] == {"shop": {"locale": "en"}}
        assert calls[0]["body"]["context"] == {"order": 8}
    events = asyncio.run(adapter.collect_trace(result.model_dump()))
    assert [e["event_type"] for e in events] == (["RETRIEVAL"] if kind == "agent" else [])
    for suffix, code, state, retry in [
        ("busy", "TARGET_RATE_LIMIT", "sent", True),
        ("down", "TARGET_HTTP_ERROR", "sent", True),
        ("auth", "TARGET_AUTH_FAILED", "sent", False),
        ("redirect", "REDIRECT_DENIED", "sent", False),
        ("invalid", "INVALID_RESPONSE", "sent", False),
        ("empty", "INVALID_RESPONSE", "sent", False),
        ("slow", "TARGET_TIMEOUT", "unknown", True),
    ]:
        changed = cfg.model_copy(
            update={"endpoint": suffix, "timeout_s": 0.08 if suffix == "slow" else 3}
        )
        before = len(calls)
        with pytest.raises(AdapterError) as err:
            asyncio.run(plugins.create(changed, policy, secrets(), "shop").execute(sample()))
        assert (err.value.code, err.value.delivery_state, err.value.retryable) == (
            code,
            state,
            retry,
        )
        assert len(calls) == before + 1
    before = len(calls)
    for candidate, gate, resolver, expected in [
        (cfg, DestinationPolicy([], True), secrets(), "DESTINATION_DENIED"),
        (cfg, policy, DevelopmentSecretResolver({}), "SECRET_VERSION_UNAVAILABLE"),
        (cfg.model_copy(update={"build": "missing"}), policy, secrets(), "VERSION_UNAVAILABLE"),
    ]:
        with pytest.raises(AdapterError) as err:
            asyncio.run(plugins.create(candidate, gate, resolver, "shop").execute(sample()))
        assert err.value.code == expected and err.value.delivery_state == "not_sent"
    assert len(calls) == before and "contract-test-sentinel" not in caplog.text


def test_chat_messages_and_agent_trace(wire: tuple[str, TargetRule, list[dict[str, Any]]]) -> None:
    url, rule, calls = wire
    policy = DestinationPolicy([rule], True)
    chat = registry.create(config(url, "openai_compatible"), policy, secrets(), "shop")
    messages = [
        {"role": "system", "content": "answer"},
        {"role": "user", "content": "existing history"},
    ]
    req = sample().model_copy(update={"input": {"query": "fallback", "messages": messages}})
    assert asyncio.run(chat.execute(req)).answer == "existing history"
    assert calls[-1]["body"]["messages"] == messages
    result = chat.normalize_response(
        {"choices": [{"message": {"parsed": {"n": 1}, "content": None}}]}
    )
    assert result.answer is None and result.structured_output == {"n": 1} and not result.usage
    for bad in [
        {"choices": []},
        {"choices": [{"message": {}}]},
        {"choices": [{"message": {"content": 3}}]},
    ]:
        with pytest.raises(AdapterError, match="INVALID_RESPONSE"):
            chat.normalize_response(bad)
    with pytest.raises(AdapterError, match="INVALID_ADAPTER_CONFIG"):
        registry.create(
            config(url, "openai_compatible").model_copy(update={"options": {}}),
            policy,
            secrets(),
            "shop",
        )
    with pytest.raises(AdapterError, match="INVALID_REQUEST"):
        asyncio.run(chat.execute(req.model_copy(update={"parameters": {"stream": True}})))
    assert len(calls) == 1
    agent = registry.create(config(url, "agent"), policy, secrets(), "shop")
    retained = agent.normalize_response({"final_answer": "retained", "events": [EVENT, EVENT]})
    assert retained.answer == "retained"
    with pytest.raises(AdapterError, match="INVALID_TRACE"):
        asyncio.run(agent.collect_trace(retained.model_dump()))
    assert (
        asyncio.run(
            agent.collect_trace(agent.normalize_response({"structured_output": {}}).model_dump())
        )
        == []
    )


def test_published_protocols_share_dataset(
    catalog: tuple[Settings, dict[str, str]], wire: tuple[str, TargetRule, list[dict[str, Any]]]
) -> None:
    settings, tokens = catalog
    url, rule, calls = wire
    settings = settings.model_copy(
        update={
            "targets": {k: [rule] for k in ("shop", "rag", "assistant")},
            "secrets": {
                k: {"service": {"v1": SecretStr("contract-test-sentinel")}}
                for k in ("shop", "rag", "assistant")
            },
        }
    )
    with TestClient(
        create_app(settings), headers={"Authorization": "Bearer " + tokens["curator"]}
    ) as client:

        def post(path: str, body: dict[str, Any]) -> dict[str, Any]:
            r = client.post("/api/v1/" + path, json=body)
            assert r.is_success, r.text
            return dict(r.json()["data"])

        d = post("datasets", dict(name="共享", slug="shared"))
        path = "datasets/" + d["id"]
        case = post(
            path + "/cases",
            dict(
                draft_id=d["draft_id"],
                case_key="shared_01",
                input={"query": "same"},
                expected={"answer": "PRIVATE_REFERENCE"},
            ),
        )
        draft = client.get("/api/v1/" + path).json()["data"]["draft"]
        version = post(
            path + "/versions",
            dict(action="publish", draft_id=draft["id"], reviewed_digest=draft["digest"]),
        )
        assert version["state"] == "PUBLISHED"
        impls = client.get("/api/v1/evaluators/implementations").json()["data"]
        exact = next(plugin for plugin in impls if plugin["impl_key"] == "exact_match")
        scorer = post("evaluators", {"name": "对比参考答案", "slug": "reference", "kind": "code"})
        score_path = "evaluators/" + scorer["id"]
        frozen_score = post(
            score_path + "/versions",
            {"version": "v1", "impl_key": exact["impl_key"], "build": exact["build"], "config": {}},
        )
        assert len(frozen_score["digest"]) == 64
        for slug, kind in [("shop", "http"), ("rag", "openai_compatible"), ("assistant", "agent")]:
            p = post(
                "projects",
                dict(
                    name=slug,
                    slug=slug,
                    kind="custom",
                    max_parallel=1,
                    target_max_parallel=1,
                    rpm=60,
                ),
            )
            pp = "projects/" + p["id"]
            cfg = config(url, kind)
            v = post(
                pp + "/versions",
                dict(label="v1", deployment_ref="deploy-a", adapter=cfg.model_dump()),
            )
            assert v["kind"] == kind and v["build"] == "1" and len(v["adapter_digest"]) == 64
            assert post(pp + "/test-connection", {"version_id": v["id"]})["ok"]
            item = client.get(
                "/api/v1/" + path + "/cases", params={"version_id": version["id"]}
            ).json()["data"]["items"][0]
            assert item["id"] == case["id"] and item["dataset_version_id"] == version["id"]

            async def execute(
                project: str, project_id: str, version_id: str, case_data: dict[str, Any]
            ) -> UnifiedResponse:
                resources = Resources(settings)
                try:
                    async with resources.transaction() as db:
                        frozen = await version_adapter_config(
                            db, UUID(project_id), UUID(version_id)
                        )
                    adapter = registry.create(
                        frozen,
                        DestinationPolicy(settings.targets[project], True),
                        DevelopmentSecretResolver(settings.secrets),
                        project,
                    )
                    return await adapter.execute(
                        sample().model_copy(
                            update={"input": case_data["input"], "case_key": case_data["case_key"]}
                        )
                    )
                finally:
                    await resources.close()

            result = asyncio.run(execute(slug, p["id"], v["id"], item))
            assert result.answer == "same" and slug in result.extensions
            graded = post(
                score_path + "/test",
                {
                    "version_id": frozen_score["id"],
                    "case": {k: item[k] for k in Case.model_fields},
                    "prediction": result.model_dump(mode="json"),
                },
            )
            # The target said "same"; it never received the private reference answer.
            assert graded["evaluator_version_id"] == frozen_score["id"]
            assert graded["status"] == "OK" and graded["score"] == 0 and graded["passed"] is False
        assert len(calls) == 3 and all(
            "PRIVATE_REFERENCE" not in json.dumps(c["body"]) for c in calls
        )
