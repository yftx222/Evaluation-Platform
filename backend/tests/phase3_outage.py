"""Opt-in fault drill on the disposable Phase 3.4 Compose project."""

import asyncio
import json
import multiprocessing
import os
import re
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from test_batch_console import child_env, process
from test_batch_console import (
    test_batch_browser_and_api_restart as test_batch_browser_and_api_restart,
)
from test_evaluation_runs import ROOT, create, prepare
from test_worker_scheduler import test_worker_crash_and_fencing as test_worker_crash_and_fencing
from test_worker_scheduler import workload as workload

from app.api.main import create_app
from app.application.runner import runs
from app.contracts.evaluators import JudgeAccount
from app.contracts.http import TargetRule
from app.infrastructure.resources import Resources, Settings


def exit_before_commit(settings: Settings, task_id: str) -> None:
    async def insert() -> None:
        resources = Resources(settings)
        async with resources.transaction() as db:
            pending, created = await runs.start(db, UUID(task_id), "offline")
            assert created and pending["total_cases"] == 3
            os._exit(77)  # Simulate process death after the inserts, before commit.

    asyncio.run(insert())


def test_dependency_outages(catalog: tuple[Settings, dict[str, str]]) -> None:
    settings, tokens = catalog
    assert os.environ.get("COMPOSE_PROJECT_NAME") == "eval-phase34-check"
    address = make_url(settings.database_url.get_secret_value())
    name = address.database or ""
    assert address.host == "127.0.0.1" and re.fullmatch(r"acceptance_[0-9a-f]{32}", name)
    admin_url = address.set(database="postgres")

    async def connections(enable: bool) -> None:
        engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
        try:
            async with engine.connect() as db:
                state = "true" if enable else "false"
                await db.execute(text(f'ALTER DATABASE "{name}" ALLOW_CONNECTIONS {state}'))
                if not enable:
                    await db.execute(
                        text("""SELECT pg_terminate_backend(pid) FROM pg_stat_activity
                        WHERE datname=:name"""),
                        {"name": name},
                    )
        finally:
            await engine.dispose()

    received, release = threading.Event(), threading.Event()
    called = []

    class Target(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            pass

        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            called.append(body["query"])
            if len(called) == 1:
                received.set()
                release.wait(18)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps({"answer": body["query"]}).encode())

    target = ThreadingHTTPServer(("127.0.0.1", 0), Target)
    thread = threading.Thread(target=target.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{target.server_port}"
    gate = TargetRule(
        scheme="http", host="127.0.0.1", port=target.server_port, networks=["127.0.0.1/32"]
    )
    config = settings.model_copy(
        update={
            "targets": {"shop": [gate, gate.model_copy(update={"port": 12345})]},
            "judge_accounts": {
                "acct": JudgeAccount(
                    endpoint=base + "/judge",
                    destination=gate,
                    models=["model-a"],
                    rpm=60,
                    max_parallel=1,
                )
            },
        }
    )
    env = {**child_env(config), "EVAL_WORKER_LEASE_S": "2.1", "EVAL_WORKER_HEARTBEAT_S": "0.3"}
    redis = "eval-phase34-check-redis-1"
    try:
        with TestClient(
            create_app(config),
            raise_server_exceptions=False,
            headers={"Authorization": "Bearer " + tokens["curator"]},
        ) as api:
            f = prepare(api)
            assert api.patch(
                ROOT + "/projects/" + f["project"]["id"], json={"rpm": 1000000}
            ).is_success
            pv = create(
                api,
                f"/projects/{f['project']['id']}/versions",
                label="fault",
                deployment_ref="pinned",
                adapter={
                    "endpoint": base + "/ask",
                    "probe_endpoint": base + "/ready",
                    "timeout_s": 18,
                },
            )
            task = create(
                api,
                "/evaluations/tasks",
                name="依赖中断",
                project_version_id=pv["id"],
                dataset_version_id=f["dv"]["id"],
                evaluator_version_ids=[f["exact"]["id"]],
            )
            path = ROOT + f"/evaluations/tasks/{task['id']}/runs"
            child = multiprocessing.get_context("spawn").Process(
                target=exit_before_commit, args=(config, task["id"])
            )
            child.start()
            try:
                child.join(15)
                child_exit = child.exitcode
                assert child_exit == 77
            finally:
                if child.is_alive():
                    child.terminate()
                    child.join(5)
                child.close()
            assert api.get(path).json()["data"]["items"] == []
            with process(["-m", "app.worker.main"], env) as worker:
                subprocess.run(["docker", "stop", redis], check=True)
                try:
                    start = api.post(path, headers={"Idempotency-Key": "offline"})
                    assert start.status_code == 202, start.text
                    run = ROOT + "/evaluations/runs/" + start.json()["data"]["id"]
                    assert received.wait(10)
                    try:
                        asyncio.run(connections(False))
                        release.set()
                        denied = api.get(run)
                        assert denied.status_code >= 500
                        # Wait through one complete lease while PostgreSQL rejects new connections.
                        end = time.monotonic() + 2.4
                        while time.monotonic() < end:
                            assert worker.poll() is None
                            time.sleep(0.1)
                    finally:
                        asyncio.run(connections(True))
                    end = time.monotonic() + 18
                    while time.monotonic() < end:
                        info = api.get(run).json()["data"]
                        if info["status"] == "PARTIAL_FAILED":
                            break
                        time.sleep(0.1)
                    assert info["status"] == "PARTIAL_FAILED", (
                        info["status"],
                        info["failure_reasons"],
                        called,
                    )
                    assert info["succeeded_cases"] == 2 and info["unknown_delivery_cases"] == 1
                    assert info["running_cases"] == 0 and info["score_counts"]["OK"] == 2
                    assert len(set(called)) == len(called) == 3 and worker.poll() is None
                    again = api.post(path, headers={"Idempotency-Key": "offline"})
                    assert again.json()["data"]["id"] == start.json()["data"]["id"]
                    evidence = dict(
                        run_id=info["id"],
                        status=info["status"],
                        succeeded=info["succeeded_cases"],
                        unknown_delivery=info["unknown_delivery_cases"],
                        ok_scores=info["score_counts"]["OK"],
                        external_calls=len(called),
                        unique_calls=len(set(called)),
                        worker_exit_code=worker.poll(),
                        redis_offline_start_status=start.status_code,
                        disconnected_db_http_status=denied.status_code,
                        uncommitted_process_exit=child_exit,
                        replay_run_id=again.json()["data"]["id"],
                    )
                finally:
                    subprocess.run(["docker", "start", redis], check=True)
    finally:
        release.set()
        target.shutdown()
        target.server_close()
        thread.join()
    print("OUTAGE", json.dumps(evidence), flush=True)
    output = os.getenv("EVAL_OUTAGE_REPORT")
    if output:
        Path(output).write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf8")
