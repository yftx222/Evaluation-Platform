"""Run explicitly with EVAL_SHOP_CODE pointing at the ecommerce code checkout."""

import json
import os
import socket
import subprocess
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from test_batch_console import child_env, process, ready
from test_evaluation_runs import ROOT, create, prepare
from test_trace_write import configured

from app.api.main import create_app
from app.infrastructure.resources import Settings
from app.plugins.evaluators import BUILD


def test_shop_agent_trace(
    catalog: tuple[Settings, dict[str, str]], request: pytest.FixtureRequest
) -> None:
    shop = Path(os.environ["EVAL_SHOP_CODE"]).resolve()
    backend = Path.cwd()
    with socket.socket() as a, socket.socket() as b:
        a.bind(("127.0.0.1", 0))
        b.bind(("127.0.0.1", 0))
        ap, sp = a.getsockname()[1], b.getsockname()[1]
    origin = f"http://127.0.0.1:{ap}"
    url = f"http://127.0.0.1:{sp}"
    settings, tokens = catalog
    settings = configured(settings, sp)
    worker_env = child_env(settings)
    with process(
        ["-m", "uvicorn", "app.api.main:app", "--port", str(ap), "--no-access-log"],
        worker_env,
    ):
        ready(origin)
        py = (
            shop
            / "evaluation"
            / ".venv"
            / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        )
        env = {
            **os.environ,
            "PYTHONPATH": os.pathsep.join(
                str(x)
                for x in [
                    backend,
                    shop / "evaluation",
                    shop / "backend",
                    shop / "backend" / "ecommerce_service",
                ]
            ),
            "TRACE_ACCEPTANCE_URL": origin,
        }
        with subprocess.Popen(
            [
                str(py),
                "-m",
                "uvicorn",
                "phase42_shop_service:application",
                "--factory",
                "--app-dir",
                "tests",
                "--host",
                "127.0.0.1",
                "--port",
                str(sp),
                "--no-access-log",
            ],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ) as svc:
            try:
                # The business app cold-loads its model SDK, tokenizer and tool catalogue.
                deadline = time.monotonic() + 80
                with httpx.Client(timeout=1, trust_env=False) as health:
                    while True:
                        assert svc.poll() is None, "客服适配服务启动失败"
                        try:
                            if health.get(url + "/ready").is_success:
                                break
                        except httpx.TransportError:
                            pass
                        assert time.monotonic() < deadline, "客服模型工具目录未就绪"
                        time.sleep(0.1)
                with TestClient(
                    create_app(settings), headers={"Authorization": "Bearer " + tokens["curator"]}
                ) as api:
                    f = prepare(api)
                    version = create(
                        api,
                        f"/projects/{f['project']['id']}/versions",
                        label="agent-live",
                        deployment_ref="local-ecommerce-executor",
                        adapter={
                            "kind": "agent",
                            "endpoint": url + "/ask",
                            "probe_endpoint": url + "/ready",
                            "timeout_s": 20,
                        },
                        trace_capabilities={"required_events": ["MODEL_CALL", "FINAL_OUTPUT"]},
                    )
                    ds = create(api, "/datasets", slug="greeting", name="合成问候")
                    dp = "/datasets/" + ds["id"]
                    create(
                        api,
                        dp + "/cases",
                        draft_id=ds["draft_id"],
                        case_key="public-greeting",
                        expected={},
                        input={"query": "你好，请用一句话介绍你能提供什么帮助。"},
                    )
                    draft = api.get(ROOT + dp).json()["data"]["draft"]
                    dv = create(
                        api,
                        dp + "/versions",
                        action="publish",
                        draft_id=draft["id"],
                        reviewed_digest=draft["digest"],
                    )
                    rule = create(
                        api, "/evaluators", kind="rule", slug="reply-present", name="回复非空"
                    )
                    ev = create(
                        api,
                        f"/evaluators/{rule['id']}/versions",
                        version="v1",
                        impl_key="rules",
                        build=BUILD,
                        config={
                            "rules": [{"path": "answer", "op": "nonempty", "reason": "回答为空"}]
                        },
                    )
                    task = create(
                        api,
                        "/evaluations/tasks",
                        name="客服 Agent 联调",
                        project_version_id=version["id"],
                        dataset_version_id=dv["id"],
                        evaluator_version_ids=[ev["id"]],
                    )
                    run = api.post(
                        ROOT + f"/evaluations/tasks/{task['id']}/runs",
                        headers={"Idempotency-Key": "smoke"},
                    )
                    assert run.status_code == 202
                    path = ROOT + "/evaluations/runs/" + run.json()["data"]["id"]
                    worker_env["EVAL_TRACE_TARGET_VERSIONS"] = json.dumps([version["id"]])
                    with process(["-m", "app.worker.main"], worker_env):
                        until = time.monotonic() + 45
                        while True:
                            status = api.get(path).json()["data"]["status"]
                            if status in {"COMPLETED", "FAILED", "PARTIAL_FAILED"}:
                                break
                            assert time.monotonic() < until
                            time.sleep(0.1)
                    [result] = api.get(path + "/results").json()["data"]["items"]
                    assert status == "COMPLETED", result.get("adapter_error_code")
                    assert result["prediction"]["answer"] and result["scores"][0]["passed"]
                    [trace_id] = result["trace_ids"]
                    trace = api.get(ROOT + "/traces/" + trace_id).json()["data"]
                    assert (
                        trace["status"] == "COMPLETE"
                        and trace["attempt_id"] == result["prediction_attempt_id"]
                    )
                    events = api.get(
                        ROOT + "/traces/" + trace_id + "/events?summary_only=true"
                    ).json()["data"]["items"]
                    assert {"AGENT_STEP", "MODEL_CALL", "FINAL_OUTPUT"} <= {
                        e["event_type"] for e in events
                    }
                    assert all("output" not in e for e in events)
                    score_id = result["scores"][0]["id"]
                    refs = api.get(ROOT + f"/evaluations/scores/{score_id}/evidence").json()[
                        "data"
                    ]["items"]
                    prediction, origin_event = refs
                    assert prediction["json_pointer"] == "/answer"
                    assert prediction["attempt_id"] == trace["attempt_id"]
                    assert origin_event["event_id"] in {
                        e["id"] for e in events if e["event_type"] == "FINAL_OUTPUT"
                    }
                    assert (
                        origin_event["status"] == "AVAILABLE"
                        and origin_event["trace_id"] == trace_id
                    )
                    assert (
                        api.get(ROOT + "/evidence/" + origin_event["evidence_id"]).json()["data"]
                        == origin_event
                    )
                    report = api.get(ROOT + "/reports/runs/" + run.json()["data"]["id"]).json()[
                        "data"
                    ]
                    assert (
                        report["summary"]["succeeded_cases"] == report["summary"]["complete"] == 1
                    )
                    assert report["summary"]["metrics"]["pass_rate"]["value"] == 1
                    assert report["usage"]["estimated_cost"] is None
                    if os.getenv("EVAL_ARCHIVE_DRILL") == "1":
                        from phase44_archive import verify_restore

                        restored = verify_restore(
                            settings,
                            tokens,
                            api,
                            run.json()["data"]["id"],
                            trace_id,
                            origin_event["evidence_id"],
                        )
                        request.node.user_properties.append(("restore", json.dumps(restored)))
                    request.node.user_properties.extend(
                        [
                            ("events", len(events)),
                            ("verified_evidence", len(refs)),
                            ("models", sum(e["event_type"] == "MODEL_CALL" for e in events)),
                            ("trace", trace["status"]),
                        ]
                    )
            finally:
                svc.terminate()
                svc.wait(timeout=10)
