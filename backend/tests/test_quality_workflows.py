import asyncio
import hashlib
import json
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import text
from test_evaluation_runs import create

from app.api.main import create_app
from app.contracts.quality import ServiceGrant
from app.infrastructure.resources import Resources, Settings
from app.worker.quality import sweep

ROOT = "/api/v1"
SAMPLES = Path(__file__).resolve().parents[2] / "contracts/quality/v1"


def test_production_and_proposal(catalog: tuple[Settings, dict[str, str]]) -> None:
    settings, tokens = catalog
    owner = {"Authorization": "Bearer " + tokens["curator"]}
    with TestClient(create_app(settings), headers=owner) as client:
        project = create(
            client,
            "/projects",
            slug="quality",
            name="Quality",
            kind="custom",
            max_parallel=2,
            target_max_parallel=1,
            rpm=60,
        )
        dataset = create(client, "/datasets", slug="quality", name="Quality")
    token = secrets.token_urlsafe()
    other = secrets.token_urlsafe()
    scope = ServiceGrant(
        subject="review-client",
        project_id=UUID(project["id"]),
        environment="test",
        datasets={UUID(dataset["id"])},
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        scopes={"trace:read", "trace:write", "proposal:write", "payload:read"},
        verifier=hashlib.sha256(token.encode()).hexdigest(),
    )
    settings = settings.model_copy(
        update={
            "production_trace_key_version": "dev",
            "production_trace_keys": {"dev": SecretStr(secrets.token_hex(32))},
            "quality_grants": [
                scope,
                scope.model_copy(
                    update={
                        "environment": "elsewhere",
                        "verifier": hashlib.sha256(other.encode()).hexdigest(),
                    }
                ),
            ],
        }
    )
    sample = json.loads((SAMPLES / "production-trace.json").read_text(encoding="utf8"))
    with TestClient(create_app(settings), headers={"Authorization": "Bearer " + token}) as client:
        write = ROOT + "/production-trace-write"
        batch = sample["batch"]
        tid = batch["trace"]["trace_id"]
        url = ROOT + "/production-traces/" + tid
        for order in ([2, 0], [1], [2, 1]):
            response = client.post(
                write + "/events:batch",
                json={**batch, "events": [batch["events"][i] for i in order]},
            )
            assert response.status_code == 200, response.text
        assert response.json()["data"]["duplicate_events"] == 2
        assert (
            client.post(
                write + "/events:batch",
                json={**batch, "events": [{**batch["events"][0], "name": "changed"}]},
            ).status_code
            == 409
        )
        assert (
            client.post(write + "/finalize", json=sample["finalize"]).json()["data"]["completeness"]
            == "COMPLETE"
        )
        assert client.get(url, headers={"Authorization": "Bearer " + other}).status_code == 404
        assert client.get(url + "/events", params={"limit": 2}).json()["data"]["items"][-1][
            "attributes"
        ] == {"call_kind": "skill"}
        cursor = client.get(url + "/events", params={"limit": 2}).json()["data"]["next_cursor"]
        assert (
            client.get(url + "/events", params={"cursor": cursor}).json()["data"]["items"][0][
                "sequence"
            ]
            == 3
        )
        proposal = dict(
            proposal_id=str(uuid4()),
            case_id=str(uuid4()),
            review_id=str(uuid4()),
            project=dict(project_id=project["id"], project_slug="quality", environment="test"),
            dataset_id=dataset["id"],
            case_key="reviewed",
            input={"query": "hello"},
            expected={},
            evidence_refs=[url],
        )
        admin_trace = ROOT + "/projects/" + project["id"] + "/production-traces/" + tid
        assert client.post(admin_trace + "/archive", headers=owner).is_success
        assert client.get(url + "/events").status_code == 410
        assert client.post(admin_trace + "/restore", headers=owner).is_success
        assert len(client.get(url + "/events").json()["data"]["items"]) == 3
        partial = {**batch["trace"], "trace_id": str(uuid4()), "agent_run_id": "partial"}
        assert client.post(
            write + "/events:batch",
            json={**batch, "trace": partial, "events": [batch["events"][2]]},
        ).is_success
        close = {**sample["finalize"], "trace_id": partial["trace_id"]}
        assert (
            client.post(write + "/finalize", json=close).json()["data"]["completeness"] == "PARTIAL"
        )
        empty = {**partial, "trace_id": str(uuid4()), "agent_run_id": "empty"}
        assert client.post(write + "/register", json=empty).is_success
        assert (
            client.post(
                write + "/finalize",
                json={**close, "trace_id": empty["trace_id"], "event_count": 0, "last_sequence": 0},
            ).json()["data"]["completeness"]
            == "MISSING"
        )
        path = ROOT + "/datasets/" + dataset["id"] + "/case-proposals"
        headers = {"Idempotency-Key": proposal["proposal_id"]}
        first = client.post(path, headers=headers, json=proposal)
        assert first.status_code == 201, first.text
        replay = client.post(path, headers=headers, json=proposal)
        assert replay.status_code == 200 and first.json()["data"] == replay.json()["data"]
        assert (
            client.post(
                path, headers=headers, json={**proposal, "expected": {"answer": "updated"}}
            ).status_code
            == 409
        )
        assert (
            client.post(
                ROOT + "/datasets/" + dataset["id"] + "/versions", json={"action": "draft"}
            ).status_code
            == 401
        )
        abandoned = {**empty, "trace_id": str(uuid4()), "agent_run_id": "abandoned"}
        assert client.post(write + "/register", json=abandoned).is_success

        async def expire() -> None:
            resources = Resources(settings)
            try:
                async with resources.transaction() as db:
                    await db.execute(
                        text("UPDATE production_traces SET deadline=now(),retained_until=now()")
                    )
                await sweep(resources)
            finally:
                await resources.close()

        asyncio.run(expire())
        assert client.get(url).json()["data"]["availability"] == "AVAILABLE"
        assert (
            client.get(ROOT + "/production-traces/" + abandoned["trace_id"]).json()["data"][
                "completeness"
            ]
            == "MISSING"
        )
        expired = client.get(ROOT + "/production-traces/" + partial["trace_id"] + "/events")
        assert expired.status_code == 410 and expired.json()["error"]["code"] == "TRACE_EXPIRED"
