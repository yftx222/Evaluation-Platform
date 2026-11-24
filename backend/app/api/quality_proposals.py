import json
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Header, Request, Response
from pydantic import Field, ValidationError
from sqlalchemy import text

from app.api.access import Db
from app.api.production_traces import bounded_json
from app.api.projects import reply
from app.api.quality_access import Grant, require
from app.application.catalog import datasets
from app.application.catalog.store import Fault, audit, digest, row
from app.contracts.adapters import Contract
from app.contracts.datasets import Case, Key, VersionAction
from app.contracts.quality import Name
from app.contracts.traces import check_fields

router = APIRouter(prefix="/api/v1/datasets", tags=["case-proposals"])


class ProjectScope(Contract):
    project_id: UUID
    project_slug: Name
    environment: Name


class Proposal(Contract):
    proposal_id: UUID
    case_id: UUID
    review_id: UUID
    project: ProjectScope
    dataset_id: UUID
    case_key: Key
    input: dict[str, Any]
    expected: dict[str, Any]
    evidence_refs: list[Annotated[str, Field(min_length=1, max_length=1024)]] = Field(
        max_length=100
    )


def permitted(g: Grant, d: UUID) -> None:
    require(g, "proposal:write")
    if d not in g.datasets:
        raise Fault(403, "SCOPE_DENIED", "该身份没有此数据集的提案权限")


async def receipt(db: Db, g: Grant, d: UUID, p: UUID) -> dict[str, Any]:
    return await row(
        db,
        """SELECT p.id AS proposal_id,p.draft_id,p.evaluation_case_id,p.case_key,
        CASE WHEN v.state='PUBLISHED' AND EXISTS(SELECT 1 FROM evaluation_cases c
        WHERE c.id=p.evaluation_case_id AND c.dataset_version_id=v.id) THEN v.id END
        published_dataset_version_id
        FROM case_proposals p JOIN dataset_versions v ON v.id=p.draft_id
        WHERE p.id=:p AND p.project_id=:project AND p.environment=:env AND p.subject=:s AND
        p.dataset_id=:d""",
        p=p,
        project=g.project_id,
        env=g.environment,
        s=g.subject,
        d=d,
    )


@router.get("/{d}/case-proposals/{p}")
async def get_proposal(d: UUID, p: UUID, req: Request, db: Db, g: Grant) -> dict[str, object]:
    permitted(g, d)
    return reply(req, await receipt(db, g, d, p))


@router.post("/{d}/case-proposals")
async def propose(
    d: UUID,
    req: Request,
    res: Response,
    db: Db,
    g: Grant,
    idempotency_key: Annotated[UUID, Header()],
) -> dict[str, object]:
    permitted(g, d)
    raw = await bounded_json(req, 131072)
    try:
        body = Proposal.model_validate(raw)
        check_fields(body.input)
        check_fields(body.expected)
        check_fields(body.evidence_refs)
        case = Case(
            case_key=body.case_key,
            input=body.input,
            expected=body.expected,
            extensions={
                "quality_proposal": {
                    "proposal_id": str(body.proposal_id),
                    "case_id": str(body.case_id),
                    "review_id": str(body.review_id),
                    "evidence_refs": body.evidence_refs,
                }
            },
        )
    except (ValidationError, ValueError, RecursionError) as exc:
        raise Fault(422, "INVALID_PROPOSAL", "请检查提案、脱敏输入和预期字段") from exc
    require(g, "proposal:write", body.project.environment)
    slug = await db.scalar(text("SELECT slug FROM projects WHERE id=:p"), {"p": g.project_id})
    if (
        body.project.project_id != g.project_id
        or body.project.project_slug != slug
        or body.dataset_id != d
    ):
        raise Fault(403, "SCOPE_DENIED", "提案不属于该项目、环境或数据集")
    if body.proposal_id != idempotency_key:
        raise Fault(422, "INVALID_KEY", "请求键须与 proposal_id 一致")
    await datasets.lock_catalog(db, d)
    fingerprint = digest(raw)
    previous = await db.scalar(
        text("SELECT digest FROM case_proposals WHERE id=:id"), {"id": body.proposal_id}
    )
    if previous is not None:
        saved = await receipt(db, g, d, body.proposal_id)
        if previous != fingerprint:
            raise Fault(409, "PROPOSAL_ID_CONFLICT", "同一提案编号已绑定其他内容")
        return reply(req, saved)
    # Keep local evidence for reviewed cases. References to AIOps events stay opaque.
    for ref in set(body.evidence_refs):
        for prefix in ("trace:", "/api/v1/production-traces/"):
            if ref.startswith(prefix):
                try:
                    tid = UUID(ref.removeprefix(prefix))
                except ValueError as exc:
                    raise Fault(422, "INVALID_EVIDENCE", "生产轨迹引用需要有效编号") from exc
                saved_trace = await row(
                    db,
                    """SELECT availability,completeness FROM production_traces
                    WHERE id=:t AND project_id=:p AND environment=:e FOR UPDATE""",
                    t=tid,
                    p=g.project_id,
                    e=g.environment,
                )
                if saved_trace["availability"] not in {"AVAILABLE", "ARCHIVED"}:
                    raise Fault(410, "EVIDENCE_EXPIRED", "提案引用的证据已不可取回")
                await db.execute(
                    text("UPDATE production_traces SET pinned=true WHERE id=:t"), {"t": tid}
                )
    draft = await db.scalar(
        text("SELECT id FROM dataset_versions WHERE dataset_id=:d AND state='DRAFT'"), {"d": d}
    )
    if draft is None:
        base = await db.scalar(
            text(
                """SELECT id FROM dataset_versions WHERE dataset_id=:d AND state='PUBLISHED'
                    ORDER BY version DESC LIMIT 1"""
            ),
            {"d": d},
        )
        draft = (
            await datasets.version_action(
                db, d, VersionAction(action="draft", base_version_id=base)
            )
        )["id"]
    sample = await datasets.add(db, d, draft, case)

    await db.execute(
        text("""INSERT INTO case_proposals
        (id,project_id,environment,subject,dataset_id,draft_id,evaluation_case_id,case_key,case_id,review_id,digest,evidence_refs)
        VALUES(:id,:p,:env,:s,:d,:draft,:sample,:k,:c,:review,:h,CAST(:refs AS jsonb))"""),
        dict(
            id=body.proposal_id,
            p=g.project_id,
            env=g.environment,
            s=g.subject,
            d=d,
            draft=draft,
            sample=sample["id"],
            k=body.case_key,
            c=body.case_id,
            review=body.review_id,
            h=fingerprint,
            refs=json.dumps(body.evidence_refs),
        ),
    )
    await audit(
        db,
        g.project_id,
        g.subject,
        req.state.request_id,
        "propose",
        "case_proposal",
        body.proposal_id,
        [],
    )
    res.status_code = 201
    return reply(req, await receipt(db, g, d, body.proposal_id))
