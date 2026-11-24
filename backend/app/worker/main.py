"""Run the independent Case scheduler with python -m app.worker.main."""

import asyncio
import logging
import signal
from collections.abc import Awaitable, Callable
from contextlib import suppress
from functools import partial

from asyncpg.exceptions import (  # type: ignore[import-untyped]
    CannotConnectNowError,
    ObjectNotInPrerequisiteStateError,
    PostgresConnectionError,
)
from redis.exceptions import RedisError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.infrastructure.resources import Resources, Settings
from app.plugins.evaluators import registry as evaluator_registry
from app.plugins.http_adapter import registry as adapter_registry
from app.worker.execution import CaseRunner
from app.worker.quality import maintain_quality
from app.worker.scheduler import CHANNEL, Lease, claim, initialize, reconcile, release, renew
from app.worker.settings import WorkerSettings

log = logging.getLogger("app.worker")
type Stage = Callable[[Resources, Lease], Awaitable[None]]


async def notifications(resources: Resources, wakeup: asyncio.Event, cfg: WorkerSettings) -> None:
    while True:
        try:
            async with resources.redis.pubsub() as channel:
                await channel.subscribe(CHANNEL)
                async for item in channel.listen():
                    if item["type"] == "message":
                        wakeup.set()
        except (RedisError, OSError):
            await asyncio.sleep(cfg.poll_s)


async def maintain(
    resources: Resources,
    lease: Lease,
    cfg: WorkerSettings,
    stop: asyncio.Event,
    stage: Stage,
) -> None:
    shutdown = asyncio.create_task(stop.wait())
    execution = asyncio.ensure_future(stage(resources, lease))
    signals = {execution, shutdown}
    try:
        while not stop.is_set():
            done, _ = await asyncio.wait(
                signals, timeout=cfg.heartbeat_s, return_when=asyncio.FIRST_COMPLETED
            )
            if done:
                if execution in done:
                    execution.result()
                break
            if not await renew(resources, lease, cfg.lease_s):
                break
    finally:
        for task in signals:
            task.cancel()
        await asyncio.gather(*signals, return_exceptions=True)
        # A control transaction may briefly own the Run row. Retry the fenced surrender;
        # a restarted worker also recovers any lease left by a database outage.
        async with asyncio.timeout(cfg.shutdown_s):
            while not await release(resources, lease):
                async with resources.transaction() as db:
                    held = await db.scalar(
                        text("""SELECT 1 FROM case_executions WHERE id=:c
                        AND status='RUNNING' AND lease_owner=:w AND lease_epoch=:e"""),
                        {"c": lease.case, "w": lease.owner, "e": lease.epoch},
                    )
                if not held:
                    break
                await asyncio.sleep(min(cfg.poll_s, 0.1))


async def serve(
    settings: Settings, cfg: WorkerSettings, stop: asyncio.Event, stage: Stage | None = None
) -> None:
    stage = stage or CaseRunner(settings)
    resources = Resources(settings)
    active: set[asyncio.Task[None]] = set()
    listener = None
    wakeup = asyncio.Event()
    stopping = asyncio.create_task(stop.wait())
    stopping.add_done_callback(lambda _: wakeup.set())

    updates: asyncio.Queue[str] = asyncio.Queue(maxsize=256)

    async def publish_progress() -> None:
        while True:
            changed = {await updates.get()}
            await asyncio.sleep(0.2)
            while not updates.empty():
                changed.add(updates.get_nowait())
            try:
                async with asyncio.timeout(0.2):
                    async with resources.redis.pipeline(transaction=False) as batch:
                        for run_id in changed:
                            batch.publish("evaluations:progress:" + run_id, "changed")
                        await batch.execute()
            except (RedisError, TimeoutError, OSError):
                pass

    publisher = asyncio.create_task(publish_progress())
    quality = asyncio.create_task(maintain_quality(resources, settings, stop))

    def finished(run_id: str, task: asyncio.Task[None]) -> None:
        active.discard(task)
        if not updates.full():
            updates.put_nowait(run_id)
        if not task.cancelled() and task.exception() is not None:
            log.warning("lease maintenance failed: %s", type(task.exception()).__name__)
        wakeup.set()

    try:
        await initialize(resources, cfg)
        log.info(
            "worker=%s slots=%s adapters=%d evaluators=%d",
            cfg.id,
            cfg.slots,
            len(adapter_registry.describe()),
            len(evaluator_registry.plugins),
        )
        listener = asyncio.create_task(notifications(resources, wakeup, cfg))
        while not stop.is_set():
            wakeup.clear()
            try:
                await reconcile(resources, cfg.batch)
                for _ in range(cfg.batch):
                    if not await release(resources):
                        break
                for _ in range(min(cfg.batch, cfg.slots - len(active))):
                    if stop.is_set():
                        break
                    lease = await claim(resources, cfg)
                    if lease is None:
                        break
                    task = asyncio.create_task(maintain(resources, lease, cfg, stop, stage))
                    active.add(task)
                    task.add_done_callback(partial(finished, str(lease.run)))
            except (
                SQLAlchemyError,
                OSError,
                TimeoutError,
                CannotConnectNowError,
                ObjectNotInPrerequisiteStateError,
                PostgresConnectionError,
            ) as exc:
                # asyncpg can reject a new connection before SQLAlchemy wraps the error.
                log.warning("database scheduling pass interrupted: %s", type(exc).__name__)
            try:
                await asyncio.wait_for(wakeup.wait(), cfg.poll_s)
            except TimeoutError:
                pass
    finally:
        stop.set()
        await stopping
        if listener is not None:
            listener.cancel()
            with suppress(asyncio.CancelledError):
                await listener
        if active:
            _, pending = await asyncio.wait(active.copy(), timeout=cfg.shutdown_s)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        publisher.cancel()
        quality.cancel()
        await asyncio.gather(publisher, quality, return_exceptions=True)
        await resources.close()


async def main() -> None:
    logging.getLogger("httpx").setLevel(logging.WARNING)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stop.set))
    settings = Settings()
    await serve(settings, WorkerSettings(), stop)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
