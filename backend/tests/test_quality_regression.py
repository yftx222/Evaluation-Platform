import asyncio
import hashlib
import json
import secrets
import socket
import threading
import time
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from uuid import UUID

from fastapi.testclient import TestClient
from playwright.sync_api import expect, sync_playwright
from pydantic import SecretStr
from sqlalchemy import text
from test_batch_console import child_env, process, ready
from test_evaluation_runs import ROOT, create
from test_quality_workflows import SAMPLES

from app.api.main import create_app
from app.contracts.http import TargetRule
from app.contracts.quality import ServiceGrant
from app.infrastructure.resources import Resources, Settings
from app.plugins.evaluators import BUILD


def test_three_trials_and_delivery(catalog: tuple[Settings, dict[str, str]]) -> None:
    settings, tokens = catalog
    calls: list[str] = []
    received: list[dict[str, Any]] = []
    delivered = threading.Event()

    class Target(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if self.path == "/events":
                received.append(data)
                if len(received) == 1:
                    self.send_response(429)
                    self.send_header("Retry-After", "1")
                    self.end_headers()
                    return
                self.send_response(202)
                payload = {
                    "request_id": "test",
                    "data": {
                        "event_id": data["event_id"],
                        "status": "RECEIVED",
                        "received_at": datetime.now(UTC).isoformat(),
                    },
                }
                delivered.set()
            else:
                calls.append(data["query"])
                self.send_response(200)
                payload = {"answer": "yes" if len(calls) in (1, 3) else "no"}
            self.end_headers()
            self.wfile.write(json.dumps(payload).encode())

    with ThreadingHTTPServer(("127.0.0.1", 0), Target) as target:
        thread = threading.Thread(target=target.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{target.server_port}"
        gate = TargetRule(
            scheme="http", host="127.0.0.1", port=target.server_port, networks=["127.0.0.1/32"]
        )
        cfg = settings.model_copy(update={"targets": {"quality": [gate]}})
        try:
            with TestClient(
                create_app(cfg), headers={"Authorization": "Bearer " + tokens["curator"]}
            ) as owner:
                project = create(
                    owner,
                    "/projects",
                    slug="quality",
                    name="Quality",
                    kind="custom",
                    target_max_parallel=1,
                    max_parallel=2,
                    rpm=1000000,
                )["id"]
                pv = create(
                    owner,
                    f"/projects/{project}/versions",
                    label="candidate",
                    deployment_ref="v1",
                    trace_capabilities={"mode": "disabled"},
                    adapter={"endpoint": base + "/ask", "probe_endpoint": base + "/ready"},
                )["id"]
                dataset = create(owner, "/datasets", slug="benchmark", name="benchmark")
                path = "/datasets/" + dataset["id"]
                create(
                    owner,
                    path + "/cases",
                    draft_id=dataset["draft_id"],
                    case_key="unstable",
                    input={"query": "hello"},
                    expected={"answer": "yes"},
                )
                draft = owner.get(ROOT + path).json()["data"]["draft"]
                version = create(
                    owner,
                    path + "/versions",
                    action="publish",
                    draft_id=draft["id"],
                    reviewed_digest=draft["digest"],
                )
                evaluator = create(owner, "/evaluators", slug="match", name="match", kind="code")
                rule = create(
                    owner,
                    "/evaluators/" + evaluator["id"] + "/versions",
                    version="1",
                    impl_key="exact_match",
                    build=BUILD,
                    config={},
                    rubrics=[
                        dict(
                            rubric_key="match",
                            metric_key="answer",
                            description="match",
                            gate_class="BLOCKING",
                        )
                    ],
                )
                task = create(
                    owner,
                    "/evaluations/tasks",
                    name="3 trials",
                    project_version_id=pv,
                    dataset_version_id=version["id"],
                    evaluator_version_ids=[rule["id"]],
                )
            token = secrets.token_urlsafe()
            scope = ServiceGrant(
                subject="ci",
                verifier=hashlib.sha256(token.encode()).hexdigest(),
                project_id=UUID(project),
                environment="test",
                expires_at=datetime.now(UTC) + timedelta(hours=1),
                scopes={
                    "batch:read",
                    "batch:start",
                    "trace:write",
                    "shadow:submit",
                    "experiment:observe",
                },
                target_versions={UUID(pv)},
            )
            config = cfg.model_copy(
                update={
                    "quality_grants": [scope],
                    "isolated_quality_versions": {"test": {UUID(pv)}},
                    "production_trace_key_version": "joint",
                    "production_trace_keys": {"joint": SecretStr(secrets.token_hex(32))},
                }
            )
            env = child_env(config)
            env["EVAL_ISOLATED_QUALITY_VERSIONS"] = json.dumps({"test": [pv]})
            env["EVAL_QUALITY_RECEIVERS"] = json.dumps(
                {project: {"test": {"endpoint": base + "/events", "credential": "local-test"}}}
            )
            with TestClient(
                create_app(config), headers={"Authorization": "Bearer " + token}
            ) as client:
                body = {"task_id": task["id"], "project_version_id": pv, "environment": "test"}
                key = {"Idempotency-Key": "release-1"}

                async def interrupt_slot(enable: bool) -> None:
                    resources = Resources(config)
                    try:
                        async with resources.transaction() as db:
                            if enable:
                                await db.execute(
                                    text("""CREATE FUNCTION interrupt_trial() RETURNS trigger
                                    LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'test slot 2'
                                    USING ERRCODE='23514'; END $$""")
                                )
                                await db.execute(
                                    text("""CREATE TRIGGER interrupt_second BEFORE INSERT
                                    ON regression_trials FOR EACH ROW WHEN (NEW.trial=2)
                                    EXECUTE FUNCTION interrupt_trial()""")
                                )
                            else:
                                for table in (
                                    "regression_batches",
                                    "regression_trials",
                                    "evaluation_runs",
                                ):
                                    assert (
                                        await db.scalar(text(f"SELECT count(*) FROM {table}")) == 0
                                    )
                                await db.execute(
                                    text("DROP TRIGGER interrupt_second ON regression_trials")
                                )
                    finally:
                        await resources.close()

                asyncio.run(interrupt_slot(True))
                assert (
                    client.post(ROOT + "/regression-batches", headers=key, json=body).status_code
                    == 409
                )
                asyncio.run(interrupt_slot(False))
                response = client.post(ROOT + "/regression-batches", headers=key, json=body)
                assert response.status_code == 200, response.text
                saved = response.json()["data"]
                assert len({t["run_id"] for t in saved["trials"]}) == 3
                assert (
                    client.post(ROOT + "/regression-batches", headers=key, json=body).json()["data"]
                    == saved
                )
                with process(["-m", "app.worker.main"], env):
                    assert delivered.wait(60), calls
                    batch = ROOT + "/regression-batches/" + saved["regression_batch_id"]
                    report = client.get(batch + "/report").json()["data"]
                    detail = client.get(batch + "/case-results").json()["data"]
                    assert report["gate"] == "FAILED" and not report["release_allowed"]
                    assert sorted(detail["items"][0]["trial_results"]) == [
                        "FAILED",
                        "PASSED",
                        "PASSED",
                    ]
                    assert len(calls) == 3 and len(received) == 2 and received[0] == received[1]
                    owner_headers = {"Authorization": "Bearer " + tokens["curator"]}
                    prefix = ROOT + "/projects/" + project
                    run = saved["trials"][0]["run_id"]
                    execution = client.get(
                        ROOT + "/evaluations/runs/" + run + "/results", headers=owner_headers
                    ).json()["data"]["items"][0]
                    rescore_url = prefix + "/case-executions/" + execution["id"] + "/rescores"
                    rescored = client.post(
                        rescore_url,
                        headers={**owner_headers, "Idempotency-Key": "new-rule"},
                        json={"evaluator_version_id": rule["id"]},
                    )
                    assert rescored.status_code == 200, rescored.text
                    assert (
                        rescored.json()["data"]["result"]["score"]["status"] == "OK"
                        and len(calls) == 3
                    )
                    online_exercise(client, owner_headers, project, pv, task["id"], received, base)
                check_quality_browser(env, tokens["curator"], project)
        finally:
            target.shutdown()
            thread.join()


def check_quality_browser(env: dict[str, str], token: str, project: str) -> None:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    with process(
        [
            "-m",
            "uvicorn",
            "phase3_web:api",
            "--app-dir",
            "tests",
            "--port",
            str(port),
            "--no-access-log",
        ],
        env,
    ):
        ready(url)
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel="chrome", headless=True)
            try:
                page = browser.new_page()
                page.goto(url)
                page.get_by_label("组织访问令牌").fill(token)
                page.get_by_role("button", name="登录", exact=True).click()
                page.get_by_role("button", name="质量工程", exact=True).click()
                page.get_by_label("项目", exact=True).select_option(project)
                page.get_by_role("button", name="查看详情").first.click()
                expect(page.get_by_role("heading", name="门禁失败")).to_be_visible()
                page.get_by_role("button", name="生产轨迹", exact=True).click()
                page.get_by_role("button", name="查看详情").first.click()
                expect(page.get_by_role("heading", name="demo-run-1 的执行时间线")).to_be_visible()
                page.get_by_role("button", name="周期巡检", exact=True).click()
                expect(page.get_by_label("最低通过率（0 至 1）")).to_be_visible()
            finally:
                browser.close()


def online_exercise(
    client: TestClient,
    owner: dict[str, str],
    project: str,
    pv: str,
    task: str,
    received: list[dict[str, Any]],
    base: str,
) -> None:
    prefix = ROOT + "/projects/" + project
    spec = {"task_id": task, "environment": "test"}
    now = datetime.now(UTC)
    inspect_plan = client.post(
        prefix + "/inspection-plans",
        headers=owner,
        json={**spec, "starts_at": now.isoformat(), "period_s": 3600, "threshold": 1},
    )
    assert inspect_plan.status_code == 201, inspect_plan.text
    shadow = client.post(
        prefix + "/shadow-plans",
        headers=owner,
        json={**spec, "sample_rate": 1, "max_parallel": 1, "timeout_s": 60},
    )
    assert shadow.status_code == 201, shadow.text
    plan = shadow.json()["data"]["id"]
    # A/B setup needs a distinct frozen control version.
    control = client.post(
        prefix + "/versions",
        headers=owner,
        json={
            "label": "control",
            "deployment_ref": "base",
            "trace_capabilities": {"mode": "disabled"},
            "adapter": {"endpoint": base + "/ask", "probe_endpoint": base + "/ready"},
        },
    )
    assert control.status_code == 201, control.text
    exp = client.post(
        prefix + "/experiments",
        headers=owner,
        json={
            "environment": "test",
            "control_id": control.json()["data"]["id"],
            "candidate_id": pv,
            "ratio": 0.5,
            "starts_at": now.isoformat(),
            "ends_at": (now + timedelta(days=1)).isoformat(),
            "max_sessions": 1,
        },
    )
    assert exp.status_code == 201, exp.text
    eid = exp.json()["data"]["id"]
    exp_url = ROOT + "/experiments/" + eid
    conversation = {"conversation_id": "joint-conversation"}
    assigned = client.post(exp_url + "/assign", json=conversation).json()["data"]
    assert client.post(exp_url + "/assign", json=conversation).json()["data"] == assigned
    assert client.post(exp_url + "/assign", json={"conversation_id": "exceeds-cap"}).json()[
        "data"
    ] == {"assigned": False}
    fixture = json.loads((SAMPLES / "production-trace.json").read_text(encoding="utf8"))
    root = fixture["batch"]["trace"]
    root.update(
        **conversation,
        experiment_id=eid,
        variant_id=assigned["variant_id"],
        deployment_version=assigned["deployment_version"],
    )
    assert client.post(
        ROOT + "/production-trace-write/events:batch", json=fixture["batch"]
    ).is_success
    assert client.post(
        ROOT + "/production-trace-write/finalize", json=fixture["finalize"]
    ).is_success
    tid = root["trace_id"]
    sample = {"trace_id": tid, "input": {"query": "hello"}, "expected": {"answer": "yes"}}
    path = ROOT + "/shadow-plans/" + plan + "/samples"
    first = client.post(path, json=sample)
    assert first.is_success, first.text
    assert client.post(path, json=sample).json()["data"] == first.json()["data"]
    result = {
        **conversation,
        "variant_id": assigned["variant_id"],
        "deployment_version": assigned["deployment_version"],
        "trace_id": tid,
        "outcome": "failed",
        "guardrail_ref": "audit:protocol-v1",
    }
    assert client.post(exp_url + "/observe", json=result).is_success
    assert client.post(exp_url + "/observe", json=result).is_success
    assert client.patch(
        prefix + "/online/experiment/" + eid, headers=owner, json={"enabled": False}
    ).is_success
    assert client.post(exp_url + "/assign", json=conversation).json()["data"] == {"assigned": False}
    review = prefix + "/shadow-plans/" + plan + "/pairs/" + tid + "/review"
    deadline = time.monotonic() + 35
    while time.monotonic() < deadline:
        resp = client.post(
            review, headers=owner, json={"decision": "DIFFERENCE", "review_ref": "audit:paired"}
        )
        if resp.is_success:
            break
        assert resp.status_code == 409, resp.text
        time.sleep(0.1)
    else:
        raise AssertionError("Shadow result did not complete")
    while time.monotonic() < deadline:
        kinds = [e["event_type"] for e in received]
        if all(
            kinds.count(k) == 1 for k in ("INSPECTION_ANOMALY", "SHADOW_DIFFERENCE", "AB_GUARDRAIL")
        ):
            break
        time.sleep(0.1)
    else:
        raise AssertionError(kinds)
    listing = client.get(ROOT + "/datasets", headers=owner).json()["data"]["items"]
    assert not any(d["slug"].startswith("shadow-") for d in listing)
