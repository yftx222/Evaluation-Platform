from typing import Annotated, Any, Literal, Self

from pydantic import Field, model_validator

from app.contracts.adapters import Contract
from app.contracts.datasets import Name
from app.contracts.evaluators import Unit, Weight
from app.plugins.scoring_functions import check_schema


class Threshold(Contract):
    threshold: Unit = 1


class Rule(Contract):
    path: Annotated[
        str, Field(pattern=r"^(answer|structured_output|extensions)(\.[\w-]+)*$", max_length=256)
    ]
    op: Literal[
        "nonempty",
        "exists",
        "exact",
        "normalized",
        "includes",
        "excludes",
        "schema",
        "at_least",
        "contains_all",
    ]
    expected: Any = None
    reason: Name = "未满足规则"
    weight: Weight = 1
    ignore_case: bool = True
    collapse_space: bool = True
    unicode_form: Literal["NFC", "NFKC", "none"] = "NFKC"

    @model_validator(mode="after")
    def operand(self) -> Self:
        if self.op in {"exact", "normalized", "includes", "excludes"} and not isinstance(
            self.expected, str
        ):
            raise ValueError("此规则需要文本 expected")
        if self.op == "at_least" and type(self.expected) not in (int, float):
            raise ValueError("数值阈值需要数字 expected")
        if self.op == "contains_all" and not isinstance(self.expected, list):
            raise ValueError("包含检查需要数组 expected")
        if self.op == "schema":
            check_schema(self.expected)
        return self


class RuleConfig(Threshold):
    rules: list[Rule] = Field(min_length=1, max_length=48)


class NumericConfig(Threshold):
    tolerance: float = Field(ge=0, allow_inf_nan=False)


class FieldsConfig(Threshold):
    fields: list[Name] = Field(min_length=1, max_length=64)


class SchemaConfig(Threshold):
    output_schema: dict[str, Any]

    @model_validator(mode="after")
    def checked(self) -> Self:
        check_schema(self.output_schema)
        return self


class JudgeConfig(Threshold):
    provider_account_id: Name
    secret_ref: Name
    secret_version: Name
    temperature: float = Field(0, ge=0, le=2)
    seed: int | None = None
    timeout_s: float = Field(20, gt=0, le=30)
    response_schema: Literal["dimensions-v1"] = "dimensions-v1"
    score_scale: Literal["0..1"] = "0..1"
    dimensions: list[Name] = Field(min_length=1, max_length=12)

    @model_validator(mode="after")
    def fixed(self) -> Self:
        if self.secret_version.lower() == "latest" or len(set(self.dimensions)) != len(
            self.dimensions
        ):
            raise ValueError("需要固定密钥版本和唯一评分维度")
        return self


class Verdict(Contract):
    score: Unit
    passed: bool = Field(strict=True)
    reason: str = Field(min_length=1, max_length=1024)
    dimensions: dict[str, Unit] = Field(min_length=1, max_length=12)
