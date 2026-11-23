"""Process fixture: exercise the production supervisor and its stage callback under test control."""

import asyncio
import os
import sys
from uuid import UUID

import httpx

from app.application.catalog.store import Fault
from app.contracts.evaluators import JudgeAccount
from app.contracts.http import TargetRule
from app.infrastructure.resources import Resources, Settings
from app.worker.main import serve
from app.worker.scheduler import Lease
from app.worker.settings import WorkerSettings


async def driver() -> None:
    cfg = WorkerSettings()
    stop = asyncio.Event()
    commands: dict[UUID, asyncio.Queue[str]] = {}
    url = os.environ["TEST_WORKER_WIRE"]
    account = JudgeAccount(
        endpoint=url,
        destination=TargetRule(host="localhost", networks=["127.0.0.1/32"]),
        models=["local"],
        max_parallel=1,
        rpm=60,
    )

    async def stage(resources: Resources, lease: Lease) -> None:
        queue = commands.setdefault(lease.case, asyncio.Queue())
        try:
            while True:
                action = await queue.get()
                permit = (
                    resources.target_permit(lease.project, 20)
                    if action == "target"
                    else resources.judge_permit("shared-integration-account", account, 20)
                )
                try:
                    async with permit, httpx.AsyncClient(trust_env=False, timeout=20) as client:
                        response = await client.get(url + "/" + action + "/" + str(lease.case))
                        response.raise_for_status()
                    print("completed " + str(lease.case), flush=True)
                except Fault as exc:
                    print(exc.code + " " + str(lease.case), flush=True)
        finally:
            commands.pop(lease.case, None)

    async def control() -> None:
        while True:
            line = (await asyncio.to_thread(sys.stdin.readline)).strip()
            if not line or line == "stop":
                stop.set()
                return
            if line == "stats":
                print("tasks " + str(len(asyncio.all_tasks())), flush=True)
            else:
                action, id_ = line.split()
                commands.setdefault(UUID(id_), asyncio.Queue()).put_nowait(action)

    async with asyncio.TaskGroup() as group:
        group.create_task(control())
        group.create_task(serve(Settings(), cfg, stop, stage))


if __name__ == "__main__":
    if "--hold" in sys.argv:

        async def hold(resources: Resources, lease: Lease) -> None:
            await asyncio.Event().wait()

        asyncio.run(serve(Settings(), WorkerSettings(), asyncio.Event(), hold))
    else:
        asyncio.run(driver())
