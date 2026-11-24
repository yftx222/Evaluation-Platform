import asyncio
import json
import logging
import socket
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from uuid import UUID

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from test_batch_console import child_env, process, ready
from test_evaluation_runs import ROOT, create, prepare

from app.api.main import create_app
from app.contracts.evaluators import JudgeAccount
from app.contracts.http import TargetRule
from app.contracts.traces import BoundTraceWriteScope
from app.infrastructure.resources import Resources, Settings
from app.worker.scheduler import Lease, claim, initialize, release
from app.worker.settings import WorkerSettings
from app.worker.trace import AttemptTraceIssuer
from trace_sdk.client import TraceSession

BATCH = ROOT + "/trace-write/events:batch"
FINAL = ROOT + "/trace-write/finalize"


def event(n: int, parent: str | None = None) -> dict[str, Any]:
    return {
        "event_key": f"step-{n}",
        "sequence": n,
        "name": "answer",
        "event_type": "MODEL_CALL",
        "parent_event_key": parent,
        "status": "SUCCEEDED",
        "started_at": "2026-11-23T10:00:00Z",
        "attributes": {"input_tokens": 10},
    }


@dataclass
class Window:
    api: TestClient
    token: str
    trace: UUID
    lease: Lease
    settings: Settings
    tokens: dict[str, str]

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": "Bearer " + self.token}

    def write(self, *events: dict[str, Any]) -> httpx.Response:
        result: httpx.Response = self.api.post(
            BATCH, headers=self.headers, json={"events": list(events)}
        )
        return result

    def close(self, n: int) -> httpx.Response:
        result: httpx.Response = self.api.post(
            FINAL,
            headers=self.headers,
            json={"outcome": "COMPLETE", "last_sequence": n, "contiguous": True},
        )

        return result

    @property
    def url(self) -> str:
        return ROOT + "/traces/" + str(self.trace)


def setup_run(api: TestClient, port: int | None = None) -> tuple[str, str]:
    f = prepare(api)
    version = f["pv"]["id"]
    if port:
        base = f"http://127.0.0.1:{port}"
        version = create(
            api,
            "/projects/" + f["project"]["id"] + "/versions",
            label="trace",
            deployment_ref="local",
            adapter={
                "endpoint": base + "/target",
                "probe_endpoint": base + "/ready",
                "timeout_s": 15,
            },
        )["id"]
    assert api.patch(ROOT + "/projects/" + f["project"]["id"], json={"rpm": 1000000}).is_success
    task = create(
        api,
        "/evaluations/tasks",
        name="Trace 验收",
        project_version_id=version,
        dataset_version_id=f["dv"]["id"],
        evaluator_version_ids=[f["exact"]["id"]],
    )
    run = api.post(
        ROOT + "/evaluations/tasks/" + task["id"] + "/runs", headers={"Idempotency-Key": "trace"}
    )
    assert run.status_code == 202, run.text
    return version, str(run.json()["data"]["id"])


def configured(settings: Settings, port: int = 12345) -> Settings:
    gate = TargetRule(scheme="http", host="127.0.0.1", port=port, networks=["127.0.0.1/32"])
    acct = JudgeAccount(
        endpoint=f"http://127.0.0.1:{port}/judge",
        destination=gate,
        models=["model-a"],
        rpm=60,
        max_parallel=1,
    )
    return settings.model_copy(
        update={
            "targets": {"shop": [gate, gate.model_copy(update={"port": 12345})]},
            "judge_accounts": {"acct": acct},
        }
    )


@pytest.fixture
def window(catalog: tuple[Settings, dict[str, str]]) -> Iterator[Window]:
    settings, tokens = catalog
    settings = configured(settings)
    with TestClient(
        create_app(settings), headers={"Authorization": "Bearer " + tokens["curator"]}
    ) as api:
        setup_run(api)

        async def issue() -> Window:
            resources = Resources(settings)
            try:
                cfg = WorkerSettings(lease_s=180)
                await initialize(resources, cfg)
                lease = await claim(resources, cfg)
                assert lease and lease.attempt
                async with resources.transaction() as db:
                    trace = await db.scalar(
                        text("SELECT trace_id FROM execution_attempts WHERE id=:a"),
                        {"a": lease.attempt},
                    )
                scope = BoundTraceWriteScope(
                    project_id=lease.project,
                    run_id=lease.run,
                    execution_id=lease.case,
                    attempt_id=lease.attempt,
                    epoch=lease.epoch,
                    trace_id=trace,
                )
                token = await AttemptTraceIssuer(resources).issue(
                    scope, datetime.now(UTC) + timedelta(minutes=2)
                )
                return Window(api, token, trace, lease, settings, tokens)
            finally:
                await resources.close()

        yield asyncio.run(issue())


def test_rotate_only_live_credential(window: Window) -> None:
    lease = window.lease
    assert lease.attempt is not None
    scope = BoundTraceWriteScope(
        project_id=lease.project,
        run_id=lease.run,
        execution_id=lease.case,
        attempt_id=lease.attempt,
        epoch=lease.epoch,
        trace_id=window.trace,
    )

    async def check() -> None:
        resources = Resources(window.settings)
        issuer = AttemptTraceIssuer(resources)
        deadline = datetime.now(UTC) + timedelta(minutes=10)
        try:
            old = window.token
            fresh = await issuer.issue(scope, deadline, previous=old)
            assert not await issuer.valid(scope, old) and await issuer.valid(scope, fresh)
            with pytest.raises(ValueError):
                await issuer.issue(scope, deadline, previous=old)
            with pytest.raises(ValueError):
                await issuer.issue(
                    scope.model_copy(update={"epoch": scope.epoch + 1}), deadline, previous=fresh
                )
            async with resources.transaction() as db:
                await db.execute(
                    text(
                        "UPDATE execution_attempts SET trace_expires_at=clock_timestamp() "
                        "WHERE id=:a"
                    ),
                    {"a": scope.attempt_id},
                )
            with pytest.raises(ValueError):
                await issuer.issue(scope, deadline, previous=fresh)
            async with resources.transaction() as db:
                await db.execute(
                    text(
                        "UPDATE case_executions SET lease_expires_at=lease_expires_at WHERE id=:c"
                    ),
                    {"c": scope.execution_id},
                )
            with pytest.raises(ValueError):
                await issuer.issue(scope, deadline, previous=fresh)
        finally:
            await resources.close()

    asyncio.run(check())
    assert window.api.get(window.url).json()["data"]["status"] == "MISSING"


def test_trace_atomic_order_and_replay(window: Window, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="uvicorn.error")
    w = window
    assert w.api.get(w.url).json()["data"]["status"] == "OPEN"
    assert w.write(event(2, "step-1")).json()["data"]["accepted"] == 1
    with ThreadPoolExecutor(max_workers=4) as pool:
        replies = list(pool.map(lambda _: w.write(event(1)).json()["data"], range(4)))
    assert sum(r["accepted"] for r in replies) == 1
    assert sum(r["duplicate"] for r in replies) == 3
    retry = w.write(event(1), event(2, "step-1")).json()["data"]
    assert (retry["accepted"], retry["duplicate"]) == (0, 2)
    for item, code in [
        (dict(event(1), name="changed"), "EVENT_KEY_CONFLICT"),
        (dict(event(1), event_key="different"), "SEQUENCE_CONFLICT"),
    ]:
        rejected = w.write(event(3), item)
        assert rejected.status_code == 409
        assert rejected.json()["error"]["code"] == code and rejected.json()["rejected"] == 2
    assert w.write(event(3, "step-4"), event(4, "step-3")).json()["error"]["code"] == "PARENT_CYCLE"
    first = w.api.get(w.url + "/events?limit=1").json()["data"]
    assert first["next_sequence"] == 1
    second = w.api.get(w.url + "/events?after_sequence=1").json()["data"]["items"]
    assert len(second) == 1 and second[0]["parent_event_id"] == first["items"][0]["id"]
    assert second[0]["started_at"] != second[0]["received_at"]
    closed = w.close(2).json()["data"]
    assert closed["status"] == "COMPLETE" and closed["event_count"] == 2
    assert w.close(99).json()["data"] == closed
    assert w.write(event(3)).json()["error"]["code"] == "TRACE_SEALED"
    records = [
        json.loads(r.getMessage())
        for r in caplog.records
        if r.getMessage().startswith('{"event": "http_request"')
    ]
    batches = [rec for rec in records if rec["route"] == BATCH]
    assert sum(rec.get("accepted", 0) for rec in batches) == 2
    assert sum(rec.get("duplicate", 0) for rec in batches) == 5
    assert any(rec["status"] == 409 and rec.get("rejected") == 2 for rec in batches)
    assert all(rec["elapsed_ms"] >= 0 for rec in batches) and w.token not in caplog.text


@pytest.mark.parametrize(
    "scenario", ["empty", "gap", "parent", "race", "expired", "lost", "cancel", "end"]
)
def test_trace_collection_window(window: Window, scenario: str) -> None:
    w = window
    if scenario == "race":
        barrier = threading.Barrier(2)

        def run(write: bool) -> httpx.Response:
            barrier.wait(timeout=5)
            return w.write(event(1)) if write else w.close(1)

        with ThreadPoolExecutor(max_workers=2) as pool:
            added, sealed = pool.map(run, [True, False])
        assert (added.status_code, sealed.json()["data"]["status"]) in [
            (200, "COMPLETE"),
            (409, "MISSING"),
        ]
        return
    if scenario in {"empty", "gap", "parent"}:
        if scenario != "empty":
            assert w.write(
                event(2) if scenario == "gap" else event(1, "other-trace-key")
            ).is_success
        closed = w.close(2 if scenario == "gap" else 1).json()["data"]
        assert (closed["status"], closed["close_reason"]) == {
            "empty": ("MISSING", "NO_EVENTS"),
            "gap": ("PARTIAL", "SEQUENCE_GAP"),
            "parent": ("PARTIAL", "PARENT_MISSING"),
        }[scenario]
        return
    assert w.write(event(1)).is_success
    if scenario == "cancel":
        assert w.api.post(ROOT + f"/evaluations/runs/{w.lease.run}/cancel").status_code == 202

    async def invalidate() -> None:
        resources = Resources(w.settings)
        try:
            async with resources.transaction() as db:
                if scenario == "expired":
                    await db.execute(
                        text("""UPDATE execution_attempts SET
                        trace_expires_at=now()-interval '1 second' WHERE id=:a"""),
                        {"a": w.lease.attempt},
                    )
                    await db.execute(
                        text("""UPDATE case_executions SET lease_expires_at=lease_expires_at
                        WHERE id=:c"""),
                        {"c": w.lease.case},
                    )
                if scenario == "lost":
                    await db.execute(
                        text("""UPDATE case_executions SET lease_expires_at=now()
                        WHERE id=:c"""),
                        {"c": w.lease.case},
                    )
            if scenario == "end":
                assert await release(resources, w.lease)
        finally:
            await resources.close()

    asyncio.run(invalidate())
    assert w.write(event(2)).status_code == w.close(2).status_code == 401
    if scenario in {"lost", "cancel"}:

        async def recover() -> None:
            resources = Resources(w.settings)
            try:
                if scenario == "lost":
                    assert await release(resources)
                else:
                    assert await release(resources, w.lease)
            finally:
                await resources.close()

        asyncio.run(recover())
        assert w.write(event(2)).status_code == 401
    if scenario in {"expired", "end", "lost", "cancel"}:
        assert w.api.get(w.url).json()["data"]["status"] == "PARTIAL"


def test_trace_input_and_access(window: Window) -> None:
    w = window
    invalid: list[tuple[dict[str, Any], str]] = [
        ({"input": {"api_key": "sensitive-test"}}, "SENSITIVE_FIELD"),
        ({"output": {"message": w.token}}, "UNSAFE_VALUE"),
        ({"name": "a@b.com"}, "UNSAFE_VALUE"),
        ({"resource": {"x": "a" * 33000}}, "EVENT_TOO_LARGE"),
        ({"duration_ms": -1}, "INVALID_EVENT"),
        ({"project_id": str(w.lease.project)}, "INVALID_EVENT"),
        ({"parent_event_key": "step-1"}, "INVALID_EVENT"),
    ]
    for payload, code in invalid:
        response = w.write(event(2), dict(event(1), **payload))
        assert response.json()["error"]["code"] == code
        assert response.json()["error"]["index"] == 1 and w.token not in response.text
    assert w.write(*[event(n + 1) for n in range(101)]).status_code == 413
    for data, status in [(b"{invalid", 422), (b"x" * 525000, 413)]:
        assert w.api.post(BATCH, headers=w.headers, content=data).status_code == status
    assert w.api.get(w.url).json()["data"]["event_count"] == 0
    foreign = {"Authorization": "Bearer " + w.tokens["rag"]}
    assert w.api.get(w.url, headers=foreign).status_code in (403, 404)
    assert w.api.get(w.url + "/events", headers=foreign).status_code in (403, 404)
    assert w.api.get(w.url, headers=w.headers).status_code == 401
    assert w.api.post(BATCH, json={"events": [event(1)]}).status_code == 401
    key_path = "/projects/" + str(w.lease.project) + "/api-keys"
    for scope, allowed in [("results:read", 200), ("project:read", 403)]:
        key = create(
            w.api,
            key_path,
            name="read",
            scopes=[scope],
            expires_at=(datetime.now(UTC) + timedelta(days=1)).isoformat(),
        )
        assert w.write(dict(event(1), output={"text": key["secret"]})).status_code == 422
        header = {"Authorization": "ApiKey " + key["secret"]}
        assert w.api.get(w.url, headers=header).status_code == allowed
        assert w.api.post(BATCH, headers=header, json={"events": [event(1)]}).status_code == 401


def test_real_worker_trace_callback(catalog: tuple[Settings, dict[str, str]]) -> None:
    settings, tokens = catalog
    with socket.socket() as unavailable, socket.socket() as port_probe:
        unavailable.bind(("127.0.0.1", 0))
        offline = f"http://127.0.0.1:{unavailable.getsockname()[1]}"
        port_probe.bind(("127.0.0.1", 0))
        port = port_probe.getsockname()[1]
        port_probe.close()
        origin = f"http://127.0.0.1:{port}"
        observed: dict[str, tuple[str, str]] = {}

        class Target(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                pass

            def do_POST(self) -> None:
                query = json.loads(self.rfile.read(int(self.headers["Content-Length"])))["query"]
                tracer = self.headers["X-Trace-Write"]
                observed[query] = (self.headers["X-Trace-Id"], tracer)
                with httpx.Client(
                    headers={"Authorization": "Bearer " + tracer}, timeout=3, trust_env=False
                ) as sender:
                    if query == "one":

                        async def collect() -> None:
                            sdk = TraceSession.from_headers(origin, dict(self.headers))
                            assert sdk
                            async with sdk:
                                assert sdk.add("MODEL_CALL", "answer")
                            assert sdk.status == "COMPLETE" and sdk.finalize_confirmed

                        asyncio.run(collect())
                    elif query == "two":
                        # A real refused callback connection must leave the prediction intact.
                        with pytest.raises(httpx.ConnectError):
                            sender.post(offline + BATCH, json={"events": [event(1)]})
                    else:
                        entry = dict(event(1, "step-1"), event_key="child")
                        written = sender.post(origin + BATCH, json={"events": [entry]})
                        assert written.is_success, written.text
                        closed = sender.post(
                            origin + FINAL,
                            json={"outcome": "COMPLETE", "last_sequence": 1, "contiguous": True},
                        )
                        assert closed.is_success, closed.text
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps({"answer": query}).encode())

        target = ThreadingHTTPServer(("127.0.0.1", 0), Target)
        thread = threading.Thread(target=target.serve_forever, daemon=True)
        thread.start()
        try:
            settings = configured(settings, target.server_port)
            hdr = {"Authorization": "Bearer " + tokens["curator"]}
            with TestClient(create_app(settings), headers=hdr) as setup:
                version, rid = setup_run(setup, target.server_port)
            env = child_env(settings)
            env["EVAL_TRACE_TARGET_VERSIONS"] = json.dumps([version])
            with process(
                [
                    "-m",
                    "uvicorn",
                    "app.api.main:app",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(port),
                    "--no-access-log",
                ],
                env,
            ):
                ready(origin)
                with (
                    process(["-m", "app.worker.main"], env),
                    httpx.Client(base_url=origin, headers=hdr, trust_env=False, timeout=3) as api,
                ):
                    url = ROOT + "/evaluations/runs/" + rid
                    deadline = time.monotonic() + 30
                    while time.monotonic() < deadline:
                        run = api.get(url).json()["data"]
                        if run["status"] in ("COMPLETED", "PARTIAL_FAILED", "FAILED"):
                            break
                        time.sleep(0.05)
                    assert run["status"] == "COMPLETED"
                    results = api.get(url + "/results").json()["data"]["items"]
                    assert len(results) == 3
                    summary = api.get(ROOT + "/reports/runs/" + rid).json()["data"]["summary"]
                    assert summary["trace_mode"] == "callback"
                    assert (
                        summary["complete"],
                        summary["partial"],
                        summary["missing"],
                        summary["not_collected"],
                    ) == (1, 1, 1, 0)
                    assert summary["trace_complete_rate"]["denominator"] == 3
                    for item in results:
                        key = item["case_key"]
                        trace_id, token = observed[key]
                        assert item["prediction"]["answer"] == key and item["trace_ids"] == [
                            trace_id
                        ]
                        trace = api.get(ROOT + "/traces/" + trace_id).json()["data"]
                        assert trace["attempt_id"] == item["prediction_attempt_id"]
                        assert (
                            trace["status"]
                            == {"one": "COMPLETE", "two": "MISSING", "three": "PARTIAL"}[key]
                        )
                        assert trace["event_count"] == int(key != "two")
                        assert token not in json.dumps(item)
                        assert (
                            api.post(
                                BATCH,
                                json={"events": [event(2)]},
                                headers={"Authorization": "Bearer " + token},
                            ).status_code
                            == 401
                        )
        finally:
            target.shutdown()
            target.server_close()
            thread.join(timeout=3)


@pytest.mark.parametrize("finalizing", [False, True])
def test_append_during_worker_exit(window: Window, finalizing: bool) -> None:
    w = window
    if finalizing:
        assert w.write(event(1)).is_success
    barrier = threading.Barrier(2)

    def post() -> httpx.Response:
        barrier.wait(timeout=5)
        return w.close(1) if finalizing else w.write(event(1))

    async def end() -> None:
        resources = Resources(w.settings)
        try:
            barrier.wait(timeout=5)
            if not await release(resources, w.lease):
                # SKIP LOCKED deliberately defers surrender while the append owns the case.
                await asyncio.to_thread(writing.result, 5)
                assert await release(resources, w.lease)
        finally:
            await resources.close()

    with ThreadPoolExecutor(max_workers=1) as pool:
        writing = pool.submit(post)
        asyncio.run(end())
        response = writing.result()
    sealed = w.api.get(w.url).json()["data"]
    outcomes = (
        [(200, "COMPLETE", 1), (401, "PARTIAL", 1)]
        if finalizing
        else [(200, "PARTIAL", 1), (401, "MISSING", 0)]
    )
    assert (response.status_code, sealed["status"], sealed["event_count"]) in outcomes
    assert w.write(event(2)).status_code == 401


def test_trace_batch_measurement(window: Window, request: pytest.FixtureRequest) -> None:
    import statistics

    w = window
    timings = []
    reads = []
    cursor = 0
    for batch in range(20):
        entries = [event(batch * 50 + n + 1) for n in range(50)]
        start = time.perf_counter()
        result = w.write(*entries)
        timings.append((time.perf_counter() - start) * 1000)
        assert result.json()["data"]["accepted"] == 50
    assert w.close(1000).json()["data"]["status"] == "COMPLETE"
    for _ in range(20):
        start = time.perf_counter()
        page = w.api.get(w.url + "/events", params={"after_sequence": cursor, "limit": 50}).json()[
            "data"
        ]
        reads.append((time.perf_counter() - start) * 1000)
        assert [e["sequence"] for e in page["items"]] == list(range(cursor + 1, cursor + 51))
        cursor += 50
    for name, samples in [("write", timings), ("read", reads)]:
        request.node.user_properties.extend(
            [
                (name + "_p50_ms", round(statistics.median(samples), 2)),
                (name + "_p95_ms", round(sorted(samples)[18], 2)),
            ]
        )
    request.node.user_properties.append(
        ("accepted_events_per_second", round(1000000 / sum(timings), 2))
    )
