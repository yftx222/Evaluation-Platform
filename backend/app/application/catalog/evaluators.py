import asyncio
import json
import logging
import re
import time
from typing import Any
from uuid import UUID, uuid4

from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.catalog.store import Fault, digest, row
from app.contracts.datasets import bounded
from app.contracts.evaluators import (
    EvaluationResult,
    JudgeAccount,
    JudgeClient,
    Member,
    Publish,
    ScoringContext,
    Trial,
)
from app.plugins.evaluators import lookup, registry
from app.plugins.scoring_functions import TYPES

CATALOG = """SELECT e.*,(SELECT version FROM evaluator_versions WHERE evaluator_id=e.id
    ORDER BY created_at DESC,id DESC LIMIT 1) AS latest_version FROM evaluators e"""
VERSIONS = """SELECT v.*,coalesce((SELECT jsonb_agg(jsonb_build_object('child_version_id',
    m.child_id,'weight',m.weight,'position',m.position) ORDER BY m.position)
    FROM composite_members m WHERE parent_id=v.id),'[]'::jsonb) AS members
    FROM evaluator_versions v"""


def check_size(data: Any, limit: int) -> None:
    try:
        bounded(data)
        if len(json.dumps(data, ensure_ascii=False, allow_nan=False).encode()) > limit:
            raise ValueError
    except (ValueError, RecursionError) as exc:
        raise Fault(422, "INPUT_LIMIT", "字段过大或嵌套过深") from exc


async def graph(
    db: AsyncSession, roots: list[UUID], *, publishing: bool = False
) -> dict[UUID, dict[str, Any]]:
    nodes: dict[UUID, dict[str, Any]] = {}
    active: set[UUID] = set()

    async def visit(id_: UUID) -> None:
        if id_ in active:
            raise Fault(422, "INVALID_GRAPH", "组合不能含环")
        if id_ in nodes:
            return
        if len(nodes) + len(active) >= 64:
            raise Fault(422, "GRAPH_LIMIT", "每次试评最多 64 个不同版本")
        active.add(id_)
        try:
            version = await row(db, VERSIONS + " WHERE v.id=:id", id=id_)
        except Fault as exc:
            if publishing:
                raise Fault(422, "INVALID_CHILD", "子版本不存在") from exc
            raise
        if publishing:
            owner = await row(
                db,
                "SELECT enabled FROM evaluators WHERE id=:e FOR SHARE",
                e=version["evaluator_id"],
            )
            if not owner["enabled"]:
                raise Fault(422, "INVALID_CHILD", "不能选用已停用的子评分器")
        registry.get(version["impl_key"], version["build"])
        for child in version["members"]:
            await visit(UUID(child["child_version_id"]))
        active.remove(id_)
        nodes[id_] = version

    for root in roots:
        await visit(root)
    return nodes


async def publish(
    db: AsyncSession, e: UUID, body: Publish, accounts: dict[str, JudgeAccount]
) -> dict[str, Any]:
    owner = await row(db, "SELECT * FROM evaluators WHERE id=:e FOR UPDATE", e=e)
    if not owner["enabled"]:
        raise Fault(409, "DISABLED", "请先启用评分器")
    check_size(body.config, 32768)
    plugin = registry.get(body.impl_key, body.build)
    if plugin.kind != owner["kind"]:
        raise Fault(422, "INVALID_KIND", "实现类型与目录不一致")
    try:
        config = plugin.schema.model_validate(body.config).model_dump(mode="json")
    except (ValidationError, ValueError, TypeError) as exc:
        location = "config"
        if isinstance(exc, ValidationError):
            location += "." + ".".join(str(x) for x in exc.errors()[0]["loc"])
        raise Fault(422, "INVALID_CONFIG", location + " 配置不符合所选实现的字段要求") from exc
    is_judge = plugin.kind == "llm_judge"
    judge_fields = [
        body.prompt_text,
        body.prompt_version,
        body.provider,
        body.model_id,
        body.rubric_version,
    ]
    if is_judge:
        account = accounts.get(config["provider_account_id"])
        if (
            not all(judge_fields)
            or not body.prompt_text
            or not body.prompt_text.strip()
            or any(
                value and "latest" in re.split(r"[^a-z0-9]+", value.lower())
                for value in (body.model_id, body.model_revision, config["secret_version"])
            )
            or not account
            or body.model_id not in account.models
        ):
            raise Fault(
                422, "INVALID_JUDGE", "需要已配置的账户、允许的模型、固定提示词版本和量表版本"
            )
    elif any(judge_fields) or body.model_revision:
        raise Fault(422, "INVALID_CONFIG", "模型字段仅适用于 LLM Judge")
    if bool(body.members) != (plugin.kind == "composite"):
        raise Fault(422, "INVALID_GRAPH", "只有组合评分器需要一个或多个子版本")
    nodes = await graph(db, [m.child_version_id for m in body.members], publishing=True)
    if len(nodes) >= 64:
        raise Fault(422, "GRAPH_LIMIT", "组合超过 64 个版本")
    ordered = sorted(body.members, key=lambda x: x.position)
    snapshot = body.model_dump(mode="json", exclude={"version", "members"})
    snapshot["config"] = config
    snapshot["members"] = [
        {**m.model_dump(mode="json"), "digest": nodes[m.child_version_id]["digest"]}
        for m in ordered
    ]
    version_id = uuid4()
    for member in ordered:
        await db.execute(
            text("""INSERT INTO composite_members(parent_id,child_id,position,weight)
          VALUES (:parent,:child,:position,:weight)"""),
            dict(
                parent=version_id,
                child=member.child_version_id,
                position=member.position,
                weight=member.weight,
            ),
        )
    await db.execute(
        text("""INSERT INTO evaluator_versions(id,evaluator_id,name,version,impl_key,build,
      config,prompt_text,prompt_version,provider,model_id,model_revision,rubric_version,
      revision_unverified,digest)
      VALUES (:id,:e,:name,:version,:impl_key,:build,CAST(:cfg AS jsonb),:prompt_text,
      :prompt_version,
      :provider,:model_id,:model_revision,:rubric_version,:unverified,:digest)"""),
        {
            **body.model_dump(exclude={"members", "config"}),
            "id": version_id,
            "e": e,
            "name": owner["name"],
            "cfg": json.dumps(config),
            "unverified": is_judge and not body.model_revision,
            "digest": digest(snapshot),
        },
    )
    return await row(db, VERSIONS + " WHERE v.id=:id", id=version_id)


logger = logging.getLogger(__name__)
TRIAL_TIMEOUT_SECONDS = 32


async def trial(
    nodes: dict[UUID, dict[str, Any]], body: Trial, judge: JudgeClient
) -> EvaluationResult:
    deadline = time.monotonic() + TRIAL_TIMEOUT_SECONDS
    results: dict[UUID, EvaluationResult] = {}
    inputs = body.model_dump(exclude_unset=True)
    for id_, v in nodes.items():
        members = [Member.model_validate(x) for x in v["members"]]
        plugin = registry.get(v["impl_key"], v["build"])
        ctx = ScoringContext(
            v,
            deadline,
            body.materials,
            judge if plugin.kind == "llm_judge" else None,
            tuple((m, results[m.child_version_id]) for m in members),
        )
        if time.monotonic() >= deadline:
            result = ctx.result("ERROR", "TRIAL_TIMEOUT")
        elif plugin.kind == "code" and any(
            type(lookup(inputs, path)) not in TYPES[t] for path, t in plugin.requirements.items()
        ):
            result = ctx.result("SKIPPED", "缺少必需评分输入：" + "、".join(plugin.requirements))
        else:
            try:
                async with asyncio.timeout(deadline - time.monotonic()):
                    raw = await plugin.factory().evaluate(body.case, body.prediction, ctx)
                if time.monotonic() >= deadline:
                    raise TimeoutError
                # Revalidate even a model constructed or mutated by a trusted plugin.
                result = EvaluationResult.model_validate(
                    raw.model_dump(warnings=False), strict=True
                )
                if (
                    result.evaluator_version_id,
                    result.evaluator_name,
                    result.evaluator_version,
                ) != (id_, v["name"], v["version"]):
                    raise ValueError("unexpected evaluator identity")
            except TimeoutError:
                logger.warning("evaluator timed out version_id=%s", id_)
                result = ctx.result("ERROR", "TRIAL_TIMEOUT")
            except Exception as exc:
                # CancelledError derives from BaseException. Never log plugin text or sample data.
                logger.error(
                    "evaluator failed version_id=%s exception_type=%s", id_, type(exc).__name__
                )
                result = ctx.result("ERROR", "EVALUATOR_FAILED")
        results[id_] = result
    return results[body.version_id]
