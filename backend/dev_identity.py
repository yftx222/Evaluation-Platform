"""Provision the first local admin. Do not overwrite existing identities or tokens."""

import hashlib
import json
import secrets
from pathlib import Path


def main() -> None:
    root = Path(__file__).resolve().parent.parent
    env_file = root / ".env"
    token_file = root / ".local" / "development-token.txt"
    existing = env_file.read_text(encoding="utf-8-sig") if env_file.exists() else ""
    if (
        any(line.startswith("EVAL_IDENTITIES=") for line in existing.splitlines())
        or token_file.exists()
    ):
        raise SystemExit("已有开发身份配置；保持现状。")
    token = secrets.token_urlsafe(32)
    identities = [
        {
            "subject": "local-admin",
            "org_roles": ["Org", "Cat"],
            "verifier": hashlib.sha256(token.encode()).hexdigest(),
        }
    ]
    if "POSTGRES_PASSWORD=" not in existing:
        existing += "\nPOSTGRES_PASSWORD=" + secrets.token_hex(24)
    token_file.parent.mkdir(exist_ok=True)
    with token_file.open("x", encoding="utf-8") as stream:
        stream.write(token)
    env_file.write_text(
        existing.rstrip() + "\nEVAL_IDENTITIES='" + json.dumps(identities) + "'\n", encoding="utf-8"
    )
    print("开发管理员已配置。令牌仅保存在 code/.local/development-token.txt。")


if __name__ == "__main__":
    main()
