"""Publish or reuse a pinned customer release from its authenticated /environment response."""

import argparse
import json
import os
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx

from app.application.catalog.store import digest
from app.contracts.catalog import CustomerManifest


def publish(
    client: httpx.Client,
    project: str,
    base: str,
    environment: dict[str, Any],
    secret_ref: str,
    secret_version: str,
) -> dict[str, Any]:
    manifest = CustomerManifest.model_validate(environment["manifest"], strict=True).model_dump()
    release = digest(manifest)
    if environment["digest"] != release:
        raise ValueError("客服环境清单摘要不匹配")
    address = f"{base.rstrip('/')}/releases/{release}"
    fields = "run_id execution_id attempt_id idempotency_key case_key input deadline_at".split()
    adapter = {
        "kind": "http",
        "build": "2",
        "endpoint": address + "/agent-runs",
        "probe_endpoint": address + "/ready",
        "secret_ref": secret_ref,
        "secret_version": secret_version,
        "timeout_s": 20,
        "request_map": {key: key for key in fields},
        "structured_path": "structured_output",
        "options": {
            "usage_path": "usage",
            "extensions_path": "extensions",
            "observed_path": "observed",
        },
    }
    trace = {
        "mode": "callback",
        "event_types": ["AGENT_STEP", "MODEL_CALL", "TOOL_CALL", "GUARDRAIL", "FINAL_OUTPUT"],
        "required_events": ["FINAL_OUTPUT"],
        "retrieval_refs": False,
    }
    body = {
        "customer_manifest": manifest,
        "deployment_ref": manifest["customer_service_sha"],
        "adapter": adapter,
        "trace_capabilities": trace,
    }
    body["label"] = "customer-" + digest(body)
    path = f"projects/{UUID(project)}/versions"

    def existing() -> dict[str, Any] | None:
        response = client.get(path, params={"q": body["label"]})
        response.raise_for_status()
        return next(
            (v for v in response.json()["data"]["items"] if v["label"] == body["label"]), None
        )

    version = existing()
    if version is None:
        response = client.post(path, json=body)
        if response.status_code == 409:
            version = existing()
        if version is None:
            response.raise_for_status()
            version = response.json()["data"]
    if version["environment"] != environment:
        raise ValueError("已发布版本与客服环境清单不符")
    return version


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for arg in ("api", "project", "target", "environment", "secret-ref", "secret-version"):
        parser.add_argument("--" + arg, required=True)
    args = parser.parse_args()
    with httpx.Client(
        base_url=args.api.rstrip("/") + "/",
        timeout=30,
        headers={"Authorization": "Bearer " + os.environ["EVAL_PUBLISH_TOKEN"]},
    ) as api:
        result = publish(
            api,
            args.project,
            args.target,
            json.loads(Path(args.environment).read_text(encoding="utf-8-sig")),
            args.secret_ref,
            args.secret_version,
        )
        print(result["id"], result["label"])
