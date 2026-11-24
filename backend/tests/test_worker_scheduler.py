import asyncio
import json
import os
import sys
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from test_evaluation_runs import ROOT, create, prepare

from app.api.main import create_app
from app.contracts.evaluators import JudgeAccount
from app.contracts.http import TargetRule
from app.infrastructure.resources import Resources, Settings
from app.worker.scheduler import Lease, claim, initialize, release, renew
from app.worker.settings import WorkerSettings


@pytest.fixture
def workload(
    catalog: tuple[Settings, dict[str, str]],
) -> Iterator[tuple[Settings, TestClient, dict[str, str]]]:
    settings, tokens = catalog
    rule = TargetRule(host="127.0.0.1", scheme="http", port=12345, networks=["127.0.0.1/32"])
    judge = JudgeAccount(
        endpoint="http://127.0.0.1:12345",
        destination=rule,
        models=["model-a"],
        max_parallel=1,
        rpm=60,
    )
    settings = settings.model_copy(
        update={"targets": {"shop": [rule], "rag": [rule]}, "judge_accounts": {"acct": judge}}
    )
    with TestClient(
        create_app(settings), headers={"Authorization": "Bearer " + tokens["curator"]}
    ) as api:
        items = prepare(api)
        shop = items["project"]["id"]
        assert api.patch(ROOT + "/projects/" + shop, json={"rpm": 1000000}).is_success
        rag = create(
            api,
            "/projects",
            name="查询",
            slug="rag",
            kind="rag",
            max_parallel=1,
            target_max_parallel=1,
            rpm=1000000,
        )
        pv = create(
            api,
            f"/projects/{rag['id']}/versions",
            label="v1",
            deployment_ref="fixed",
            adapter={
                "endpoint": "http://127.0.0.1:12345/query",
                "probe_endpoint": "http://127.0.0.1:12345/ready",
            },
        )
        d = items["dataset"]["id"]
        draft = create(api, f"/datasets/{d}/versions", action="draft")
        data = "\n".join(
            json.dumps(
                {"case_key": str(i), "input": {"query": str(i)}, "expected": {"answer": str(i)}}
            )
            for i in range(120)
        )
        res = api.post(
            ROOT + "/datasets/import",
            files={"file": ("suite.jsonl", data)},
            data={"dataset_id": d, "draft_id": draft["id"], "format": "jsonl"},
        )
        assert res.is_success, res.text
        reviewed = api.get(ROOT + "/datasets/" + d).json()["data"]["draft"]
        dv = create(
            api,
            f"/datasets/{d}/versions",
            action="publish",
            draft_id=draft["id"],
            reviewed_digest=reviewed["digest"],
        )
        tasks = {}
        for slug, version, limit in [("shop", items["pv"], 1), ("rag", pv, 2)]:
            tasks[slug] = create(
                api,
                "/evaluations/tasks",
                name=slug,
                project_version_id=version["id"],
                dataset_version_id=dv["id"],
                evaluator_version_ids=[items["root"]["id"]],
                policy={"max_parallel": limit},
            )["id"]
        yield settings, api, {**tasks, "shop_id": shop, "rag_id": rag["id"]}


def launch(api: TestClient, t: str) -> UUID:
    reply = api.post(
        ROOT + f"/evaluations/tasks/{t}/runs", json={}, headers={"Idempotency-Key": uuid4().hex}
    )
    assert reply.status_code == 202, reply.text
    assert reply.json()["data"]["total_cases"] == 120
    return UUID(reply.json()["data"]["id"])


async def send(proc: asyncio.subprocess.Process, command: str) -> None:
    assert proc.stdin is not None
    proc.stdin.write((command + "\n").encode())
    await proc.stdin.drain()


async def answer(proc: asyncio.subprocess.Process) -> str:
    assert proc.stdout is not None
    async with asyncio.timeout(8):
        return (await proc.stdout.readline()).decode().strip()


@asynccontextmanager
async def worker(
    settings: Settings,
    owner: str,
    url: str = "",
    *,
    driver: bool = True,
    global_slots: int = 3,
    slots: int = 2,
    with_redis: bool = False,
) -> AsyncIterator[asyncio.subprocess.Process]:
    env = {
        **os.environ,
        "EVAL_DATABASE_URL": settings.database_url.get_secret_value(),
        "EVAL_REDIS_URL": settings.redis_url.get_secret_value()
        if with_redis
        else "redis://127.0.0.1:1",
        "EVAL_WORKER_ID": owner,
        "EVAL_WORKER_SLOTS": str(slots),
        "EVAL_WORKER_GLOBAL_SLOTS": str(global_slots),
        "EVAL_WORKER_POLL_S": "45" if with_redis else "0.08",
        "EVAL_WORKER_LEASE_S": "2.4",
        "EVAL_WORKER_HEARTBEAT_S": "0.3",
        "TEST_WORKER_WIRE": url,
        "PYTHONPATH": str(Path.cwd()),
        "PYTHONUNBUFFERED": "1",
    }
    args = ["tests/worker_driver.py"] if driver else ["tests/worker_driver.py", "--hold"]
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        *args,
        env=env,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
    )
    try:
        yield proc
    finally:
        if proc.returncode is None:
            if driver:
                await send(proc, "stop")
            else:
                proc.kill()
            try:
                await asyncio.wait_for(proc.wait(), 10)
            except TimeoutError:
                proc.kill()
                await proc.wait()
                raise
        if driver:
            assert proc.returncode == 0


async def wait_rows(
    resources: Resources, sql: str, expected: int, **params: Any
) -> list[dict[str, Any]]:
    async with asyncio.timeout(16):
        while True:
            async with resources.transaction() as db:
                rows = [dict(v) for v in (await db.execute(text(sql), params)).mappings()]
            if len(rows) == expected:
                return rows
            await asyncio.sleep(0.05)


LIVE = """SELECT c.id,c.lease_owner,c.lease_epoch,c.run_id,c.lease_expires_at,r.project_id
        FROM case_executions c JOIN evaluation_runs r ON r.id=c.run_id
        WHERE c.status='RUNNING' AND c.lease_expires_at>clock_timestamp()"""


def test_waiting_case_keeps_other_samples_unstarted(
    workload: tuple[Settings, TestClient, dict[str, str]],
) -> None:
    settings, api, tasks = workload
    run = launch(api, tasks["shop"])  # 120 samples, one admitted case at a time.

    async def exercise() -> None:
        resources = Resources(settings)
        cfg = WorkerSettings(id="first")
        try:
            await initialize(resources, cfg)
            first = await claim(resources, cfg)
            assert first and first.run == run and await release(resources, first)
            async with resources.transaction() as db:
                await db.execute(
                    text("""UPDATE case_executions SET next_retry_at=now()+interval '1 hour'
                            WHERE id=:id"""),
                    {"id": first.case},
                )
            competitor = WorkerSettings(id="second")
            assert await claim(resources, competitor) is None
            other_run = launch(api, tasks["rag"])
            other = await claim(resources, competitor)
            assert other and other.run == other_run
            async with resources.transaction() as db:
                assert (
                    await db.scalar(
                        text(
                            "SELECT count(*) FROM case_executions "
                            "WHERE run_id=:r AND started_at IS NULL"
                        ),
                        {"r": run},
                    )
                    == 119
                )
                await db.execute(
                    text("UPDATE case_executions SET next_retry_at=now() WHERE id=:id"),
                    {"id": first.case},
                )
            resumed = await claim(resources, competitor)
            assert resumed and resumed.case == first.case and resumed.epoch > first.epoch
        finally:
            await resources.close()

    asyncio.run(exercise())


def test_real_workers_quota_fairness(workload: tuple[Settings, TestClient, dict[str, str]]) -> None:
    settings, api, tasks = workload
    a, b = launch(api, tasks["shop"]), launch(api, tasks["shop"])

    async def exercise() -> None:
        resources = Resources(settings)
        arrivals: asyncio.Queue[tuple[str, asyncio.Event]] = asyncio.Queue()
        gates: list[asyncio.Event] = []

        async def wire(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            try:
                request = await reader.readuntil(b"\r\n\r\n")
                path = request.split(b" ")[1].decode()
                gate = asyncio.Event()
                gates.append(gate)
                arrivals.put_nowait((path, gate))
                await gate.wait()
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        server = await asyncio.start_server(wire, "127.0.0.1", 0)
        url = "http://127.0.0.1:" + str(server.sockets[0].getsockname()[1])

        async def arrived(action: str, case: str) -> asyncio.Event:
            path, gate = await asyncio.wait_for(arrivals.get(), 8)
            assert path == f"/{action}/{case}"
            return gate

        try:
            async with server, worker(settings, "first", url) as first:
                shop = await wait_rows(resources, LIVE, 2)
                assert {x["run_id"] for x in shop} == {a, b}
                assert {x["lease_owner"] for x in shop} == {"first"}
                launch(api, tasks["shop"])
                assert (
                    await claim(resources, WorkerSettings(id="competitor", global_slots=3)) is None
                )
                late = launch(api, tasks["rag"])
                async with worker(settings, "second", url) as second:
                    live = await wait_rows(resources, LIVE, 3)
                    assert len({x["id"] for x in live}) == 3
                    rag = next(x for x in live if x["run_id"] == late)
                    assert rag["lease_owner"] == "second"
                    c1, c2 = str(shop[0]["id"]), str(shop[1]["id"])
                    c3 = str(rag["id"])
                    await send(first, "target " + c1)
                    gate1 = await arrived("target", c1)
                    await send(first, "target " + c2)
                    assert await answer(first) == "TARGET_LIMIT " + c2
                    await send(second, "target " + c3)
                    gate2 = await arrived("target", c3)
                    gate1.set()
                    assert await answer(first) == "completed " + c1
                    await send(first, "judge " + c1)
                    judge_gate = await arrived("judge", c1)
                    await send(first, "target " + c2)
                    target_gate = await arrived("target", c2)
                    gate2.set()
                    assert await answer(second) == "completed " + c3
                    await send(second, "judge " + c3)
                    assert await answer(second) == "JUDGE_LIMIT " + c3
                    judge_gate.set()
                    target_gate.set()
                    assert {await answer(first), await answer(first)} == {
                        "completed " + c1,
                        "completed " + c2,
                    }
                    await send(second, "judge " + c3)
                    (await arrived("judge", c3)).set()
                    assert await answer(second) == "completed " + c3
                    # Leave eligible work beyond each cap and verify admission directly.
                    assert api.patch(
                        ROOT + "/projects/" + tasks["shop_id"], json={"max_parallel": 3}
                    ).is_success
                    assert (
                        await claim(resources, WorkerSettings(id="competitor", global_slots=3))
                        is None
                    )
                    await send(first, "stats")
                    task_count = int((await answer(first)).split()[1])
                    assert task_count < 40
                    print(f"480 samples, worker first asyncio tasks={task_count}, active cases=3")
                    result = api.post(ROOT + f"/evaluations/runs/{late}/pause", json={})
                    assert result.is_success
                    await wait_rows(
                        resources,
                        "SELECT id FROM evaluation_runs WHERE id=:r AND status='PAUSED'",
                        1,
                        r=late,
                    )
                    assert not await wait_rows(resources, LIVE + " AND r.id=:r", 0, r=late)
            assert not await wait_rows(resources, LIVE, 0)
        finally:
            for gate in gates:
                gate.set()
            await resources.close()

    asyncio.run(exercise())


def single_task(api: TestClient, tasks: dict[str, str]) -> str:
    previous = api.get(ROOT + "/evaluations/tasks/" + tasks["rag"]).json()["data"]
    ds = create(api, "/datasets", slug="single", name="单样本恢复")
    path = f"/datasets/{ds['id']}"
    create(
        api,
        path + "/cases",
        draft_id=ds["draft_id"],
        case_key="once",
        input={"query": "hi"},
        expected={"answer": "ok"},
    )
    draft = api.get(ROOT + path).json()["data"]["draft"]
    dv = create(
        api,
        path + "/versions",
        draft_id=ds["draft_id"],
        action="publish",
        reviewed_digest=draft["digest"],
    )
    task = create(
        api,
        "/evaluations/tasks",
        name="恢复",
        project_version_id=previous["project_version_id"],
        evaluator_version_ids=previous["evaluator_version_ids"],
        dataset_version_id=dv["id"],
    )
    return str(task["id"])


def test_worker_crash_and_fencing(workload: tuple[Settings, TestClient, dict[str, str]]) -> None:
    settings, api, tasks = workload
    task_id = single_task(api, tasks)
    launch_single = api.post(
        ROOT + f"/evaluations/tasks/{task_id}/runs",
        json={},
        headers={"Idempotency-Key": "single"},
    )
    assert launch_single.status_code == 202
    run_id = UUID(launch_single.json()["data"]["id"])

    async def exercise() -> None:
        resources = Resources(settings)
        try:
            async with worker(settings, "victim", driver=False, global_slots=1) as victim:
                old = (await wait_rows(resources, LIVE, 1))[0]
                async with worker(settings, "survivor", global_slots=1) as survivor:
                    # Observe a real renewal before the forced exit.
                    await wait_rows(
                        resources,
                        LIVE + " AND c.lease_expires_at>:expiry",
                        1,
                        expiry=old["lease_expires_at"],
                    )
                    victim.kill()
                    await victim.wait()
                    recovered = (
                        await wait_rows(resources, LIVE + " AND c.lease_owner='survivor'", 1)
                    )[0]
                    assert (
                        recovered["id"] == old["id"]
                        and recovered["lease_epoch"] > old["lease_epoch"]
                    )
                    async with resources.transaction() as db:
                        history = (
                            (
                                await db.execute(
                                    text("""SELECT * FROM execution_attempts
                            WHERE execution_id=:c ORDER BY attempt_no"""),
                                    {"c": old["id"]},
                                )
                            )
                            .mappings()
                            .all()
                        )
                        assert [v["status"] for v in history] == ["ABANDONED", "CREATED"]
                        assert (
                            history[0]["external_request_key"] == history[1]["external_request_key"]
                        )
                        assert history[0]["request_digest"] == history[1]["request_digest"]
                        with pytest.raises(IntegrityError):
                            async with db.begin_nested():
                                await db.execute(
                                    text("""UPDATE case_executions SET prediction='{}',
                                    prediction_attempt_id=:a WHERE id=:c"""),
                                    {"a": history[0]["id"], "c": old["id"]},
                                )
                        await db.execute(
                            text("""UPDATE execution_attempts SET status='RUNNING',
                            delivery_state='unknown' WHERE id=:a"""),
                            {"a": history[1]["id"]},
                        )
                    stale = Lease(
                        old["project_id"],
                        run_id,
                        old["id"],
                        "victim",
                        old["lease_epoch"],
                        history[0]["id"],
                    )
                    assert not await renew(resources, stale, 2)
                    await send(survivor, "stop")
                    await asyncio.wait_for(survivor.wait(), 8)
                    await wait_rows(
                        resources,
                        "SELECT id FROM case_executions WHERE id=:c AND status='UNKNOWN_DELIVERY'",
                        1,
                        c=old["id"],
                    )
                    assert not await wait_rows(resources, LIVE, 0)
        finally:
            await resources.close()

    asyncio.run(exercise())


def test_redis_wakes_competing_workers(
    workload: tuple[Settings, TestClient, dict[str, str]],
) -> None:
    settings, api, tasks = workload
    t = single_task(api, tasks)
    assert api.patch(ROOT + "/projects/" + tasks["rag_id"], json={"max_parallel": 2}).is_success

    async def exercise() -> None:
        resources = Resources(settings)
        try:
            async with (
                worker(settings, "left", with_redis=True, slots=1),
                worker(settings, "right", with_redis=True, slots=1),
            ):
                async with asyncio.timeout(12):
                    while True:
                        subscribers = await resources.redis.pubsub_numsub("evaluations:wake")
                        if subscribers[0][1] >= 2:
                            break
                        await asyncio.sleep(0.05)
                owners = set()
                for key in ("first", "second"):
                    res = api.post(
                        ROOT + f"/evaluations/tasks/{t}/runs",
                        json={},
                        headers={"Idempotency-Key": key},
                    )
                    assert res.status_code == 202
                    r = UUID(res.json()["data"]["id"])
                    async with asyncio.timeout(8):
                        claimed = (await wait_rows(resources, LIVE + " AND r.id=:r", 1, r=r))[0]
                    owners.add(claimed["lease_owner"])
                    async with resources.transaction() as db:
                        assert (
                            await db.scalar(
                                text("SELECT count(*) FROM execution_attempts WHERE run_id=:r"),
                                {"r": r},
                            )
                            == 1
                        )
                assert owners == {"left", "right"}
        finally:
            await resources.close()

    asyncio.run(exercise())
