"""Run three explicitly requested, billable trials on one public arithmetic example."""

from pathlib import Path
from typing import Any

import httpx


def main() -> None:
    root = Path(__file__).resolve().parents[2]
    token = (root / ".local" / "development-token.txt").read_text(encoding="utf-8-sig").strip()
    with httpx.Client(
        base_url="http://127.0.0.1:18000/api/v1/",
        timeout=40,
        trust_env=False,
        headers={"Authorization": "Bearer " + token},
    ) as client:

        def call(method: str, path: str, **kwargs: Any) -> Any:
            response = client.request(method, path, **kwargs)
            if not response.is_success:
                raise RuntimeError(
                    f"{response.status_code} {response.json().get('error', {}).get('code')}"
                )
            return response.json()["data"]

        slug = "deepseek-calibration"
        entries = call("GET", "evaluators", params={"q": slug})["items"]
        entry = next((e for e in entries if e["slug"] == slug), None)
        if not entry:
            entry = call(
                "POST",
                "evaluators",
                json={"kind": "llm_judge", "slug": slug, "name": "DeepSeek 单样本校准"},
            )
        path = f"evaluators/{entry['id']}"
        plugin = next(
            p for p in call("GET", "evaluators/implementations") if p["impl_key"] == "chat_judge"
        )
        versions = call("GET", path + "/versions")["items"]
        pair = []
        for i, instruction in enumerate(
            [
                "核对预测与参考答案。算术正确给 1 分，错误给 0 分。",
                "独立验证计算，再检查是否与参考答案一致。正确给 1 分，错误给 0 分。",
            ],
            1,
        ):
            label = f"p{i}-{plugin['build'][:12]}"
            v = next((x for x in versions if x["version"] == label), None)
            if not v:
                v = call(
                    "POST",
                    path + "/versions",
                    json={
                        "version": label,
                        "impl_key": "chat_judge",
                        "build": plugin["build"],
                        "config": {**plugin["example"], "timeout_s": 30},
                        "provider": "openai_compatible",
                        "model_id": "deepseek-flash",
                        "prompt_text": instruction,
                        "prompt_version": f"p{i}",
                        "rubric_version": "arithmetic-1",
                    },
                )
            pair.append(v)
        for v in (pair[0], pair[0], pair[1]):
            result = call(
                "POST",
                path + "/test",
                json={
                    "version_id": v["id"],
                    "case": {
                        "case_key": "public_arithmetic",
                        "input": {"query": "2 + 2 等于多少？"},
                        "expected": {"answer": "4"},
                    },
                    "prediction": {"answer": "4"},
                },
            )
            d = result["details"]
            print(
                v["version"],
                result["status"],
                result["score"],
                d.get("model"),
                d.get("usage"),
                "revision_unverified=",
                d.get("revision_unverified"),
                flush=True,
            )
            if result["status"] != "OK":
                raise RuntimeError(result["reason"] + " " + str(d.get("http_status", "")))


if __name__ == "__main__":
    main()
