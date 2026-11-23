import hashlib
import secrets
from dataclasses import dataclass
from typing import Protocol

from pydantic import BaseModel, SecretStr


@dataclass(frozen=True)
class Principal:
    issuer: str
    subject: str
    org_roles: frozenset[str]


class IdentityProvider(Protocol):
    def authenticate(self, token: str) -> Principal | None: ...
    def resolve_member(self, issuer: str, subject: str) -> bool: ...


class DevelopmentIdentity(BaseModel):
    subject: str
    verifier: str
    org_roles: list[str] = []


class DevelopmentAuthProvider:
    issuer = "development"

    def __init__(self, identities: list[DevelopmentIdentity]) -> None:
        self.identities = identities

    def authenticate(self, token: str) -> Principal | None:
        digest = hashlib.sha256(token.encode()).hexdigest()
        for identity in self.identities:
            if secrets.compare_digest(digest, identity.verifier):
                return Principal(self.issuer, identity.subject, frozenset(identity.org_roles))
        return None

    def resolve_member(self, issuer: str, subject: str) -> bool:
        return issuer == self.issuer and any(i.subject == subject for i in self.identities)


class SecretResolver(Protocol):
    def resolve(self, project: str, ref: str, version: str) -> SecretStr | None: ...


class DevelopmentSecretResolver:
    """Only explicitly provisioned development values, keyed by project/ref/version."""

    def __init__(self, values: dict[str, dict[str, dict[str, SecretStr]]]) -> None:
        self.values = values

    def resolve(self, project: str, ref: str, version: str) -> SecretStr | None:
        return self.values.get(project, {}).get(ref, {}).get(version)
