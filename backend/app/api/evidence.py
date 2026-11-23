"""Read frozen score citations with the same project permission as a trace detail."""

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Request
from sqlalchemy import text

from app.api.access import Actor, Db, authorize
from app.api.projects import reply
from app.application.catalog.store import Fault, row
from app.application.trace.evidence import SCOPE, resolve
from app.contracts.evidence import CaseEvidence, Evidence, EvidenceResponse, ScoreEvidence

router = APIRouter(prefix="/api/v1", tags=["evidence"])


async def scope_for(db: Db, who: Actor, execution: UUID) -> dict[str, Any]:
    owner = await row(
        db,
        """SELECT r.project_id FROM case_executions c
        JOIN evaluation_runs r ON r.id=c.run_id WHERE c.id=:id""",
        id=execution,
    )
    await authorize(db, who, owner["project_id"], "R", key_read=True, key_scope="results:read")
    return await row(db, SCOPE, id=execution)


async def scores_for(db: Db, execution: UUID) -> list[dict[str, Any]]:
    scores = await db.execute(
        text("""SELECT s.id,s.evaluator_version_id,s.created_at,s.evidence,
        v.kind,s.details->'children' children FROM evaluation_scores s
        JOIN evaluator_versions ev ON ev.id=s.evaluator_version_id
        JOIN evaluators v ON v.id=ev.evaluator_id
        WHERE s.execution_id=:id ORDER BY s.created_at,s.id"""),
        {"id": execution},
    )
    return [dict(s) for s in scores.mappings()]


async def entry(
    db: Db, scope: dict[str, Any], score: dict[str, Any], ordinal: int
) -> dict[str, Any]:
    result = await resolve(db, scope, score["evidence"][ordinal])
    return {
        **result,
        "evidence_id": f"{score['id']}.{ordinal}",
        "score_id": score["id"],
        "evaluator_version_id": score["evaluator_version_id"],
        "created_at": score["created_at"],
    }


async def group(
    db: Db, scope: dict[str, Any], score: dict[str, Any], scores: list[dict[str, Any]]
) -> dict[str, Any]:
    children = []
    if score["kind"] == "composite":
        by_version = {str(s["evaluator_version_id"]): s["id"] for s in scores}
        for member in score["children"] or []:
            version = member["child_version_id"]
            children.append(
                {
                    "score_id": by_version.get(version),
                    "evaluator_version_id": version,
                    "position": member["position"],
                    "weight": member["weight"],
                }
            )
    items = [await entry(db, scope, score, i) for i in range(len(score["evidence"]))]
    return {
        "score_id": score["id"],
        "evaluator_version_id": score["evaluator_version_id"],
        "items": items,
        "children": children,
    }


@router.get(
    "/case-executions/{execution_id}/evidence", response_model=EvidenceResponse[CaseEvidence]
)
async def execution_evidence(
    req: Request, db: Db, who: Actor, execution_id: UUID
) -> dict[str, object]:
    scope = await scope_for(db, who, execution_id)
    scores = await scores_for(db, execution_id)
    return reply(req, {"scores": [await group(db, scope, s, scores) for s in scores]})


async def scope_for_score(db: Db, who: Actor, score_id: UUID) -> dict[str, Any]:
    result = await row(db, "SELECT execution_id FROM evaluation_scores WHERE id=:id", id=score_id)
    return await scope_for(db, who, result["execution_id"])


@router.get(
    "/evaluations/scores/{score_id}/evidence", response_model=EvidenceResponse[ScoreEvidence]
)
async def score_evidence(req: Request, db: Db, who: Actor, score_id: UUID) -> dict[str, object]:
    scope = await scope_for_score(db, who, score_id)
    scores = await scores_for(db, scope["id"])
    score = next(s for s in scores if s["id"] == score_id)
    return reply(req, await group(db, scope, score, scores))


@router.get("/evidence/{evidence_id}", response_model=EvidenceResponse[Evidence])
async def evidence_detail(req: Request, db: Db, who: Actor, evidence_id: str) -> dict[str, object]:
    missing = Fault(404, "EVIDENCE_UNAVAILABLE", "证据不存在或当前不可见")
    raw, _, number = evidence_id.partition(".")
    try:
        score_id = UUID(raw)
        ordinal = int(number)
        if str(ordinal) != number or ordinal < 0:
            raise ValueError
    except ValueError:
        raise missing from None
    try:
        scope = await scope_for_score(db, who, score_id)
    except Fault as exc:
        if exc.status == 404:
            raise missing from None
        raise
    score = await row(
        db,
        "SELECT id,evaluator_version_id,evidence,created_at FROM evaluation_scores WHERE id=:id",
        id=score_id,
    )
    if ordinal >= len(score["evidence"]):
        raise missing
    return reply(req, await entry(db, scope, score, ordinal))
