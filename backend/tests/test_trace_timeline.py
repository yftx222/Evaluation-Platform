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
from app.worker.scheduler import claim, initialize, reconcile
from app.worker.settings import WorkerSettings
from trace_sdk.client import TraceSession


@pytest.mark.parametrize(
    "path",
    [
        "retry",
        "rate_limit",
        "overflow",
        "cancel",
        "agent_error",
        "offline",
        "unavailable",
        "timeout",
    ],
)
def test_sdk_delivery(window: Window, path: str) -> None:
    w = window
    batches: list[bytes] = []
    final = []
    unblock = threading.Event()

    class Proxy(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            pass

        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers["Content-Length"]))
            if path == "unavailable" and self.path.endswith(":batch"):
                batches.append(body)
                self.send_response(500)
                self.end_headers()
                return
            if path == "timeout" and self.path.endswith(":batch"):
                # Hold the transport until the SDK's own deadline has expired.
                unblock.wait(6)
                return
            response = w.api.post(
                self.path, content=body, headers={"Authorization": self.headers["Authorization"]}
            )
            status = response.status_code
            if self.path.endswith(":batch"):
                batches.append(body)
                if path == "offline" or path in {"retry", "rate_limit"} and len(batches) == 1:
                    status = 429 if path == "rate_limit" else 500
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
            timeout=0.6 if path == "timeout" else 5,
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
        elif path == "agent_error":
            with pytest.raises(ValueError, match="agent error"):
                async with sdk:
                    raise ValueError("agent error")
        elif path == "timeout":
            async with sdk:
                pass
            assert sdk.status == "PARTIAL" and not sdk.finalize_confirmed
        else:
            assert await sdk.flush() == (path in {"retry", "rate_limit"})
        if path in {"cancel", "agent_error", "timeout"}:
            assert sdk.finish().done()
        waiter = sdk.finish()
        waiter.cancel()
        await asyncio.gather(sdk.finish(), sdk.finish())
        assert sdk.finish().done()
        if path == "unavailable":
            assert sdk.confirmed_status == "MISSING"
            assert w.api.get(w.url).json()["data"]["event_count"] == 0
        assert sdk._token == "" and sdk.finalize_confirmed == (path != "timeout")
        assert sdk.status == ("COMPLETE" if path in {"retry", "rate_limit"} else "PARTIAL")

    try:
        asyncio.run(exercise())
    finally:
        unblock.set()
        server.shutdown()
        server.server_close()
        thread.join()
    assert b"secret-fixture" not in b"".join(batches)
    if path in {"retry", "rate_limit"}:
        assert len(batches) == 2 and batches[0] == batches[1]
        assert w.api.get(w.url).json()["data"]["event_count"] == 2
    if path in {"offline", "unavailable"}:
        assert len(batches) == 3 and len(set(batches)) == 1
    assert len(final) == int(path != "timeout")


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


@pytest.mark.parametrize(
    ("kind", "corrupt"),
    [("agent", False), ("openai_compatible", False), ("openai_compatible", True)],
)
def test_embedded_version_contract(
    catalog: tuple[Settings, dict[str, str]],
    kind: str,
    corrupt: bool,
    request: pytest.FixtureRequest,
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
            ty = "FINAL_OUTPUT" if query == "one" or corrupt else "MODEL_CALL"
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
                if corrupt and query == "two":
                    # Even if the required event was received, a malformed sibling forces PARTIAL.
                    trace["events"].append({})
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
                        await reconcile(resources, 20)
                finally:
                    await resources.close()

            asyncio.run(execute())
            items = api.get(ROOT + f"/evaluations/runs/{rid}/results").json()["data"]["items"]
            assert len(items) == 3
            report = api.get(ROOT + f"/reports/runs/{rid}").json()["data"]["summary"]
            assert (
                report["trace_mode"] == "embedded"
                and report["trace_complete_rate"]["denominator"] == 3
            )
            assert (
                report["complete"],
                report["partial"],
                report["missing"],
                report["not_collected"],
            ) == (1, 1, 1, 0)
            for signal, expected in [("trace_partial", "two"), ("trace_missing", "three")]:
                url = ROOT + f"/reports/runs/{rid}"
                query = {"signal": signal}
                selected = api.get(url + "/bad-cases", params=query).json()["data"]["items"]
                assert [c["case_key"] for c in selected] == [expected]
                assert api.get(url + "/export", params=query).json()["cases"] == selected
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
                    assert summary["close_reason"] == (
                        "SOURCE_INCOMPLETE" if corrupt else "REQUIRED_EVENT_MISSING"
                    )
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
