"""Execute one durable logical step per lease; hand off only after its product commits."""

import asyncio
import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.access.identity import DevelopmentSecretResolver, SecretResolver
from app.application.catalog.evaluators import evaluate_node
from app.application.catalog.store import Fault, row, version_adapter_config
from app.contracts.adapters import AdapterError, TraceWriteScope, UnifiedRequest, UnifiedResponse
from app.contracts.evaluators import EvaluationResult, Trial
from app.contracts.runs import ExecutionPolicy
from app.infrastructure.attempt_http import Wire, observe
from app.infrastructure.resources import Resources, Settings
from app.plugins.http_adapter import DestinationPolicy, registry
from app.plugins.judge import CompatibleJudge
from app.worker.retry import QUOTA_ERRORS, delay, quota_delay
from app.worker.scheduler import Lease
from app.worker.trace import AttemptTraceIssuer


class LostLease(Exception):
    pass


async def lock_step(db: AsyncSession, lease: Lease) -> str:
    status = await db.scalar(
        text("""SELECT r.status FROM evaluation_runs r
        JOIN case_executions c ON r.id=c.run_id WHERE r.id=:r AND c.id=:c AND c.status='RUNNING'
        AND c.lease_owner=:w AND c.lease_epoch=:e AND c.lease_expires_at>clock_timestamp()
        FOR UPDATE OF r,c"""),
        {"r": lease.run, "c": lease.case, "w": lease.owner, "e": lease.epoch},
    )
    if status is None:
        raise LostLease
    return str(status)


@dataclass
class Outcome:
    prediction: UnifiedResponse | None = None
    score: EvaluationResult | None = None
    code: str | None = None
    delivery: str = "not_sent"


async def yield_case(
    db: AsyncSession,
    lease: Lease,
    status: str,
    seconds: float = 0,
    adapter_error: str | None = None,
) -> None:
    await db.execute(
        text("""UPDATE case_executions SET status=:s,lease_owner=NULL,
        lease_expires_at=NULL,next_retry_at=CASE WHEN :s='RETRY_WAIT' THEN
        clock_timestamp()+make_interval(secs=>:wait) END,finished_at=CASE WHEN :s<>'RETRY_WAIT'
        THEN clock_timestamp() END,stage=CASE WHEN :s='SUCCEEDED' THEN 'FINALIZE' ELSE stage END,
        adapter_error_code=coalesce(:error,adapter_error_code) WHERE id=:c"""),
        {"s": status, "wait": seconds, "error": adapter_error, "c": lease.case},
    )


async def record_score(db: AsyncSession, lease: Lease, result: EvaluationResult) -> None:
    values = result.model_dump()
    values.update(
        c=lease.case,
        a=lease.attempt,
        details=json.dumps(result.details),
        evidence=json.dumps(result.evidence),
    )
    inserted = await db.scalar(
        text("""INSERT INTO evaluation_scores(execution_id,
        evaluator_version_id,created_from_attempt_id,status,score,passed,reason,details,evidence)
        VALUES(:c,:evaluator_version_id,:a,:status,:score,:passed,:reason,CAST(:details AS jsonb),
        CAST(:evidence AS jsonb)) ON CONFLICT(execution_id,evaluator_version_id) DO NOTHING
        RETURNING id"""),
        values,
    )
    if not inserted:
        existing = await row(
            db,
            """SELECT created_from_attempt_id,status,score,passed,reason,details,
            evidence FROM evaluation_scores WHERE execution_id=:c AND evaluator_version_id=:e""",
            c=lease.case,
            e=result.evaluator_version_id,
        )
        if (
            existing.pop("created_from_attempt_id") != lease.attempt
            or any(
                existing[k] != v
                for k, v in result.model_dump(include=set(existing)).items()
                if k != "score"
            )
            or (float(existing["score"]) if existing["score"] is not None else None) != result.score
        ):
            raise RuntimeError("conflicting official score")


class CaseRunner:
    def __init__(self, settings: Settings, secrets: SecretResolver | None = None) -> None:
        if settings.mode == "production" and secrets is None:
            raise RuntimeError("生产 Worker 需要正式密钥提供方")
        self.settings = settings
        self.secrets = secrets or DevelopmentSecretResolver(settings.secrets)

    async def __call__(self, resources: Resources, lease: Lease) -> None:
        try:
            await self.step(resources, lease)
        except LostLease:
            return

    async def step(self, resources: Resources, lease: Lease) -> None:
        async with resources.transaction() as db:
            if await lock_step(db, lease) != "RUNNING":
                return
            if lease.attempt is None:
                errors = await db.scalar(
                    text("""SELECT count(*) FROM evaluation_scores
                    WHERE execution_id=:c AND status='ERROR'"""),
                    {"c": lease.case},
                )
                await yield_case(db, lease, "FAILED" if errors else "SUCCEEDED")
                return
            state = await row(
                db,
                """SELECT a.*,r.config_snapshot snap,r.started_at run_start,c.started_at case_start,
                c.prediction,s.case_key,s.input,s.expected,s.category,s.difficulty,s.tags,
                s.split,s.extensions FROM execution_attempts a
                JOIN evaluation_runs r ON r.id=a.run_id
                JOIN case_executions c ON c.id=a.execution_id
                JOIN evaluation_cases s ON s.id=c.case_id
                WHERE a.id=:a AND a.execution_id=:c AND a.epoch=:e AND a.status='CREATED'""",
                a=lease.attempt,
                c=lease.case,
                e=lease.epoch,
            )
        snap = state["snap"]
        policy = ExecutionPolicy.model_validate(snap["policy"])
        deadline = state["case_start"] + timedelta(seconds=policy.case_timeout_s)
        if policy.run_timeout_s is not None:
            deadline = min(deadline, state["run_start"] + timedelta(seconds=policy.run_timeout_s))
        node = next(
            (n for n in snap["evaluator_plan"] if n["id"] == str(state["evaluator_version_id"])),
            None,
        )

        async def before_send() -> None:
            async with resources.transaction() as db:
                if await lock_step(db, lease) != "RUNNING":
                    raise asyncio.CancelledError
                if datetime.now(UTC) >= deadline:
                    raise TimeoutError
                await db.execute(
                    text("""UPDATE execution_attempts SET status='RUNNING',
                    started_at=coalesce(started_at,clock_timestamp()),delivery_state='unknown'
                    WHERE id=:a"""),
                    {"a": lease.attempt},
                )

        wire = Wire(before_send, state["external_request_key"] or "", lease.attempt)
        outcome = Outcome()
        remaining = (deadline - datetime.now(UTC)).total_seconds()
        try:
            if remaining <= 0:
                raise TimeoutError
            async with asyncio.timeout(remaining):
                with observe(wire):
                    if node is None:
                        await self.target(resources, lease, state, deadline, wire, outcome)
                    else:
                        outcome.score = await self.score(resources, lease, state, node, remaining)
                        if outcome.score.status == "ERROR":
                            outcome.code = outcome.score.reason
                            if wire.status == 429:
                                outcome.code = "JUDGE_RATE_LIMIT"
                        outcome.delivery = (
                            "sent"
                            if wire.status is not None
                            else "unknown"
                            if wire.started
                            else "not_sent"
                        )
                        if wire.connect_failed and outcome.code:
                            outcome.code = "JUDGE_CONNECT_FAILED"
                            outcome.delivery = "not_sent"
        except (AdapterError, Fault) as exc:
            outcome.code = exc.code
            outcome.delivery = exc.delivery_state if isinstance(exc, AdapterError) else "not_sent"
        except TimeoutError:
            outcome.code = "CASE_TIMEOUT"
            outcome.delivery = "unknown" if wire.started else "not_sent"
        if wire.trace_token:
            # A misconfigured echo service could copy its credential into a mapped field.
            if outcome.prediction and wire.trace_token in outcome.prediction.model_dump_json():
                outcome = Outcome(code="SENSITIVE_RESPONSE", delivery="sent")
            if wire.request_id and wire.trace_token in wire.request_id:
                wire.request_id = None
        if node and outcome.code and outcome.score is None:
            outcome.score = EvaluationResult(
                evaluator_version_id=UUID(node["id"]),
                evaluator_name=node["name"],
                evaluator_version=node["version"],
                status="ERROR",
                reason=outcome.code,
            )
        await self.commit(resources, lease, state, policy, deadline, outcome, wire)

    async def target(
        self,
        resources: Resources,
        lease: Lease,
        state: dict[str, Any],
        deadline: datetime,
        wire: Wire,
        outcome: Outcome,
    ) -> None:
        snap = state["snap"]
        version = snap["project_version"]
        vid = UUID(version["id"])
        async with resources.transaction() as db:
            cfg = await version_adapter_config(db, lease.project, vid)
        if (
            cfg.model_dump(mode="json") != snap["adapter"]
            or registry.digest(cfg.kind, cfg.build) != snap["implementation_digest"]
        ):
            raise AdapterError("VERSION_UNAVAILABLE", "not_sent")
        slug = version["slug"]
        adapter = registry.create(
            cfg,
            DestinationPolicy(
                self.settings.targets.get(slug, []), self.settings.mode == "development"
            ),
            self.secrets,
            slug,
        )
        assert lease.attempt is not None
        request = UnifiedRequest(
            run_id=lease.run,
            execution_id=lease.case,
            attempt_id=lease.attempt,
            case_key=state["case_key"],
            input=state["input"],
            parameters={"seed": snap["policy"]["seed"]}
            if snap["policy"].get("seed") is not None
            else {},
            idempotency_key=wire.key,
            deadline_at=deadline,
        )
        async with resources.target_permit(lease.project, cfg.timeout_s):
            destination = urlsplit(cfg.address(cfg.endpoint))
            trace_transport = destination.scheme == "https" or (
                self.settings.mode == "development" and destination.hostname in ("127.0.0.1", "::1")
            )
            if vid in self.settings.trace_target_versions and trace_transport:
                scope = TraceWriteScope(
                    project_id=lease.project,
                    run_id=lease.run,
                    execution_id=lease.case,
                    attempt_id=lease.attempt,
                    epoch=lease.epoch,
                )
                wire.trace_token = await AttemptTraceIssuer(resources).issue(scope, deadline)
            prediction = await adapter.execute(request)
            outcome.prediction = UnifiedResponse.model_validate(
                prediction.model_dump(), strict=True
            )
            outcome.delivery = "sent"

    async def score(
        self,
        resources: Resources,
        lease: Lease,
        state: dict[str, Any],
        node: dict[str, Any],
        remaining: float,
    ) -> EvaluationResult:
        plan = {n["id"]: n for n in state["snap"]["evaluator_plan"]}
        async with resources.transaction() as db:
            if await lock_step(db, lease) != "RUNNING":
                raise LostLease
            if not state["external_request_key"]:
                await db.execute(
                    text("""UPDATE execution_attempts SET status='RUNNING',
                    started_at=clock_timestamp() WHERE id=:a"""),
                    {"a": lease.attempt},
                )
            scores = (
                await db.execute(
                    text("""SELECT evaluator_version_id,status,score,passed,
                reason,details,evidence FROM evaluation_scores WHERE execution_id=:c"""),
                    {"c": lease.case},
                )
            ).mappings()
            children = {
                s["evaluator_version_id"]: EvaluationResult.model_validate(
                    {
                        **s,
                        "evaluator_name": plan[str(s["evaluator_version_id"])]["name"],
                        "evaluator_version": plan[str(s["evaluator_version_id"])]["version"],
                    }
                )
                for s in scores
            }
        body = Trial.model_validate(
            {
                "version_id": node["id"],
                "case": {
                    k: state[k]
                    for k in (
                        "case_key",
                        "input",
                        "expected",
                        "category",
                        "difficulty",
                        "tags",
                        "split",
                        "extensions",
                    )
                },
                "prediction": state["prediction"],
                "materials": {
                    "approved_policy_ids": state["snap"]["policy"].get("approved_policy_ids", [])
                },
            }
        )
        return await evaluate_node(
            UUID(node["id"]),
            node,
            body,
            CompatibleJudge(self.settings, self.secrets, resources),
            time.monotonic() + remaining,
            children,
            timeout_code="EVALUATOR_TIMEOUT",
        )

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
        async with resources.transaction() as db:
            run_status = await lock_step(db, lease)
            if run_status == "CANCEL_REQUESTED":
                return
            stage, code = state["stage"], outcome.code
            wait = None
            if code and code in policy.retryable_error_codes:
                previous = await row(
                    db,
                    """SELECT count(*) FILTER (WHERE error_code IN ('TARGET_LIMIT','JUDGE_LIMIT'))
                        AS waits, count(*) FILTER (WHERE coalesce(error_code,'') NOT IN
                        ('TARGET_LIMIT','JUDGE_LIMIT')) AS calls FROM execution_attempts
                        WHERE execution_id=:c AND logical_step_key=:key AND id<>:a
                        AND status<>'ABANDONED'""",
                    c=lease.case,
                    key=state["logical_step_key"],
                    a=lease.attempt,
                )
                if code in QUOTA_ERRORS and outcome.delivery == "not_sent":
                    wait = quota_delay(
                        policy, previous["waits"] + 1, wire.quota_available_in, deadline
                    )
                elif code not in QUOTA_ERRORS:
                    verified = (
                        policy.target_replay == "verified_idempotent"
                        and UUID(state["snap"]["project_version"]["id"])
                        in self.settings.verified_idempotent_versions
                    )
                    wait = delay(
                        policy,
                        stage=stage,
                        count=previous["calls"] + 1,
                        code=code,
                        delivery=outcome.delivery,
                        status=wire.status,
                        header=wire.retry_after,
                        deadline=deadline,
                        verified=verified,
                    )
            unknown = outcome.delivery == "unknown" and wait is None
            terminal = (
                "UNKNOWN_DELIVERY"
                if unknown
                else "TIMED_OUT"
                if code and "TIMEOUT" in code
                else "FAILED"
                if code
                else "SUCCEEDED"
            )
            # A judge may skip or reject input before the HTTP hook executes.
            await db.execute(
                text("""UPDATE execution_attempts SET status='RUNNING',
                started_at=clock_timestamp() WHERE id=:a AND status='CREATED'"""),
                {"a": lease.attempt},
            )
            await db.execute(
                text("""UPDATE execution_attempts SET status=:s,delivery_state=:d,
                trace_verifier=NULL,trace_expires_at=NULL,finished_at=clock_timestamp(),
                latency_ms=greatest(0,extract(epoch FROM clock_timestamp()-started_at)*1000)::int,
                error_code=:code,sanitized_error=:code,provider_request_id=:provider
                WHERE id=:a"""),
                {
                    "a": lease.attempt,
                    "s": terminal,
                    "d": outcome.delivery,
                    "code": code,
                    "provider": wire.request_id[:250] if wire.request_id else None,
                },
            )
            if wait is not None:
                await yield_case(db, lease, "RETRY_WAIT", wait)
                return
            if outcome.prediction:
                await db.execute(
                    text("""UPDATE case_executions SET prediction=CAST(:p AS jsonb),
                    prediction_attempt_id=:a,stage='SCORING',latency_ms=:ms WHERE id=:c"""),
                    {
                        "p": outcome.prediction.model_dump_json(),
                        "a": lease.attempt,
                        "c": lease.case,
                        "ms": outcome.prediction.latency_ms,
                    },
                )
            elif outcome.score:
                await record_score(db, lease, outcome.score)
            if unknown or code == "CASE_TIMEOUT" or (stage == "ADAPTER" and code):
                await yield_case(
                    db, lease, terminal, adapter_error=code if stage == "ADAPTER" else None
                )
            else:
                await yield_case(db, lease, "RETRY_WAIT")
