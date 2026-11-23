"""Interrupt the actual Worker subprocess while the test service holds its HTTP response."""

import asyncio
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from test_evaluation_runs import ROOT, create, prepare

from app.api.main import create_app
from app.contracts.adapters import TraceWriteScope
from app.contracts.evaluators import JudgeAccount
from app.contracts.http import TargetRule
from app.infrastructure.resources import Resources, Settings
from app.worker.trace import AttemptTraceIssuer


@pytest.mark.parametrize(
    "scenario",
    [
        "pause",
        "cancel",
        "kill_target",
        "kill_judge",
        "invalid",
        "read_timeout",
        "disconnect",
        "judge_timeout",
        "503",
        "verified_503",
        "judge_invalid",
        "judge_429",
        "echo_token",
        "before_send",
        "before_prediction",
        "after_prediction",
        "after_score",
        "before_summary",
        "missing_adapter",
        "missing_evaluator",
    ],
)
def test_live_delivery(catalog: tuple[Settings, dict[str, str]], scenario: str) -> None:
    settings, tokens = catalog
    arrived, unblock = threading.Event(), threading.Event()
    calls: list[tuple[str, str, str | None]] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            pass

        def do_POST(self) -> None:
            self.rfile.read(int(self.headers["Content-Length"]))
            calls.append(
                (self.path, self.headers["Idempotency-Key"], self.headers.get("X-Trace-Write"))
            )
            status = 200
            if self.path == "/target":
                payload = b'{"answer":"prediction"}'
                if scenario == "echo_token":
                    payload = json.dumps({"answer": self.headers["X-Trace-Write"]}).encode()
                if scenario == "disconnect":
                    return
                if scenario == "invalid":
                    payload = b"invalid"
                if scenario in ("503", "verified_503") and len(calls) == 1:
                    status = 503
                if scenario in ("pause", "cancel", "kill_target", "read_timeout"):
                    arrived.set()
                    unblock.wait(18)
            else:
                payload = b"invalid"
                if scenario == "judge_429":
                    if sum(path == "/judge" for path, _, _ in calls) == 1:
                        status = 429
                    else:
                        verdict = {
                            "score": 1,
                            "passed": True,
                            "reason": "ok",
                            "dimensions": {"accuracy": 1},
                        }
                        payload = json.dumps(
                            {
                                "model": "model-a",
                                "choices": [{"message": {"content": json.dumps(verdict)}}],
                            }
                        ).encode()
                if scenario in ("kill_judge", "judge_timeout"):
                    arrived.set()
                    unblock.wait(18)
            self.send_response(status)
            if status == 429:
                self.send_header("Retry-After", "1")
            self.end_headers()
            try:
                self.wfile.write(payload)
            except (ConnectionError, OSError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    gate = TargetRule(
        scheme="http", host="127.0.0.1", port=server.server_port, networks=["127.0.0.1/32"]
    )
    base = f"http://127.0.0.1:{server.server_port}"
    judge = JudgeAccount(
        endpoint=base + "/judge", destination=gate, models=["model-a"], max_parallel=1, rpm=60
    )
    targets = {"shop": [gate, gate.model_copy(update={"port": 12345})]}
    settings = settings.model_copy(update={"targets": targets, "judge_accounts": {"acct": judge}})
    try:
        with TestClient(
            create_app(settings), headers={"Authorization": "Bearer " + tokens["curator"]}
        ) as api:
            f = prepare(api)
            project = f["project"]["id"]
            assert api.patch(ROOT + "/projects/" + project, json={"rpm": 1000000}).is_success
            pv = create(
                api,
                f"/projects/{project}/versions",
                label="fault",
                deployment_ref="fixed",
                adapter={
                    "endpoint": base + "/target",
                    "probe_endpoint": base + "/ready",
                    "timeout_s": 1 if scenario == "read_timeout" else 16,
                },
            )
            ds = create(api, "/datasets", slug="single", name="故障验收")
            path = "/datasets/" + ds["id"]
            create(
                api,
                path + "/cases",
                draft_id=ds["draft_id"],
                case_key="one",
                input={"query": "hi"},
                expected={"answer": "prediction"},
            )
            draft = api.get(ROOT + path).json()["data"]["draft"]
            dv = create(
                api,
                path + "/versions",
                action="publish",
                draft_id=ds["draft_id"],
                reviewed_digest=draft["digest"],
            )
            use_judge = scenario in ("kill_judge", "judge_invalid", "judge_429", "judge_timeout")
            if scenario == "judge_timeout":
                j = f["judge"]
                f["judge"] = create(
                    api,
                    f"/evaluators/{j['evaluator_id']}/versions",
                    version="short",
                    impl_key=j["impl_key"],
                    build=j["build"],
                    config={**j["config"], "timeout_s": 1},
                    provider=j["provider"],
                    model_id=j["model_id"],
                    prompt_text=j["prompt_text"],
                    prompt_version=j["prompt_version"],
                    rubric_version=j["rubric_version"],
                )
            roots = [f["judge" if use_judge else "exact"]["id"]]
            if scenario == "after_score":
                owner = create(api, "/evaluators", kind="rule", slug="new-rule", name="非空")
                roots.append(
                    create(
                        api,
                        f"/evaluators/{owner['id']}/versions",
                        version="one",
                        impl_key="rules",
                        build=f["exact"]["build"],
                        config={"rules": [{"path": "answer", "op": "nonempty", "reason": "empty"}]},
                    )["id"]
                )
            task = create(
                api,
                "/evaluations/tasks",
                name="故障验收",
                project_version_id=pv["id"],
                dataset_version_id=dv["id"],
                evaluator_version_ids=roots,
                policy={
                    "adapter_max_attempts": 3,
                    "evaluator_max_attempts": 3,
                    "target_replay": "verified_idempotent",
                    "initial_backoff_ms": 10,
                    "jitter": False,
                },
            )
            run = api.post(
                ROOT + f"/evaluations/tasks/{task['id']}/runs", headers={"Idempotency-Key": "run"}
            )
            assert run.status_code == 202, run.text
            rid = run.json()["data"]["id"]
            url = ROOT + "/evaluations/runs/" + rid

            async def exercise() -> None:
                env = {
                    **os.environ,
                    "EVAL_DATABASE_URL": settings.database_url.get_secret_value(),
                    "EVAL_REDIS_URL": settings.redis_url.get_secret_value(),
                    "EVAL_TARGETS": json.dumps({"shop": [g.model_dump() for g in targets["shop"]]}),
                    "EVAL_JUDGE_ACCOUNTS": json.dumps({"acct": judge.model_dump()}),
                    "EVAL_SECRETS": json.dumps({"judge:acct": {"judge-ref": {"v1": "local-only"}}}),
                    "EVAL_TRACE_TARGET_VERSIONS": json.dumps([pv["id"]]),
                    "EVAL_VERIFIED_IDEMPOTENT_VERSIONS": json.dumps(
                        [pv["id"]] if scenario == "verified_503" else []
                    ),
                    "PYTHONPATH": os.getcwd(),
                    "EVAL_WORKER_SLOTS": "1",
                    "EVAL_WORKER_POLL_S": "0.06",
                    "EVAL_WORKER_LEASE_S": "2.1",
                    "EVAL_WORKER_HEARTBEAT_S": "0.3",
                }

                async def start(checkpoint: bool = False) -> asyncio.subprocess.Process:
                    args = (
                        ["tests/worker_checkpoint.py", scenario]
                        if checkpoint
                        else ["-m", "app.worker.main"]
                    )
                    return await asyncio.create_subprocess_exec(sys.executable, *args, env=env)

                async def until(expected: str) -> None:
                    async with asyncio.timeout(12):
                        while True:
                            info = api.get(url).json()["data"]
                            if info["status"] == expected:
                                return
                            assert info["status"] not in {
                                "COMPLETED",
                                "FAILED",
                                "CANCELLED",
                                "PARTIAL_FAILED",
                            }, info["status"]
                            await asyncio.sleep(0.04)

                crashes = {
                    "before_send",
                    "before_prediction",
                    "after_prediction",
                    "after_score",
                    "before_summary",
                }
                proc = await start(scenario in crashes or scenario.startswith("missing_"))
                resources = Resources(settings)
                try:
                    if scenario in crashes:
                        assert await asyncio.wait_for(proc.wait(), 12) == 77
                        proc = await start()
                    if scenario in ("pause", "cancel", "kill_target", "kill_judge"):
                        assert await asyncio.to_thread(arrived.wait, 12)
                        if scenario != "kill_judge":
                            token = calls[0][2]
                            assert token
                            async with resources.transaction() as db:
                                c = (
                                    await db.execute(
                                        text("SELECT id,lease_epoch FROM case_executions")
                                    )
                                ).one()
                            scope = TraceWriteScope(
                                project_id=UUID(project),
                                run_id=UUID(rid),
                                execution_id=c.id,
                                attempt_id=UUID(token.partition(".")[0]),
                                epoch=c.lease_epoch,
                            )
                            issuer = AttemptTraceIssuer(resources)
                            assert await issuer.valid(scope, token)
                            assert not await issuer.valid(
                                scope.model_copy(update={"epoch": scope.epoch + 1}), token
                            )
                        if scenario in ("pause", "cancel"):
                            assert api.post(url + "/" + scenario).status_code == 202
                            assert token and not await issuer.valid(scope, token)
                            if scenario == "pause":
                                unblock.set()
                                await until("PAUSED")
                                assert api.post(url + "/resume").status_code == 200
                        else:
                            proc.kill()
                            await proc.wait()
                            proc = await start()
                        if scenario != "cancel":
                            unblock.set()
                    expected = {
                        **{k: "COMPLETED" for k in crashes - {"before_prediction"}},
                        "pause": "COMPLETED",
                        "verified_503": "COMPLETED",
                        "judge_429": "COMPLETED",
                        "cancel": "CANCELLED",
                    }.get(scenario, "FAILED")
                    await until(expected)
                    result = api.get(url + "/results").json()["data"]["items"][0]
                    if scenario in (
                        "kill_target",
                        "kill_judge",
                        "cancel",
                        "read_timeout",
                        "before_prediction",
                        "judge_timeout",
                        "disconnect",
                    ):
                        assert result["status"] == "UNKNOWN_DELIVERY"
                    if scenario == "echo_token":
                        assert result["adapter_error_code"] == "SENSITIVE_RESPONSE"
                        assert result["prediction"] is None
                        token = calls[0][2]
                        assert token and token not in json.dumps(result)
                    if scenario == "invalid":
                        assert result["adapter_error_code"] == "INVALID_RESPONSE"
                    if scenario == "503":
                        assert result["adapter_error_code"] == "TARGET_HTTP_ERROR"
                    if scenario == "judge_invalid":
                        score = result["scores"][0]
                        assert (
                            score["status"] == "ERROR"
                            and score["score"] is None
                            and score["passed"] is None
                        )
                        assert score["reason"] == "JUDGE_INVALID_RESPONSE"
                    if scenario == "missing_adapter":
                        assert result["adapter_error_code"] == "VERSION_UNAVAILABLE"
                    if scenario == "missing_evaluator":
                        assert result["scores"][0]["reason"] == "VERSION_UNAVAILABLE"
                    if scenario in crashes - {"before_prediction"}:
                        assert len(result["scores"]) == len(roots)
                        assert len({s["evaluator_version_id"] for s in result["scores"]}) == len(
                            roots
                        )
                        assert result["prediction_attempt_id"]
                    targets_seen = [key for path, key, _ in calls if path == "/target"]
                    assert len(targets_seen) == int(scenario != "missing_adapter") + (
                        scenario == "verified_503"
                    )
                    assert len(set(targets_seen)) == int(scenario != "missing_adapter")
                    judge_keys = [key for path, key, _ in calls if path == "/judge"]
                    assert len(judge_keys) == int(use_judge) + int(scenario == "judge_429")
                    if scenario == "judge_429":
                        assert len(set(judge_keys)) == 1 and judge_keys[0] != targets_seen[0]
                        assert len(result["scores"]) == 1 and result["scores"][0]["status"] == "OK"
                        async with resources.transaction() as db:
                            gap = await db.scalar(
                                text("""SELECT extract(epoch FROM b.started_at-a.finished_at)
                                FROM execution_attempts a JOIN execution_attempts b
                                USING(logical_step_key) WHERE a.error_code='JUDGE_RATE_LIMIT'
                                AND b.status='SUCCEEDED'""")
                            )
                            assert gap >= 1
                    assert all(c[2] is None for c in calls if c[0] == "/judge")
                    async with resources.transaction() as db:
                        assert not await db.scalar(
                            text(
                                "SELECT count(*) FROM execution_attempts "
                                "WHERE trace_verifier IS NOT NULL"
                            )
                        )
                finally:
                    unblock.set()
                    if proc.returncode is None:
                        proc.kill()
                    await proc.wait()
                    await resources.close()

            asyncio.run(exercise())
    finally:
        unblock.set()
        server.shutdown()
        server.server_close()
        thread.join()
