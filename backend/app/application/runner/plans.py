import json
import re
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.catalog.evaluators import graph
from app.application.catalog.store import Fault, digest, row, version_adapter_config
from app.contracts.adapters import UnifiedRequest
from app.contracts.http import AdapterConfig
from app.contracts.reports import ReportPolicy
from app.contracts.runs import TaskCreate
from app.plugins.evaluator_releases import installed
from app.plugins.evaluators import lookup
from app.plugins.http_adapter import registry as adapters
from app.plugins.scoring_functions import TYPES

MAX_RUN_CASES = 10_000

TASK = """SELECT id,project_id,name,description,archived_at,project_version_id,dataset_version_id,
    config_snapshot->'roots' AS evaluator_version_ids, config_snapshot->'policy' AS policy,
    config_digest,created_by,created_at FROM evaluation_tasks"""


async def check_inputs(
    db: AsyncSession, vid: UUID, cfg: AdapterConfig, project: str, nodes: dict[UUID, dict[str, Any]]
) -> None:
    requirements = {}
    for version, node in nodes.items():
        declared = installed(node["impl_key"], node["build"]).requirements
        for path in declared:
            if cfg.kind == "http" and (
                (path.startswith("prediction.answer") and not cfg.answer_path)
                or (path.startswith("prediction.structured_output") and not cfg.structured_path)
            ):
                raise Fault(
                    422, "INCOMPATIBLE_OUTPUT", f"评分器 {version} 需要 {path}，请检查响应映射"
                )
        required = {path: typ for path, typ in declared.items() if path.startswith("case.")}
        if required:
            requirements[version] = required
    rows = await db.stream(
        text("""SELECT case_key,input,expected,extensions FROM evaluation_cases
                        WHERE dataset_version_id=:v"""),
        {"v": vid},
    )
    compatible: set[UUID] = set()
    count = 0
    mapping = adapters.mapping_for(cfg, project)
    placeholder = UUID(int=0)
    now = datetime.now(UTC)
    async for sample in rows.mappings():
        count += 1
        try:
            mapping.request_body(
                UnifiedRequest(
                    run_id=placeholder,
                    execution_id=placeholder,
                    attempt_id=placeholder,
                    case_key=sample["case_key"],
                    input=sample["input"],
                    deadline_at=now,
                    idempotency_key="preflight",
                )
            )
        except (KeyError, ValueError, TypeError) as exc:
            raise Fault(
                422, "INCOMPATIBLE_INPUT", f"样本 {sample['case_key']} 与接入协议不兼容"
            ) from exc
        for version, required in requirements.items():
            if version not in compatible and all(
                type(lookup({"case": dict(sample)}, path)) in TYPES[typ]
                for path, typ in required.items()
            ):
                compatible.add(version)
    if not count:
        raise Fault(422, "EMPTY_DATASET", "发布版本没有样本")
    if requirements.keys() - compatible:
        raise Fault(422, "INCOMPATIBLE_INPUT", "数据集中没有样本满足所选评分器的必需字段")


async def snapshot(
    db: AsyncSession,
    p: UUID,
    body: TaskCreate,
    *,
    validate_inputs: bool,
    report_policy: ReportPolicy | None = None,
) -> dict[str, Any]:
    pv = await row(
        db,
        """SELECT v.id,v.label,v.deployment_ref,v.digest,v.adapter_id,v.trace_capabilities,
                 a.digest AS adapter_digest,a.config->'_environment' AS environment,
                 p.slug,p.enabled FROM project_versions v
                 JOIN project_adapters a ON a.id=v.adapter_id JOIN projects p ON p.id=v.project_id
                 WHERE v.id=:v AND p.id=:p FOR SHARE OF p""",
        v=body.project_version_id,
        p=p,
    )
    if not pv.pop("enabled"):
        raise Fault(409, "PROJECT_DISABLED", "项目已停用")
    if pv["environment"] is None:
        pv.pop("environment")
    dv = await row(
        db,
        """SELECT v.id,v.dataset_id,v.version,v.digest,v.state,d.archived,
                  (SELECT count(*) FROM evaluation_cases WHERE dataset_version_id=v.id) sample_count
                  FROM dataset_versions v JOIN datasets d ON d.id=v.dataset_id WHERE v.id=:v
                  FOR SHARE OF d,v""",
        v=body.dataset_version_id,
    )
    if dv.pop("archived"):
        raise Fault(409, "ARCHIVED", "数据集已归档")
    if dv.pop("state") != "PUBLISHED":
        raise Fault(422, "UNPUBLISHED_DATASET", "必须选择已发布的数据集版本")
    if dv.pop("sample_count") > MAX_RUN_CASES:
        raise Fault(422, "RUN_CASE_LIMIT", f"单次评测最多允许 {MAX_RUN_CASES} 条样本，请拆分数据集")
    cfg = await version_adapter_config(db, p, body.project_version_id)
    if "execute" not in adapters.capabilities(cfg.kind, cfg.build):
        raise Fault(422, "INCOMPATIBLE_ADAPTER", "该构建未声明执行样本能力")
    nodes = await graph(db, body.evaluator_version_ids, publishing=True)
    references = [pv["deployment_ref"], cfg.secret_version, cfg.options.get("model")]
    for node in nodes.values():
        references.extend(
            node[k] for k in ("model_id", "model_revision", "prompt_version", "rubric_version")
        )
        references.append(node["config"].get("secret_version"))
    if any(
        isinstance(ref, str) and "latest" in re.split(r"[^a-z0-9]+", ref.lower())
        for ref in references
    ):
        raise Fault(422, "FLOATING_REFERENCE", "请使用固定的部署、模型、密钥和评分配置版本")
    if validate_inputs:
        await check_inputs(db, body.dataset_version_id, cfg, pv["slug"], nodes)
    plan = []
    for node in nodes.values():
        entry = {k: v for k, v in node.items() if k != "created_at"}
        entry["config_digest"] = digest(entry["config"])
        plan.append(entry)
    serialized: dict[str, Any] = json.loads(
        json.dumps(
            {
                "schema_version": 1,
                "required_adapter_capabilities": ["execute"],
                "project_id": p,
                "project_version": pv,
                "dataset_version": dv,
                "adapter": cfg.model_dump(),
                "implementation_digest": adapters.digest(cfg.kind, cfg.build),
                "roots": body.evaluator_version_ids,
                "evaluator_plan": plan,
                "policy": body.policy.model_dump(),
            },
            default=str,
        )
    )

    if report_policy is not None:
        serialized["report_policy"] = report_policy.model_dump(mode="json")
    return serialized


def public_snapshot(snap: dict[str, Any]) -> dict[str, Any]:
    # Return immutable identifiers and metadata. Prompt, credential references and endpoints
    # stay in the internal execution snapshot.
    return {
        **{
            k: snap[k]
            for k in (
                "schema_version",
                "project_id",
                "project_version",
                "dataset_version",
                "roots",
                "policy",
                "implementation_digest",
            )
        },
        "required_adapter_capabilities": snap.get("required_adapter_capabilities", ["execute"]),
        "adapter": {k: snap["adapter"][k] for k in ("kind", "build")},
        "evaluator_plan": [
            {k: v for k, v in n.items() if k not in {"config", "prompt_text"}}
            for n in snap["evaluator_plan"]
        ],
    }


async def create_task(
    db: AsyncSession,
    p: UUID,
    body: TaskCreate,
    actor: str,
    report_policy: ReportPolicy | None = None,
) -> dict[str, Any]:
    snap = await snapshot(db, p, body, validate_inputs=True, report_policy=report_policy)
    task = uuid4()
    positions = {str(root): i for i, root in enumerate(body.evaluator_version_ids)}
    await db.execute(
        text("""INSERT INTO task_evaluators(task_id,evaluator_version_id,position)
                       VALUES (:t,:v,:pos)"""),
        [
            {"t": task, "v": UUID(n["id"]), "pos": positions.get(n["id"])}
            for n in snap["evaluator_plan"]
        ],
    )
    await db.execute(
        text("""INSERT INTO evaluation_tasks(id,project_id,project_version_id,
        dataset_version_id,name,description,config_snapshot,config_digest,created_by)
        VALUES(:id,:p,:pv,:dv,:name,:description,CAST(:snap AS jsonb),:digest,:actor)"""),
        dict(
            id=task,
            p=p,
            pv=body.project_version_id,
            dv=body.dataset_version_id,
            name=body.name,
            description=body.description,
            snap=json.dumps(snap),
            digest=digest(snap),
            actor=actor,
        ),
    )
    return await row(db, TASK + " WHERE id=:t", t=task)
