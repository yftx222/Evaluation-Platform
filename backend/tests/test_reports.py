import asyncio
import csv
import io
import json
import os
import subprocess
import sys
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import text
from test_evaluation_runs import ROOT, create, prepare
from test_trace_write import configured

from app.api.main import create_app
from app.contracts.reports import Rate, ReportPolicy
from app.infrastructure.resources import Resources, Settings
from app.worker.execution import CaseRunner
from app.worker.scheduler import claim, initialize, reconcile
from app.worker.settings import WorkerSettings


def test_reports_and_comparison(catalog: tuple[Settings, dict[str, str]]) -> None:
    settings, tokens = catalog
    poor: set[str] = set()

    class Target(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            pass

        def do_POST(self) -> None:
            data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            query = data["messages"][0]["content"]
            result = {
                "choices": [{"message": {"content": "wrong" if query in poor else query}}],
                "model": "local",
                "model_revision": "r1",
                "usage": {"prompt_tokens": 0, "completion_tokens": 4},
            }
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps(result).encode())

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Target)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        settings = configured(settings, srv.server_port)

        async def drain() -> None:
            res = Resources(settings)
            try:
                cfg = WorkerSettings()
                runner = CaseRunner(settings)
                await initialize(res, cfg)
                async with asyncio.timeout(20):
                    while lease := await claim(res, cfg):
                        await runner(res, lease)
                    await reconcile(res, 20)
            finally:
                await res.close()

        with TestClient(
            create_app(settings), headers={"Authorization": "Bearer " + tokens["curator"]}
        ) as api:
            f = prepare(api)
            pid = f["project"]["id"]
            assert api.patch(ROOT + "/projects/" + pid, json={"rpm": 1000000}).is_success
            origin = f"http://127.0.0.1:{srv.server_port}"
            adapter = {
                "kind": "openai_compatible",
                "options": {"model": "local"},
                "endpoint": origin + "/ask",
                "probe_endpoint": origin + "/ready",
            }
            version = create(
                api,
                f"/projects/{pid}/versions",
                label="reports-1",
                deployment_ref="r1",
                adapter=adapter,
                trace_capabilities={"mode": "disabled", "event_types": []},
            )
            settings.report_policies[UUID(version["id"])] = ReportPolicy(
                max_case_tokens=3,
                target=Rate(
                    model="local",
                    provider="local-provider",
                    revision="r1",
                    input_per_million=1,
                    output_per_million=2,
                ),
            )
            ds = create(api, "/datasets", slug="report-cases", name="报告题库")
            path = "/datasets/" + ds["id"]
            for key in ("-123", "two", "three"):
                create(
                    api,
                    path + "/cases",
                    draft_id=ds["draft_id"],
                    case_key=key,
                    input={"query": key, "password": "should-never-be-exported"},
                    expected={"answer": key},
                    category="qa",
                    difficulty="reader@example.org",
                    tags=["test", "shared"],
                )
            draft = api.get(ROOT + path).json()["data"]["draft"]
            dv = create(
                api,
                path + "/versions",
                action="publish",
                draft_id=draft["id"],
                reviewed_digest=draft["digest"],
            )
            body = {
                "name": "reports",
                "project_version_id": version["id"],
                "dataset_version_id": dv["id"],
                "evaluator_version_ids": [f["exact"]["id"]],
            }
            task = create(api, "/evaluations/tasks", **body)
            settings.report_policies.clear()

            def start(t: dict[str, Any]) -> str:
                response = api.post(
                    ROOT + f"/evaluations/tasks/{t['id']}/runs",
                    headers={"Idempotency-Key": uuid4().hex},
                )
                assert response.status_code == 202, response.text
                return str(response.json()["data"]["id"])

            rp = ROOT + "/reports/runs/"

            def read(url: str, **args: Any) -> dict[str, Any]:
                response = api.get(url, **args)
                assert response.status_code == 200, response.text
                return dict(response.json()["data"])

            a = start(task)
            assert api.get(rp + a).status_code == 409
            poor.add("two")
            asyncio.run(drain())

            # Use normal INSERTs with valid UUIDs that contain a phone-like substring.
            async def set_test_id_default() -> None:
                resources = Resources(settings)
                try:
                    async with resources.transaction() as db:
                        await db.execute(
                            text("""ALTER TABLE case_executions ALTER COLUMN id SET DEFAULT
                                (left(gen_random_uuid()::text,24) || 'a13800138000')::uuid""")
                        )
                finally:
                    await resources.close()

            asyncio.run(set_test_id_default())
            b = start(task)
            poor.clear()
            poor.add("three")
            asyncio.run(drain())
            result = read(rp + b)
            summary = result["summary"]
            assert summary["metrics"] == read(ROOT + "/evaluations/runs/" + b)["metrics"]
            assert summary["total_tokens"] == 12 and summary["input_known"] == 0
            assert summary["metrics"]["pass_rate"] == {
                "numerator": 2,
                "denominator": 3,
                "value": 2 / 3,
            }
            assert summary["evaluators"][0]["errors"] == 0
            assert summary["trace_mode"] == "disabled"
            assert (
                summary["not_collected"],
                summary["missing"],
                summary["partial"],
                summary["complete"],
            ) == (3, 0, 0, 0)
            assert summary["trace_complete_rate"] == {
                "numerator": 0,
                "denominator": 0,
                "value": None,
            }
            disabled = read(ROOT + f"/evaluations/runs/{b}/results")["items"]
            assert all(
                c["trace_status"] == "NOT_COLLECTED" and not c["trace_ids"] for c in disabled
            )
            assert all(a["trace_id"] is None for c in disabled for a in c["attempts"])

            async def allocated() -> int:
                res = Resources(settings)
                try:
                    async with res.transaction() as db:
                        return int(
                            await db.scalar(
                                text("SELECT count(*) FROM traces WHERE run_id=:r"), {"r": UUID(b)}
                            )
                            or 0
                        )
                finally:
                    await res.close()

            assert asyncio.run(allocated()) == 0
            assert result["usage"]["estimated_cost"] == 24 / 1000000
            assert result["usage"]["models"][0]["provider"] == "local-provider"
            assert (
                next(t for t in result["process"]["timings"] if t["stage"] == "TOOL_CALL")[
                    "availability"
                ]
                == "NOT_APPLICABLE"
            )
            groups = read(rp + b + "/metrics", params={"dimension": "tag", "limit": 1})
            assert groups["items"][0]["cases"] == 3 and groups["next_offset"] == 1
            comp = read(ROOT + "/reports/compare", params={"run_id": [a, b]})["comparisons"][0]
            assert comp["comparable"] and comp["deltas"]["pass_rate"] == 0
            assert comp["changes"] == {"matched": 3, "new_failure": 1, "fixed": 1, "regressed": 1}
            for signal, key in [("new_failure", "three"), ("fixed", "two"), ("regressed", "three")]:
                page = read(rp + b + "/bad-cases", params={"signal": signal, "baseline_id": a})
                assert [v["case_key"] for v in page["items"]] == [key]
                assert read(page["items"][0]["evidence_url"])["scores"]
                if key == "three":
                    assert page["items"][0]["execution_id"].endswith("a13800138000")
            assert len(read(rp + b + "/bad-cases", params={"signal": "token_limit"})["items"]) == 3
            expected_keys = {"-123", "two", "three"}
            expected_signals = {
                "all": expected_keys,
                "any": expected_keys,
                "case_failed": set(),
                "score_error": set(),
                "low_score": {"three"},
                "trace_partial": set(),
                "trace_missing": set(),
                "new_failure": {"three"},
                "fixed": {"two"},
                "regressed": {"three"},
                "timed_out": set(),
                "child_failed": set(),
                "token_limit": expected_keys,
                "latency_limit": set(),
            }
            for baseline in (None, a):
                for signal, keys in expected_signals.items():
                    args = {"signal": signal, **({"baseline_id": baseline} if baseline else {})}
                    if not baseline and signal in ("new_failure", "fixed", "regressed"):
                        for route in ("/bad-cases", "/export"):
                            res = api.get(rp + b + route, params=args)
                            assert (
                                res.status_code == 422
                                and res.json()["error"]["code"] == "BASELINE_REQUIRED"
                            )
                        continue
                    items = []
                    offset = 0
                    while True:
                        part = read(
                            rp + b + "/bad-cases", params={**args, "offset": offset, "limit": 1}
                        )
                        items.extend(part["items"])
                        if part["next_offset"] is None:
                            break
                        offset += 1
                        assert part["next_offset"] == offset <= len(keys)
                    assert {c["case_key"] for c in items} == keys and len(items) == len(keys)
                    assert all(
                        c["trace_status"] == "NOT_COLLECTED" and c["trace_id"] is None
                        for c in items
                    )
                    exported_cases = api.get(rp + b + "/export", params=args).json()["cases"]
                    assert exported_cases == items
                    csv_response = api.get(rp + b + "/export", params={**args, "format": "csv"})
                    assert csv_response.is_success
                    rows = csv.DictReader(io.StringIO(csv_response.content.decode("utf-8-sig")))
                    assert [v["execution_id"] for v in rows if v["record_type"] == "case"] == [
                        v["execution_id"] for v in items
                    ]
            private_group = read(rp + b + "/metrics", params={"dimension": "difficulty"})
            assert private_group["items"][0]["label"] == "[REDACTED]"
            wire = api.get(rp + b + "/export")
            assert "should-never-be-exported" not in wire.text
            exported = wire.json()
            assert len(exported["cases"]) == 3 and exported["report"]["summary"] == summary
            tab = api.get(rp + b + "/export", params={"format": "csv", "signal": "all"})
            assert "'-123" in [
                v["case_key"] for v in csv.DictReader(io.StringIO(tab.content.decode("utf-8-sig")))
            ]
            assert api.get(
                rp + b, headers={"Authorization": "Bearer " + tokens["rag"]}
            ).status_code in (403, 404)
            credential = create(
                api,
                f"/projects/{pid}/api-keys",
                name="report",
                scopes=["results:read"],
                expires_at=(datetime.now(UTC) + timedelta(days=1)).isoformat(),
            )
            headers = {"Authorization": "ApiKey " + credential["secret"]}
            assert api.get(rp + b, headers=headers).status_code == 200
            assert api.get(rp + b + "/export", headers=headers).status_code == 403
            assert (
                api.get(
                    ROOT + "/reports/compare", params={"run_id": [a, b]}, headers=headers
                ).status_code
                == 403
            )
            # The account has no secret in this fixture; its real Judge and Composite produce ERROR.
            failing = create(
                api, "/evaluations/tasks", **{**body, "evaluator_version_ids": [f["root"]["id"]]}
            )
            err_run = start(failing)
            asyncio.run(drain())
            errors = read(rp + err_run)["summary"]
            assert errors["score_counts"]["ERROR"] > 0 and errors["failed_cases"] == 3
            judge = next(v for v in errors["evaluators"] if v["id"] == f["judge"]["id"])
            assert judge["errors"] == 3 and judge["ok"] == 0 and judge["mean_score"] is None
            assert judge["pass_rate"]["value"] is None
            for signal in ("score_error", "child_failed", "case_failed"):
                assert (
                    len(read(rp + err_run + "/bad-cases", params={"signal": signal})["items"]) == 3
                )

            pv = create(
                api,
                f"/projects/{pid}/versions",
                label="reports-2",
                deployment_ref="r2",
                adapter=adapter,
            )
            later = create(
                api,
                "/evaluations/tasks",
                **{
                    **body,
                    "project_version_id": pv["id"],
                    "dataset_version_id": f["dv"]["id"],
                    "evaluator_version_ids": [f["root"]["id"]],
                },
            )
            z = start(later)
            assert api.post(ROOT + f"/evaluations/runs/{z}/cancel", json={}).is_success
            if read(ROOT + f"/evaluations/runs/{z}")["status"] != "CANCELLED":
                asyncio.run(drain())
            third = read(ROOT + "/reports/compare", params={"run_id": [a, b, z]})["comparisons"][1]
            assert {
                "project_version",
                "dataset_version",
                "evaluator_dag",
                "trace_capabilities",
            } <= set(third["differences"])
            assert not third["comparable"] and third["changes"] is None and third["deltas"] is None
            assert read(rp + z)["summary"]["total_tokens"] is None
            assert api.get(rp + z + "/bad-cases", params={"baseline_id": a}).status_code == 409
            # Exercise a populated upgrade from 0010 with genuine legacy disabled-mode traces.
            historical = start(task)

            def migrate(command: str, revision: str) -> None:
                subprocess.run(
                    [sys.executable, "-m", "alembic", command, revision],
                    check=True,
                    env={
                        **os.environ,
                        "EVAL_DATABASE_URL": settings.database_url.get_secret_value(),
                    },
                )

            migrate("downgrade", "0010_trace_archives")

            async def create_legacy_attempt() -> str:
                res = Resources(settings)
                try:
                    lease = await claim(res, WorkerSettings())
                    assert lease and lease.run == UUID(historical)
                    await CaseRunner(settings)(res, lease)
                    async with res.transaction() as db:
                        return str(
                            await db.scalar(
                                text("SELECT id FROM traces WHERE attempt_id=:a"),
                                {"a": lease.attempt},
                            )
                        )
                finally:
                    await res.close()

            legacy_id = asyncio.run(create_legacy_attempt())
            before = read(ROOT + "/traces/" + legacy_id)
            assert before["status"] == "MISSING"
            migrate("upgrade", "head")
            asyncio.run(drain())
            assert read(ROOT + "/traces/" + legacy_id) == before
            assert read(rp + b) == result
            history = read(rp + historical)["summary"]
            assert history["not_collected"] == 3 and history["missing"] == 0
            historical_cases = read(ROOT + f"/evaluations/runs/{historical}/results")["items"]
            assert all(
                c["trace_status"] == "NOT_COLLECTED" and not c["trace_ids"]
                for c in historical_cases
            )
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join()
