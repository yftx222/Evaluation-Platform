"""Executable wire examples. These checks do not start either service."""

import copy
import hashlib
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from jsonschema import Draft202012Validator, FormatChecker  # type: ignore[import-untyped]

from app.contracts.traces import TraceEventInput, TraceFinalize, canonical

LOCAL = Path(__file__).resolve().parents[2] / "contracts/quality/v1"
SHARED = Path(os.environ.get("QUALITY_PEER_CONTRACT_DIR", LOCAL))
FORMATS = FormatChecker()


@FORMATS.checks("date-time", raises=ValueError)  # type: ignore[untyped-decorator]
def aware_timestamp(value: object) -> bool:
    return not isinstance(value, str) or datetime.fromisoformat(value).tzinfo is not None


def read(name: str, directory: Path = LOCAL) -> Any:
    return json.loads((directory / name).read_text(encoding="utf-8-sig"))


def validator(file: str, entry: str) -> Any:
    schema = read(file)
    return Draft202012Validator({**schema, "$ref": f"#/$defs/{entry}"}, format_checker=FORMATS)


def exchanges() -> dict[str, Any]:
    return {sample["action"]: sample for sample in read("exchanges.json", SHARED)}


@pytest.mark.parametrize(
    "file", ["event.schema.json", "feedback.json", "batch-completed.json", "exchanges.json"]
)
def test_shared_wire_artifact(file: str) -> None:
    assert read(file) == read(file, SHARED)


@pytest.mark.parametrize(
    "file", ["event.schema.json", "production-trace.schema.json", "evaluation.schema.json"]
)
def test_schema_is_valid(file: str) -> None:
    Draft202012Validator.check_schema(read(file))
    assert read(file) == read(file, SHARED)


def test_receive_replay_lookup_and_conflict() -> None:
    samples = exchanges()
    for action, status in [("receive", 202), ("retry", 202), ("lookup", 200)]:
        item = samples[action]
        assert item["status"] == status
        validator("event.schema.json", "receipt").validate(item["body"])
        assert item["body"]["data"] == samples["receive"]["body"]["data"]
    original = read(samples["receive"]["request_file"], SHARED)
    validator("event.schema.json", "event").validate(original)
    assert original["event_id"] == samples["receive"]["body"]["data"]["event_id"]
    assert samples["lookup"]["path"] == "/api/v1/quality-events/" + original["event_id"]
    assert samples["retry"]["request_file"] == samples["receive"]["request_file"]
    changed = copy.deepcopy(original)
    changed["payload"]["comment"] = samples["conflict"]["replace"]["/payload/comment"]
    assert changed["event_id"] == original["event_id"]
    assert hashlib.sha256(canonical(original)).hexdigest() == (
        "630ab2c078a01d3919966ca654972419eb418add01e96f1ad0f2a22e64d0f1a7"
    )
    assert (
        hashlib.sha256(canonical(original)).digest() != hashlib.sha256(canonical(changed)).digest()
    )
    assert samples["outside_scope"]["authorized_environment"] != original["project"]["environment"]
    for action, status, code in [
        ("conflict", 409, "EVENT_ID_CONFLICT"),
        ("outside_scope", 403, "SCOPE_DENIED"),
        ("trace_missing", 404, "NOT_FOUND"),
    ]:
        item = samples[action]
        validator("event.schema.json", "error").validate(item["body"])
        assert (item["status"], item["body"]["error"]["code"]) == (status, code)
        assert item["body"]["error"]["retryable"] is False


def test_partial_and_archived_are_separate_axes() -> None:
    samples = exchanges()
    for name, pair in [
        ("trace_partial", ("PARTIAL", "AVAILABLE")),
        ("trace_archived", ("COMPLETE", "ARCHIVED")),
    ]:
        item = samples[name]
        validator("production-trace.schema.json", "traceResponse").validate(item["body"])
        data = item["body"]["data"]
        assert item["path"].endswith("/" + data["trace_id"])
        assert (data["completeness"], data["availability"]) == pair


def test_proposal_replay_and_case_results() -> None:
    samples = exchanges()
    first, replay = samples["submit_proposal"], samples["proposal_replay"]
    request = first["request"]
    validator("evaluation.schema.json", "proposal").validate(request)
    assert request["proposal_id"] == first["idempotency_key"] == replay["idempotency_key"]
    assert (
        first["path"]
        == replay["path"]
        == f"/api/v1/datasets/{request['dataset_id']}/case-proposals"
    )
    assert (first["status"], replay["status"]) == (201, 200)
    for item in (first, replay):
        validator("evaluation.schema.json", "proposalResponse").validate(item["body"])
    receipt = first["body"]["data"]
    assert receipt == replay["body"]["data"]
    assert (receipt["proposal_id"], receipt["case_key"]) == (
        request["proposal_id"],
        request["case_key"],
    )
    assert not validator("evaluation.schema.json", "proposal").is_valid(
        dict(request, action="publish")
    )
    case_page = samples["case_results"]
    validator("evaluation.schema.json", "caseResultsResponse").validate(case_page["body"])
    batch = read("batch-completed.json", SHARED)
    validator("event.schema.json", "event").validate(batch)
    payload = batch["payload"]
    assert payload["case_results_ref"] == case_page["path"]
    assert payload["regression_batch_id"] == case_page["body"]["data"]["regression_batch_id"]
    assert payload["dataset_version_id"] == case_page["body"]["data"]["dataset_version_id"]
    # Publishing this exact draft changes its state in place; its sample IDs survive.
    assert receipt["draft_id"] == payload["dataset_version_id"]
    assert sorted(x["trial"] for x in payload["trials"]) == [1, 2, 3]
    assert len({x["run_id"] for x in payload["trials"]}) == 3
    items = case_page["body"]["data"]["items"]
    linked = next(row for row in items if row["case_key"] == receipt["case_key"])
    assert linked["evaluation_case_id"] == receipt["evaluation_case_id"]
    assert linked["trial_results"] == ["PASSED", "FAILED", "PASSED"]
    assert any(row["trial_results"] == ["PASSED"] * 3 for row in items)
    assert payload["gate"] == "FAILED"


def test_production_trace_contract_and_existing_event_semantics() -> None:
    sample = read("production-trace.json")
    assert sample == read("production-trace.json", SHARED)
    schema = "production-trace.schema.json"
    assert read(schema)["$defs"]["traceEvent"] == TraceEventInput.model_json_schema()
    for name, entry in [
        ("batch", "writeBatch"),
        ("finalize", "finalize"),
        ("detail", "traceResponse"),
    ]:
        validator(schema, entry).validate(sample[name])
    events = sample["batch"]["events"]
    root = sample["batch"]["trace"]
    assert UUID(root["trace_id"]) == UUID(sample["finalize"]["trace_id"])
    assert root["trace_id"] == sample["detail"]["data"]["trace_id"]
    assert root["agent_run_id"] == sample["detail"]["data"]["agent_run_id"]
    emitted = [TraceEventInput.model_validate(e) for e in events]
    final = TraceFinalize.model_validate(
        {k: v for k, v in sample["finalize"].items() if k != "trace_id"}
    )
    assert final.event_count == final.last_sequence == len(emitted)
    assert [e.sequence for e in emitted] == list(range(1, len(emitted) + 1))
    assert {e.parent_event_key for e in emitted[1:]} == {emitted[0].event_key}
    pages = sample["pages"]
    for page in pages:
        validator(schema, "eventsPage").validate(page)
    assert pages[0]["data"]["next_cursor"] and pages[-1]["data"]["next_cursor"] is None
    assert [e for page in pages for e in page["data"]["items"]] == events
    assert not validator(schema, "writeBatch").is_valid(
        dict(sample["batch"], attempt_id=root["trace_id"])
    )


@pytest.mark.parametrize(
    "bad",
    [
        {"schema_version": 1},
        {"event_type": "UNRECOGNIZED"},
        {"event_id": "not-a-uuid"},
        {"occurred_at": "2026-02-30T12:00:00Z"},
        {"occurred_at": "2026-11-24T12:00:00"},
    ],
)
def test_invalid_quality_event(bad: dict[str, Any]) -> None:
    event = read("feedback.json", SHARED)
    assert not validator("event.schema.json", "event").is_valid(event | bad)


def test_retries_cannot_fill_missing_trial() -> None:
    event = read("batch-completed.json", SHARED)
    event["payload"]["trials"][2] = event["payload"]["trials"][1]
    assert not validator("event.schema.json", "event").is_valid(event)
