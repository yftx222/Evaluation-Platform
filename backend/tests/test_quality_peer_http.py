"""Run with QUALITY_PEER_ROOT pointing to an AIOps checkout with its own Python env."""

import asyncio
import hashlib
import json
import os
import secrets
import socket
import subprocess
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from conftest import disposable_db
from fastapi.testclient import TestClient
from quality_joint_fixture import run_reviewed_fixture
from sqlalchemy import text
from test_batch_console import child_env, process, ready
from test_evaluation_runs import create
from test_quality_workflows import SAMPLES

from app.api.main import create_app
from app.application.runner.regression import enqueue
from app.contracts.quality import ServiceGrant
from app.infrastructure.resources import Resources, Settings
from trace_sdk.production import ProductionTraceSession


def port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture
def peer_db(services: Settings) -> Iterator[str]:
    if not os.getenv("QUALITY_PEER_ROOT"):
        pytest.skip("Set QUALITY_PEER_ROOT to run live AIOps interoperability")
    yield from disposable_db(services)


@contextmanager
def peer_server(python: Path, code: Path, env: dict[str, str], number: int) -> Iterator[None]:
    with subprocess.Popen(
        [
            python,
            "-B",
            "-m",
            "uvicorn",
            "--factory",
            "agent.incidents.api:create_app",
            "--port",
            str(number),
            "--no-access-log",
        ],
        env=env,
        cwd=code,
    ) as child:
        try:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline and child.poll() is None:
                try:
                    if httpx.get(
                        f"http://127.0.0.1:{number}/health", timeout=0.5, trust_env=False
                    ).is_success:
                        break
                except httpx.TransportError:
                    pass
                time.sleep(0.05)
            else:
                raise AssertionError("AIOps service failed to become ready")
            yield
        finally:
            child.terminate()
            child.wait(timeout=10)


def test_real_quality_peer(
    peer_db: str,
    catalog: tuple[Settings, dict[str, str]],
    tmp_path: Path,
    record_property: Callable[[str, object], None],
) -> None:
    peer = Path(os.environ["QUALITY_PEER_ROOT"]).resolve()
    peer_code = peer / "code"
    python = peer / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    cfg, tokens = catalog
    with TestClient(
        create_app(cfg), headers={"Authorization": "Bearer " + tokens["curator"]}
    ) as api:
        project = create(
            api,
            "/projects",
            slug="quality",
            name="Quality",
            kind="custom",
            max_parallel=2,
            target_max_parallel=1,
            rpm=60,
        )["id"]
        dataset = create(api, "/datasets", slug="joint", name="联测题库")["id"]
    recipient = secrets.token_urlsafe()
    sources = peer_code / "incident-sources.example.json"
    senders = tmp_path / "senders.json"
    senders.write_text(
        json.dumps(
            [
                dict(
                    source_system="evaluation-platform",
                    project_id=project,
                    project_slug="quality",
                    environment="test",
                    token_env="JOINT_RECEIVE",
                    event_types=["INSPECTION_ANOMALY", "REGRESSION_BATCH_COMPLETED"],
                )
            ]
        ),
        encoding="utf8",
    )
    peer_env = {
        **os.environ,
        "PYTHONDONTWRITEBYTECODE": "1",
        "AIOPS_INCIDENT_DATABASE_URL": peer_db.replace("+asyncpg", "+psycopg"),
        "AIOPS_INCIDENT_SOURCES_FILE": str(sources),
        "AIOPS_QUALITY_SENDERS_FILE": str(senders),
        "JOINT_RECEIVE": recipient,
    }
    operators = tmp_path / "operators.json"
    operators.write_text(
        json.dumps(
            [
                {
                    "actor": "integration-test-reviewer",
                    "project_id": project,
                    "environment": "test",
                    "review": True,
                    "token_env": "JOINT_REVIEW",
                }
            ]
        ),
        encoding="utf8",
    )
    peer_env.update(
        AIOPS_QUALITY_OPERATORS_FILE=str(operators), JOINT_REVIEW=secrets.token_urlsafe()
    )
    for source in json.loads(sources.read_text(encoding="utf8"))["sources"].values():
        peer_env[source["token_env"]] = secrets.token_urlsafe()
    subprocess.run(
        [python, "-B", "-m", "agent.incidents.migrate"], cwd=peer_code, env=peer_env, check=True
    )
    sender = secrets.token_urlsafe()
    grant = ServiceGrant(
        verifier=hashlib.sha256(sender.encode()).hexdigest(),
        subject="joint-test",
        project_id=UUID(project),
        environment="test",
        expires_at=datetime.now(UTC) + timedelta(minutes=15),
        scopes={"trace:read", "trace:write", "batch:read"},
    )
    env = child_env(cfg)
    proposer = secrets.token_urlsafe()
    proposal_grant = grant.model_copy(
        update={
            "subject": "aiops-proposal",
            "scopes": {"proposal:write"},
            "datasets": {UUID(dataset)},
            "verifier": hashlib.sha256(proposer.encode()).hexdigest(),
        }
    )
    env.update(
        EVAL_QUALITY_GRANTS=json.dumps(
            [g.model_dump(mode="json") for g in (grant, proposal_grant)]
        ),
        EVAL_PRODUCTION_TRACE_KEY_VERSION="test",
        EVAL_PRODUCTION_TRACE_KEYS=json.dumps({"test": secrets.token_hex(32)}),
    )
    api_port, peer_port = port(), port()
    target_port = port()
    env["EVAL_TARGETS"] = json.dumps(
        {
            "quality": [
                {
                    "scheme": "http",
                    "host": "127.0.0.1",
                    "port": target_port,
                    "networks": ["127.0.0.1/32"],
                }
            ]
        }
    )
    api_url = f"http://127.0.0.1:{api_port}"
    inbox = f"http://127.0.0.1:{peer_port}/api/v1/quality-events"
    with (
        peer_server(python, peer_code, peer_env, peer_port),
        process(
            ["-m", "uvicorn", "app.api.main:app", "--port", str(api_port), "--no-access-log"], env
        ),
    ):
        ready(api_url)

        async def send() -> str:
            trace = ProductionTraceSession(
                api_url,
                sender,
                agent_run_id="joint-run",
                conversation_id="joint-c",
                message_id="joint-m",
                environment="test",
                deployment_version="joint-v1",
            )
            root = trace.add("AGENT_STEP", "quality")
            trace.add("FINAL_OUTPUT", "result", parent_event_key=root, output={"answer": "ok"})
            await trace.finish()
            assert trace.confirmed_status == "COMPLETE"
            return str(trace.trace_id)

        tid = asyncio.run(send())
        record_property("fixture_trace_id", tid)
        record_property("project_id", project)
        exercise_lost_receipt(cfg, env, UUID(project), tid, inbox, recipient)
        reader_code = """import os
from datetime import datetime, UTC
from uuid import UUID
from agent.quality_cases.evidence import Message,Scope,PlatformReader,service_client
with service_client(os.environ['JOINT_PLATFORM'],os.environ['JOINT_TRACE']) as http:
    reader=PlatformReader(http,Scope(UUID(os.environ['JOINT_PROJECT']),'test'))
    msg=Message(message_id='joint-m',role='assistant',content='ok',created_at=datetime.now(UTC),agent_run_id='joint-run')
    result=reader.for_conversation('joint-c',[msg])
    assert [e['event_type'] for e in result[0]['events']]==['AGENT_STEP','FINAL_OUTPUT'],result
"""
        subprocess.run(
            [python, "-B", "-c", reader_code],
            cwd=peer_code,
            env={
                **peer_env,
                "JOINT_PLATFORM": api_url,
                "JOINT_TRACE": sender,
                "JOINT_PROJECT": project,
            },
            check=True,
        )
        event = json.loads((SAMPLES / "batch-completed.json").read_text(encoding="utf8"))
        event.update(
            event_id=str(uuid4()),
            event_type="INSPECTION_ANOMALY",
            project=dict(project_id=project, project_slug="quality", environment="test"),
            correlation={"trace_id": tid},
            payload={"plan_ref": "joint-fixture"},
        )
        with httpx.Client(
            headers={"Authorization": "Bearer " + recipient}, trust_env=False
        ) as sender_http:
            first = sender_http.post(inbox, json=event)
            assert first.status_code == 202, first.text
            replay = sender_http.post(inbox, json=event)
            assert replay.status_code == 202 and replay.json()["data"] == first.json()["data"]
            assert (
                sender_http.get(inbox + "/" + event["event_id"]).json()["data"]
                == first.json()["data"]
            )
            assert (
                sender_http.post(
                    inbox, json={**event, "payload": {"plan_ref": "conflict"}}
                ).status_code
                == 409
            )
            assert (
                sender_http.post(
                    inbox, json={**event, "project": {**event["project"], "environment": "prod"}}
                ).status_code
                == 403
            )
        record_property("fixture_event_id", event["event_id"])
        workbench = f"http://127.0.0.1:{peer_port}/api/v1/quality-cases"
        with httpx.Client(
            headers={"Authorization": "Bearer " + peer_env["JOINT_REVIEW"]}, trust_env=False
        ) as operator:
            candidates = operator.get(workbench).json()["items"]
            cid = next(
                c["id"]
                for c in candidates
                if event["event_id"]
                in [
                    s["event_id"] for s in operator.get(workbench + "/" + c["id"]).json()["sources"]
                ]
            )
            joint_env = {
                **peer_env,
                "JOINT_CASE": cid,
                "JOINT_PROJECT": project,
                "JOINT_TRACE": sender,
                "JOINT_PLATFORM": api_url,
                "JOINT_PROPOSER": proposer,
            }
            # Exercise unresolved review using a fixture without a conversation.
            common = """import os
from uuid import UUID
from agent.incidents.store import IncidentStore
from agent.quality_cases.evidence import Scope,PlatformReader,ConversationReader,service_client
engine=IncidentStore(os.environ['AIOPS_INCIDENT_DATABASE_URL']).engine
scope=Scope(UUID(os.environ['JOINT_PROJECT']),'test')
"""
            stage = (
                common
                + """from agent.quality_cases.workflow import process
with service_client(os.environ['JOINT_PLATFORM'],os.environ['JOINT_TRACE']) as http:
    state=process(engine,UUID(os.environ['JOINT_CASE']),ConversationReader(http,scope),
                  PlatformReader(http,scope),max_attempts=1)
    assert state=='UNRESOLVED',state
"""
            )
            subprocess.run([python, "-B", "-c", stage], cwd=peer_code, env=joint_env, check=True)
            rid = str(uuid4())
            decision = {
                "review_id": rid,
                "classification": "BAD",
                "responsibility": "joint-test",
                "reason": "integration fixture",
                "actions": ["RUBRIC_CHANGE"],
                "sample": {
                    "dataset_id": dataset,
                    "case_key": "joint-reviewed",
                    "query": "fixture",
                    "criteria": ["fixture criterion"],
                },
            }
            approved = operator.post(workbench + "/" + cid + "/reviews", json=decision)
            assert approved.status_code == 200, approved.text
            pid = approved.json()["proposal_id"]
            assert (
                operator.post(workbench + "/" + cid + "/reviews", json=decision).json()[
                    "proposal_id"
                ]
                == pid
            )
            stage = (
                common
                + """from agent.quality_cases.operations import next_task,send_proposal,detail
with service_client(os.environ['JOINT_PLATFORM'],os.environ['JOINT_TRACE']) as http:
    with service_client(os.environ['JOINT_PLATFORM'],os.environ['JOINT_PROPOSER']) as write:
        assert next_task(engine,PlatformReader(http,scope),write)=='DONE'
        proposal=detail(engine,scope,UUID(os.environ['JOINT_CASE']))['proposals'][0]
        assert send_proposal(write,proposal['body'])==proposal['result']
"""
            )
            subprocess.run([python, "-B", "-c", stage], cwd=peer_code, env=joint_env, check=True)
            for field, value in (
                ("fixture_case_id", cid),
                ("fixture_review_id", rid),
                ("proposal_id", pid),
            ):
                record_property(field, value)
        with httpx.Client(
            base_url=api_url + "/api/v1",
            headers={"Authorization": "Bearer " + tokens["curator"]},
            trust_env=False,
        ) as owner:
            draft = owner.get(f"datasets/{dataset}").json()["data"]["draft"]
            assert draft["sample_count"] == 1
            published = owner.post(
                f"datasets/{dataset}/versions",
                json={
                    "action": "publish",
                    "draft_id": draft["id"],
                    "reviewed_digest": draft["digest"],
                },
            )
            assert published.status_code == 201, published.text
            check = owner.get(
                f"datasets/{dataset}/case-proposals/{pid}",
                headers={"Authorization": "Bearer " + proposer},
            )
            assert check.json()["data"]["published_dataset_version_id"] == draft["id"]
            result = run_reviewed_fixture(
                owner, project, draft["id"], target_port, env, inbox, recipient
            )
            record_property("regression_batch_id", result)
            sync = (
                common
                + """from agent.quality_cases.operations import next_task,detail
with service_client(os.environ['JOINT_PLATFORM'],os.environ['JOINT_TRACE']) as http:
    assert next_task(engine,PlatformReader(http,scope),http)=='DONE'
    verified=detail(engine,scope,UUID(os.environ['JOINT_CASE']))['regression_samples']
    assert len(verified)==1 and str(verified[0]['batch_id'])==os.environ['JOINT_BATCH']
    assert verified[0]['trial_results']==['PASSED']*3
"""
            )
            subprocess.run(
                [python, "-B", "-c", sync],
                cwd=peer_code,
                env={**joint_env, "JOINT_BATCH": result},
                check=True,
            )


def exercise_lost_receipt(
    cfg: Settings, env: dict[str, str], project: UUID, tid: str, inbox: str, token: str
) -> None:
    receipts = []

    class Relay(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers["Content-Length"]))
            with httpx.Client(trust_env=False) as client:
                answer = client.post(
                    inbox,
                    content=body,
                    headers={
                        "Authorization": self.headers["Authorization"],
                        "Content-Type": "application/json",
                    },
                )
            receipts.append(answer.json()["data"])
            if len(receipts) == 1:
                self.close_connection = True
                return
            self.send_response(answer.status_code)
            self.end_headers()
            self.wfile.write(answer.content)

    async def create_event() -> UUID:
        resources = Resources(cfg)
        try:
            async with resources.transaction() as db:
                return await enqueue(
                    db,
                    project,
                    "quality",
                    "test",
                    "joint-v1",
                    "INSPECTION_ANOMALY",
                    {"plan_ref": "lost-receipt"},
                    {"trace_id": tid},
                )
        finally:
            await resources.close()

    async def verify(eid: UUID) -> None:
        resources = Resources(cfg)
        try:
            deadline = time.monotonic() + 25
            while time.monotonic() < deadline:
                async with resources.transaction() as db:
                    saved = (
                        await db.execute(
                            text("SELECT state,attempts,receipt FROM quality_outbox WHERE id=:id"),
                            {"id": eid},
                        )
                    ).one()
                if saved.state == "DELIVERED":
                    assert saved.attempts == 2 and receipts == [saved.receipt, saved.receipt]
                    return
                await asyncio.sleep(0.1)
            raise AssertionError("Neither a lost response nor replay may lose the durable receipt")
        finally:
            await resources.close()

    with ThreadingHTTPServer(("127.0.0.1", 0), Relay) as relay:
        worker_env = {
            **env,
            "EVAL_QUALITY_RECEIVERS": json.dumps(
                {
                    str(project): {
                        "test": {
                            "endpoint": f"http://127.0.0.1:{relay.server_port}/events",
                            "credential": token,
                        }
                    }
                }
            ),
        }
        thread = threading.Thread(target=relay.serve_forever, daemon=True)
        thread.start()
        try:
            eid = asyncio.run(create_event())
            with process(["-m", "app.worker.main"], worker_env):
                asyncio.run(verify(eid))
        finally:
            relay.shutdown()
            thread.join()
