from typing import Any

from app.application.runner.regression import evaluate_case


def test_threshold_and_evidence_do_not_hide_red_lines() -> None:
    rule = dict(
        rubric_key="safety",
        gate_class="BLOCKING",
        evaluator_ref="self",
        evidence_required=True,
        minimum=0,
        threshold=1,
        weight=1,
    )
    snap: dict[str, Any] = {"roots": ["v1"], "evaluator_plan": [{"id": "v1", "rubrics": [rule]}]}

    def slot(value: float, valid: bool = True) -> dict[str, Any]:
        return {
            "status": "SUCCEEDED",
            "scores": {
                "v1": {
                    "status": "OK",
                    "passed": value >= 0.5,
                    "score": value,
                    "verified_evidence": valid,
                }
            },
        }

    missing = evaluate_case([slot(1), slot(1, False), slot(1)], snap)
    assert missing["gate"] == "BLOCKED" and missing["trial_results"] == [
        "PASSED",
        "BLOCKED",
        "PASSED",
    ]
    rule.update(gate_class="THRESHOLD", minimum=0.3, threshold=0.8)
    assert evaluate_case([slot(0.9), slot(0.8), slot(0.4)], snap)["gate"] == "FAILED"
    assert evaluate_case([slot(1), slot(1), slot(0.29)], snap)["gate"] == "FAILED"
    rule.update(gate_class="BLOCKING")
    snap["evaluator_plan"][0]["rubrics"].append(
        {**rule, "rubric_key": "bonus", "gate_class": "BONUS", "weight": 2}
    )
    failed = evaluate_case([slot(0), slot(1), slot(1)], snap)
    assert failed["gate"] == "FAILED" and failed["bonus_points"] > 0
