import asyncio
import json
import threading
from collections import Counter
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import text
from test_evaluation_runs import ROOT, create, prepare

from app.api.main import create_app
from app.contracts.adapters import TraceWriteScope
from app.contracts.evaluators import JudgeAccount
from app.contracts.http import TargetRule
from app.contracts.runs import ExecutionPolicy
from app.infrastructure.resources import Resources, Settings
from app.plugins.evaluator_releases import PHASE2_BUILD
from app.plugins.evaluators import BUILD
from app.worker.execution import CaseRunner
from app.worker.retry import delay, quota_delay, retry_after
from app.worker.scheduler import claim, initialize, reconcile, release
from app.worker.settings import WorkerSettings
from app.worker.trace import AttemptTraceIssuer


def test_retry_decisions() -> None:
    now = datetime.now(UTC)
    assert retry_after("Wed, 21 Oct 2015 07:28:00 GMT", now) == 0
    assert retry_after("bad date", now) is None
    pol = ExecutionPolicy(adapter_max_attempts=2, jitter=False)

    def check(
        code: str,
        delivery: str,
        status: int | None,
        hint: str | None = None,
        verified: bool = False,
        count: int = 1,
    ) -> float | None:
        return delay(
            pol,
            stage="ADAPTER",
            count=count,
            code=code,
            delivery=delivery,
            status=status,
            header=hint,
            deadline=now + timedelta(seconds=15),
            verified=verified,
        )

    deadline = now + timedelta(seconds=30)
    assert [quota_delay(pol, n, None, deadline) for n in (1, 2, 3, 40)] == [0.5, 1, 2, 10]
    assert quota_delay(pol, 40, 3, deadline) == 3
    assert quota_delay(pol, 1, 60, deadline) is None
    tiny = ExecutionPolicy(initial_backoff_ms=1, max_backoff_ms=1)
    assert quota_delay(tiny, 1, None, deadline) == 0.25
    assert check("TARGET_LIMIT", "unknown", None, count=0) is None
    assert check("TARGET_RATE_LIMIT", "sent", 429, "4") == 4
    assert check("TARGET_RATE_LIMIT", "sent", 429, "25") is None
    assert check("TARGET_HTTP_ERROR", "sent", 503) is None
    assert check("TARGET_HTTP_ERROR", "sent", 503, verified=True) == 0.5
    assert check("TARGET_TIMEOUT", "unknown", None) is None
    assert check("TARGET_TIMEOUT", "not_sent", None) == 0.5
    assert check("TARGET_TIMEOUT", "not_sent", None, count=2) is None
    for code in ("INVALID_RESPONSE", "DESTINATION_DENIED", "TARGET_AUTH_FAILED", "INVALID_REQUEST"):
        assert check(code, "sent", 403, verified=True) is None


@pytest.mark.parametrize("protocol", ["http", "openai_compatible", "agent"])
def test_batch_execution(catalog: tuple[Settings, dict[str, str]], protocol: str) -> None:
    settings, tokens = catalog
    calls: list[tuple[str, str, dict[str, Any], str | None]] = []
    attempts: Counter[str] = Counter()
    verdict = json.dumps(
        {"score": 1, "passed": True, "dimensions": {"accuracy": 1}, "reason": "correct"}
    )

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            pass

        def do_POST(self) -> None:
            data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            key = self.headers["Idempotency-Key"]
            calls.append((self.path, key, data, self.headers.get("X-Trace-Write")))
            attempts[key] += 1
            status = 200
            if self.path == "/judge":
                assert self.headers["Authorization"] == "Bearer local-judge"
                response = {"model": "model-a", "choices": [{"message": {"content": verdict}}]}
            else:
                query = data.get("query") or data["messages"][0]["content"]
                response = {
                    "answer": query,
                    "final_answer": query,
                    "choices": [{"message": {"content": query}}],
                }
                if protocol == "agent":
                    response["events"] = [{"input": {"password": "private-telemetry"}}]
                if protocol == "http" and query == "one" and attempts[key] == 1:
                    status = 429
            self.send_response(status)
            self.send_header("Retry-After", "1")
            self.end_headers()
            self.wfile.write(json.dumps(response).encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_port
        base = f"http://127.0.0.1:{port}"
        gate = TargetRule(scheme="http", host="127.0.0.1", port=port, networks=["127.0.0.1/32"])
        account = JudgeAccount(
            endpoint=base + "/judge", destination=gate, models=["model-a"], max_parallel=1, rpm=120
        )
        settings = settings.model_copy(
            update={
                "targets": {"shop": [gate, gate.model_copy(update={"port": 12345})]},
                "judge_accounts": {"acct": account},
                "secrets": {"judge:acct": {"judge-ref": {"v1": SecretStr("local-judge")}}},
            }
        )
        with TestClient(
            create_app(settings), headers={"Authorization": "Bearer " + tokens["curator"]}
        ) as api:
            f = prepare(api, PHASE2_BUILD if protocol == "agent" else BUILD)
            p = f["project"]["id"]
            assert api.patch(ROOT + "/projects/" + p, json={"rpm": 1000000}).is_success
            pv = create(
                api,
                f"/projects/{p}/versions",
                label="batch",
                deployment_ref="build-2",
                adapter={
                    "kind": protocol,
                    "endpoint": base + "/target",
                    "probe_endpoint": base + "/ready",
                    "options": {"model": "chat-v1"} if protocol == "openai_compatible" else {},
                },
            )
            settings.trace_target_versions.add(UUID(pv["id"]))
            ev = create(api, "/evaluators", kind="rule", slug="nonempty", name="非空")
            rule = create(
                api,
                f"/evaluators/{ev['id']}/versions",
                version="1",
                impl_key="rules",
                build=BUILD,
                config={"rules": [{"path": "answer", "op": "nonempty", "reason": "没有答案"}]},
            )
            bus = create(api, "/evaluators", kind="business", slug="policy", name="业务")
            business = create(
                api,
                f"/evaluators/{bus['id']}/versions",
                version="1",
                impl_key="approved_policy",
                build=BUILD,
                config={},
            )
            # Business needs a declared policy on at least one frozen input.
            path = "/datasets/" + f["dataset"]["id"]
            draft = create(api, path + "/versions", action="draft")
            for key in ("one", "two", "three"):
                create(
                    api,
                    path + "/cases",
                    draft_id=draft["id"],
                    case_key=key,
                    input={"query": key},
                    extensions={"policy_id": "A"} if key == "one" else {},
                    expected={"answer": key, "private_marker": "target-must-never-get-this"},
                )
            reviewed = api.get(ROOT + path).json()["data"]["draft"]
            dv = create(
                api,
                path + "/versions",
                action="publish",
                draft_id=draft["id"],
                reviewed_digest=reviewed["digest"],
            )
            task = create(
                api,
                "/evaluations/tasks",
                name="批量验收",
                project_version_id=pv["id"],
                dataset_version_id=dv["id"],
                evaluator_version_ids=[f["root"]["id"], rule["id"], business["id"]],
                policy={
                    "adapter_max_attempts": 2,
                    "initial_backoff_ms": 10,
                    "jitter": False,
                    "approved_policy_ids": ["A"],
                },
            )
            resp = api.post(
                ROOT + f"/evaluations/tasks/{task['id']}/runs", headers={"Idempotency-Key": "run"}
            )
            assert resp.status_code == 202, resp.text
            run_id = UUID(resp.json()["data"]["id"])

            async def exercise() -> None:
                resources = Resources(settings)
                cfg = WorkerSettings(id="first")
                runner = CaseRunner(settings)
                try:
                    await initialize(resources, cfg)
                    async with asyncio.timeout(25):
                        while True:
                            lease = await claim(resources, cfg)
                            if lease:
                                await runner(resources, lease)
                                await release(resources, lease)
                                # Each next owner must continue from persisted products.
                                cfg.id = "second" if cfg.id == "first" else "first"
                            else:
                                await reconcile(resources, 5)
                                async with resources.transaction() as db:
                                    status = await db.scalar(
                                        text("SELECT status FROM evaluation_runs WHERE id=:r"),
                                        {"r": run_id},
                                    )
                                if status in ("COMPLETED", "FAILED", "PARTIAL_FAILED"):
                                    assert status == "COMPLETED", status
                                    break
                                await asyncio.sleep(0.02)
                    async with resources.transaction() as db:
                        assert not await db.scalar(
                            text(
                                "SELECT count(*) FROM execution_attempts "
                                "WHERE trace_verifier IS NOT NULL"
                            )
                        )
                        if protocol == "http":
                            gap = await db.scalar(
                                text("""SELECT extract(epoch FROM b.started_at-a.finished_at)
                                FROM execution_attempts a JOIN execution_attempts b
                                USING(logical_step_key)
                                WHERE a.error_code='TARGET_RATE_LIMIT' AND b.status='SUCCEEDED'""")
                            )
                            assert gap >= 1
                        records = (
                            (
                                await db.execute(
                                    text("""SELECT a.id,a.epoch,a.execution_id
                            FROM execution_attempts a WHERE stage='ADAPTER'""")
                                )
                            )
                            .mappings()
                            .all()
                        )
                    for v in records:
                        scope = TraceWriteScope(
                            project_id=UUID(p),
                            run_id=run_id,
                            execution_id=v["execution_id"],
                            attempt_id=v["id"],
                            epoch=v["epoch"],
                        )
                        bearer = next(
                            t
                            for path, _, _, t in calls
                            if path == "/target" and t and t.startswith(str(v["id"]))
                        )
                        assert not await AttemptTraceIssuer(resources).valid(scope, bearer)
                finally:
                    await resources.close()

            asyncio.run(exercise())
            details = api.get(ROOT + f"/evaluations/runs/{run_id}").json()["data"]
            for metric, numerator, denominator in [
                ("case_success_rate", 3, 3),
                ("score_execution_success_rate", 16, 16),
                ("score_coverage", 16, 18),
                ("pass_rate", 15, 16),
            ]:
                assert details["metrics"][metric] == {
                    "numerator": numerator,
                    "denominator": denominator,
                    "value": numerator / denominator,
                }
            report = api.get(ROOT + f"/reports/runs/{run_id}").json()["data"]
            assert report["summary"]["metrics"] == details["metrics"]
            assert report["summary"]["score_counts"]["SKIPPED"] == 2
            assert report["summary"]["total_tokens"] is None
            assert any(v["members"] for v in report["summary"]["evaluators"])
            assert details["metrics"]["case_success_rate"]["value"] == 1
            assert details["metrics"]["score_coverage"]["numerator"] == 16
            assert details["metrics"]["score_coverage"]["denominator"] == 18
            assert details["metrics"]["by_version"][business["id"]]["skipped"] == 2
            results = api.get(ROOT + f"/evaluations/runs/{run_id}/results").json()["data"]["items"]
            assert len(results) == 3
            for item in results:
                assert "private-telemetry" not in json.dumps(item["prediction"])
                hist = {a["id"]: a for a in item["attempts"]}
                assert hist[item["prediction_attempt_id"]]["status"] == "SUCCEEDED"
                assert len(item["scores"]) == 6
                assert all(
                    hist[s["created_from_attempt_id"]]["evaluator_version_id"]
                    == s["evaluator_version_id"]
                    for s in item["scores"]
                )
            groups = api.get(ROOT + f"/case-executions/{item['id']}/evidence").json()["data"][
                "scores"
            ]
            versions = {g["evaluator_version_id"]: g for g in groups}
            parent = versions[f["root"]["id"]]
            assert not parent["items"] and len(parent["children"]) == 2
            for child in parent["children"]:
                original = versions[child["evaluator_version_id"]]
                assert child["score_id"] == original["score_id"]
                assert child["weight"] == child["position"] + 1
                child_path = ROOT + f"/evaluations/scores/{child['score_id']}/evidence"
                assert api.get(child_path).json()["data"] == original
            for leaf in (rule, f["exact"], f["judge"]):
                citation = versions[leaf["id"]]["items"][0]
                assert citation["status"] == "AVAILABLE" and citation["excerpt"] == item["case_key"]
            assert len([c for c in calls if c[0] == "/judge"]) == 3
            assert len([c for c in calls if c[0] == "/target"]) == 3 + (protocol == "http")
            for path, key, data, bearer in calls:
                assert len(key) == 64
                if path == "/target":
                    assert bearer and bearer not in json.dumps(results)
                    assert not any(
                        s in json.dumps(data)
                        for s in ("private_marker", "private-prompt", "expected")
                    )
                else:
                    assert bearer is None
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
