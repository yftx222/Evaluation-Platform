"""Explicit D3 acceptance with a real DataAgent, Worker and disposable catalog."""

import hashlib
import json
import os
import re
import time
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from test_batch_console import child_env, process
from test_evaluation_runs import ROOT, create

from app.api.main import create_app
from app.contracts.http import TargetRule
from app.infrastructure.resources import Settings
from app.plugins.evaluators import BUILD


def test_repeat_frozen_case(
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
    env["EVAL_SECRETS"] = json.dumps({"dataagent": {"dataagent-login": {"v1": login}}})
    with TestClient(
        create_app(settings), headers={"Authorization": "Bearer " + tokens["curator"]}
    ) as api:
        project = create(
            api,
            "/projects",
            slug="dataagent",
            name="环境复现",
            kind="dataagent",
            max_parallel=1,
            target_max_parallel=1,
            rpm=120,
        )
        vp = f"/projects/{project['id']}/versions"
        config = {
            "kind": "dataagent",
            "build": "3",
            "endpoint": base + "/api/v1/chat/stream",
            "probe_endpoint": base + "/_eval/status",
            "secret_ref": "dataagent-login",
            "secret_version": "v1",
            "options": {"role": "dataagent_eval_d1", "fixture_version": "mfg-service-demo-v1"},
        }
        version = create(
            api,
            vp,
            label="d3",
            deployment_ref="dataagent-local-d3",
            adapter=config,
            trace_capabilities={"mode": "disabled", "event_types": []},
        )
        frozen = version["environment"]
        request.node.user_properties.append(("environment", json.dumps(frozen, ensure_ascii=False)))
        ds = create(api, "/datasets", slug="repro", name="固定客户样本")
        dp = "/datasets/" + ds["id"]
        create(
            api,
            dp + "/cases",
            draft_id=ds["draft_id"],
            case_key="customers",
            input={
                "turns": [
                    {
                        "message": (
                            "请实际查询 dataagent_mfg_service_demo.mfg_customer 中 "
                            "demo-service 的去重 customer_id。"
                            "最终只用一行按升序列出每个编号，不需要报告或附件。"
                        )
                    }
                ]
            },
            expected={"ids": ["C04", "C07", "C10"]},
        )
        draft = api.get(ROOT + dp).json()["data"]["draft"]
        dv = create(
            api,
            dp + "/versions",
            action="publish",
            draft_id=draft["id"],
            reviewed_digest=draft["digest"],
        )
        rule = create(api, "/evaluators", kind="rule", slug="fixture-ids", name="合成客户编号")
        ev = create(
            api,
            f"/evaluators/{rule['id']}/versions",
            version="v1",
            impl_key="rules",
            build=BUILD,
            config={
                "rules": [
                    {"path": "answer", "op": "includes", "expected": i, "reason": "缺少 " + i}
                    for i in ("C04", "C07", "C10")
                ]
            },
        )
        task = create(
            api,
            "/evaluations/tasks",
            name="重复执行",
            project_version_id=version["id"],
            dataset_version_id=dv["id"],
            evaluator_version_ids=[ev["id"]],
            policy={"case_timeout_s": 600},
        )
        observations: list[dict[str, Any]] = []
        with process(["-m", "app.worker.main"], env) as worker:
            for key in ("first", "repeat"):
                res = api.post(
                    ROOT + f"/evaluations/tasks/{task['id']}/runs", headers={"Idempotency-Key": key}
                )
                assert res.status_code == 202, res.text
                path = ROOT + "/evaluations/runs/" + res.json()["data"]["id"]
                end = time.monotonic() + 650
                while True:
                    run = api.get(path).json()["data"]
                    if run["status"] in {"COMPLETED", "FAILED", "PARTIAL_FAILED", "CANCELLED"}:
                        break
                    assert worker.poll() is None and time.monotonic() < end
                    time.sleep(1)
                [case] = api.get(path + "/results").json()["data"]["items"]
                assert run["status"] == "COMPLETED", case.get("adapter_error_code")
                assert run["snapshot"]["project_version"]["environment"] == frozen
                pred = case["prediction"]
                assert pred["observed"]["environment_digest"] == frozen["digest"]
                ids = sorted(set(re.findall(r"\bC\d{2}\b", pred["answer"])))
                assert ids == ["C04", "C07", "C10"] and case["scores"][0]["passed"]
                observation = {
                    "run": run["id"],
                    "case": "customers",
                    "ids": ids,
                    "score": case["scores"][0]["score"],
                    "snapshot_digest": run["snapshot_digest"],
                    "environment_digest": frozen["digest"],
                    "project_version": version["id"],
                    "dataset_version": dv["id"],
                    "evaluator_version": ev["id"],
                    "conversation": pred["extensions"]["dataagent"]["conversation_id"],
                    "latency_ms": pred["latency_ms"],
                    "answer": pred["answer"],
                    "text_digest": hashlib.sha256(pred["answer"].encode()).hexdigest(),
                }
                request.node.user_properties.append(
                    (key, json.dumps(observation, ensure_ascii=False))
                )
                observations.append(observation)
        first, repeat = observations
        assert first["run"] != repeat["run"] and first["conversation"] != repeat["conversation"]
        assert first["snapshot_digest"] == repeat["snapshot_digest"]
        mismatch = {
            **config,
            "options": {"role": "dataagent_eval_d1", "fixture_version": "wrong-fixture"},
        }
        invalid = api.post(
            ROOT + vp, json={"label": "mismatch", "deployment_ref": "d3-check", "adapter": mismatch}
        )
        assert (
            invalid.status_code == 422
            and invalid.json()["error"]["code"] == "DATAAGENT_ENVIRONMENT_CHANGED"
        )
        assert [v["id"] for v in api.get(ROOT + vp).json()["data"]["items"]] == [version["id"]]
