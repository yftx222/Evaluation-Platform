"""Opt-in D4 acceptance against the persistent local catalog and actual pilot records."""

import json
import os
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.plugins.dataagent import Case as DataAgentInput
from scripts.dataagent_assets import ASSETS, call, convert, publish


@pytest.fixture(scope="module")
def api() -> Any:
    with httpx.Client(
        base_url="http://127.0.0.1:18000/api/v1/",
        timeout=45,
        trust_env=False,
        headers={"Authorization": "Bearer " + os.environ["EVAL_CATALOG_TOKEN"]},
    ) as client:
        yield client


@pytest.fixture(scope="module")
def assets(api: httpx.Client) -> dict[str, Any]:
    return publish(api)


def test_source_and_repeat_publish(api: httpx.Client, assets: dict[str, Any]) -> None:
    src = Path(os.environ["DATAAGENT_EVALS_DIR"])
    cases = convert(src)
    raw = json.loads((src / "pilot_cases.json").read_text(encoding="utf-8-sig"))
    cards = json.loads((src / "scorecards.json").read_text(encoding="utf-8-sig"))
    native = [json.loads(s) for s in (ASSETS / "pilot.jsonl").read_bytes().splitlines()]
    assert cases == native and len(raw) == len(native) == 18
    by_id = {c["case_key"]: c for c in native}
    for original in raw:
        key, turns = original["id"], original["turns"]
        case = by_id[key]
        assert case["input"] == {"turns": [{"message": t["message"]} for t in turns]}
        assert len(DataAgentInput.model_validate(case["input"]).turns) == len(turns)
        assert case["expected"]["turn_answers"] == [t["expected"] for t in turns]
        assert case["expected"]["criteria"] == [t["check"] for t in turns]
        assert case["expected"]["scorecard"] == cards[key]
    assert len(by_id["CNV-01"]["input"]["turns"]) == 2
    assert assets["dataset"]["turns"] == 19 and publish(api) == assets


def test_archived_delivery_and_answer_rubric(
    api: httpx.Client, assets: dict[str, Any], request: pytest.FixtureRequest
) -> None:
    cases = {c["case_key"]: c for c in convert(Path(os.environ["DATAAGENT_EVALS_DIR"]))}
    trials = [json.loads(s) for s in Path(os.environ["DATAAGENT_TRIALS"]).read_bytes().splitlines()]
    assert len(trials) == len(cases) and {t["case"] for t in trials} == cases.keys()

    def evaluate(slug: str, key: str, prediction: dict[str, Any]) -> dict[str, Any]:
        v = assets["evaluators"][slug]
        result: dict[str, Any] = call(
            api,
            "POST",
            f"evaluators/{v['evaluator_id']}/test",
            json={"version_id": v["version_id"], "case": cases[key], "prediction": prediction},
        )
        assert result["evaluator_version_id"] == v["version_id"] and result["status"] == "OK"
        return result

    for t in trials:
        turns = t["turns"]
        assert [i["question"] for i in turns] == [
            i["message"] for i in cases[t["case"]]["input"]["turns"]
        ]
        assert t["closed"] and all(i["status"] == "done" for i in turns)
        result = evaluate(
            "dataagent-final-answer",
            t["case"],
            {
                "answer": turns[-1]["answer"],
                "structured_output": {
                    "role": "assistant",
                    "status": "finished",
                    "turns": len(turns),
                },
            },
        )
        assert result["passed"] and result["score"] == 1
    request.node.user_properties.append(("historical_delivery_passed", len(trials)))
    missing = evaluate("dataagent-final-answer", "ORD-01", {"answer": ""})
    assert missing["passed"] is False and missing["score"] == 0

    # Send only the archived aggregate headline to the Judge. Keep raw audit/SQL data local.
    order = next(t for t in trials if t["case"] == "ORD-01")
    headline = order["turns"][-1]["answer"].splitlines()[0]
    assert headline == "**2026 年 9 月全站下单订单笔数：206 笔。**"
    for label, answer, expected in (
        ("archived_headline", headline, 1),
        ("wrong_count_control", headline.replace("206", "301"), 0),
    ):
        verdict = evaluate("dataagent-pilot-answer-rubric", "ORD-01", {"answer": answer})
        request.node.user_properties.append(
            (
                label,
                json.dumps(
                    {
                        "score": verdict["score"],
                        "passed": verdict["passed"],
                        "model": verdict["details"]["model"],
                        "usage": verdict["details"]["usage"],
                    }
                ),
            )
        )
        assert verdict["score"] == expected and verdict["passed"] is bool(expected)
