import asyncio
import hashlib
import json
import os
import sys
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.application.catalog.store import Fault
from app.contracts.adapters import Contract, UnifiedResponse
from app.contracts.datasets import Case as CaseView
from app.contracts.evaluators import BaseEvaluator, EvaluationResult, Kind, ScoringContext
from app.plugins.evaluator_configs import (
    FieldsConfig,
    JudgeConfig,
    NumericConfig,
    Rule,
    RuleConfig,
    SchemaConfig,
    Threshold,
)
from app.plugins.scoring_functions import conforms

MISSING = object()


def lookup(data: Any, path: str) -> Any:
    for key in path.split("."):
        if not isinstance(data, dict) or key not in data:
            return MISSING
        data = data[key]
    return data


def matches(rule: Rule, value: Any) -> bool:
    if rule.op == "exists":
        return value is not MISSING
    if value is MISSING:
        return False
    if rule.op == "nonempty":
        return value is not None and (
            bool(value.strip()) if isinstance(value, str) else value not in ([], {})
        )
    if rule.op == "schema":
        return conforms(value, rule.expected)
    if rule.op == "at_least":
        return type(value) in (int, float) and value >= rule.expected
    if rule.op == "contains_all":
        return isinstance(value, list) and all(x in value for x in rule.expected)
    if not isinstance(value, str):
        return False
    if rule.op == "normalized":

        def normalize(s: str) -> str:
            if rule.unicode_form != "none":
                s = unicodedata.normalize(rule.unicode_form, s)
            if rule.collapse_space:
                s = " ".join(s.split())
            return s.casefold() if rule.ignore_case else s

        return normalize(value) == normalize(rule.expected)
    if rule.op == "includes":
        return rule.expected in value
    if rule.op == "excludes":
        return rule.expected not in value
    return bool(value == rule.expected)


class Rules:
    async def evaluate(
        self, case: CaseView, prediction: UnifiedResponse, context: ScoringContext
    ) -> EvaluationResult:
        cfg = RuleConfig.model_validate(context.version["config"])
        data = prediction.model_dump(exclude_unset=True)
        hits = [matches(r, lookup(data, r.path)) for r in cfg.rules]
        score = sum(r.weight for r, hit in zip(cfg.rules, hits, strict=True) if hit) / sum(
            r.weight for r in cfg.rules
        )
        failed = [r.reason for r, hit in zip(cfg.rules, hits, strict=True) if not hit]
        return context.result(
            "OK",
            "；".join(failed) or "全部规则命中",
            score=score,
            passed=score >= cfg.threshold,
            details={"hits": hits},
            evidence=[
                "prediction:/" + r.path.replace(".", "/")
                for r in cfg.rules
                if lookup(data, r.path) is not MISSING
            ],
        )


class Code:
    async def evaluate(
        self, case: CaseView, prediction: UnifiedResponse, context: ScoringContext
    ) -> EvaluationResult:
        version = context.version
        payload = json.dumps(
            {
                "key": version["impl_key"],
                "config": version["config"],
                "expected": case.expected,
                "prediction": prediction.model_dump(),
            },
            ensure_ascii=False,
        ).encode()
        if len(payload) > 131072:
            return context.result("ERROR", "CODE_INPUT_LIMIT")
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-I",
            "-S",
            str(Path(__file__).with_name("scoring_functions.py")),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env={k: os.environ[k] for k in ("SYSTEMROOT", "WINDIR") if k in os.environ},
        )
        try:
            async with asyncio.timeout(min(3, context.deadline_at - time.monotonic())):
                out, _ = await proc.communicate(payload)
            if proc.returncode or out.strip() not in {b"true", b"false"}:
                return context.result("ERROR", "CODE_EXECUTION_FAILED")
            score = float(out.strip() == b"true")
            return context.result(
                "OK",
                "匹配" if score else "未匹配",
                score=score,
                passed=score >= version["config"]["threshold"],
                evidence=[
                    "prediction:/answer"
                    if version["impl_key"] == "exact_match"
                    else "prediction:/structured_output"
                ],
            )
        except TimeoutError:
            return context.result("ERROR", "CODE_TIMEOUT")
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()


class Business:
    async def evaluate(
        self, case: CaseView, prediction: UnifiedResponse, context: ScoringContext
    ) -> EvaluationResult:
        policy = case.extensions.get("policy_id")
        approved = context.materials.get("approved_policy_ids")
        if (
            not isinstance(policy, str)
            or not isinstance(approved, list)
            or not all(isinstance(k, str) for k in approved)
        ):
            return context.result("SKIPPED", "需要 policy_id 和只读材料 approved_policy_ids")
        selected = (prediction.structured_output or {}).get("policy_id")
        score = float(selected == policy and policy in approved)
        return context.result(
            "OK",
            "策略编号符合" if score else "策略编号未匹配",
            score=score,
            passed=score >= context.version["config"]["threshold"],
            evidence=["prediction:/structured_output"]
            if prediction.structured_output is not None
            else [],
        )


class Judge:
    async def evaluate(
        self, case: CaseView, prediction: UnifiedResponse, context: ScoringContext
    ) -> EvaluationResult:
        if context.judge is None:
            return context.result("ERROR", "JUDGE_UNAVAILABLE")
        return await context.judge.grade(case, prediction, context)


class Composite:
    async def evaluate(
        self, case: CaseView, prediction: UnifiedResponse, context: ScoringContext
    ) -> EvaluationResult:
        children = context.children
        details = {
            "children": [
                {
                    **m.model_dump(mode="json"),
                    "score": r.score,
                    "status": r.status,
                    "reason": r.reason,
                }
                for m, r in children
            ]
        }
        for status in ("ERROR", "SKIPPED"):
            if any(r.status == status for _, r in children):
                return context.result(
                    "ERROR" if status == "ERROR" else "SKIPPED", "子评分器未完成", details=details
                )
        score = sum(m.weight * r.score for m, r in children if r.score is not None) / sum(
            m.weight for m, _ in children
        )
        return context.result(
            "OK",
            "按权重合成",
            score=score,
            passed=score >= context.version["config"]["threshold"],
            details=details,
        )


@dataclass(frozen=True)
class Plugin:
    kind: Kind
    impl_key: str
    build: str
    factory: Callable[[], BaseEvaluator]
    schema: type[Contract]
    requirements: dict[str, str]
    example: dict[str, Any]


class Registry:
    def __init__(self) -> None:
        self.plugins: dict[tuple[str, str], Plugin] = {}

    def register(self, plugin: Plugin) -> None:
        key = (plugin.impl_key, plugin.build)
        if key in self.plugins:
            raise RuntimeError("重复评分器注册")
        self.plugins[key] = plugin

    def get(self, key: str, build: str) -> Plugin:
        plugin = self.plugins.get((key, build))
        if plugin is None:
            raise Fault(422, "VERSION_UNAVAILABLE", "该实现或构建未部署")
        return plugin


registry = Registry()
# Freeze source identity across host and container line endings.
sources = [
    Path(__file__).with_name(n)
    for n in ("evaluators.py", "scoring_functions.py", "evaluator_configs.py", "judge.py")
]
sources.append(Path(__file__).parents[1] / "contracts" / "evaluators.py")
BUILD = hashlib.sha256(
    b"\0".join(p.read_text(encoding="utf8").encode() for p in sources)
).hexdigest()


def register(
    kind: Kind,
    key: str,
    factory: Callable[[], BaseEvaluator],
    schema: type[Contract],
    requirements: dict[str, str],
    example: dict[str, Any],
) -> None:
    registry.register(Plugin(kind, key, BUILD, factory, schema, requirements, example))


register(
    "rule",
    "rules",
    Rules,
    RuleConfig,
    {},
    {"rules": [{"path": "answer", "op": "nonempty", "reason": "回答为空"}]},
)
register(
    "code",
    "exact_match",
    Code,
    Threshold,
    {"case.expected.answer": "string", "prediction.answer": "string"},
    {},
)
register(
    "code",
    "numeric_tolerance",
    Code,
    NumericConfig,
    {
        "case.expected.structured_output.value": "number",
        "prediction.structured_output.value": "number",
    },
    {"tolerance": 0.01},
)
register(
    "code",
    "required_fields",
    Code,
    FieldsConfig,
    {"prediction.structured_output": "object"},
    {"fields": ["value"]},
)
register(
    "code",
    "json_schema_match",
    Code,
    SchemaConfig,
    {"prediction.structured_output": "object"},
    {"output_schema": {"type": "object", "required": ["value"]}},
)
register(
    "business",
    "approved_policy",
    Business,
    Threshold,
    {"case.extensions.policy_id": "string", "materials.approved_policy_ids": "string[]"},
    {},
)
register("composite", "weighted", Composite, Threshold, {}, {"threshold": 0.8})
register(
    "llm_judge",
    "chat_judge",
    Judge,
    JudgeConfig,
    {},
    {
        "threshold": 0.8,
        "dimensions": ["正确性"],
        "provider_account_id": "deepseek-ecommerce",
        "secret_ref": "judge-api",
        "secret_version": "v1",
    },
)
