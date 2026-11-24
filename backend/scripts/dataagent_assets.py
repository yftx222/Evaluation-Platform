"""Package DataAgent's pilot cases and publish unchanged assets through the catalog API."""

import argparse
import json
import os
import re
from pathlib import Path
from typing import Any

import httpx

from app.application.catalog.dataset_files import compact
from app.application.catalog.store import digest
from app.contracts.datasets import Case
from app.contracts.evaluators import Publish
from app.plugins.evaluator_releases import installed

ASSETS = Path(__file__).resolve().parents[1] / "assets" / "dataagent"
DATASET = {
    "slug": "dataagent-ecommerce-pilot",
    "name": "DataAgent 电商试点评测",
    "description": "适用于 2026 年 9 月电商快照，请使用匹配的电商只读测试库。",
}


def convert(source: Path) -> list[dict[str, Any]]:
    cases = json.loads((source / "pilot_cases.json").read_text(encoding="utf-8-sig"))
    facts = json.loads((source / "scorecards.json").read_text(encoding="utf-8-sig"))
    keys = [case["id"] for case in cases]
    if not keys or len(keys) != len(set(keys)) or set(keys) != set(facts):
        raise ValueError("题号重复或评分卡与题库不匹配")
    rubric = {}
    for line in (source / "rubric.md").read_text(encoding="utf-8-sig").splitlines():
        if re.match(r"^\| [A-Z]{3}-\d{2} \|", line):
            key, *cells = [c.strip() for c in line.strip("|").split("|")]
            if key in keys:
                if key in rubric or len(cells) != 4:
                    raise ValueError(f"量表题号重复或列数不符 {key}")
                rubric[key] = dict(
                    zip(("title", "scope", "criterion", "reference"), cells, strict=True)
                )
    result = []
    for case in cases:
        key, turns = case["id"], case["turns"]
        if not turns or any(set(t) != {"message", "expected", "check"} for t in turns):
            raise ValueError(f"无法映射题目轮次 {key}")
        item = Case(
            case_key=key,
            input={"turns": [{"message": t["message"]} for t in turns]},
            expected={
                "answer": turns[-1]["expected"],
                "criteria": [t["check"] for t in turns],
                "turn_answers": [t["expected"] for t in turns],
                "scorecard": facts[key],
                "rubric": rubric[key],
            },
            category=key.split("-")[0],
            tags=["dataagent", "ecommerce-2026-09", "pilot"],
        )
        result.append(item.model_dump())
    return sorted(result, key=lambda c: c["case_key"])


def call(client: httpx.Client, method: str, path: str, **kwargs: Any) -> Any:
    response = client.request(method, path, **kwargs)
    response.raise_for_status()
    return response.json()["data"]


def entry(client: httpx.Client, path: str, spec: dict[str, str]) -> str:
    page = call(client, "GET", path, params={"q": spec["slug"], "limit": 200})
    item = next((i for i in page["items"] if i["slug"] == spec["slug"]), None)
    if item is None:
        item = call(client, "POST", path, json=spec)
    if item.get("archived") or item.get("enabled") is False or item.get("kind") != spec.get("kind"):
        raise ValueError(f"目录状态或评分器类型不符 {spec['slug']}")
    return f"{path}/{item['id']}"


def publish_dataset(client: httpx.Client) -> dict[str, Any]:
    raw = (ASSETS / "pilot.jsonl").read_bytes()
    cases = sorted(
        (Case.model_validate_json(line).model_dump() for line in raw.splitlines()),
        key=lambda c: c["case_key"],
    )
    fingerprint = digest(cases)
    path = entry(client, "datasets", DATASET)
    catalog = call(client, "GET", path)
    version = next(
        (
            v
            for v in catalog["versions"]
            if v["state"] == "PUBLISHED" and v["digest"] == fingerprint
        ),
        None,
    )
    if version is None:
        draft = catalog["draft"]
        if any(v["state"] == "PUBLISHED" for v in catalog["versions"]) or not draft:
            raise ValueError("目录已有其他发布版本，请人工审核差异")
        if not draft["sample_count"]:
            form = {"dataset_id": catalog["id"], "draft_id": draft["id"], "format": "jsonl"}
            call(client, "POST", "datasets/import", data=form, files={"file": ("pilot.jsonl", raw)})
        version = call(
            client,
            "POST",
            path + "/versions",
            json={"action": "publish", "draft_id": draft["id"], "reviewed_digest": fingerprint},
        )
    exported = client.get(path + "/export", params={"version_id": version["id"], "format": "json"})
    exported.raise_for_status()
    if exported.json() != cases or version["digest"] != fingerprint:
        raise ValueError("发布版本回读与迁移包不同")
    return {
        "dataset_id": catalog["id"],
        "version_id": version["id"],
        "digest": fingerprint,
        "cases": len(cases),
        "turns": sum(len(c["input"]["turns"]) for c in cases),
    }


def publish(client: httpx.Client) -> dict[str, Any]:
    result = {"dataset": publish_dataset(client), "evaluators": {}}
    for spec in json.loads((ASSETS / "evaluators.json").read_text(encoding="utf8")):
        path = entry(client, "evaluators", spec["catalog"])
        body = Publish.model_validate(spec["release"])
        snapshot = body.model_dump(mode="json", exclude={"version"})
        snapshot["config"] = (
            installed(body.impl_key, body.build)
            .schema.model_validate(body.config)
            .model_dump(mode="json")
        )
        versions = call(client, "GET", path + "/versions", params={"limit": 200})["items"]
        version = next((v for v in versions if v["version"] == body.version), None)
        if version is None:
            version = call(client, "POST", path + "/versions", json=body.model_dump(mode="json"))
        if version["digest"] != digest(snapshot):
            raise ValueError(f"同名评分器版本内容不同 {path}")
        result["evaluators"][spec["catalog"]["slug"]] = {
            "evaluator_id": version["evaluator_id"],
            "version_id": version["id"],
            "digest": version["digest"],
            "build": version["build"],
        }
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("convert").add_argument("source", type=Path)
    commands.add_parser("publish")
    args = parser.parse_args()
    if args.command == "convert":
        payload = "".join(compact(case) + "\n" for case in convert(args.source))
        (ASSETS / "pilot.jsonl").write_text(payload, encoding="utf8", newline="\n")
        return
    with httpx.Client(
        base_url="http://127.0.0.1:18000/api/v1/",
        timeout=45,
        trust_env=False,
        headers={"Authorization": "Bearer " + os.environ["EVAL_CATALOG_TOKEN"]},
    ) as client:
        print(json.dumps(publish(client), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
