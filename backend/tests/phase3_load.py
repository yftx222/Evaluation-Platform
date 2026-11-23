"""Opt-in Phase 3 acceptance, run explicitly against the disposable Compose test services."""

import asyncio
import json
import math
import os
import subprocess
import sys
import threading
import time
from collections import Counter
from collections.abc import Sequence
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy import event, text
from test_evaluation_runs import ROOT, create, prepare

from app.api.main import create_app
from app.contracts.evaluators import JudgeAccount
from app.contracts.http import TargetRule
from app.infrastructure.resources import Resources, Settings


def distribution(samples: Sequence[float]) -> dict[str, float | int]:
    ordered = sorted(samples)
    if not ordered:
        return {"n": 0}
    return {
        "n": len(samples),
        **{
            label: round(ordered[math.ceil(len(samples) * q) - 1], 3)
            for label, q in (("p50", 0.5), ("p95", 0.95), ("max", 1.0))
        },
    }


def test_batch_load(catalog: tuple[Settings, dict[str, str]]) -> None:
    settings, tokens = catalog
    lock = threading.Lock()
    counts: Counter[str] = Counter()
    active: Counter[str] = Counter()
    peaks: Counter[str] = Counter()
    verdict = json.dumps(dict(score=1, passed=True, reason="ok", dimensions={"accuracy": 1}))

    class Endpoint(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            pass

        def do_POST(self) -> None:
            data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            with lock:
                counts[self.path] += 1
                active[self.path] += 1
                peaks[self.path] = max(peaks[self.path], active[self.path])
            try:
                # Controlled service time makes overlapping requests observable.
                time.sleep(0.025)
                if self.path.startswith("/judge"):
                    response = {"model": "model-a", "choices": [{"message": {"content": verdict}}]}
                else:
                    response = {"answer": data["query"], "final_answer": data["query"]}
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps(response).encode())
            finally:
                with lock:
                    active[self.path] -= 1

    http = ThreadingHTTPServer(("127.0.0.1", 0), Endpoint)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{http.server_port}"
    gate = TargetRule(
        scheme="http", host="127.0.0.1", port=http.server_port, networks=["127.0.0.1/32"]
    )
    targets = {slug: [gate, gate.model_copy(update={"port": 12345})] for slug in ("shop", "rag")}
    accounts = {
        k: JudgeAccount(
            endpoint=base + "/judge-" + k,
            destination=gate,
            models=["model-a"],
            max_parallel=1,
            rpm=120,
        )
        for k in ("acct", "other")
    }
    settings = settings.model_copy(update={"targets": targets, "judge_accounts": accounts})
    env = {
        **os.environ,
        "EVAL_DATABASE_URL": settings.database_url.get_secret_value(),
        "EVAL_REDIS_URL": settings.redis_url.get_secret_value(),
        "EVAL_TARGETS": json.dumps(
            {k: [g.model_dump() for g in gates] for k, gates in targets.items()}
        ),
        "EVAL_JUDGE_ACCOUNTS": json.dumps({k: a.model_dump() for k, a in accounts.items()}),
        "EVAL_SECRETS": json.dumps(
            {"judge:" + k: {"judge-ref": {"v1": "test-only"}} for k in accounts}
        ),
        "EVAL_WORKER_GLOBAL_SLOTS": "8",
        "EVAL_WORKER_SLOTS": "4",
        "EVAL_POOL_SIZE": "10",
        "EVAL_WORKER_POLL_S": "0.1",
    }
    report: dict[str, Any] = {"tiers": []}
    procs: list[subprocess.Popen[bytes]] = []
    init_ms: list[float] = []
    try:
        app = create_app(settings)
        with TestClient(app, headers={"Authorization": "Bearer " + tokens["curator"]}) as api:

            def before(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
                if statement.startswith("INSERT INTO case_executions"):
                    conn.info["batch_init"] = time.perf_counter()

            def after(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
                if statement.startswith("INSERT INTO case_executions"):
                    init_ms.append(1000 * (time.perf_counter() - conn.info.pop("batch_init")))

            event.listen(app.state.resources.engine.sync_engine, "before_cursor_execute", before)
            event.listen(app.state.resources.engine.sync_engine, "after_cursor_execute", after)
            f = prepare(api)
            shop = f["project"]["id"]
            assert api.patch(
                ROOT + "/projects/" + shop,
                json={"rpm": 1000000, "max_parallel": 5, "target_max_parallel": 2},
            ).is_success
            rag = create(
                api,
                "/projects",
                name="RAG",
                kind="rag",
                slug="rag",
                rpm=1000000,
                max_parallel=5,
                target_max_parallel=2,
            )["id"]
            versions = [
                create(
                    api,
                    f"/projects/{p}/versions",
                    label="batch",
                    deployment_ref="fixed",
                    adapter={
                        "kind": k,
                        "endpoint": base + "/" + slug,
                        "probe_endpoint": base + "/ready",
                    },
                )["id"]
                for p, k, slug in ((shop, "http", "shop"), (rag, "agent", "rag"))
            ]
            ev = create(api, "/evaluators", name="非空", slug="nonempty", kind="rule")
            rule = create(
                api,
                f"/evaluators/{ev['id']}/versions",
                version="1",
                impl_key="rules",
                build=f["judge"]["build"],
                config={"rules": [{"op": "nonempty", "path": "answer", "reason": "empty"}]},
            )["id"]
            judge = f["judge"]
            second = create(
                api,
                f"/evaluators/{judge['evaluator_id']}/versions",
                version="other",
                impl_key="chat_judge",
                build=judge["build"],
                config={**judge["config"], "provider_account_id": "other"},
                provider="openai_compatible",
                model_id="model-a",
                prompt_text="grade",
                prompt_version="p1",
                rubric_version="r1",
            )["id"]
            dbs = {}
            for n in (100, 500, 1000):
                ds = create(api, "/datasets", slug="load-" + str(n), name=f"{n} 条")
                content = "\n".join(
                    json.dumps(
                        {
                            "case_key": str(i),
                            "input": {"query": str(i)},
                            "expected": {"answer": str(i)},
                        }
                    )
                    for i in range(n)
                )
                reply = api.post(
                    ROOT + "/datasets/import",
                    files={"file": ("batch.jsonl", content)},
                    data={"dataset_id": ds["id"], "draft_id": ds["draft_id"], "format": "jsonl"},
                )
                assert reply.is_success, reply.text
                dp = "/datasets/" + ds["id"]
                draft = api.get(ROOT + dp).json()["data"]["draft"]
                dbs[n] = create(
                    api,
                    dp + "/versions",
                    action="publish",
                    draft_id=ds["draft_id"],
                    reviewed_digest=draft["digest"],
                )["id"]
            for idx in range(2):
                procs.append(
                    subprocess.Popen(
                        [sys.executable, "-m", "app.worker.main"],
                        env={**env, "EVAL_WORKER_ID": "bench-" + str(idx)},
                        stdout=subprocess.DEVNULL,
                    )
                )

            async def execute() -> None:
                res = Resources(settings)
                try:
                    for n in (100, 500, 1000):
                        starts = []
                        latencies = []
                        locks = []
                        connections = []
                        redis_buffer = []
                        cpu: list[float] = []
                        cpu_times: list[float] = []
                        rss: list[float] = []
                        next_cpu = 0.0
                        stage_rates: list[float] = []
                        claim_rates: list[float] = []
                        throughput: list[float] = []
                        plans = (
                            [(0, [f["root"]["id"]]), (1, [rule, judge["id"], second])]
                            if n == 100
                            else [(int(n == 1000), [rule])]
                        )
                        ids = []
                        previous_counts = dict(counts)
                        for project, roots in plans:
                            task = create(
                                api,
                                "/evaluations/tasks",
                                name="负载",
                                project_version_id=versions[project],
                                dataset_version_id=dbs[n],
                                evaluator_version_ids=roots,
                                policy={
                                    "max_parallel": 5,
                                    "case_timeout_s": 600,
                                    "initial_backoff_ms": 400,
                                    "max_backoff_ms": 4000,
                                },
                            )
                            path = ROOT + f"/evaluations/tasks/{task['id']}/runs"
                            begin = time.perf_counter()
                            rep = api.post(path, headers={"Idempotency-Key": "load"})
                            assert rep.status_code == 202, rep.text
                            starts.append(1000 * (time.perf_counter() - begin))
                            run = rep.json()["data"]
                            ids.append(UUID(run["id"]))
                            assert (
                                api.post(path, headers={"Idempotency-Key": "load"}).json()["data"][
                                    "id"
                                ]
                                == run["id"]
                            )
                        begin = last = time.perf_counter()
                        old_attempts = old_claims = old_done = 0
                        while True:
                            assert all(p.poll() is None for p in procs)
                            async with res.transaction() as db:
                                sample = (
                                    (
                                        await db.execute(
                                            text("""SELECT
                            (SELECT count(*) FROM pg_stat_activity WHERE
                            datname=current_database()) connections,
                            (SELECT count(*) FROM pg_stat_activity WHERE
                            datname=current_database() AND wait_event_type='Lock') locks,
                            (SELECT count(*) FROM execution_attempts WHERE run_id=ANY(:ids))
                            attempts,
                            (SELECT coalesce(sum(lease_epoch),0) FROM case_executions
                             WHERE run_id=ANY(:ids)) claims,
                            (SELECT count(*) FROM case_executions WHERE run_id=ANY(:ids) AND
                            finished_at IS NOT NULL) done"""),
                                            {"ids": ids},
                                        )
                                    )
                                    .mappings()
                                    .one()
                                )
                            connections.append(sample["connections"])
                            locks.append(sample["locks"])
                            clients = await res.redis.client_list()
                            redis_buffer.append(
                                sum(int(c.get("omem", 0)) for c in clients if int(c.get("sub", 0)))
                            )
                            if os.name == "nt" and time.perf_counter() > next_cpu:
                                parents = ",".join(str(p.pid) for p in procs)
                                cmd = (
                                    f"$roots=@({parents}); $children=Get-CimInstance Win32_Process "
                                    "| Where-Object {$roots -contains $_.ParentProcessId}; "
                                    "$ids=if($children){$children.ProcessId}else{$roots}; "
                                    "Get-Process -Id $ids | Select-Object CPU,WorkingSet64 "
                                    "| ConvertTo-Json -Compress"
                                )
                                processes = json.loads(
                                    await asyncio.to_thread(
                                        subprocess.check_output,
                                        ["powershell", "-NoProfile", "-Command", cmd],
                                    )
                                )
                                cpu_times.append(time.perf_counter())
                                cpu.append(sum(v["CPU"] for v in processes))
                                rss.extend(v["WorkingSet64"] / 1048576 for v in processes)
                                next_cpu = time.perf_counter() + 4
                            now = time.perf_counter()
                            interval = now - last
                            stage_rates.append((sample["attempts"] - old_attempts) / interval)
                            # No recovery occurs in this load test. Each claim increments the epoch.
                            claim_rates.append((int(sample["claims"]) - old_claims) / interval)
                            old_claims = int(sample["claims"])
                            throughput.append((sample["done"] - old_done) / (now - last))
                            old_attempts, old_done, last = sample["attempts"], sample["done"], now
                            states = []
                            for rid in ids:
                                start = time.perf_counter()
                                info = api.get(ROOT + f"/evaluations/runs/{rid}").json()["data"]
                                latencies.append(1000 * (time.perf_counter() - start))
                                states.append(info["status"])
                                if info["status"] in ("FAILED", "PARTIAL_FAILED", "CANCELLED"):
                                    raise AssertionError(str(info["failure_reasons"]))
                            if all(state == "COMPLETED" for state in states):
                                break
                            assert now - begin < 1000
                            await asyncio.sleep(0.6)
                        async with res.transaction() as db:
                            params = {"ids": ids}

                            async def numbers(
                                sql: str, params: dict[str, list[UUID]] = params
                            ) -> list[float]:
                                return [
                                    float(v) for v in (await db.scalars(text(sql), params)).all()
                                ]

                            elapsed = await numbers(
                                "SELECT extract(epoch FROM finished_at-started_at)*1000 "
                                "FROM case_executions WHERE run_id=ANY(:ids)"
                            )
                            lag = await numbers("""SELECT extract(epoch FROM
                            r.finished_at-max(c.finished_at))*1000
                            FROM evaluation_runs r JOIN case_executions c ON c.run_id=r.id WHERE
                            r.id=ANY(:ids) GROUP BY r.id""")
                            waits = await numbers("""SELECT extract(epoch FROM
                            next_start-finished_at)*1000 FROM
                            (SELECT a.*,lead(created_at) OVER(PARTITION BY logical_step_key ORDER
                            BY attempt_no) next_start
                            FROM execution_attempts a WHERE run_id=ANY(:ids)) s WHERE
                            error_code='JUDGE_LIMIT' AND next_start IS NOT NULL""")
                            missing = await db.scalar(
                                text("""SELECT count(*) FROM case_executions c
                            JOIN evaluation_runs r ON r.id=c.run_id LEFT JOIN execution_attempts
                            a ON a.id=c.prediction_attempt_id
                            WHERE r.id=ANY(:ids) AND (c.status<>'SUCCEEDED' OR c.lease_owner IS
                            NOT NULL OR
                               a.status IS DISTINCT FROM 'SUCCEEDED' OR a.execution_id<>c.id OR
                               (SELECT count(*) FROM evaluation_scores WHERE execution_id=c.id)
                               <>jsonb_array_length(r.config_snapshot->'evaluator_plan'))"""),
                                params,
                            )
                            assert not missing and len(elapsed) == n * len(ids)
                            errors = (
                                await db.execute(
                                    text("""SELECT error_code,count(*) n FROM execution_attempts
                            WHERE run_id=ANY(:ids) AND error_code IS NOT NULL GROUP BY
                            error_code"""),
                                    params,
                                )
                            ).all()
                            owners = await numbers("""SELECT count(*) FROM execution_attempts
                            WHERE run_id=ANY(:ids)
                                GROUP BY lease_owner""")
                            assert len(owners) == 2
                            # Each case also has one finalization claim without an attempt.
                            epochs = await numbers(
                                "SELECT sum(lease_epoch) FROM case_executions "
                                "WHERE run_id=ANY(:ids)"
                            )
                            assert epochs == [sum(owners) + len(elapsed)]
                            invalid_scores = await db.scalar(
                                text("""SELECT count(*) FROM evaluation_scores s
                                    JOIN case_executions c ON c.id=s.execution_id
                                    LEFT JOIN execution_attempts a ON a.id=s.created_from_attempt_id
                                    WHERE c.run_id=ANY(:ids) AND
                                    (a.execution_id IS DISTINCT FROM c.id OR
                                     a.evaluator_version_id IS DISTINCT FROM s.evaluator_version_id
                                     OR a.stage IS DISTINCT FROM 'EVALUATOR'
                                     OR s.status <> 'OK')"""),
                                params,
                            )
                            assert invalid_scores == 0
                        rec = {
                            "cases_per_run": n,
                            "runs": len(ids),
                            "wall_s": round(time.perf_counter() - begin, 3),
                            "run_create_ms": distribution(starts),
                            "initialize_ms": distribution(init_ms[-len(ids) :]),
                            "case_ms": distribution(elapsed),
                            "api_ms": distribution(latencies),
                            "merge_ms": distribution(lag),
                            "admission_wait_after_judge_limit_ms": distribution(waits),
                            "attempts_created_per_s": distribution(stage_rates),
                            "claims_per_s": distribution(claim_rates),
                            "completed_per_s": distribution(throughput),
                            "db_connections": distribution(connections),
                            "db_lock_waiters": distribution(locks),
                            "redis_pubsub_output_bytes": distribution(redis_buffer),
                            "attempts_by_worker": owners,
                            "cpu_seconds_two_workers_observed_delta": round(cpu[-1] - cpu[0], 3)
                            if cpu
                            else None,
                            "worker_cpu_pct_one_core_total": distribution(
                                [
                                    100 * (c - p) / (t - s)
                                    for c, p, t, s in zip(
                                        cpu[1:],
                                        cpu[:-1],
                                        cpu_times[1:],
                                        cpu_times[:-1],
                                        strict=True,
                                    )
                                ]
                            ),
                            "worker_rss_mib": distribution(rss),
                            "errors": {str(k): v for k, v in errors},
                            "requests": {
                                k: v - previous_counts.get(k, 0) for k, v in counts.items()
                            },
                        }
                        report["tiers"].append(rec)
                        print("TIER", json.dumps(rec), flush=True)
                finally:
                    await res.close()

            asyncio.run(execute())
            report["peak_http_concurrency"] = dict(peaks)
            assert counts["/judge-acct"] == 200 and counts["/judge-other"] == 100
            assert peaks["/judge-acct"] == peaks["/judge-other"] == 1
            assert all(peaks[path] <= 2 for path in ("/shop", "/rag"))
            output = os.getenv("EVAL_BENCH_REPORT")
            if output:
                Path(output).write_text(json.dumps(report, indent=2) + "\n", encoding="utf8")
    finally:
        for proc in procs:
            proc.terminate()
            proc.wait(timeout=10)
        http.shutdown()
        http.server_close()
        thread.join()
