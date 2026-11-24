import asyncio
import csv
import io
import json
import socket
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient
from openpyxl import Workbook, load_workbook  # type: ignore[import-untyped]
from playwright.sync_api import expect, sync_playwright
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from app.api.main import create_app
from app.application.catalog.dataset_files import MAX_BYTES, ExportWriter, parse
from app.application.catalog.store import Fault
from app.contracts.datasets import Case, Format
from app.infrastructure.resources import Settings

ROOT = "/api/v1/datasets"


def example(key: str) -> dict[str, Any]:
    return {
        "case_key": key,
        "input": {"query": "商品？", "context": {"空": "", "无": None, "列表": []}},
        "expected": {"answer": "有", "criteria": ["完整"]},
        "extensions": {"domain": {}},
        "tags": ["中文"],
        "category": "客服",
        "difficulty": "medium",
        "split": "test",
    }


def test_catalog_snapshots_and_files(catalog: tuple[Settings, dict[str, str]]) -> None:
    settings, tokens = catalog

    def auth(name: str) -> dict[str, str]:
        return {"Authorization": "Bearer " + tokens[name]}

    with TestClient(create_app(settings), headers=auth("curator")) as client:

        def make(slug: str) -> tuple[str, str]:
            response = client.post(ROOT, json={"name": slug, "slug": slug})
            assert response.status_code == 201, response.text
            d = response.json()["data"]
            return d["id"], d["draft_id"]

        def upload(d: str, v: str, fmt: str, content: bytes, preview: bool = False) -> Any:
            return client.post(
                ROOT + "/import",
                files={"file": ("cases." + fmt, content)},
                data={"dataset_id": d, "draft_id": v, "format": fmt, "preview": str(preview)},
            )

        d, v = make("original")
        path = ROOT + "/" + d

        def info() -> dict[str, Any]:
            return dict(client.get(path).json()["data"]["draft"])

        first = info()["digest"]

        def publish(h: str) -> int:
            return int(
                client.post(
                    path + "/versions",
                    json={"action": "publish", "draft_id": v, "reviewed_digest": h},
                ).status_code
            )

        assert publish(first) == 422
        item = client.post(path + "/cases", json={**example("edit"), "draft_id": v})
        assert item.status_code == 201, item.text
        cpath = path + "/cases/" + item.json()["data"]["id"]
        changed = client.patch(cpath, json={"draft_id": v, "category": "changed"})
        assert changed.status_code == 200 and changed.json()["data"]["category"] == "changed"
        assert client.delete(cpath, params={"draft_id": v}).status_code == 204
        assert info()["digest"] == first
        rows = [example("case_" + str(i)) for i in range(101)]
        raw = json.dumps(rows[::-1]).encode()
        assert upload(d, v, "json", raw, True).json()["data"]["count"] == 101
        assert info()["sample_count"] == 0
        assert upload(d, v, "json", raw).json()["data"]["inserted"] == 101
        assert publish(first) == 409
        for bad, field in [
            ([example("new"), {**example("bad"), "unknown": "private"}], "row"),
            ([example("new"), example("new")], "case_key"),
            ([{**example("bad"), "input": {"attachments": ["file:///bad"]}}], "input"),
        ]:
            result = upload(d, v, "json", json.dumps(bad).encode())
            assert result.status_code == 422 and field in result.json()["error"]["message"]
            assert "private" not in result.text
        assert (
            upload(d, v, "json", json.dumps([example("new"), rows[0]]).encode()).status_code == 409
        )
        assert upload(d, v, "json", b"x" * (MAX_BYTES + 1)).status_code == 413
        assert info()["sample_count"] == 101
        current = info()["digest"]
        assert publish(current) == 201
        assert (
            client.post(path + "/cases", json={**example("new"), "draft_id": v}).status_code == 409
        )
        filtered = client.get(
            path + "/cases",
            params=dict(
                version_id=v,
                category="客服",
                difficulty="medium",
                tag="中文",
                split="test",
                q="商品",
                limit=1,
            ),
        ).json()["data"]
        assert filtered["items"] and filtered["next_cursor"]
        for fmt in ("json", "jsonl", "csv", "xlsx"):
            exported = client.get(path + "/export", params={"version_id": v, "format": fmt})
            assert exported.status_code == 200
            other, other_v = make("copy-" + fmt)
            uploaded = upload(other, other_v, fmt, exported.content)
            assert uploaded.status_code == 200, uploaded.text
            assert client.get(ROOT + "/" + other).json()["data"]["draft"]["digest"] == current

        def clone() -> int:
            return int(
                client.post(
                    path + "/versions", json={"action": "draft", "base_version_id": v}
                ).status_code
            )

        with ThreadPoolExecutor(2) as pool:
            assert sorted(pool.map(lambda _: clone(), range(2))) == [201, 409]
        draft = info()
        assert draft["base_version_id"] == v and draft["digest"] == current
        assert draft["changes"] == dict(added=0, modified=0, removed=0)
        private_case = {**example("export-check"), "extensions": {"api_key": "sensitive-marker"}}
        assert (
            client.post(path + "/cases", json={**private_case, "draft_id": draft["id"]}).status_code
            == 201
        )
        out = client.get(path + "/export", params={"version_id": draft["id"], "format": "json"})
        assert (
            out.status_code == 200
            and "sensitive-marker" not in out.text
            and "[REDACTED]" in out.text
        )
        for slug in ("shop", "rag"):
            projects = "/api/v1/projects"
            project = client.post(
                projects,
                json=dict(
                    name=slug, slug=slug, kind="rag", rpm=60, max_parallel=2, target_max_parallel=1
                ),
            ).json()["data"]
            assert (
                client.post(
                    projects + "/" + project["id"] + "/members",
                    json=dict(issuer="development", subject=slug, role="R"),
                ).status_code
                == 201
            )
            assert len(client.get(projects, headers=auth(slug)).json()["data"]["items"]) == 1
            same = client.get(path + "/cases", params={"version_id": v}, headers=auth(slug))
            assert (
                same.status_code == 200
                and same.json()["data"]["items"][0]["dataset_version_id"] == v
            )
            assert client.patch(path, json={"name": "no"}, headers=auth(slug)).status_code == 403
        assert client.patch(path, json={"archived": True}).status_code == 200
        assert (
            client.post(
                path + "/cases", json={**example("new"), "draft_id": draft["id"]}
            ).status_code
            == 409
        )
        assert client.get(path + "/versions").json()["data"]["items"][1]["digest"] == current

        async def direct_write() -> None:
            engine = create_async_engine(settings.database_url.get_secret_value())
            try:
                for sql in [
                    "DELETE FROM dataset_versions WHERE id=:v",
                    "UPDATE evaluation_cases SET category='invalid' WHERE dataset_version_id=:v",
                    "DELETE FROM evaluation_cases WHERE dataset_version_id=:v",
                ]:
                    with pytest.raises(IntegrityError):
                        async with engine.begin() as conn:
                            await conn.execute(text(sql), {"v": v})
            finally:
                await engine.dispose()

        asyncio.run(direct_write())


@pytest.mark.parametrize("fmt", ["json", "jsonl", "csv", "xlsx"])
def test_formula_like_values_roundtrip(fmt: Format) -> None:
    values = [
        "=1+1",
        "+SUM(1,2)",
        "-1+2",
        "@SUM(1)",
        " \t=1+1",
        "\r=1+1",
        "\n=1+1",
        'text,"quoted"\n=1+1',
        "'=1+1",
        "＝1+1",
    ]
    cases = [
        Case.model_validate({**example(str(i)), "category": value, "input": {"query": value}})
        for i, value in enumerate(values)
    ]
    stream = io.BytesIO()
    writer = ExportWriter(stream, fmt)
    for case in cases:
        writer.append(case.model_dump())
    writer.finish()
    assert parse(stream.getvalue(), fmt, {}) == cases
    if fmt == "xlsx":
        book = load_workbook(stream, read_only=True, data_only=False)
        try:
            assert all(cell.data_type == "s" for row in book.active.rows for cell in row)
        finally:
            book.close()
    if fmt == "csv":
        for row in csv.reader(io.StringIO(stream.getvalue().decode(), newline="")):
            assert all(not cell.lstrip().startswith(("=", "+", "-", "@", "＝")) for cell in row)


def test_raw_spreadsheet_cells_are_text() -> None:
    values = ["=1+1", " \t+2", "-1", "@SUM(1)", "\tplain", "\r=1", "＝1+1"]
    stream = io.BytesIO()
    csv_writer = ExportWriter(stream, "csv")
    csv_writer.table(values)
    csv_writer.finish()
    assert list(csv.reader(io.StringIO(stream.getvalue().decode(), newline="")))[1] == [
        "'" + value for value in values
    ]
    stream = io.BytesIO()
    xlsx_writer = ExportWriter(stream, "xlsx")
    xlsx_writer.table(values)
    xlsx_writer.finish()
    book = load_workbook(stream, read_only=True, data_only=False)
    try:
        row = list(book.active.rows)[1]
        assert all(cell.data_type == "s" for cell in row)
        # XML readers may normalize raw CR to LF. Case fields above use JSON escaping.
        assert [cell.value.replace("\r", "\n") for cell in row] == [
            value.replace("\r", "\n") for value in values
        ]
    finally:
        book.close()


def test_file_mapping_and_formula() -> None:
    content = '编号,问题,答案\na,"{""query"":""hi""}",{}\n'.encode()
    mapping = {"编号": "case_key", "问题": "input", "答案": "expected"}
    assert parse(content, "csv", mapping)[0].input == {"query": "hi"}
    wb = Workbook()
    wb.active.append(list(mapping))
    wb.active.append(["a", '{"query":"hi"}', "{}"])
    b = io.BytesIO()
    wb.save(b)
    assert parse(b.getvalue(), "xlsx", mapping)[0].case_key == "a"
    wb.active["C2"] = "=1+1"
    b = io.BytesIO()
    wb.save(b)
    with pytest.raises(Fault, match="公式"):
        parse(b.getvalue(), "xlsx", mapping)


def test_browser_catalog(catalog: tuple[Settings, dict[str, str]]) -> None:
    settings, tokens = catalog
    app = create_app(settings)
    app.mount("/", StaticFiles(directory=Path("../frontend-ios/dist"), html=True))
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        server = uvicorn.Server(uvicorn.Config(app, log_level="warning", access_log=False))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
        thread.start()
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(channel="chrome", headless=True)
                page = browser.new_page()
                page.goto(f"http://127.0.0.1:{sock.getsockname()[1]}/")
                page.get_by_label("组织访问令牌").fill(tokens["curator"])
                page.get_by_role("button", name="登录", exact=True).click()
                page.get_by_role("button", name="数据集管理", exact=True).click()
                page.get_by_label("数据集标识").fill("browser")
                page.get_by_label("数据集名称").fill("浏览器题库")
                page.get_by_role("button", name="创建数据集").click()
                page.get_by_role("button", name="新增样本").click()
                page.get_by_label("样本标识").fill("manual")
                page.get_by_label("输入 JSON").fill('{"query":"question"}')
                page.get_by_role("button", name="保存样本").click()
                page.locator("summary").filter(has_text="manual").click()
                page.get_by_role("button", name="编辑样本").click()
                page.get_by_label("参考答案与 criteria JSON").fill('{"answer":"edited"}')
                page.get_by_role("button", name="保存样本").click()
                page.get_by_label("上传文件").set_input_files(
                    {
                        "name": "sample.json",
                        "mimeType": "application/json",
                        "buffer": json.dumps([example("imported")]).encode(),
                    }
                )
                page.get_by_role("button", name="预览导入").click()
                page.get_by_role("button", name="确认导入").click()
                expect(page.locator("summary").filter(has_text="imported")).to_be_visible()
                page.get_by_role("button", name="审核草稿").click()
                page.get_by_role("button", name="确认发布").click()
                expect(page.get_by_role("button", name="从此版本创建草稿")).to_be_visible()
                expect(page.get_by_role("button", name="新增样本")).to_have_count(0)
                page.get_by_role("button", name="评分器管理", exact=True).click()
                page.get_by_label("评分器标识").fill("browser_score")
                page.get_by_label("评分器名称").fill("浏览器评分")
                page.get_by_role("button", name="创建评分器", exact=True).click()
                page.get_by_label("版本号").fill("v1")
                page.get_by_role("button", name="发布版本", exact=True).click()
                page.get_by_role("button", name="试评一次").click()
                expect(page.get_by_role("heading", name="v1 · OK")).to_be_visible()
                browser.close()
        finally:
            server.should_exit = True
            thread.join(8)
