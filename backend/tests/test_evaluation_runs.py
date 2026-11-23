import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from redis import Redis
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.api.main import create_app
from app.contracts.evaluators import JudgeAccount
from app.contracts.http import TargetRule
from app.infrastructure.resources import Resources, Settings
from app.plugins.evaluators import BUILD

ROOT = "/api/v1"


def create(client: TestClient, path: str, **body: Any) -> dict[str, Any]:
    res = client.post(ROOT + path, json=body)
    assert res.status_code == 201, res.text
    return dict(res.json()["data"])


def prepare(client: TestClient, build: str = BUILD) -> dict[str, Any]:
    p = create(
        client,
        "/projects",
        slug="shop",
        name="商店",
        kind="custom",
        max_parallel=2,
        target_max_parallel=1,
        rpm=60,
    )
    pv = create(
        client,
        f"/projects/{p['id']}/versions",
        label="v1",
        deployment_ref="stable-1",
        adapter=dict(
            endpoint="http://127.0.0.1:12345/query",
            probe_endpoint="http://127.0.0.1:12345/ready",
            secret_ref="private-ref",
            secret_version="v1",
        ),
    )
    ds = create(client, "/datasets", slug="shared", name="题库")
    dp = f"/datasets/{ds['id']}"
    for key in ("one", "two", "three"):
        create(
            client,
            dp + "/cases",
            draft_id=ds["draft_id"],
            case_key=key,
            input={"query": key},
            expected={"answer": key},
        )
    draft = client.get(ROOT + dp).json()["data"]["draft"]
    dv = create(
        client,
        dp + "/versions",
        action="publish",
        draft_id=draft["id"],
        reviewed_digest=draft["digest"],
    )
    leaf = create(client, "/evaluators", kind="code", slug="answer", name="答案")
    exact = create(
        client,
        f"/evaluators/{leaf['id']}/versions",
        version="v1",
        impl_key="exact_match",
        build=build,
        config={},
    )
    owner = create(client, "/evaluators", kind="llm_judge", slug="judge", name="裁判")
    judge = create(
        client,
        f"/evaluators/{owner['id']}/versions",
        version="v1",
        impl_key="chat_judge",
        build=build,
        config=dict(
            provider_account_id="acct",
            secret_ref="judge-ref",
            secret_version="v1",
            dimensions=["accuracy"],
        ),
        provider="openai_compatible",
        model_id="model-a",
        prompt_text="private-prompt",
        prompt_version="p1",
        rubric_version="r1",
    )
    parent = create(client, "/evaluators", kind="composite", slug="combined", name="组合")
    cp = f"/evaluators/{parent['id']}/versions"

    def combine(label: str, *members: dict[str, Any]) -> dict[str, Any]:
        return create(
            client,
            cp,
            version=label,
            impl_key="weighted",
            build=build,
            config={},
            members=[
                dict(position=i, child_version_id=m["id"], weight=i + 1)
                for i, m in enumerate(members)
            ],
        )

    sub = combine("v1", exact, judge)
    root = combine("v2", sub, exact)
    return dict(project=p, pv=pv, dataset=ds, dv=dv, root=root, exact=exact, judge=judge)


def test_plans_runs_and_recovery(catalog: tuple[Settings, dict[str, str]]) -> None:
    settings, tokens = catalog
    gate = TargetRule(scheme="http", host="127.0.0.1", port=12345, networks=["127.0.0.1/32"])
    account = JudgeAccount(
        endpoint="http://127.0.0.1:12345/judge",
        destination=gate,
        models=["model-a"],
        max_parallel=1,
        rpm=10,
    )
    settings = settings.model_copy(
        update={"targets": {"shop": [gate]}, "judge_accounts": {"acct": account}}
    )
    headers = {"Authorization": "Bearer " + tokens["curator"]}
    with TestClient(create_app(settings), headers=headers) as client:
        f = prepare(client)
        p = f["project"]["id"]
        body = dict(
            project_id=p,
            project_version_id=f["pv"]["id"],
            dataset_version_id=f["dv"]["id"],
            evaluator_version_ids=[f["root"]["id"]],
            name="回归",
        )
        draft = create(client, "/datasets/" + f["dataset"]["id"] + "/versions", action="draft")
        for override in (
            {"project_id": str(uuid4())},
            {"dataset_version_id": draft["id"]},
            {"evaluator_version_ids": [str(uuid4())]},
            {"policy": {"max_parallel": 0}},
        ):
            assert (
                client.post(ROOT + "/evaluations/tasks", json={**body, **override}).status_code
                == 422
            )
        incompatible = create(
            client,
            f"/projects/{p}/versions",
            label="v2",
            deployment_ref="stable-2",
            adapter=dict(
                endpoint="http://127.0.0.1:12345/query",
                probe_endpoint="http://127.0.0.1:12345/ready",
                request_map={"body": "input.missing"},
            ),
        )
        assert (
            client.post(
                ROOT + "/evaluations/tasks", json={**body, "project_version_id": incompatible["id"]}
            ).json()["error"]["code"]
            == "INCOMPATIBLE_INPUT"
        )
        task = create(client, "/evaluations/tasks", **body)
        path = ROOT + "/evaluations/tasks/" + task["id"]
        assert client.patch(path, json={"dataset_version_id": draft["id"]}).status_code == 422
        assert (
            client.patch(path, json={"description": "备注"}).json()["data"]["config_digest"]
            == task["config_digest"]
        )
        assert (
            client.get(ROOT + "/evaluations/tasks", params={"project_id": p}).json()["data"][
                "items"
            ][0]["id"]
            == task["id"]
        )
        assert (
            client.get(path, headers={"Authorization": "Bearer " + tokens["shop"]}).status_code
            == 404
        )
        plan = client.get(path).json()["data"]["snapshot"]
        assert len(plan["evaluator_plan"]) == 4 and plan["roots"] == [f["root"]["id"]]
        assert "private-prompt" not in json.dumps(plan) and "private-ref" not in json.dumps(plan)
        assert client.post(path + "/runs", json={}).status_code == 422

        def launch(key: str) -> dict[str, Any]:
            res = client.post(path + "/runs", headers={"Idempotency-Key": key}, json={})
            assert res.status_code == 202, res.text
            return dict(res.json()["data"])

        # Listen on a real subscription before starting the competing requests.
        redis = Redis.from_url(settings.redis_url.get_secret_value(), decode_responses=True)
        with redis, redis.pubsub() as events:  # type: ignore[no-untyped-call]
            events.subscribe("evaluations:wake")
            assert events.get_message(timeout=2)["type"] == "subscribe"
            with ThreadPoolExecutor(max_workers=4) as pool:
                same = list(pool.map(launch, ["one"] * 4))
            run = same[0]
            assert events.get_message(timeout=2)["data"] == run["id"]
        with ThreadPoolExecutor(max_workers=2) as pool:
            different = list(pool.map(launch, ["two", "three"]))
        assert len({x["id"] for x in same}) == 1
        assert len({x["id"] for x in [run, *different]}) == 3
        assert run["status"] == "QUEUED" and run["total_cases"] == run["pending_cases"] == 3
        assert run["snapshot"] == plan and run["snapshot_digest"] == task["config_digest"]
        rp = ROOT + "/evaluations/runs/" + run["id"]
        res = client.get(rp + "/results", params={"limit": 2}).json()["data"]
        remaining = client.get(
            rp + "/results", params={"limit": 2, "cursor": res["next_cursor"]}
        ).json()["data"]
        items = res["items"] + remaining["items"]
        assert len({v["case_key"] for v in items}) == 3
        assert all(
            v["status"] == "READY"
            and not v["attempts"]
            and v["prediction"] is None
            and not v["scores"]
            for v in items
        )
        for action, state in (
            ("pause", "PAUSED"),
            ("pause", "PAUSED"),
            ("resume", "QUEUED"),
            ("cancel", "CANCELLED"),
            ("cancel", "CANCELLED"),
        ):
            assert client.post(rp + "/" + action, json={}).json()["data"]["status"] == state
        assert client.get(rp).json()["data"]["cancelled_cases"] == 3
        assert client.post(rp + "/resume", json={}).status_code == 409
        key = create(
            client,
            f"/projects/{p}/api-keys",
            name="结果",
            scopes=["results:read"],
            expires_at="2090-01-01T00:00:00Z",
        )
        reader = {"Authorization": "ApiKey " + key["secret"]}
        assert client.get(rp, headers=reader).status_code == 200
        assert client.get(path, headers=reader).status_code == 403
        assert client.post(rp + "/pause", headers=reader, json={}).status_code == 403
        asyncio.run(check_db(settings, task, run, different[0], f))
    # A fresh API process connects to the same database; Redis is unreachable.
    down = settings.model_copy(update={"redis_url": SecretStr("redis://127.0.0.1:1/0")})
    with TestClient(create_app(down), headers=headers) as disconnected:
        response = disconnected.post(
            path + "/runs", headers={"Idempotency-Key": "offline"}, json={}
        )
        assert response.status_code == 202, response.text
        offline = response.json()["data"]
    with TestClient(create_app(settings), headers=headers) as restarted:
        saved = restarted.get(ROOT + "/evaluations/runs/" + offline["id"]).json()["data"]
        assert saved["status"] == "QUEUED" and saved["pending_cases"] == 3
        replay = restarted.post(path + "/runs", headers={"Idempotency-Key": "offline"}, json={})
        assert replay.json()["data"]["id"] == offline["id"]
        assert len(restarted.get(path + "/runs").json()["data"]["items"]) == 4


async def check_db(
    settings: Settings,
    task: dict[str, Any],
    cancelled: dict[str, Any],
    queued: dict[str, Any],
    fixture: dict[str, Any],
) -> None:
    resources = Resources(settings)
    try:
        async with resources.transaction() as db:
            params: dict[str, Any] = dict(
                t=UUID(task["id"]),
                r=UUID(queued["id"]),
                stopped=UUID(cancelled["id"]),
                e=UUID(fixture["exact"]["id"]),
                a=uuid4(),
            )
            saved = await db.scalar(
                text("SELECT config_snapshot FROM evaluation_runs WHERE id=:r"), params
            )
            assert saved["adapter"]["secret_ref"] == "private-ref"
            judge = next(n for n in saved["evaluator_plan"] if n["impl_key"] == "chat_judge")
            assert (
                judge["prompt_text"] == "private-prompt"
                and judge["config"]["provider_account_id"] == "acct"
            )
            assert (
                await db.scalar(
                    text("SELECT count(*) FROM task_evaluators WHERE task_id=:t"), params
                )
                == 4
            )
            params["c"] = await db.scalar(
                text("SELECT id FROM case_executions WHERE run_id=:r LIMIT 1"), params
            )

            async def reject(sql: str) -> None:
                with pytest.raises(IntegrityError):
                    async with db.begin_nested():
                        await db.execute(text(sql), params)

            await reject("UPDATE evaluation_tasks SET config_digest='other' WHERE id=:t")
            await reject("UPDATE evaluation_runs SET config_snapshot='{}' WHERE id=:r")
            await reject("UPDATE evaluation_runs SET status='QUEUED' WHERE id=:stopped")
            await reject("""INSERT INTO case_executions(run_id,dataset_version_id,case_id)
                SELECT run_id,dataset_version_id,case_id FROM case_executions WHERE id=:c""")
            await reject("UPDATE case_executions SET status='SUCCEEDED' WHERE id=:c")
            await db.execute(
                text("UPDATE evaluation_runs SET status='RUNNING' WHERE id=:r"), params
            )
            await db.execute(
                text("""UPDATE case_executions SET status='RUNNING',lease_epoch=1,
                lease_owner='tester',lease_expires_at=now()+interval '1 minute' WHERE id=:c"""),
                params,
            )
            attempt = """INSERT INTO execution_attempts(id,execution_id,run_id,epoch,lease_owner,
                stage,evaluator_version_id,attempt_no,logical_step_key,external_request_key,
                request_digest)
                VALUES (:a,:c,:r,1,'tester','EVALUATOR',:e,2,'step','external','d')"""
            await reject(attempt)
            adapter_id = uuid4()
            await db.execute(
                text("""INSERT INTO execution_attempts(id,execution_id,run_id,epoch,
                lease_owner,stage,attempt_no,logical_step_key,external_request_key,request_digest)
                VALUES(:adapter,:c,:r,1,'tester','ADAPTER',1,'prediction','target-key','d')"""),
                {**params, "adapter": adapter_id},
            )
            for state in ("RUNNING", "SUCCEEDED"):
                await db.execute(
                    text("UPDATE execution_attempts SET status=:s WHERE id=:adapter"),
                    {"s": state, "adapter": adapter_id},
                )
            await db.execute(
                text("""UPDATE case_executions SET prediction='{"answer":"ok"}',
                prediction_attempt_id=:adapter,stage='SCORING' WHERE id=:c"""),
                {**params, "adapter": adapter_id},
            )
            await db.execute(text(attempt), params)
            await reject(attempt.replace(":a,:c", "gen_random_uuid(),:c"))
            for state in ("RUNNING", "SUCCEEDED"):
                await db.execute(
                    text("UPDATE execution_attempts SET status=:s WHERE id=:a"),
                    {**params, "s": state},
                )
            score = """INSERT INTO evaluation_scores(execution_id,evaluator_version_id,
                created_from_attempt_id,
                status,score,passed,reason) VALUES(:c,:e,:a,'OK',0,false,'valid low score')"""
            await reject(score.replace("'OK'", "'ERROR'"))
            await reject(score.replace(":c,:e,:a", ":c,gen_random_uuid(),:a"))
            await reject(
                "UPDATE case_executions SET prediction='{}',prediction_attempt_id=:a WHERE id=:c"
            )
            await db.execute(text(score), params)
            await reject(score)
            await reject("UPDATE evaluation_scores SET score=1 WHERE execution_id=:c")
            await db.execute(
                text(attempt.replace(":a,:c", "gen_random_uuid(),:c").replace(":e,2", ":e,3")),
                params,
            )
            await db.execute(text("UPDATE case_executions SET lease_epoch=2 WHERE id=:c"), params)
            await reject(
                "UPDATE execution_attempts SET status='RUNNING' WHERE execution_id=:c "
                "AND attempt_no=3"
            )
            # Roll back test products before returning the database to the API.
            await db.rollback()
    finally:
        await resources.close()
