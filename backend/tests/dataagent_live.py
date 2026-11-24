"""Explicit D1/D2 acceptance against a running DataAgent evaluation deployment."""

import hashlib
import json
import os
import time
from collections import Counter
from contextlib import ExitStack

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from test_batch_console import child_env, process, ready
from test_evaluation_runs import ROOT, create

from app.api.main import create_app
from app.contracts.http import TargetRule
from app.infrastructure.resources import Settings
from app.plugins.evaluators import BUILD


def test_dataagent_multiturn_report(
    catalog: tuple[Settings, dict[str, str]], request: pytest.FixtureRequest
) -> None:
    settings, tokens = catalog
    login = os.environ["DATAAGENT_D1_LOGIN"]
    port = int(os.getenv("DATAAGENT_D1_PORT", "7026"))
    base = f"http://127.0.0.1:{port}"
    gate = TargetRule(scheme="http", host="127.0.0.1", port=port, networks=["127.0.0.1/32"])
    settings = settings.model_copy(
        update={
            "targets": {"dataagent": [gate]},
            "secrets": {"dataagent": {"dataagent-login": {"v1": SecretStr(login)}}},
        }
    )
    env = child_env(settings)
    trace_port = os.getenv("DATAAGENT_D2_API_PORT")
    env["EVAL_SECRETS"] = json.dumps({"dataagent": {"dataagent-login": {"v1": login}}})
    with (
        ExitStack() as services,
        TestClient(
            create_app(settings), headers={"Authorization": "Bearer " + tokens["curator"]}
        ) as api,
    ):
        project = create(
            api,
            "/projects",
            slug="dataagent",
            name="问数归因",
            kind="dataagent",
            max_parallel=1,
            target_max_parallel=1,
            rpm=6,
        )
        version = create(
            api,
            f"/projects/{project['id']}/versions",
            label="d2" if trace_port else "d1",
            deployment_ref="ac53ea5",
            adapter={
                "kind": "dataagent",
                "build": "2" if trace_port else "1",
                "endpoint": base + "/api/v1/chat/stream",
                "probe_endpoint": base + "/_eval/status",
                "timeout_s": 20,
                "secret_ref": "dataagent-login",
                "secret_version": "v1",
                "options": {"role": "dataagent_eval_d1", "fixture_version": "mfg-service-demo-v1"},
            },
        )
        ds = create(api, "/datasets", slug="cohort", name="合成客户范围")
        if trace_port:
            env["EVAL_TRACE_TARGET_VERSIONS"] = json.dumps([version["id"]])
            services.enter_context(
                process(
                    ["-m", "uvicorn", "app.api.main:app", "--port", trace_port, "--no-access-log"],
                    env,
                )
            )
            ready("http://127.0.0.1:" + trace_port)
        dp = "/datasets/" + ds["id"]
        create(
            api,
            dp + "/cases",
            draft_id=ds["draft_id"],
            case_key="customer-report",
            input={
                "turns": [
                    {
                        "message": (
                            (
                                "请先用语义召回工具核对客户字段，再委派 Explorer 实际执行 SQL。"
                                if trace_port
                                else ""
                            )
                            + "请实际查询只读演示库 dataagent_mfg_service_demo 的 mfg_customer 表，"
                            "列出 demo-service 范围内去重的 customer_id 并统计客户数。"
                        )
                    },
                    {
                        "message": (
                            "请把刚才实际查询到的客户编号逐行整理成 CSV 报告。"
                            "请 Analyst 制作文件并用 publish_artifact 正式交付 CSV 附件，"
                            "最终答复请附上文件并列出客户编号。"
                        )
                    },
                ]
            },
            expected={},
        )
        draft = api.get(ROOT + dp).json()["data"]["draft"]
        dv = create(
            api,
            dp + "/versions",
            action="publish",
            draft_id=draft["id"],
            reviewed_digest=draft["digest"],
        )
        rule = create(api, "/evaluators", kind="rule", slug="answer-present", name="回答非空")
        ev = create(
            api,
            f"/evaluators/{rule['id']}/versions",
            version="v1",
            impl_key="rules",
            build=BUILD,
            config={"rules": [{"path": "answer", "op": "nonempty", "reason": "回答为空"}]},
        )
        task = create(
            api,
            "/evaluations/tasks",
            name="DataAgent D1",
            project_version_id=version["id"],
            dataset_version_id=dv["id"],
            evaluator_version_ids=[ev["id"]],
            policy={"case_timeout_s": 900},
        )
        res = api.post(
            ROOT + f"/evaluations/tasks/{task['id']}/runs", headers={"Idempotency-Key": "cohort-d1"}
        )
        assert res.status_code == 202, res.text
        run_id = res.json()["data"]["id"]
        path = ROOT + "/evaluations/runs/" + run_id
        with process(["-m", "app.worker.main"], env) as worker:
            deadline = time.monotonic() + 930
            while True:
                status = api.get(path).json()["data"]["status"]
                if status in {"COMPLETED", "FAILED", "PARTIAL_FAILED", "CANCELLED"}:
                    break
                assert worker.poll() is None and time.monotonic() < deadline
                time.sleep(1)
        [result] = api.get(path + "/results").json()["data"]["items"]
        assert status == "COMPLETED", result.get("adapter_error_code")
        prediction = result["prediction"]
        if trace_port:
            [tid] = result["trace_ids"]
            trace_path = ROOT + "/traces/" + tid
            trace = api.get(trace_path).json()["data"]
            assert trace["attempt_id"] == result["prediction_attempt_id"]
            events = []
            cursor = 0
            while True:
                page = api.get(
                    trace_path + "/events", params={"limit": 100, "after_sequence": cursor}
                ).json()["data"]["items"]
                if not page:
                    break
                events.extend(page)
                cursor = page[-1]["sequence"]
            counts = Counter(e["event_type"] for e in events)
            request.node.user_properties.extend(
                [
                    ("trace", tid),
                    ("trace_status", trace["status"]),
                    ("types", json.dumps(counts)),
                    ("parent_links", sum(bool(e["parent_event_id"]) for e in events)),
                ]
            )
            assert trace["status"] == "COMPLETE", (trace.get("close_reason"), counts)
            assert {
                "AGENT_STEP",
                "TOOL_CALL",
                "RETRIEVAL",
                "MODEL_CALL",
                "FINAL_OUTPUT",
            } <= counts.keys()
            assert {"delegation", "doris.query", "publish_artifact"} <= {e["name"] for e in events}
            assert any(e["attributes"].get("delivery_status") == "verified" for e in events)
            assert {
                e["attributes"].get("turn") for e in events if e["event_type"] == "MODEL_CALL"
            } == {1, 2}
            assert any(
                e["parent_event_id"]
                and e["attributes"].get("role") in {"explorer", "analyst", "reviewer"}
                for e in events
            )
            assert all(not e["input"] and not e["output"] for e in events)
        ext = prediction["extensions"]["dataagent"]
        request.node.user_properties.extend(
            [("run", run_id), ("conversation", ext["conversation_id"])]
        )
        assert (
            prediction["structured_output"]["turns"] == 2 and ext["execution"]["running"] is False
        )
        assert result["scores"][0]["passed"] and all(
            k in prediction["answer"] for k in ("C04", "C07", "C10")
        )
        reports = [
            a for a in ext["artifacts"] if a["ref"].lower().endswith(".csv") and a["turn"] == 2
        ]
        assert reports, "最终答复没有 CSV 发布回执"
        with httpx.Client(base_url=base, trust_env=False, timeout=10) as public:
            auth = public.post("/api/v1/auth/login", json=json.loads(login))
            auth.raise_for_status()
            public.headers["Authorization"] = "Bearer " + auth.json()["access_token"]
            cid = ext["conversation_id"]
            history = public.get(f"/api/v1/chat/ls/{cid}")
            history.raise_for_status()
            messages = history.json()["messages"]
            final_paths = {
                a["f_path"]
                for a in messages[-1].get("attachments") or []
                if a.get("delivery_status") == "verified"
            }
            published_paths = {
                a["f_path"]
                for m in messages
                if m["role"] == "tool"
                for a in m.get("attachments") or []
                if a.get("delivery_status") == "verified"
            }
            for a in reports:
                assert a["verified"] and a["receipt_id"] and a["http_status"] == 200
                assert a["attached"] == (a["ref"] in final_paths)
                assert a["ref"] in published_paths
                delivery = public.get(
                    "/api/v1/chat/attachment/get",
                    params={"conversation_id": cid, "f_path": a["ref"]},
                )
                delivery.raise_for_status()
                assert all(k in delivery.content for k in (b"C04", b"C07", b"C10"))
                assert hashlib.sha256(delivery.content).hexdigest() == a["sha256"]
                assert len(delivery.content) == a["size"] > 0
                request.node.user_properties.append(("csv", json.dumps(a, ensure_ascii=False)))
