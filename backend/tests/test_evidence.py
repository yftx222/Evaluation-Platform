import asyncio
import statistics
import time
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from test_evaluation_runs import ROOT, create

from app.application.catalog.store import row
from app.application.trace.evidence import SCOPE, bind, locate, resolve
from app.infrastructure.resources import Resources, Settings


def test_json_pointer() -> None:
    doc = {"a/b": {"~key": [None, "ok"]}, "": {"": 0}, "nested": {"api_key": {"value": "private"}}}
    assert locate(doc, "/a~1b/~0key/1") == ("ok", False)
    assert locate(doc, "//") == (0, False)
    assert locate(doc, "/nested/api_key/value") == ("private", True)
    for path in ("x", "/a~1b/~0key/01", "/a~1b/~0key/-", "/a~1b/~0key/2", "/a~2b", "/z"):
        with pytest.raises(ValueError):
            locate(doc, path)


def check_evidence(
    api: TestClient,
    settings: Settings,
    items: list[dict[str, Any]],
    fixture: dict[str, Any],
    outsider: str,
    request: pytest.FixtureRequest,
) -> None:
    case = next(i for i in items if i["case_key"] == "one")
    other = next(i for i in items if i["case_key"] == "two")
    score = case["scores"][0]
    execution_url = ROOT + f"/case-executions/{case['id']}/evidence"
    score_url = ROOT + f"/evaluations/scores/{score['id']}/evidence"
    group = api.get(score_url).json()["data"]
    assert api.get(execution_url).json()["data"]["scores"] == [group]
    prediction, event = group["items"]
    assert prediction["excerpt"] == "one" and prediction["json_pointer"] == "/answer"
    assert prediction["attempt_id"] == case["prediction_attempt_id"]
    assert event["source_type"] == "TRACE_EVENT"
    assert event["metadata"] == {"relation": "prediction_provenance"}
    detail_url = ROOT + "/evidence/" + event["evidence_id"]
    trace_url = ROOT + f"/traces/{event['trace_id']}/events/{event['event_id']}"
    assert api.get(detail_url).json()["data"] == event
    elapsed = []
    for _ in range(20):
        start = time.perf_counter()
        assert api.get(score_url).json()["data"] == group
        elapsed.append((time.perf_counter() - start) * 1000)
    request.node.user_properties.extend(
        [
            ("score_reads", len(elapsed)),
            ("score_read_p50_ms", round(statistics.median(elapsed), 3)),
            ("score_read_p95_ms", round(sorted(elapsed)[18], 3)),
        ]
    )
    assert api.get(trace_url).json()["data"]["output"]["output_ref"] == event["excerpt"]
    for url in (detail_url, trace_url, execution_url, score_url):
        denied = api.get(url, headers={"Authorization": "Bearer " + outsider})
        assert denied.status_code in (403, 404)
        assert "output_ref" not in denied.text
    assert (
        api.get(ROOT + f"/evidence/{score['id']}.500").json()["error"]["code"]
        == "EVIDENCE_UNAVAILABLE"
    )
    foreign = create(
        api,
        "/projects",
        slug="other",
        name="其他项目",
        kind="custom",
        max_parallel=1,
        target_max_parallel=1,
        rpm=60,
    )
    for project, allowed_scope, status in [
        (fixture["project"]["id"], "results:read", 200),
        (fixture["project"]["id"], "project:read", 403),
        (foreign["id"], "results:read", 403),
    ]:
        key = create(
            api,
            f"/projects/{project}/api-keys",
            name="读取证据",
            scopes=[allowed_scope],
            expires_at=(datetime.now(UTC) + timedelta(days=1)).isoformat(),
        )
        headers = {"Authorization": "ApiKey " + key["secret"]}
        for url in (trace_url, detail_url, score_url, execution_url):
            assert api.get(url, headers=headers).status_code == status
    foreign_event = api.get(ROOT + "/traces/" + other["trace_ids"][0] + "/events").json()["data"][
        "items"
    ][0]["id"]

    async def validate() -> None:
        resources = Resources(settings)
        try:
            async with resources.transaction() as db:
                scope = await row(db, SCOPE, id=UUID(case["id"]))
                refs = [
                    "case_input:/query",
                    "expected:/answer",
                    "external:policy/A",
                    "trace_event:" + event["event_id"] + "#/output/document_refs/0",
                ]
                for ref, value in zip(refs, ("one", "one", "A", "doc-1"), strict=True):
                    resolved = await resolve(db, scope, ref)
                    assert resolved["status"] == "AVAILABLE" and resolved["excerpt"] == value
                    if ref.startswith(("expected:", "case_input:")):
                        assert (
                            str(resolved["metadata"]["dataset_version_id"]) == fixture["dv"]["id"]
                        )
                prefix = "trace_event:" + event["event_id"] + "#/output/"
                hidden = await resolve(db, scope, prefix + "contact")
                assert hidden["status"] == "REDACTED" and hidden["excerpt"] is None
                limited = await resolve(db, scope, prefix + "long")
                assert limited["excerpt"] == "x" * 239 + "…"
                rejected = await bind(
                    db,
                    UUID(case["id"]),
                    [
                        "prediction:/missing",
                        f"trace_event:{foreign_event}#/output",
                        f"trace_event:{uuid4()}",
                        "external:https://example.invalid/",
                        "external:policy/unknown",
                        "invented:raw",
                        "output_event:" + event["event_id"],
                    ],
                )
                assert rejected == [
                    "unavailable:prediction:INVALID_POINTER",
                    "unavailable:trace_event:SOURCE_MISSING",
                    "unavailable:trace_event:SOURCE_MISSING",
                    "unavailable:external:UNTRUSTED_SOURCE",
                    "unavailable:external:UNTRUSTED_SOURCE",
                    "unavailable:unknown:INVALID_REFERENCE",
                    "unavailable:output_event:INVALID_REFERENCE",
                ]
                for ref in rejected:
                    invalid = await resolve(db, scope, ref)
                    assert (
                        invalid["status"] == "EVIDENCE_UNAVAILABLE" and invalid["source_id"] is None
                    )
                await db.execute(
                    text("INSERT INTO trace_archives(trace_id) VALUES (:id)"),
                    {"id": UUID(event["trace_id"])},
                )
        finally:
            await resources.close()

    # Publish a genuinely different successor before re-reading the saved score.
    path = "/datasets/" + fixture["dataset"]["id"]
    draft = create(api, path + "/versions", action="draft")
    create(
        api,
        path + "/cases",
        draft_id=draft["id"],
        case_key="one",
        input={"query": "changed"},
        expected={"answer": "changed"},
    )
    digest = api.get(ROOT + path).json()["data"]["draft"]["digest"]
    successor = create(
        api, path + "/versions", action="publish", draft_id=draft["id"], reviewed_digest=digest
    )
    assert successor["id"] != fixture["dv"]["id"]
    assert api.get(score_url).json()["data"] == group
    asyncio.run(validate())
    archived = api.get(detail_url).json()["data"]
    assert archived["status"] == "EVIDENCE_UNAVAILABLE" and archived["reason"] == "SOURCE_ARCHIVED"
    assert archived["excerpt"] is None and archived["event_id"] == event["event_id"]
    assert api.get(trace_url).status_code == 410
    assert api.get(score_url).json()["data"]["items"][0] == prediction
