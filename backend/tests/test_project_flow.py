import asyncio
import hashlib
import json
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import uvicorn
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient
from playwright.sync_api import expect, sync_playwright
from pydantic import SecretStr

from app.api.main import create_app
from app.application.access.identity import DevelopmentIdentity, DevelopmentSecretResolver
from app.contracts.adapters import AdapterError, UnifiedRequest
from app.contracts.http import AdapterConfig, TargetRule
from app.infrastructure.resources import Settings
from app.plugins.http_adapter import DestinationPolicy, registry


class Target(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        pass  # avoid logging test headers and query data

    def do_GET(self) -> None:
        if self.path.startswith("/slow"):
            time.sleep(0.15)
        status = {"/denied": 401, "/redirect": 302}.get(self.path, 200)
        self.send_response(status)
        if status == 302:
            self.send_header("Location", "/ok")
        self.end_headers()
        try:
            self.wfile.write(b"invalid" if self.path == "/bad" else b'{"answer":"ok"}')
        except (BrokenPipeError, ConnectionResetError):
            return

    def do_POST(self) -> None:
        data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.send_response(200)
        self.end_headers()
        self.wfile.write(
            json.dumps(
                {
                    "answer": data["prompt"],
                    "payload": {
                        "body": data,
                        "language": self.headers.get("Accept-Language"),
                        "idempotency": self.headers.get("Idempotency-Key"),
                    },
                }
            ).encode()
        )


@pytest.fixture
def target() -> Iterator[tuple[str, TargetRule]]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Target)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield (
            f"http://localhost:{server.server_port}",
            TargetRule(
                scheme="http",
                host="localhost",
                port=server.server_port,
                networks=["127.0.0.0/8", "::1/128"],
            ),
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def local_target(target: tuple[str, TargetRule]) -> tuple[str, TargetRule]:
    url, rule = target
    return url.replace("localhost", "127.0.0.1"), rule.model_copy(update={"host": "127.0.0.1"})


@pytest.fixture
def environment(
    clean_db: str, services: Settings, target: tuple[str, TargetRule]
) -> tuple[Settings, str, str, str]:
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        env={**os.environ, "EVAL_DATABASE_URL": clean_db},
        check=True,
    )
    url, rule = local_target(target)
    admin, member = secrets.token_urlsafe(), secrets.token_urlsafe()
    users = [
        DevelopmentIdentity(subject=s, verifier=hashlib.sha256(t.encode()).hexdigest(), org_roles=r)
        for t, s, r in [(admin, "owner", ["Org"]), (member, "member", ["Cat"])]
    ]
    return (
        services.model_copy(
            update={
                "database_url": SecretStr(clean_db),
                "identities": users,
                "targets": {slug: [rule] for slug in ["shop", "rag", "browser"]},
            }
        ),
        admin,
        member,
        url,
    )


def request() -> UnifiedRequest:
    return UnifiedRequest(
        run_id=uuid4(),
        execution_id=uuid4(),
        attempt_id=uuid4(),
        case_key="001",
        input={"query": "hello"},
        deadline_at=datetime.now(UTC) + timedelta(seconds=10),
        idempotency_key="case-key",
    )


def test_two_adapters_and_errors(target: tuple[str, TargetRule]) -> None:
    url, rule = local_target(target)
    policy = DestinationPolicy([rule], True)
    resolver = DevelopmentSecretResolver({})
    cfg = AdapterConfig(
        endpoint=url + "/predict",
        probe_endpoint=url + "/ok",
        request_map={"prompt": "input.query"},
        headers={"Accept-Language": "zh-CN"},
        structured_path="payload",
    )
    for project, version in [
        ("shop", cfg),
        ("rag", cfg.model_copy(update={"method": "GET", "structured_path": None})),
    ]:
        adapter = registry.create(version, policy, resolver, project)
        result = asyncio.run(adapter.execute(request()))
        assert result.answer == ("hello" if project == "shop" else "ok")
        if project == "shop":
            assert result.structured_output == {
                "body": {"prompt": "hello"},
                "language": "zh-CN",
                "idempotency": "case-key",
            }
    for path, code in [
        ("/denied", "TARGET_AUTH_FAILED"),
        ("/bad", "INVALID_RESPONSE"),
        ("/redirect", "REDIRECT_DENIED"),
        ("/slow", "TARGET_TIMEOUT"),
    ]:
        version = cfg.model_copy(
            update={
                "endpoint": url + path,
                "method": "GET",
                "request_map": {},
                "timeout_s": 0.05 if path == "/slow" else 2,
            }
        )
        with pytest.raises(AdapterError) as caught:
            asyncio.run(registry.create(version, policy, resolver, "shop").execute(request()))
        assert caught.value.code == code


def test_dns_policy(target: tuple[str, TargetRule], monkeypatch: pytest.MonkeyPatch) -> None:
    url, rule = target
    policy = DestinationPolicy([rule], True)
    answers = ["127.0.0.1"]

    def resolve(*args: Any, **kwargs: Any) -> list[tuple[Any, ...]]:
        return [
            (
                socket.AF_INET6 if ":" in ip else socket.AF_INET,
                socket.SOCK_STREAM,
                6,
                "",
                (ip, rule.port),
            )
            for ip in answers
        ]

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    pinned, host, _ = asyncio.run(policy.approve(url + "/ok"))
    assert pinned.host == "127.0.0.1" and host == "localhost"
    answers.append("2001:db8::1")
    with pytest.raises(AdapterError, match="DESTINATION_DENIED"):
        asyncio.run(policy.approve(url + "/ok"))
    answers[:] = ["192.0.2.1"]
    with pytest.raises(AdapterError, match="DESTINATION_DENIED"):
        asyncio.run(policy.approve(url + "/ok"))


def test_project_lifecycle(environment: tuple[Settings, str, str, str]) -> None:
    settings, admin, member, url = environment
    with TestClient(create_app(settings)) as client:
        client.headers["Authorization"] = "Bearer " + admin
        p = "/api/v1/projects"
        projects = []
        for slug in ["shop", "rag"]:
            result = client.post(
                p,
                json=dict(
                    slug=slug,
                    name=slug,
                    kind="custom",
                    max_parallel=2,
                    target_max_parallel=1,
                    rpm=600,
                ),
            )
            assert result.status_code == 201, result.text
            path = p + "/" + result.json()["data"]["id"]
            projects.append(path)
            adapter = {
                "endpoint": url + "/predict",
                "probe_endpoint": url + "/ok",
                "secret_ref": "demo",
                "secret_version": "1",
            }
            settings.secrets[slug] = {"demo": {"1": SecretStr("local-test-value")}}
            published = client.post(
                path + "/versions",
                json={"label": "v1", "deployment_ref": "deploy1", "adapter": adapter},
            )
            assert published.status_code == 201, published.text
            version = published.json()["data"]["id"]
            probe = client.post(path + "/test-connection", json={"version_id": version})
            assert probe.status_code == 200 and probe.json()["data"]["ok"], probe.text
            settings.secrets[slug]["demo"]["2"] = SecretStr("replacement")
            del settings.secrets[slug]["demo"]["1"]
            time.sleep(60 / 600 + 0.02)  # next permitted call for this project
            assert (
                client.post(path + "/test-connection", json={"version_id": version}).json()["data"][
                    "code"
                ]
                == "SECRET_VERSION_UNAVAILABLE"
            )
        a, b = projects
        owner = client.get(a + "/members").json()["data"]["items"][0]["id"]
        assert client.delete(a + "/members/" + owner).status_code == 409
        joined = client.post(
            a + "/members", json={"issuer": "development", "subject": "member", "role": "A"}
        )
        assert joined.status_code == 201
        client.headers["Authorization"] = "Bearer " + member
        assert client.get(a + "/members").status_code == 200
        assert client.get(b).status_code == 404
        assert client.get(a + "/permissions").json()["data"]["role"] == "A"
        expiry = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
        made = client.post(
            a + "/api-keys",
            json={"name": "automation", "scopes": ["project:read"], "expires_at": expiry},
        )
        assert made.status_code == 201, made.text
        data = made.json()["data"]
        key_path = a + "/api-keys/" + data["key"]["id"]
        old = {"Authorization": "ApiKey " + data["secret"]}
        assert client.get(a, headers=old).status_code == 200
        assert client.get(b, headers=old).status_code == 403
        assert client.get(a + "/members", headers=old).status_code == 403
        rotated = client.post(key_path + "/rotate", json={"expires_at": expiry}).json()["data"]
        assert client.get(a, headers=old).status_code == 401
        new = {"Authorization": "ApiKey " + rotated["secret"]}
        assert client.get(a, headers=new).status_code == 200
        assert client.delete(a + "/api-keys/" + rotated["key"]["id"]).status_code == 204
        assert client.get(a, headers=new).status_code == 401
        short = client.post(
            a + "/api-keys",
            json={
                "name": "short",
                "scopes": ["results:read"],
                "expires_at": (datetime.now(UTC) + timedelta(seconds=1)).isoformat(),
            },
        ).json()["data"]
        scoped = {"Authorization": "ApiKey " + short["secret"]}
        assert client.get(a, headers=scoped).status_code == 403
        time.sleep(1.1)
        assert client.get(a, headers=scoped).status_code == 401
        assert "verifier" not in client.get(a + "/api-keys").text


def test_browser_project_release(environment: tuple[Settings, str, str, str]) -> None:
    settings, admin, _, url = environment
    app = create_app(settings)
    app.mount("/", StaticFiles(directory=Path("../frontend/dist"), html=True), name="console")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, log_level="warning", access_log=False))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
        thread.start()
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(channel="chrome", headless=True)
                page = browser.new_page()
                page.goto(f"http://127.0.0.1:{port}/")
                page.get_by_label("组织访问令牌").fill(admin)
                page.get_by_role("button", name="登录", exact=True).click()
                page.get_by_label("项目标识").fill("browser")
                page.get_by_label("项目名称").fill("浏览器验收")
                page.get_by_role("button", name="创建项目", exact=True).click()
                page.get_by_role("button", name="版本", exact=True).click()
                for label, value in [
                    ("版本标签", "v1"),
                    ("部署标识", "build1"),
                    ("业务地址", url + "/predict"),
                    ("只读探测地址", url + "/ok"),
                ]:
                    page.get_by_label(label).fill(value)
                page.get_by_role("button", name="发布版本").click()
                page.get_by_role("button", name="测试连接").click()
                expect(page.get_by_role("status").filter(has_text="连接成功")).to_be_visible()
                page.get_by_label("接入协议与构建").select_option("openai_compatible:1")
                page.get_by_label("版本标签").fill("v2")
                page.get_by_label("模型标识").fill("local-chat")
                page.get_by_role("button", name="发布版本").click()
                expect(
                    page.locator(".row").filter(has_text="openai_compatible / 1")
                ).to_be_visible()
                browser.close()
        finally:
            server.should_exit = True
            thread.join(8)
