import asyncio
import csv
import io
import json
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
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
            assert summary["missing"] == 3 and summary["evaluators"][0]["errors"] == 0
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
            assert len(read(rp + b + "/bad-cases", params={"signal": "token_limit"})["items"]) == 3
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
            assert {"project_version", "dataset_version", "evaluator_dag"} <= set(
                third["differences"]
            )
            assert not third["comparable"] and third["changes"] is None and third["deltas"] is None
            assert read(rp + z)["summary"]["total_tokens"] is None
            assert api.get(rp + z + "/bad-cases", params={"baseline_id": a}).status_code == 409
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join()
