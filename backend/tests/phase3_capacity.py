"""Opt-in real Run initialization measurements. No Worker executes the created runs."""

import asyncio
import json
import os
import time
import tracemalloc
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import event, text
from test_evaluation_runs import ROOT, create, prepare

from app.api.main import create_app
from app.contracts.evaluators import JudgeAccount
from app.contracts.http import TargetRule
from app.infrastructure.resources import Resources, Settings


def test_run_initialization_capacity(catalog: tuple[Settings, dict[str, str]]) -> None:
    settings, tokens = catalog
    gate = TargetRule(host="127.0.0.1", scheme="http", port=12345, networks=["127.0.0.1/32"])
    account = JudgeAccount(
        endpoint="http://127.0.0.1:12345",
        destination=gate,
        models=["model-a"],
        max_parallel=1,
        rpm=60,
    )
    settings = settings.model_copy(
        update={"targets": {"shop": [gate]}, "judge_accounts": {"acct": account}}
    )
    report: list[dict[str, Any]] = []
    app = create_app(settings)
    with TestClient(app, headers={"Authorization": "Bearer " + tokens["curator"]}) as api:
        f = prepare(api)
        engine = app.state.resources.engine.sync_engine
        tx_ms: list[float] = []
        backend_kib: list[float] = []
        measuring = False

        @event.listens_for(engine, "begin")
        def begin(conn: Any) -> None:
            if measuring:
                conn.info["measure_begin"] = time.perf_counter()

        @event.listens_for(engine, "checkin")
        def finish(dbapi: Any, record: Any) -> None:
            start = record.info.pop("measure_begin", None)
            if start is not None:
                tx_ms.append(round(1000 * (time.perf_counter() - start), 2))

        @event.listens_for(engine, "after_cursor_execute")
        def memory(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
            if measuring and statement.startswith("UPDATE case_executions SET status='READY'"):
                # Same PostgreSQL backend, inside the transaction, including deferred triggers.
                probe = conn.connection.cursor()
                try:
                    probe.execute("SELECT sum(used_bytes)/1024 FROM pg_backend_memory_contexts")
                    backend_kib.append(round(float(probe.fetchone()[0]), 1))
                finally:
                    probe.close()

        async def exercise() -> None:
            nonlocal measuring
            res = Resources(settings)
            try:
                for n in (1000, 5000, 10000, 10001):
                    dataset = create(api, "/datasets", name=f"capacity {n}", slug=f"capacity-{n}")
                    async with res.transaction() as db:
                        await db.execute(
                            text("""INSERT INTO evaluation_cases
                            (dataset_version_id,case_key,input,expected,tags,split,extensions)
                            SELECT :v,i::text,jsonb_build_object('query',i::text),
                            jsonb_build_object('answer',i::text),'{}','test','{}'
                            FROM generate_series(1,:n) AS i"""),
                            {"v": UUID(dataset["draft_id"]), "n": n},
                        )
                    dp = "/datasets/" + dataset["id"]
                    draft = api.get(ROOT + dp).json()["data"]["draft"]
                    version = create(
                        api,
                        dp + "/versions",
                        action="publish",
                        draft_id=draft["id"],
                        reviewed_digest=draft["digest"],
                    )
                    body = dict(
                        name=f"init {n}",
                        project_version_id=f["pv"]["id"],
                        evaluator_version_ids=[f["exact"]["id"]],
                        dataset_version_id=version["id"],
                    )
                    task = api.post(ROOT + "/evaluations/tasks", json=body)
                    if n == 10001:
                        assert (
                            task.status_code == 422
                            and task.json()["error"]["code"] == "RUN_CASE_LIMIT"
                        )
                        break
                    assert task.status_code == 201, task.text
                    path = ROOT + "/evaluations/tasks/" + task.json()["data"]["id"] + "/runs"

                    def start(path: str = path, n: int = n) -> float:
                        begin_at = time.perf_counter()
                        response = api.post(path, headers={"Idempotency-Key": uuid4().hex})
                        ms = (time.perf_counter() - begin_at) * 1000
                        assert response.status_code == 202, response.text
                        run = response.json()["data"]
                        assert run["total_cases"] == run["pending_cases"] == n
                        return round(ms, 2)

                    for parallel in (1, 2):
                        tx_ms.clear()
                        backend_kib.clear()
                        samples = []
                        async with res.engine.connect() as obs:
                            obs = await obs.execution_options(isolation_level="AUTOCOMMIT")
                            await obs.execute(text("SELECT 1"))
                            tracemalloc.start()
                            measuring = True
                            try:
                                writers = asyncio.gather(
                                    *(asyncio.to_thread(start) for _ in range(parallel))
                                )
                                while not writers.done():
                                    sample = (
                                        await obs.execute(
                                            text("""SELECT
                                      count(*) FILTER(WHERE state<>'idle') AS busy,
                                      count(*) FILTER(WHERE wait_event_type='Lock') AS waiters,
                                      coalesce(max(extract(epoch FROM clock_timestamp()-l.since))
                                        FILTER(WHERE wait_event_type='Lock'),0)*1000 AS wait_ms
                                      FROM pg_stat_activity a LEFT JOIN LATERAL
                                      (SELECT min(waitstart) since FROM pg_locks
                                       WHERE pid=a.pid AND NOT granted) l ON true
                                      WHERE datname=current_database()
                                      AND a.pid<>pg_backend_pid()""")
                                        )
                                    ).one()
                                    samples.append(sample)
                                    await asyncio.sleep(0.01)
                                api_ms = await writers
                                _, peak = tracemalloc.get_traced_memory()
                            finally:
                                measuring = False
                                tracemalloc.stop()
                        assert len(tx_ms) == len(backend_kib) == parallel and samples
                        rec = dict(
                            cases=n,
                            simultaneous_runs=parallel,
                            api_ms=api_ms,
                            begin_to_checkin_ms=list(tx_ms),
                            active_db_peak=max(s.busy for s in samples),
                            lock_waiters_peak=max(s.waiters for s in samples),
                            observed_lock_wait_ms=round(max(float(s.wait_ms) for s in samples), 2),
                            python_heap_peak_kib=round(peak / 1024, 1),
                            pg_backend_after_ready_kib=list(backend_kib),
                        )
                        report.append(rec)
                        print("CAPACITY", json.dumps(rec), flush=True)
            finally:
                await res.close()

        asyncio.run(exercise())
    output = os.getenv("EVAL_CAPACITY_REPORT")
    if output:
        Path(output).write_text(json.dumps(report, indent=2) + "\n", encoding="utf8")
