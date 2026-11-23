import math
import re
from typing import Annotated, Any, Literal, Self
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import Field, StringConstraints, field_validator, model_validator

from app.contracts.adapters import Contract

Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=160)]
Key = Annotated[str, StringConstraints(pattern=r"^[\w.-]{1,128}$")]
Split = Literal["train", "validation", "test"]
Format = Literal["json", "jsonl", "csv", "xlsx"]


class DatasetCreate(Contract):
    slug: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_-]{0,63}$")]
    name: Name
    description: str | None = Field(None, max_length=4000)


class DatasetPatch(Contract):
    name: Name | None = None
    description: str | None = Field(None, max_length=4000)
    archived: bool | None = None


class VersionAction(Contract):
    action: Literal["draft", "publish"]
    base_version_id: UUID | None = None
    draft_id: UUID | None = None
    reviewed_digest: Annotated[str, StringConstraints(pattern="^[a-f0-9]{64}$")] | None = None

    @model_validator(mode="after")
    def check_action(self) -> Self:
        if self.action == "publish":
            if not self.draft_id or not self.reviewed_digest or self.base_version_id:
                raise ValueError("发布需要草稿和审核摘要")
        elif self.draft_id or self.reviewed_digest:
            raise ValueError("草稿只能指定基线")
        return self


def bounded(value: Any, depth: int = 0) -> None:
    if depth > 12 or (isinstance(value, str) and (len(value) > 16000 or "\x00" in value)):
        raise ValueError("字段长度、内容或嵌套深度不合法")
    if isinstance(value, str):
        value.encode("utf8")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("非有限数值")
    if isinstance(value, (dict, list)):
        if len(value) > 1024:
            raise ValueError("集合超限")
        for item in value:
            bounded(item, depth + 1)
            if isinstance(value, dict):
                bounded(value[item], depth + 1)


class Case(Contract):
    case_key: Key
    input: dict[str, Any]
    expected: dict[str, Any]
    category: str | None = Field(None, max_length=160)
    difficulty: str | None = Field(None, max_length=80)
    tags: list[Name] = Field(default_factory=list, max_length=64)
    split: Split = "test"
    extensions: dict[str, Any] = Field(default_factory=dict)

    @field_validator("input", "expected", "extensions", "category", "difficulty", "tags")
    @classmethod
    def limits(cls, value: Any) -> Any:
        bounded(value)
        return value

    @field_validator("input")
    @classmethod
    def check_input(cls, v: dict[str, Any]) -> dict[str, Any]:
        if not v:
            raise ValueError("输入不能为空")
        for key, t in [
            ("query", str),
            ("context", dict),
            ("messages", list),
            ("attachments", list),
        ]:
            if key in v and not isinstance(v[key], t):
                raise ValueError("输入字段类型错误")
        for ref in v.get("attachments", []):
            if not isinstance(ref, str) or len(ref) > 2048:
                raise ValueError("附件必须为引用字符串")
            url = urlsplit(ref)
            if url.scheme == "https" and url.hostname and not url.username and not url.password:
                continue
            if not re.fullmatch(r"asset:[a-zA-Z0-9_-]{1,128}", ref):
                raise ValueError("附件引用无效")
        return v

    @field_validator("expected")
    @classmethod
    def check_expected(cls, v: dict[str, Any]) -> dict[str, Any]:
        if "answer" in v and not isinstance(v["answer"], str):
            raise ValueError("answer 必须为文本")
        if "structured_output" in v and not isinstance(v["structured_output"], dict):
            raise ValueError("structured_output 必须为对象")
        if "criteria" in v and (
            not isinstance(v["criteria"], list)
            or not all(isinstance(x, str) for x in v["criteria"])
        ):
            raise ValueError("criteria 必须为字符串列表")
        return v


class CaseCreate(Case):
    draft_id: UUID


class CasePatch(Contract):
    draft_id: UUID
    case_key: Key | None = None
    input: dict[str, Any] | None = None
    expected: dict[str, Any] | None = None
    category: str | None = None
    difficulty: str | None = None
    tags: list[str] | None = None
    split: Split | None = None
    extensions: dict[str, Any] | None = None
