import asyncio
import json
import threading
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest
from fastapi.testclient import TestClient
from test_evaluation_runs import ROOT, create, prepare
from test_evidence import check_evidence
from test_trace_write import Window, configured, event
from test_trace_write import window as window

from app.api.main import create_app
from app.infrastructure.resources import Resources, Settings
from app.worker.execution import CaseRunner
from app.worker.scheduler import claim, initialize
from app.worker.settings import WorkerSettings
from trace_sdk.client import TraceSession


@pytest.mark.parametrize("path", ["retry", "overflow", "cancel", "offline"])
def test_sdk_delivery(window: Window, path: str) -> None:
    w = window
    batches: list[bytes] = []
    final = []

    class Proxy(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            pass

        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers["Content-Length"]))
            response = w.api.post(
                self.path, content=body, headers={"Authorization": self.headers["Authorization"]}
            )
            status = response.status_code
            if self.path.endswith(":batch"):
                batches.append(body)
                if path == "offline" or path == "retry" and len(batches) == 1:
                    status = 503
            else:
                final.append(json.loads(body))
            self.send_response(status)
            self.end_headers()
            self.wfile.write(response.content)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Proxy)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    async def exercise() -> None:
        sdk = TraceSession(
            f"http://127.0.0.1:{server.server_port}",
            w.trace,
            w.token,
            capacity=1 if path == "overflow" else 12,
        )
        root = sdk.add("AGENT_STEP", "plan", input={"api_key": "secret-fixture"})
        assert root
        if path == "overflow":
            assert sdk.add("CUSTOM", "dropped") is None
        else:
            assert sdk.add(
                "TOOL_CALL", "lookup", parent_event_key=root, attributes={"tool_name": "fetch"}
            )
        if path == "cancel":
            with pytest.raises(asyncio.CancelledError):
                async with sdk:
                    raise asyncio.CancelledError
        else:
            assert await sdk.flush() == (path == "retry")
        await sdk.finish()
        assert sdk.status == ("COMPLETE" if path == "retry" else "PARTIAL")

    try:
        asyncio.run(exercise())
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    assert b"secret-fixture" not in b"".join(batches)
    if path == "retry":
        assert len(batches) == 2 and batches[0] == batches[1]
        assert w.api.get(w.url).json()["data"]["event_count"] == 2
    if path == "offline":
        assert len(batches) == 3 and len(set(batches)) == 1
    assert len(final) == 1


def test_paginated_timeline_and_private_detail(window: Window) -> None:
    w = window
    root = event(1)
    child = dict(event(2, "step-1"), event_type="TOOL_CALL", output={"result_summary": "found"})
    assert w.write(root, child).is_success

    def query(**params: Any) -> list[dict[str, Any]]:
        return list(
            w.api.get(w.url + "/events", params={"summary_only": True, **params}).json()["data"][
                "items"
            ]
        )

    roots = query(roots=True)
    assert len(roots) == 1 and roots[0]["has_children"] and roots[0]["input_tokens"] == 10
    found = query(parent_id=roots[0]["id"], event_type="TOOL_CALL", started_from=root["started_at"])
    assert len(found) == 1 and not {"input", "output", "attributes", "resource"}.intersection(
        found[0]
    )
    assert not query(started_to="2020-01-01T00:00:00Z")
    detail = w.url + "/events/" + found[0]["id"]
    assert w.api.get(detail).json()["data"]["output"] == {"result_summary": "found"}
    assert w.api.get(
        detail, headers={"Authorization": "Bearer " + w.tokens["rag"]}
    ).status_code in {403, 404}
    assert w.api.get(detail, headers=w.headers).status_code == 401


@pytest.mark.parametrize("kind", ["agent", "openai_compatible"])
def test_embedded_version_contract(
    catalog: tuple[Settings, dict[str, str]], kind: str, request: pytest.FixtureRequest
) -> None:
    settings, tokens = catalog

    class Target(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            pass

        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            query = body.get("query") or body["messages"][0]["content"]
            stamp = datetime.now(UTC).isoformat()
            entry = dict(
                event(1),
                started_at=stamp,
                output={
                    "api_key": "private-value",
                    "document_refs": ["doc-1"],
                    "contact": "person@example.org",
                    "long": "x" * 300,
                    "output_ref": "prediction.answer",
                },
            )
            trace: dict[str, Any] = {
                "finalize": {"outcome": "COMPLETE", "last_sequence": 1, "contiguous": True}
            }
            record: dict[str, Any] = {"extensions": {"trace": trace}}
            if kind == "openai_compatible":
                trace["finalize"].update(contiguous=False, event_count=1, last_sequence=8)
                entry["sequence"] = 8
            ty = "FINAL_OUTPUT" if query == "one" else "MODEL_CALL"
            if kind == "agent":
                legacy = {
                    "source_event_key": "span",
                    "metadata": {"status": "SUCCEEDED"},
                    "sequence": 1,
                    "event_type": ty,
                    "occurred_at": stamp,
                    "output": entry["output"],
                    "input_tokens": 12,
                }
                record.update(final_answer=query, events=[legacy if query != "three" else {}])
            else:
                trace["events"] = [
                    dict(entry, event_type=ty, status="UNKNOWN") if query != "three" else {}
                ]
                record["choices"] = [{"message": {"content": query}}]
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps(record).encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Target)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        settings = configured(settings, server.server_port)
        with TestClient(
            create_app(settings), headers={"Authorization": "Bearer " + tokens["curator"]}
        ) as api:
            f = prepare(api)
            base = f"http://127.0.0.1:{server.server_port}"
            version = create(
                api,
                "/projects/" + f["project"]["id"] + "/versions",
                label="embedded",
                deployment_ref="local",
                adapter={
                    "kind": kind,
                    "endpoint": base + "/ask",
                    "probe_endpoint": base + "/ready",
                    "options": {"model": "local"} if kind == "openai_compatible" else {},
                },
                trace_capabilities={
                    "mode": "embedded",
                    "contiguous": kind != "openai_compatible",
                    "event_types": ["MODEL_CALL", "FINAL_OUTPUT"],
                    "required_events": ["FINAL_OUTPUT"],
                },
            )
            assert version["trace_capabilities"]["mode"] == "embedded"
            assert api.patch(
                ROOT + "/projects/" + f["project"]["id"], json={"rpm": 1000000}
            ).is_success
            task = create(
                api,
                "/evaluations/tasks",
                name="embedded",
                policy={"approved_policy_ids": ["A"]},
                project_version_id=version["id"],
                dataset_version_id=f["dv"]["id"],
                evaluator_version_ids=[f["exact"]["id"]],
            )
            rid = api.post(
                ROOT + f"/evaluations/tasks/{task['id']}/runs", headers={"Idempotency-Key": "run"}
            ).json()["data"]["id"]

            async def execute() -> None:
                resources = Resources(settings)
                cfg = WorkerSettings()
                runner = CaseRunner(settings)
                try:
                    await initialize(resources, cfg)
                    async with asyncio.timeout(15):
                        while (lease := await claim(resources, cfg)) is not None:
                            await runner(resources, lease)
                finally:
                    await resources.close()

            asyncio.run(execute())
            items = api.get(ROOT + f"/evaluations/runs/{rid}/results").json()["data"]["items"]
            assert len(items) == 3
            for item in items:
                key = item["case_key"]
                assert item["prediction"]["answer"] == key
                assert "private-value" not in json.dumps(item)
                trace_id = item["trace_ids"][0]
                summary = api.get(ROOT + "/traces/" + trace_id).json()["data"]
                assert (
                    summary["status"]
                    == {"one": "COMPLETE", "two": "PARTIAL", "three": "MISSING"}[key]
                )
                if key == "two":
                    assert summary["close_reason"] == "REQUIRED_EVENT_MISSING"
                assert summary["capabilities"] == version["trace_capabilities"]
                events = api.get(ROOT + "/traces/" + trace_id + "/events").json()["data"]["items"]
                assert "private-value" not in json.dumps(events)
            if kind == "agent":
                check_evidence(api, settings, items, f, tokens["rag"], request)
            else:
                case = next(i for i in items if i["case_key"] == "one")
                result = api.get(ROOT + f"/evaluations/scores/{case['scores'][0]['id']}/evidence")
                assert [e["source_type"] for e in result.json()["data"]["items"]] == ["PREDICTION"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
