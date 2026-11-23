"""Restore a synthetic acceptance database and verify its report, trace and evidence via HTTP."""

import asyncio
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.engine import make_url

from app.api.main import create_app
from app.infrastructure.resources import Resources, Settings


def verify_restore(
    settings: Settings, tokens: dict[str, str], api: TestClient, run: str, trace: str, evidence: str
) -> dict[str, Any]:
    url = make_url(settings.database_url.get_secret_value())
    source = url.database
    assert source and re.fullmatch(r"acceptance_[0-9a-f]{32}", source) and url.host == "127.0.0.1"
    project = os.environ["COMPOSE_PROJECT_NAME"]
    assert project == "eval-phase4-check"
    restored = "acceptance_" + uuid4().hex
    cmd = ["docker", "compose", "-p", project, "exec", "-T", "postgres"]
    cwd = Path(__file__).resolve().parents[2]
    root = "/api/v1"
    trace_url = f"{root}/traces/{trace}"
    evidence_url = f"{root}/evidence/{evidence}"
    report_url = f"{root}/reports/runs/{run}"

    def read(api: TestClient, path: str) -> Any:
        response = api.get(path)
        assert response.is_success, response.status_code
        return response.json()["data"]

    paths = [report_url, trace_url, trace_url + "/events", evidence_url]
    for e in read(api, trace_url + "/events")["items"]:
        paths.append(trace_url + "/events/" + e["id"])
    expected = [read(api, p) for p in paths]
    assert len(paths) > 4
    clone = settings.model_copy(
        update={
            "database_url": SecretStr(
                url.set(database=restored).render_as_string(hide_password=False)
            )
        }
    )

    async def mark(archived: bool) -> None:
        res = Resources(clone)
        try:
            async with res.transaction() as db:
                await db.execute(
                    text(
                        "INSERT INTO trace_archives(trace_id) VALUES (:t)"
                        if archived
                        else "DELETE FROM trace_archives WHERE trace_id=:t"
                    ),
                    {"t": UUID(trace)},
                )
        finally:
            await res.close()

    with tempfile.TemporaryFile("w+b") as copy:
        subprocess.run(
            [*cmd, "pg_dump", "-U", "eval", "-Fc", source], cwd=cwd, stdout=copy, check=True
        )
        length = copy.tell()
        copy.seek(0)
        subprocess.run(
            [*cmd, "createdb", "-U", "eval", "-T", "template0", restored], cwd=cwd, check=True
        )
        try:
            subprocess.run(
                [
                    *cmd,
                    "pg_restore",
                    "-U",
                    "eval",
                    "-d",
                    restored,
                    "--exit-on-error",
                    "--single-transaction",
                    "--no-owner",
                ],
                cwd=cwd,
                stdin=copy,
                check=True,
            )
            with TestClient(
                create_app(clone), headers={"Authorization": "Bearer " + tokens["curator"]}
            ) as restored_api:
                assert [read(restored_api, p) for p in paths] == expected
                for p in (report_url, trace_url, evidence_url):
                    assert restored_api.get(
                        p, headers={"Authorization": "Bearer " + tokens["rag"]}
                    ).status_code in (403, 404)
                asyncio.run(mark(True))
                assert restored_api.get(trace_url).status_code == 410
                archived = read(restored_api, evidence_url)
                assert archived["reason"] == "SOURCE_ARCHIVED" and archived["excerpt"] is None
                asyncio.run(mark(False))
                assert [read(restored_api, p) for p in paths] == expected
        finally:
            subprocess.run([*cmd, "dropdb", "-U", "eval", restored], cwd=cwd, check=True)
    return {"backup_bytes": length, "restored_events": len(paths) - 4, "verified_paths": len(paths)}
