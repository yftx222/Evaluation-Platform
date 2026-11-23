from typing import Annotated, Literal, Self

from pydantic import AwareDatetime, Field, StringConstraints, model_validator

from app.contracts.adapters import Contract
from app.contracts.http import AdapterConfig

type Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=160)]
type Kind = Literal["agent", "rag", "dataagent", "llm", "custom"]
type Role = Literal["R", "W", "A"]
type Scope = Literal["project:read", "results:read"]
type Limit = Annotated[int, Field(gt=0, le=1000000)]


class ProjectCreate(Contract):
    slug: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_-]{0,63}$")]
    name: Name
    kind: Kind
    max_parallel: Limit
    target_max_parallel: Limit
    rpm: Limit


class ProjectPatch(Contract):
    name: Name | None = None
    enabled: bool | None = None
    max_parallel: Limit | None = None
    target_max_parallel: Limit | None = None
    rpm: Limit | None = None

    @model_validator(mode="after")
    def require_values(self) -> Self:
        if not self.model_fields_set or any(
            getattr(self, k) is None for k in self.model_fields_set
        ):
            raise ValueError("至少提供一项非空变更")
        return self


class MemberRole(Contract):
    role: Role


class MemberCreate(MemberRole):
    issuer: Name
    subject: Name


class KeyExpiry(Contract):
    expires_at: AwareDatetime


class KeyCreate(KeyExpiry):
    name: Name
    scopes: list[Scope] = Field(min_length=1, max_length=2)


class Publish(Contract):
    label: Name
    deployment_ref: Name
    adapter: AdapterConfig
