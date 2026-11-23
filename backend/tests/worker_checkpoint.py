"""Exit a disposable Worker at a specified durability boundary; the replacement uses the CLI."""

import asyncio
import os
import sys
from datetime import datetime
from typing import Any

from app.contracts.runs import ExecutionPolicy
from app.infrastructure.attempt_http import Wire
from app.infrastructure.resources import Resources, Settings
from app.plugins.evaluators import BUILD
from app.plugins.evaluators import registry as evaluators
from app.plugins.http_adapter import registry as adapters
from app.worker.execution import CaseRunner, Outcome
from app.worker.main import serve
from app.worker.scheduler import Lease
from app.worker.settings import WorkerSettings


class CheckpointRunner(CaseRunner):
    async def step(self, resources: Resources, lease: Lease) -> None:
        if point == "before_send":
            os._exit(77)
        await super().step(resources, lease)
        if point == "before_summary" and lease.attempt is None:
            os._exit(77)

    async def commit(
        self,
        resources: Resources,
        lease: Lease,
        state: dict[str, Any],
        policy: ExecutionPolicy,
        deadline: datetime,
        outcome: Outcome,
        wire: Wire,
    ) -> None:
        if point == "before_prediction" and outcome.prediction:
            os._exit(77)
        await super().commit(resources, lease, state, policy, deadline, outcome, wire)
        if (point == "after_prediction" and outcome.prediction) or (
            point == "after_score" and outcome.score
        ):
            os._exit(77)


if __name__ == "__main__":
    point = sys.argv[1]
    config = Settings()
    if point == "missing_adapter":
        del adapters._plugins["http", "2"]
    if point == "missing_evaluator":
        del evaluators.plugins["exact_match", BUILD]
    asyncio.run(serve(config, WorkerSettings(), asyncio.Event(), CheckpointRunner(config)))
