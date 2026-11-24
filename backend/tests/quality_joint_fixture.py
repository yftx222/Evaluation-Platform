"""Run a peer-reviewed test sample and regrade its frozen answer."""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx
from test_batch_console import process

from app.plugins.evaluators import BUILD


def run_reviewed_fixture(
    owner: httpx.Client,
    project: str,
    dataset_version: str,
    target_port: int,
    env: dict[str, str],
    inbox: str,
    recipient: str,
) -> str:
    target_calls = []

    class Target(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            target_calls.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"answer":"fixture"}')

    def post(path: str, body: dict[str, Any]) -> dict[str, Any]:
        res = owner.post(path, json=body)
        assert res.status_code == 201, res.text
        return dict(res.json()["data"])

    dest = f"http://127.0.0.1:{target_port}"
    candidate = post(
        f"projects/{project}/versions",
        dict(
            label="joint-candidate",
            deployment_ref="joint-v1",
            trace_capabilities={"mode": "disabled"},
            adapter={"endpoint": dest + "/ask", "probe_endpoint": dest + "/ready"},
        ),
    )
    evaluator = post("evaluators", dict(kind="rule", slug="joint-rule", name="Joint fixture rule"))

    def publish_rule(label: str, expected: str) -> str:
        return str(
            post(
                f"evaluators/{evaluator['id']}/versions",
                dict(
                    version=label,
                    impl_key="rules",
                    build=BUILD,
                    config={"rules": [{"path": "answer", "op": "exact", "expected": expected}]},
                ),
            )["id"]
        )

    first = publish_rule("initial", "fixture")
    task = post(
        "evaluations/tasks",
        dict(
            name="reviewed fixture",
            project_version_id=candidate["id"],
            dataset_version_id=dataset_version,
            evaluator_version_ids=[first],
        ),
    )
    started = owner.post(
        f"projects/{project}/regression-batches",
        json={"task_id": task["id"], "project_version_id": candidate["id"], "environment": "test"},
        headers={"Idempotency-Key": "joint-trigger"},
    )
    assert started.is_success, started.text
    batch = started.json()["data"]
    bid = str(batch["regression_batch_id"])
    with ThreadingHTTPServer(("127.0.0.1", target_port), Target) as target:
        thread = threading.Thread(target=target.serve_forever, daemon=True)
        thread.start()
        try:
            worker_env = {
                **env,
                "EVAL_QUALITY_RECEIVERS": json.dumps(
                    {project: {"test": {"endpoint": inbox, "credential": recipient}}}
                ),
            }
            with process(["-m", "app.worker.main"], worker_env):
                deadline = time.monotonic() + 35
                while time.monotonic() < deadline:
                    report = owner.get(f"projects/{project}/regression-batches/{bid}").json()[
                        "data"
                    ]["report"]
                    if report:
                        assert report["gate"] == "PASSED", report
                        break
                    time.sleep(0.1)
                else:
                    raise AssertionError("Published review sample missed its regression gate")
                run = batch["trials"][0]["run_id"]
                case = owner.get(f"evaluations/runs/{run}/results").json()["data"]["items"][0]
                second = publish_rule("revised", "more precise expected answer")
                res = owner.post(
                    f"projects/{project}/case-executions/{case['id']}/rescores",
                    headers={"Idempotency-Key": "revised-rule"},
                    json={"evaluator_version_id": second},
                )
                assert res.json()["data"]["result"]["score"]["passed"] is False
                assert case["scores"][0]["passed"] and len(target_calls) == 3
                while time.monotonic() < deadline:
                    status = owner.get(
                        f"projects/{project}/quality-deliveries", params={"batch_id": bid}
                    )
                    assert status.is_success, status.text
                    if any(r["state"] == "DELIVERED" for r in status.json()["data"]["items"]):
                        break
                    time.sleep(0.1)
                else:
                    raise AssertionError("AIOps batch receipt missing")
        finally:
            target.shutdown()
            thread.join()
    return bid
