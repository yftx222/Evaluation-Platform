"""Protocol regressions. The separate dataagent_live test drives the real deployment."""

import asyncio
import hashlib
import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from uuid import uuid4

import pytest
from pydantic import SecretStr
from test_adapter_protocols import sample

from app.application.access.identity import DevelopmentSecretResolver
from app.contracts.adapters import AdapterError, BaseAdapter
from app.contracts.http import AdapterConfig, TargetRule
from app.infrastructure.attempt_http import Wire, observe
from app.plugins.dataagent_profile import ProfiledDataAgentAdapter
from app.plugins.http_adapter import DestinationPolicy, registry


@pytest.fixture
def deployment(request: pytest.FixtureRequest) -> Iterator[tuple[BaseAdapter, dict[str, Any]]]:
    report = b"customer\nC04\n"
    digest = hashlib.sha256(report).hexdigest()
    ref = "outputs/cohort_" + digest + ".csv"
    state: dict[str, Any] = dict(histories={}, posts=0, resumes=0, admin=False, writes=False)

    class Endpoint(BaseHTTPRequestHandler):
        def log_message(self, *args: object) -> None:
            pass

        def reply(self, data: object) -> None:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(data if isinstance(data, bytes) else json.dumps(data).encode())

        def do_GET(self) -> None:
            assert "X-Trace-Write" not in self.headers
            if self.path == "/_eval/status":
                self.reply({"experience_writes": state["writes"]})
                return
            assert self.headers["Authorization"] == "Bearer test"
            if self.path == "/_eval/environment":
                self.reply(state["profiles"].pop(0))
            elif self.path.endswith("/run"):
                self.reply({"running": state["running"]})
            elif self.path.endswith("/events"):
                state["resumes"] += 1
                state["running"] = False
                self.reply(b'data: {"type":"done"}\n\n')
            elif "/ls/" in self.path:
                self.reply({"messages": state["histories"][self.path.split("/")[-1]]})
            elif "/attachment/get?" in self.path:
                self.reply(b"damaged" if state.get("damaged") else report)
            else:
                self.send_error(404)

        def do_POST(self) -> None:
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            if self.headers.get("X-Trace-Write"):
                state.setdefault("traced", []).append(
                    (self.path, self.headers["X-Eval-Turn"], self.headers["X-Trace-Write"])
                )
            if self.path == "/_eval/trace/finish":
                self.send_error(503)
                return
            if self.path.endswith("/auth/login"):
                assert json.loads(raw) == {"identifier": "reader", "password": "test"}
                self.reply(
                    dict(
                        access_token="test",
                        refresh_token="refresh",
                        expires_in=600,
                        user=dict(is_admin=state["admin"], is_active=True, doris_role="demo"),
                    )
                )
                return
            assert self.headers["Authorization"] == "Bearer test"
            if self.path.endswith("/create"):
                cid = str(uuid4())
                state["histories"][cid] = []
                self.reply({"conversation_id": cid})
            elif self.path.endswith("/upload"):
                assert b"sample.csv" in raw and b"fixture" in raw
                self.reply({"attachment": {"f_path": "uploads/sample.csv"}})
            elif self.path.endswith("/stream"):
                envelope = json.loads(raw)
                assert set(envelope) == {"conversation_id", "message"}
                history = state["histories"][envelope["conversation_id"]]
                first_turn = not history
                msg = envelope["message"]
                assert msg["attachments"] == ([] if history else [{"f_path": "uploads/sample.csv"}])
                history.append({"role": "user", **msg})
                history.append(
                    {
                        "role": "tool",
                        "parts": [
                            dict(
                                type="tool_result",
                                name="publish_artifact",
                                content=json.dumps(
                                    dict(status="verified", receipt_id="receipt", f_path=ref)
                                ),
                            )
                        ],
                    }
                )
                history.append(
                    {
                        "role": "assistant",
                        "parts": [{"type": "text", "text": "report"}],
                        "attachments": [{"f_path": ref, "delivery_status": "verified"}]
                        if first_turn
                        else [],
                    }
                )
                state["posts"] += 1
                state["running"] = True
                # Subscriber drops before the done frame. The server has accepted the message.
                self.reply(b"")
            else:
                self.send_error(404)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Endpoint)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    cfg = AdapterConfig(
        build=getattr(request, "param", "1"),
        kind="dataagent",
        endpoint=base + "/api/v1/chat/stream",
        probe_endpoint=base + "/_eval/status",
        options={"role": "demo", "fixture_version": "v1"},
        secret_ref="login",
        secret_version="v1",
    )
    policy = DestinationPolicy(
        [
            TargetRule(
                scheme="http", host="127.0.0.1", port=server.server_port, networks=["127.0.0.1/32"]
            )
        ],
        True,
    )
    resolver = DevelopmentSecretResolver(
        {
            "demo": {
                "login": {"v1": SecretStr(json.dumps({"identifier": "reader", "password": "test"}))}
            }
        }
    )
    try:
        yield registry.create(cfg, policy, resolver, "demo"), state
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_reconnect_and_isolate(deployment: tuple[BaseAdapter, dict[str, Any]]) -> None:
    adapter, state = deployment
    req = sample().model_copy(
        update={
            "input": {
                "turns": [{"message": "query"}, {"message": "report"}],
                "attachments": [{"filename": "sample.csv", "content_base64": "Zml4dHVyZQ=="}],
            }
        }
    )
    for _ in range(2):
        result = asyncio.run(
            adapter.execute(req.model_copy(update={"deadline_at": sample().deadline_at}))
        )
        info: Any = result.extensions["demo"]
        assert result.answer == "report" and len(info["artifacts"]) == 2
        assert all(a["verified"] and a["size"] > 0 for a in info["artifacts"])
        assert [a["attached"] for a in info["artifacts"]] == [True, False]
    assert len(state["histories"]) == 2 and state["posts"] == state["resumes"] == 4
    state["damaged"] = True
    with pytest.raises(AdapterError, match="DATAAGENT_ARTIFACT_DIGEST_MISMATCH"):
        asyncio.run(adapter.execute(req.model_copy(update={"deadline_at": sample().deadline_at})))


@pytest.mark.parametrize(
    ("flag", "code"),
    [("writes", "DATAAGENT_EXPERIENCE_WRITES_ENABLED"), ("admin", "DATAAGENT_PROFILE_DENIED")],
)
def test_refuse_unsafe_profile(
    deployment: tuple[BaseAdapter, dict[str, Any]], flag: str, code: str
) -> None:
    adapter, state = deployment
    state[flag] = True
    with pytest.raises(AdapterError, match=code):
        asyncio.run(adapter.validate_connection())
    assert not state["histories"]


@pytest.mark.parametrize("deployment", ["2"], indirect=True)
def test_trace_outage_preserves_answer(deployment: tuple[BaseAdapter, dict[str, Any]]) -> None:
    adapter, state = deployment

    async def before() -> None:
        pass

    async def renew() -> str:
        return "fresh"

    req = sample().model_copy(
        update={
            "input": {
                "turns": [{"message": "query"}],
                "attachments": [{"filename": "sample.csv", "content_base64": "Zml4dHVyZQ=="}],
            }
        }
    )
    wire = Wire(before, "test", trace_id=uuid4(), trace_token="original", renew_trace=renew)
    with observe(wire):
        result = asyncio.run(adapter.execute(req))
    assert result.answer == "report" and state["posts"] == 1
    assert state["traced"] == [
        ("/api/v1/chat/stream", "1", "original"),
        ("/_eval/trace/finish", "1", "fresh"),
    ]


def profile_response(prompt: str) -> dict[str, Any]:
    sha = "a" * 64
    manifest = {
        "schema_version": 1,
        "profile": "dataagent_eval_readonly",
        "git": "a" * 40,
        **dict.fromkeys(
            (
                "source",
                "evaluation_code",
                "dependency_lock",
                "config",
                "skills",
                "ontology",
                "metadata_yaml",
                "metadata_catalog",
            ),
            sha,
        ),
        "prompts": prompt,
        "indices": {"column": {"uuid": "index-a", "docs": 5, "shards_digest": sha}},
        "models": {
            r: {"route": "test", "model": "v1", "provider": "local"}
            for r in ("planner", "analyst", "reviewer", "explorer")
        },
        "embedding": {"model": "local", "dimensions": 8, "weights": None, "probe_digest": sha},
        "fixture": {"database": "demo", "version": "v1", "tables": 1, "rows": 3, "digest": sha},
        "identity": {"user": "reader", "role": "demo", "grants_digest": sha},
        "sandbox": {
            "image": "demo",
            "memory": "1g",
            "nano_cpus": 1,
            "pids": 4,
            "max_file_bytes": 4,
        },
        "experience_writes": False,
    }
    raw = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    return {"digest": hashlib.sha256(raw).hexdigest(), "manifest": manifest}


@pytest.mark.parametrize("deployment", ["3"], indirect=True)
@pytest.mark.parametrize("scenario", ["before", "after", "invalid-digest"])
def test_refuse_environment_drift(
    deployment: tuple[BaseAdapter, dict[str, Any]], scenario: str
) -> None:
    adapter, state = deployment
    assert isinstance(adapter, ProfiledDataAgentAdapter)
    frozen, changed = profile_response("a" * 64), profile_response("b" * 64)
    adapter.pinned_digest = frozen["digest"]
    if scenario == "invalid-digest":
        changed["digest"] = frozen["digest"]
    state["profiles"] = [frozen, changed] if scenario == "after" else [changed]
    req = sample().model_copy(
        update={
            "input": {
                "turns": [{"message": "hello"}],
                "attachments": [{"filename": "sample.csv", "content_base64": "Zml4dHVyZQ=="}],
            }
        }
    )
    with pytest.raises(AdapterError) as exc:
        asyncio.run(adapter.execute(req))
    assert exc.value.code == (
        "DATAAGENT_ENVIRONMENT_UNAVAILABLE"
        if scenario == "invalid-digest"
        else "DATAAGENT_ENVIRONMENT_CHANGED"
    )
    assert exc.value.delivery_state == ("sent" if scenario == "after" else "not_sent")
    assert len(state["histories"]) == int(scenario == "after")
