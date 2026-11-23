"""Opt-in end-to-end report and telemetry measurements with a real API and Worker."""

import asyncio
import json
import os
import socket
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest
from phase3_load import distribution
from sqlalchemy import text
from test_batch_console import child_env, process, ready
from test_evaluation_runs import ROOT, prepare
from test_trace_write import configured

from app.infrastructure.resources import Resources, Settings


def process_sample(pids: list[int]) -> list[dict[str, Any]]:
    if sys.platform == "win32":
        ids = ",".join(map(str, pids))
        cmd = (
            f"$roots=@({ids});$all=Get-CimInstance Win32_Process;"
            "foreach($root in $roots){$ids=@($root)+@($all | Where-Object ParentProcessId -eq $root"
            " | ForEach-Object ProcessId);$p=Get-Process -Id $ids;"
            "[pscustomobject]@{cpu=($p.CPU|Measure-Object -Sum).Sum;"
            "rss=($p.WorkingSet64|Measure-Object -Sum).Sum}|ConvertTo-Json -Compress}"
        )
        out = subprocess.check_output(["powershell", "-NoProfile", "-Command", cmd], text=True)
        return [json.loads(line) for line in out.splitlines() if line.strip()]
    samples = []
    for pid in pids:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        samples.append(
            {
                "cpu": (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK"),
                "rss": int(fields[21]) * os.sysconf("SC_PAGE_SIZE"),
            }
        )
    return samples


def test_report_scale(
    catalog: tuple[Settings, dict[str, str]], request: pytest.FixtureRequest
) -> None:
    settings, tokens = catalog
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        base = f"http://127.0.0.1:{probe.getsockname()[1]}"
    client = httpx.Client(base_url=base, timeout=20, trust_env=False)
    batches: list[float] = []
    duplicates = 0
    conflicts = 0
    lock = threading.Lock()
    active = peak = 0

    class Agent(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            pass

        def do_POST(self) -> None:
            nonlocal duplicates, conflicts, active, peak
            query = json.loads(self.rfile.read(int(self.headers["Content-Length"])))["query"]
            auth = {"Authorization": "Bearer " + self.headers["X-Trace-Write"]}
            now = datetime.now(UTC).isoformat()
            types = ["AGENT_STEP", "RETRIEVAL", "TOOL_CALL", "MODEL_CALL", "FINAL_OUTPUT"]
            events = [
                {
                    "sequence": i + 1,
                    "event_key": str(i + 1),
                    "event_type": kind,
                    "name": kind,
                    "status": "SUCCEEDED",
                    "started_at": now,
                    "duration_ms": 1,
                    "parent_event_key": "1" if i else None,
                    "attributes": {"input_tokens": 2, "output_tokens": 1}
                    if kind == "MODEL_CALL"
                    else {},
                    "output": {"output_ref": "prediction.answer"} if kind == "FINAL_OUTPUT" else {},
                }
                for i, kind in enumerate(types)
            ]

            def send(payload: list[dict[str, Any]]) -> httpx.Response:
                nonlocal active, peak
                with lock:
                    active += 1
                    peak = max(peak, active)
                try:
                    start = time.perf_counter()
                    res = client.post(
                        ROOT + "/trace-write/events:batch", headers=auth, json={"events": payload}
                    )
                    with lock:
                        batches.append((time.perf_counter() - start) * 1000)
                    return res
                finally:
                    with lock:
                        active -= 1

            try:
                assert send(events).json()["data"]["accepted"] == 5
                if int(query) % 25 == 0:
                    count = send(events).json()["data"]["duplicate"]
                    assert count == 5
                    collision = send([dict(events[0], name="different")])
                    assert collision.status_code == 409
                    with lock:
                        duplicates += count
                        conflicts += 1
                res = client.post(
                    ROOT + "/trace-write/finalize",
                    headers=auth,
                    json={"outcome": "COMPLETE", "contiguous": True, "last_sequence": 5},
                )
                res.raise_for_status()
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps({"final_answer": query}).encode())
            except Exception:
                self.send_error(500)
                raise

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Agent)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    settings = configured(settings, srv.server_port)
    env = {
        **child_env(settings),
        "EVAL_POOL_SIZE": "16",
        "EVAL_WORKER_SLOTS": "8",
        "EVAL_WORKER_BATCH": "8",
        "EVAL_WORKER_POLL_S": "0.04",
    }

    async def size() -> int:
        res = Resources(settings)
        try:
            async with res.transaction() as db:
                return int(await db.scalar(text("SELECT pg_database_size(current_database())")))
        finally:
            await res.close()

    try:
        with process(
            [
                "-m",
                "uvicorn",
                "phase44_bench_api:api",
                "--app-dir",
                "tests",
                "--port",
                base.rsplit(":", 1)[1],
                "--log-level",
                "warning",
                "--no-access-log",
            ],
            env,
        ) as service:
            ready(base)
            with httpx.Client(
                base_url=base,
                headers={"Authorization": "Bearer " + tokens["curator"]},
                timeout=20,
                trust_env=False,
            ) as api:
                f = prepare(api)  # type: ignore[arg-type]
                pid = f["project"]["id"]
                api.patch(
                    ROOT + "/projects/" + pid,
                    json={"rpm": 1000000, "max_parallel": 8, "target_max_parallel": 8},
                ).raise_for_status()
                origin = f"http://127.0.0.1:{srv.server_port}"

                def post(path: str, **data: Any) -> dict[str, Any]:
                    response = api.post(ROOT + path, json=data)
                    assert response.status_code == 201, response.text
                    return dict(response.json()["data"])

                ver = post(
                    f"/projects/{pid}/versions",
                    label="report-load",
                    deployment_ref="fixed",
                    adapter={
                        "kind": "agent",
                        "endpoint": origin + "/ask",
                        "probe_endpoint": origin + "/ready",
                        "timeout_s": 20,
                    },
                    trace_capabilities={
                        "required_events": ["RETRIEVAL", "TOOL_CALL", "FINAL_OUTPUT"]
                    },
                )
                ev = post("/evaluators", kind="rule", slug="has-answer", name="回答非空")
                rule = post(
                    f"/evaluators/{ev['id']}/versions",
                    version="1",
                    impl_key="rules",
                    build=f["exact"]["build"],
                    config={"rules": [{"op": "nonempty", "path": "answer"}]},
                )
                env["EVAL_TRACE_TARGET_VERSIONS"] = json.dumps([ver["id"]])
                with process(["-m", "app.worker.main"], env) as worker:
                    for n in (100, 500, 1000):
                        ds = post("/datasets", slug=f"scale-{n}", name=f"{n} 条")
                        dp = "/datasets/" + ds["id"]
                        content = "\n".join(
                            json.dumps(
                                {
                                    "case_key": str(i),
                                    "input": {"query": str(i)},
                                    "category": str(i % 10),
                                    "expected": {"answer": str(i)},
                                }
                            )
                            for i in range(n)
                        )
                        api.post(
                            ROOT + "/datasets/import",
                            files={"file": ("cases.jsonl", content)},
                            data={
                                "format": "jsonl",
                                "dataset_id": ds["id"],
                                "draft_id": ds["draft_id"],
                            },
                        ).raise_for_status()
                        draft = api.get(ROOT + dp).json()["data"]["draft"]
                        version = post(
                            dp + "/versions",
                            action="publish",
                            draft_id=draft["id"],
                            reviewed_digest=draft["digest"],
                        )
                        task = post(
                            "/evaluations/tasks",
                            name="报告负载",
                            project_version_id=ver["id"],
                            dataset_version_id=version["id"],
                            evaluator_version_ids=[rule["id"]],
                            policy={"max_parallel": 8},
                        )
                        before = asyncio.run(size())
                        ins = len(api.get("/_acceptance/insert-timings").json())
                        start_batch = len(batches)
                        prev_dup, prev_conflict = duplicates, conflicts
                        peak = 0
                        samples = [process_sample([service.pid, worker.pid])]
                        start = time.perf_counter()
                        next_sample = start + 3
                        run = api.post(
                            ROOT + f"/evaluations/tasks/{task['id']}/runs",
                            headers={"Idempotency-Key": "scale"},
                        )
                        assert run.status_code == 202, run.text
                        rid = run.json()["data"]["id"]
                        while True:
                            status = api.get(ROOT + f"/evaluations/runs/{rid}").json()["data"][
                                "status"
                            ]
                            if status == "COMPLETED":
                                break
                            assert (
                                worker.poll() is None
                                and status in ("QUEUED", "RUNNING")
                                and time.perf_counter() - start < 600
                            ), status
                            if time.perf_counter() > next_sample:
                                samples.append(process_sample([service.pid, worker.pid]))
                                next_sample = time.perf_counter() + 5
                            time.sleep(0.2)
                        elapsed = time.perf_counter() - start
                        samples.append(process_sample([service.pid, worker.pid]))
                        report_ms = []
                        trace_ms = []
                        report_path = ROOT + f"/reports/runs/{rid}"
                        sample = api.get(
                            report_path + "/bad-cases", params={"signal": "all", "limit": 1}
                        ).json()["data"]["items"][0]
                        for _ in range(20):
                            at = time.perf_counter()
                            info = api.get(report_path).json()["data"]
                            report_ms.append((time.perf_counter() - at) * 1000)
                            assert (
                                info["summary"]["complete"] == n
                                and info["summary"]["total_tokens"] == n * 3
                            )
                            at = time.perf_counter()
                            page = api.get(
                                ROOT + f"/traces/{sample['trace_id']}/events",
                                params={"limit": 2, "summary_only": True},
                            )
                            trace_ms.append((time.perf_counter() - at) * 1000)
                            assert (
                                len(page.json()["data"]["items"]) == 2
                                and page.json()["data"]["next_sequence"] == 2
                            )
                        writes = api.get("/_acceptance/insert-timings").json()[ins:]
                        assert len(writes) == n and peak > 1
                        record = {
                            "cases": n,
                            "events": n * 5,
                            "elapsed_s": round(elapsed, 3),
                            "events_per_run_second": round(n * 5 / elapsed, 2),
                            "batch_ms": distribution(batches[start_batch:]),
                            "db_insert_ms": distribution(writes),
                            "trace_page_ms": distribution(trace_ms),
                            "report_ms": distribution(report_ms),
                            "peak_batch_concurrency": peak,
                            "duplicate_events": duplicates - prev_dup,
                            "conflicting_requests": conflicts - prev_conflict,
                            "db_growth_bytes": asyncio.run(size()) - before,
                            "resources": {
                                name: {
                                    "cpu_seconds": round(
                                        samples[-1][i]["cpu"] - samples[0][i]["cpu"], 2
                                    ),
                                    "peak_sampled_rss_mib": round(
                                        max(s[i]["rss"] for s in samples) / 1048576, 2
                                    ),
                                }
                                for i, name in enumerate(("api", "worker"))
                            },
                        }
                        request.node.user_properties.append((f"tier_{n}", json.dumps(record)))
                        print("REPORT_LOAD", json.dumps(record), flush=True)
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join()
        client.close()
