import json
import os
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx
from fastapi.testclient import TestClient
from playwright.sync_api import expect, sync_playwright
from redis import Redis
from test_evaluation_runs import ROOT, create, prepare

from app.api.main import create_app
from app.contracts.evaluators import JudgeAccount
from app.contracts.http import TargetRule
from app.infrastructure.resources import Settings


def child_env(config: Settings) -> dict[str, str]:
    return {
        **os.environ,
        "EVAL_DATABASE_URL": config.database_url.get_secret_value(),
        "EVAL_REDIS_URL": config.redis_url.get_secret_value(),
        "EVAL_IDENTITIES": json.dumps([i.model_dump() for i in config.identities]),
        "EVAL_TARGETS": json.dumps(
            {k: [g.model_dump() for g in v] for k, v in config.targets.items()}
        ),
        "EVAL_JUDGE_ACCOUNTS": json.dumps(
            {k: v.model_dump() for k, v in config.judge_accounts.items()}
        ),
        "EVAL_WORKER_POLL_S": "0.08",
    }


@contextmanager
def process(args: list[str], env: dict[str, str]) -> Iterator[subprocess.Popen[bytes]]:
    child = subprocess.Popen([sys.executable, *args], env=env, stdout=subprocess.DEVNULL)
    try:
        yield child
    finally:
        if child.poll() is None:
            child.terminate()
        try:
            child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=5)
            raise


def ready(url: str) -> None:
    end = time.monotonic() + 12
    while time.monotonic() < end:
        try:
            if httpx.get(url + "/ready", timeout=1, trust_env=False).status_code == 200:
                return
        except httpx.TransportError:
            pass
        time.sleep(0.05)
    raise AssertionError("API 未就绪")


def test_batch_browser_and_api_restart(catalog: tuple[Settings, dict[str, str]]) -> None:
    settings, tokens = catalog
    arrived, proceed = threading.Event(), threading.Event()

    class Target(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            pass

        def do_POST(self) -> None:
            data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            arrived.set()
            proceed.wait(16)
            self.send_response(200)
            self.end_headers()
            stamp = datetime.now(UTC).isoformat()
            events = [
                {
                    "event_key": "step",
                    "sequence": 1,
                    "name": "选择资料",
                    "event_type": "AGENT_STEP",
                    "status": "SUCCEEDED",
                    "started_at": stamp,
                },
                {
                    "event_key": "lookup",
                    "sequence": 2,
                    "parent_event_key": "step",
                    "name": "查找文档",
                    "event_type": "RETRIEVAL",
                    "status": "SUCCEEDED",
                    "started_at": stamp,
                    "output": {
                        "document_refs": ["public-42"],
                        "scores": [0.88],
                        "api_key": "mask-me",
                    },
                },
                {
                    "event_key": "final",
                    "sequence": 3,
                    "parent_event_key": "step",
                    "name": "完成回答",
                    "event_type": "FINAL_OUTPUT",
                    "status": "SUCCEEDED",
                    "started_at": stamp,
                    "output": {"output_ref": "prediction.answer"},
                },
            ]
            wire = {
                "answer": data["query"],
                "observations": {
                    "trace": {
                        "events": events,
                        "finalize": {"outcome": "COMPLETE", "contiguous": True, "last_sequence": 3},
                    }
                },
            }
            self.wfile.write(json.dumps(wire).encode())

    target = ThreadingHTTPServer(("127.0.0.1", 0), Target)
    th = threading.Thread(target=target.serve_forever, daemon=True)
    th.start()
    gate = TargetRule(
        scheme="http", host="127.0.0.1", port=target.server_port, networks=["127.0.0.1/32"]
    )
    local = f"http://127.0.0.1:{target.server_port}"
    acct = JudgeAccount(
        endpoint=local + "/judge", destination=gate, models=["model-a"], rpm=120, max_parallel=1
    )
    config = settings.model_copy(
        update={
            "targets": {"shop": [gate, gate.model_copy(update={"port": 12345})]},
            "judge_accounts": {"acct": acct},
        }
    )
    headers = {"Authorization": "Bearer " + tokens["curator"]}
    try:
        with TestClient(create_app(config), headers=headers) as api:
            f = prepare(api)
            project = f["project"]["id"]
            assert api.patch(ROOT + "/projects/" + project, json={"rpm": 1000000}).is_success
            ver = create(
                api,
                "/projects/" + project + "/versions",
                label="console-v1",
                deployment_ref="fixed",
                trace_capabilities={"mode": "embedded", "required_events": ["FINAL_OUTPUT"]},
                adapter={
                    "endpoint": local + "/ask",
                    "options": {"extensions_path": "observations"},
                    "probe_endpoint": local + "/ready",
                    "timeout_s": 20,
                },
            )
            ds = create(api, "/datasets", slug="console", name="控制台验收题库")
            path = "/datasets/" + ds["id"]
            content = "\n".join(
                json.dumps(
                    {
                        "case_key": f"sample-{i}",
                        "input": {"query": str(i), "secret": "mask-me"},
                        "expected": {"answer": str(i)},
                    }
                )
                for i in range(22)
            )
            assert api.post(
                ROOT + "/datasets/import",
                files={"file": ("batch.jsonl", content)},
                data={"dataset_id": ds["id"], "draft_id": ds["draft_id"], "format": "jsonl"},
            ).is_success
            d = api.get(ROOT + path).json()["data"]["draft"]
            dv = create(
                api,
                path + "/versions",
                action="publish",
                draft_id=ds["draft_id"],
                reviewed_digest=d["digest"],
            )
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        url = f"http://127.0.0.1:{port}"
        args = [
            "-m",
            "uvicorn",
            "phase3_web:api",
            "--app-dir",
            "tests",
            "--port",
            str(port),
            "--no-access-log",
        ]
        env = child_env(config)
        with process(["-m", "app.worker.main"], env) as worker, sync_playwright() as pw:
            browser = pw.chromium.launch(channel="chrome", headless=True)
            page = browser.new_page()
            # Abort the browser's real streaming fetch at a known boundary. Keep the
            # React component mounted so its real Last-Event-ID reconnect is exercised.
            page.add_init_script("""const fetchOriginal = window.fetch.bind(window);
              window.fetch = (url, opts) => {
                if (String(url).endsWith('/progress')) {
                  const cut = new AbortController();
                  window.disconnectProgress = () => cut.abort();
                  opts = {...opts, signal: AbortSignal.any([opts.signal, cut.signal])};
                }
                return fetchOriginal(url, opts);
              };""")
            with process(args, env) as first:
                ready(url)
                page.goto(url)
                page.get_by_label("组织访问令牌").fill(tokens["curator"])
                page.get_by_role("button", name="登录", exact=True).click()
                page.get_by_role("button", name="批量评测").click()
                page.get_by_role("button", name="新建评测任务").click()
                page.get_by_label("任务名称").fill("控制台批量任务")
                page.get_by_label("项目版本", exact=True).select_option(ver["id"])
                page.get_by_label("数据集", exact=True).select_option(ds["id"])
                page.get_by_label("数据集发布版本").select_option(dv["id"])
                page.get_by_label("评分器目录").select_option(f["exact"]["evaluator_id"])
                page.get_by_role("checkbox", name="v1").check()
                page.get_by_role("button", name="预览评分计划").click()
                expect(page.get_by_text("评分依赖与权重")).to_be_visible()
                page.get_by_role("button", name="确认创建").click()
                with page.expect_response(
                    lambda res: res.request.method == "POST" and res.url.endswith("/runs")
                ) as started:
                    page.get_by_role("button", name="启动运行").click()
                run = started.value.json()["data"]
                request_key = started.value.request.all_headers()["idempotency-key"]
                runpath = ROOT + "/evaluations/runs/" + run["id"]
                assert arrived.wait(8)
                with httpx.stream(
                    "GET", url + runpath + "/progress", headers=headers, timeout=5
                ) as stream:
                    assert stream.status_code == 200
                    first_event = next(
                        json.loads(line[6:])
                        for line in stream.iter_lines()
                        if line.startswith("data: ")
                    )
                    assert first_event["total"] == 22 and "secret" not in first_event
                expect(page.get_by_text("实时连接", exact=True)).to_be_visible()
                page.context.set_offline(True)
                page.evaluate("window.disconnectProgress()")
                channel = "evaluations:progress:" + run["id"]
                with Redis.from_url(config.redis_url.get_secret_value()) as redis:
                    until = time.monotonic() + 5
                    while redis.pubsub_numsub(channel)[0][1] != 0:
                        assert time.monotonic() < until, "SSE subscriber survived disconnect"
                        time.sleep(0.025)
                old_pid = first.pid
            with process(args, env) as restarted:
                ready(url)
                assert restarted.pid != old_pid
                with page.expect_request(
                    lambda req: (
                        req.url.endswith("/progress") and bool(req.headers.get("last-event-id"))
                    )
                ):
                    page.context.set_offline(False)
                with httpx.Client(base_url=url, headers=headers, timeout=20) as client:
                    assert (
                        client.get(runpath).json()["data"]["snapshot_digest"]
                        == run["snapshot_digest"]
                    )
                    assert client.get(
                        runpath + "/progress", headers={"Authorization": "Bearer " + tokens["rag"]}
                    ).status_code in (403, 404)
                    with client.stream(
                        "GET",
                        runpath + "/progress",
                        headers={"Last-Event-ID": first_event["event_id"]},
                    ) as events:
                        received = set()
                        for line in events.iter_lines():
                            if line.startswith("event: "):
                                received.add(line[7:])
                            if line.startswith("data: "):
                                data = json.loads(line[6:])
                                if not proceed.is_set():
                                    assert data["resync"] and data["run_id"] == run["id"]
                                    proceed.set()
                                if "run.finished" in received:
                                    assert data["succeeded"] == 22
                                    break
                        assert received >= {
                            "run.status",
                            "run.progress",
                            "case.completed",
                            "run.finished",
                        }
                    expect(page.get_by_text("已收敛", exact=True)).to_be_visible(timeout=15000)
                    page.get_by_role("button", name="下一页", exact=True).click()
                    expect(page.get_by_text("第 2 页", exact=True)).to_be_visible()
                    page.get_by_role("button", name="sample-").first.click()
                    expect(page.get_by_text("原始输入（已脱敏）")).to_be_visible()
                    expect(page.locator("article")).not_to_contain_text("mask-me")
                    detail_requests = []
                    page.on(
                        "request",
                        lambda req: (
                            detail_requests.append(req.url) if "/events/" in req.url else None
                        ),
                    )
                    page.get_by_role("button", name="查看时间线").click()
                    timeline = page.get_by_role("region", name="轨迹时间线")
                    expect(timeline.get_by_text("#2 检索 · 查找文档")).to_be_visible()
                    assert detail_requests == []
                    timeline.get_by_role("button", name="按父子关系查看").click()
                    timeline.get_by_role("button", name="子事件").first.click()
                    child = (
                        timeline.get_by_role("listitem")
                        .filter(has=page.get_by_text("#2 检索 · 查找文档", exact=True))
                        .last
                    )
                    child.get_by_role("button", name="事件详情").click()
                    expect(child).to_contain_text("public-42")
                    expect(child).not_to_contain_text("mask-me")
                    assert len(detail_requests) == 1
                    page.get_by_role("button", name="查看评分证据").click()
                    citations = page.get_by_role("region", name="评分证据")
                    expect(citations.get_by_text("/answer", exact=True)).to_be_visible()
                    citations.get_by_role("button", name="定位轨迹事件").click()
                    linked = timeline.get_by_role("region", name="证据对应事件")
                    expect(linked).to_contain_text("#3 完成回答")
                    expect(linked).to_contain_text("prediction.answer")
                    page.get_by_role("button", name="查看评测报告").click()
                    report = page.get_by_role("region", name="评测报告", exact=True)
                    expect(report.get_by_text("评分器表现", exact=True)).to_be_visible()
                    expect(report.get_by_text("采集方式 响应内嵌", exact=False)).to_contain_text(
                        "22/22"
                    )
                    report.get_by_label("分组维度").select_option("tag")
                    report.get_by_label("筛选条件").select_option("all")
                    current_page = page.get_by_role("table").filter(
                        has=page.get_by_role("columnheader", name="尝试次数", exact=True)
                    )
                    visible_cases = set(current_page.get_by_role("button").all_inner_texts())
                    candidates = report.get_by_role("button", name="sample-")
                    expect(candidates.first).to_be_visible()
                    candidate_name = next(
                        k for k in candidates.all_inner_texts() if k not in visible_cases
                    )
                    report.get_by_role("button", name=candidate_name, exact=True).click()
                    expect(
                        page.get_by_role("heading", name="样本详情 · " + candidate_name, exact=True)
                    ).to_be_visible()
                    expect(page.locator("article")).to_be_focused()
                    with page.expect_download() as downloaded:
                        report.get_by_role("button", name="导出报告 CSV").click()
                    assert downloaded.value.suggested_filename.endswith(".csv")
                    tp = ROOT + "/evaluations/tasks/" + run["task_id"]
                    page.get_by_role("button", name="归档任务").click()
                    expect(page.get_by_text("已归档", exact=True).last).to_be_visible()
                    assert client.delete(tp).status_code == 204
                    original = client.post(tp + "/runs", headers={"Idempotency-Key": request_key})
                    assert original.status_code == 202
                    assert original.json()["data"]["id"] == run["id"]
                    assert (
                        client.post(tp + "/runs", headers={"Idempotency-Key": "new"}).status_code
                        == 409
                    )
                    assert client.patch(tp, json={"description": "x"}).status_code == 409
                    assert client.get(runpath).json()["data"]["score_counts"]["OK"] == 22
                    with TestClient(create_app(config), headers=headers) as setup:
                        for mode in ("disabled", "callback"):
                            pv = create(
                                setup,
                                f"/projects/{project}/versions",
                                label=mode,
                                deployment_ref="fixed",
                                adapter={
                                    "endpoint": local + "/ask",
                                    "probe_endpoint": local + "/ready",
                                },
                                trace_capabilities={"mode": mode},
                            )
                            create(
                                setup,
                                "/evaluations/tasks",
                                name=mode,
                                project_version_id=pv["id"],
                                dataset_version_id=f["dv"]["id"],
                                evaluator_version_ids=[f["exact"]["id"]],
                            )
                    page.get_by_label("任务状态").select_option("false")
                    for mode, label, counts, fraction in [
                        ("disabled", "关闭", "缺失 0 · 未采集 3", "不适用（0/0）"),
                        ("callback", "回调", "缺失 3 · 未采集 0", "0/3"),
                    ]:
                        page.get_by_role("button", name=mode, exact=True).click()
                        page.get_by_role("button", name="启动运行").click()
                        page.get_by_role("button", name="查看评测报告").click(timeout=20000)
                        coverage = (
                            page.get_by_role("region", name="评测报告", exact=True)
                            .locator("p")
                            .filter(has_text="采集方式 " + label)
                        )
                        expect(coverage).to_contain_text(counts)
                        expect(coverage).to_contain_text(fraction)
            browser.close()
        if os.name == "posix":
            assert worker.returncode == 0, "Worker failed to shut down cleanly on SIGTERM"
    finally:
        proceed.set()
        target.shutdown()
        target.server_close()
        th.join()
