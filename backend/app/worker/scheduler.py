import re
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.catalog.store import digest, row
from app.infrastructure.resources import Resources
from app.worker.settings import WorkerSettings

DUE = """(c.status='READY' OR (c.status='RETRY_WAIT' AND c.next_retry_at<=clock_timestamp()))"""
LIVE = """c.status='RUNNING' AND c.lease_expires_at>clock_timestamp()"""
CHANNEL = "evaluations:wake"
MIN_SCHEMA = "0010_trace_archives"


@dataclass(frozen=True)
class Lease:
    project: UUID
    run: UUID
    case: UUID
    owner: str
    epoch: int
    attempt: UUID | None


async def initialize(resources: Resources, cfg: WorkerSettings) -> None:
    async with resources.transaction() as db:
        revisions = (
            list(await db.scalars(text("SELECT version_num FROM alembic_version")))
            if await db.scalar(text("SELECT to_regclass('alembic_version')"))
            else []
        )
        revision = revisions[0] if len(revisions) == 1 else ""
        # Four-digit revisions share the current schema contract. Breaking migrations
        # must start a new generation (e.g. v2_0001_...) before changing Worker semantics.
        match = re.fullmatch(r"(\d{4})_[a-z][a-z0-9_]*", revision, flags=re.ASCII)
        if (
            not match
            or int(match[1]) < int(MIN_SCHEMA[:4])
            or (match[1] == MIN_SCHEMA[:4] and revision != MIN_SCHEMA)
        ):
            current = ", ".join(revisions) or "未迁移"
            raise RuntimeError(f"Worker 需要 {MIN_SCHEMA} 或同系列后续单一版本，当前为 {current}")
        await db.execute(
            text("""INSERT INTO worker_capacity(slots) VALUES (:slots)
                                 ON CONFLICT(singleton) DO NOTHING"""),
            {"slots": cfg.global_slots},
        )
        if await db.scalar(text("SELECT slots FROM worker_capacity")) != cfg.global_slots:
            raise RuntimeError("Worker global_slots 与数据库共享容量不一致")


async def new_attempt(
    db: AsyncSession, run: dict[str, Any], case: dict[str, Any], owner: str
) -> UUID | None:
    stage = case["stage"]
    evaluator = None
    external = True
    if case["prediction"] is not None:
        if stage != "SCORING":
            return None
        done = set(
            (
                await db.scalars(
                    text("""SELECT evaluator_version_id FROM evaluation_scores
                                   WHERE execution_id=:c"""),
                    {"c": case["id"]},
                )
            ).all()
        )
        node = next((n for n in run["plan"] if UUID(n["id"]) not in done), None)
        if node is None:
            return None
        evaluator = UUID(node["id"])
        external = node["impl_key"] == "chat_judge"
        stage = "EVALUATOR"
    elif stage != "ADAPTER":
        raise RuntimeError("评分阶段缺少持久化预测")
    key = digest([run["id"], case["id"], stage, evaluator])
    source = await row(
        db, "SELECT input,case_key FROM evaluation_cases WHERE id=:id", id=case["case_id"]
    )
    payload = (
        digest([run["config_digest"], stage, evaluator, source, case["prediction"]])
        if external
        else None
    )
    attempt = await row(
        db,
        """INSERT INTO execution_attempts(execution_id,run_id,epoch,lease_owner,
        stage,evaluator_version_id,attempt_no,logical_step_key,external_request_key,request_digest)
        VALUES(:c,:r,:epoch,:owner,:stage,:e,:n,:key,:external,:digest) RETURNING id""",
        c=case["id"],
        r=run["id"],
        epoch=case["lease_epoch"],
        owner=owner,
        stage=stage,
        e=evaluator,
        n=case["attempt_count"] + 1,
        key=key,
        external=key if external else None,
        digest=payload,
    )
    await db.execute(
        text("UPDATE case_executions SET attempt_count=attempt_count+1 WHERE id=:c"),
        {"c": case["id"]},
    )
    return UUID(str(attempt["id"]))


async def claim(resources: Resources, cfg: WorkerSettings) -> Lease | None:
    async with resources.transaction() as db:
        # Serialize admission only. Heartbeats, processing and outbound calls never hold this lock.
        capacity = await db.scalar(text("SELECT slots FROM worker_capacity FOR UPDATE"))
        active = await db.scalar(text(f"SELECT count(*) FROM case_executions c WHERE {LIVE}"))
        if active >= capacity:
            return None
        selection = await db.execute(
            text(f"""SELECT p.id project,r.id run
            FROM projects p JOIN evaluation_runs r ON r.project_id=p.id
            WHERE p.enabled AND r.status IN ('QUEUED','RUNNING')
            AND EXISTS(SELECT 1 FROM case_executions c WHERE c.run_id=r.id AND {DUE})
            AND (SELECT count(*) FROM case_executions c JOIN evaluation_runs v ON v.id=c.run_id
                 WHERE v.project_id=p.id AND {LIVE})<p.max_parallel
            AND (SELECT count(*) FROM case_executions c WHERE c.run_id=r.id AND {LIVE})
                 <CAST(r.config_snapshot->'policy'->>'max_parallel' AS integer)
            ORDER BY p.last_claim_at NULLS FIRST,p.id,
              (SELECT a.created_at FROM execution_attempts a WHERE a.run_id=r.id
               ORDER BY a.created_at DESC LIMIT 1) NULLS FIRST,r.created_at,r.id
            LIMIT 1 FOR UPDATE OF p,r SKIP LOCKED""")
        )
        chosen = selection.mappings().one_or_none()
        if chosen is None:
            return None
        candidate = await db.execute(
            text(f"""SELECT c.id,c.status FROM case_executions c
            WHERE c.run_id=:r AND {DUE} ORDER BY c.next_retry_at NULLS LAST,c.id
            LIMIT 1 FOR UPDATE SKIP LOCKED"""),
            {"r": chosen["run"]},
        )
        item = candidate.mappings().one_or_none()
        if item is None:
            return None
        run = await row(
            db,
            """UPDATE evaluation_runs SET status='RUNNING',
            started_at=coalesce(started_at,clock_timestamp()) WHERE id=:r RETURNING id,
            config_digest,config_snapshot->'evaluator_plan' plan""",
            r=chosen["run"],
        )
        if item["status"] == "RETRY_WAIT":
            await db.execute(
                text("UPDATE case_executions SET status='READY' WHERE id=:id"), {"id": item["id"]}
            )
        case = await row(
            db,
            """UPDATE case_executions SET status='RUNNING', lease_owner=:w,
            lease_epoch=lease_epoch+1,lease_expires_at=clock_timestamp()+make_interval(secs=>:ttl),
            next_retry_at=NULL,started_at=coalesce(started_at,clock_timestamp())
            WHERE id=:c RETURNING id,case_id,lease_epoch,attempt_count,stage,prediction""",
            c=item["id"],
            w=cfg.id,
            ttl=cfg.lease_s,
        )
        attempt = await new_attempt(db, run, case, cfg.id)
        await db.execute(
            text("UPDATE projects SET last_claim_at=clock_timestamp() WHERE id=:p"),
            {"p": chosen["project"]},
        )
        return Lease(
            chosen["project"], chosen["run"], item["id"], cfg.id, case["lease_epoch"], attempt
        )


async def renew(resources: Resources, lease: Lease, ttl: float) -> bool:
    async with resources.transaction() as db:
        result = await db.scalar(
            text("""UPDATE case_executions c SET
            lease_expires_at=clock_timestamp()+make_interval(secs=>:ttl)
            FROM evaluation_runs r,projects p WHERE r.id=c.run_id AND p.id=r.project_id
            AND p.enabled AND (r.status='RUNNING' OR (r.status='PAUSE_REQUESTED' AND EXISTS(
                SELECT 1 FROM execution_attempts a WHERE a.execution_id=c.id AND
                a.epoch=c.lease_epoch AND a.status='RUNNING' AND a.delivery_state='unknown')))
            AND c.id=:c AND c.status='RUNNING'
            AND lease_owner=:w AND lease_epoch=:epoch AND lease_expires_at>clock_timestamp()
            RETURNING c.id"""),
            {"c": lease.case, "w": lease.owner, "epoch": lease.epoch, "ttl": ttl},
        )
        return result is not None


async def release(resources: Resources, lease: Lease | None = None) -> bool:
    """Resolve one expired lease or surrender exactly the identified live lease."""
    async with resources.transaction() as db:
        params: dict[str, Any] = {}
        match = "c.lease_expires_at<=clock_timestamp()"
        if lease:
            match = "c.id=:c AND c.lease_owner=:w AND c.lease_epoch=:epoch"
            params = {"c": lease.case, "w": lease.owner, "epoch": lease.epoch}
        result = await db.execute(
            text(f"""SELECT c.id,c.run_id,c.lease_epoch,c.prediction_attempt_id,
            r.status run_status FROM evaluation_runs r JOIN case_executions c ON c.run_id=r.id
            WHERE c.status='RUNNING' AND {match} ORDER BY c.lease_expires_at,c.id
            LIMIT 1 FOR UPDATE OF r,c SKIP LOCKED"""),
            params,
        )
        case = result.mappings().one_or_none()
        if case is None:
            return False
        params = {"c": case["id"], "epoch": case["lease_epoch"], "r": case["run_id"]}
        # A committed prediction/score proves the outcome of its logical step. Other external
        # calls past the send boundary must not be replayed by the scheduler.
        uncertain = await db.scalar(
            text("""SELECT EXISTS(SELECT 1 FROM execution_attempts a
            WHERE a.execution_id=:c AND a.epoch=:epoch AND a.external_request_key IS NOT NULL
            AND (a.delivery_state<>'not_sent' OR a.status IN ('RUNNING','UNKNOWN_DELIVERY'))
            AND (a.stage<>'ADAPTER' OR CAST(:prediction AS uuid) IS NULL) AND NOT EXISTS
            (SELECT 1 FROM evaluation_scores s WHERE s.execution_id=a.execution_id
             AND s.evaluator_version_id=a.evaluator_version_id))"""),
            {**params, "prediction": case["prediction_attempt_id"]},
        )
        await db.execute(
            text("""UPDATE execution_attempts SET status='ABANDONED',
            delivery_state=CASE WHEN external_request_key IS NOT NULL AND status='RUNNING'
               AND delivery_state='not_sent' THEN 'unknown' ELSE delivery_state END,
            trace_verifier=NULL,trace_expires_at=NULL,finished_at=clock_timestamp(),
            error_code='LEASE_RELEASED' WHERE execution_id=:c AND epoch=:epoch
            AND status IN ('CREATED','RUNNING')"""),
            params,
        )
        status = (
            "UNKNOWN_DELIVERY"
            if uncertain
            else ("CANCELLED" if case["run_status"] == "CANCEL_REQUESTED" else "RETRY_WAIT")
        )
        await db.execute(
            text("""UPDATE case_executions SET status=:status,lease_owner=NULL,
            lease_epoch=lease_epoch+1,lease_expires_at=NULL,
            next_retry_at=CASE WHEN :status='RETRY_WAIT' THEN clock_timestamp() END,
            finished_at=CASE WHEN :status<>'RETRY_WAIT' THEN clock_timestamp() END
            WHERE id=:c AND status='RUNNING' AND lease_epoch=:epoch"""),
            {**params, "status": status},
        )
        if case["run_status"] in ("PAUSE_REQUESTED", "CANCEL_REQUESTED"):
            target = "PAUSED" if case["run_status"] == "PAUSE_REQUESTED" else "CANCELLED"
            await db.execute(
                text("""UPDATE evaluation_runs SET status=:target,
                finished_at=CASE WHEN :target='CANCELLED' THEN clock_timestamp() END
                WHERE id=:r AND NOT EXISTS(SELECT 1 FROM case_executions
                WHERE run_id=:r AND status='RUNNING')"""),
                {**params, "target": target},
            )
        return True


async def reconcile(resources: Resources, batch: int) -> None:
    """Rebuild terminal run state from committed cases, including after process death."""
    async with resources.transaction() as db:
        await db.execute(
            text("""WITH ready AS (
            SELECT r.id,r.status FROM evaluation_runs r
            WHERE r.status IN ('RUNNING','PAUSE_REQUESTED','CANCEL_REQUESTED')
            AND NOT EXISTS (SELECT 1 FROM case_executions c WHERE c.run_id=r.id
            AND (c.status='RUNNING' OR (r.status='RUNNING' AND c.status IN
            ('READY','PENDING','RETRY_WAIT'))))
            ORDER BY r.created_at,r.id LIMIT :batch FOR UPDATE SKIP LOCKED
        ), decisions AS (SELECT id,CASE
            WHEN status='CANCEL_REQUESTED' THEN 'CANCELLED'
            WHEN status='PAUSE_REQUESTED' AND EXISTS(SELECT 1 FROM case_executions c
                WHERE c.run_id=ready.id AND c.status IN ('PENDING','READY','RETRY_WAIT'))
                THEN 'PAUSED'
            WHEN NOT EXISTS(SELECT 1 FROM case_executions c WHERE c.run_id=ready.id
                AND c.status<>'SUCCEEDED') THEN 'COMPLETED'
            WHEN EXISTS(SELECT 1 FROM case_executions c WHERE c.run_id=ready.id
                AND c.status='SUCCEEDED') THEN 'PARTIAL_FAILED' ELSE 'FAILED' END next FROM ready)
            UPDATE evaluation_runs r SET status=d.next,finished_at=CASE WHEN d.next<>'PAUSED'
            THEN clock_timestamp() END FROM decisions d WHERE r.id=d.id"""),
            {"batch": batch},
        )
