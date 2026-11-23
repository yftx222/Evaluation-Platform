"""Bounded import parsers and incremental writers for the public Case representation."""

import csv
import io
import json
import re
from collections.abc import Iterable, Iterator
from typing import Any, BinaryIO
from xml.etree.ElementTree import ParseError
from zipfile import BadZipFile, ZipFile

from defusedxml.common import DefusedXmlException  # type: ignore[import-untyped]
from openpyxl import Workbook, load_workbook  # type: ignore[import-untyped]
from pydantic import ValidationError

from app.application.catalog.store import Fault
from app.contracts.datasets import Case, Format

FIELDS = tuple(Case.model_fields)
MAX_BYTES = 4 * 1024 * 1024
MAX_ROWS = 5000


def invalid(line: int, field: str, reason: str) -> Fault:
    return Fault(422, "IMPORT_INVALID", f"第 {line} 行 · {field} · {reason}")


def sensitive(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            k: "[REDACTED]"
            if re.search(r"password|secret|token|authorization|api[_-]?key", k, re.I)
            else sensitive(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [sensitive(v) for v in value]
    if isinstance(value, str):
        return re.sub(r"(?i)\bbearer\s+[^\s,;]+", "Bearer [REDACTED]", value)
    return value


def public_case(case: dict[str, Any]) -> dict[str, Any]:
    return {k: sensitive(case[k]) for k in FIELDS}


def compact(value: object) -> str:
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    )


def table_rows(
    rows: Iterable[Iterable[Any]], mapping: dict[str, str]
) -> Iterator[tuple[int, object]]:
    iterator = iter(rows)
    columns = list(next(iterator, ()))
    if (
        not columns
        or len(columns) > 32
        or len(set(columns)) != len(columns)
        or not all(isinstance(h, str) for h in columns)
        or set(mapping) - set(columns)
    ):
        raise invalid(1, "header", "列名重复或映射无效")
    names = [mapping.get(c, c) for c in columns]
    if set(names) - set(FIELDS) or len(set(names)) != len(names):
        raise invalid(1, "header", "未知或重复的目标字段")
    for i, values in enumerate(iterator, 2):
        cells = list(values)
        if len(cells) != len(names):
            raise invalid(i, "row", "列数与表头不符")
        record = {}
        for key, v in zip(names, cells, strict=True):
            if v is None or v == "":
                continue
            if not isinstance(v, str):
                raise invalid(i, key, "请使用文本或 JSON 单元格")
            try:
                record[key] = (
                    json.loads(v)
                    if key in ("input", "expected", "tags", "extensions")
                    or v.startswith('"')
                    or v == "null"
                    else v
                )
            except ValueError as exc:
                raise invalid(i, key, "单元格 JSON 格式不正确") from exc
        yield i, record


def source(data: bytes, fmt: Format, mapping: dict[str, str]) -> Iterator[tuple[int, object]]:
    if fmt == "xlsx":
        buf = io.BytesIO(data)
        with ZipFile(buf) as archive:
            entries = archive.infolist()
            if len(entries) > 128 or sum(x.file_size for x in entries) > 16 * MAX_BYTES:
                raise Fault(413, "FILE_LIMIT", "工作簿解压后超过限制")
            if any(x.filename.lower().endswith(".bin") for x in entries):
                raise invalid(1, "file", "不接受宏工作簿")
        wb = load_workbook(buf, read_only=True, data_only=False, keep_links=False)
        try:
            if len(wb.worksheets) != 1:
                raise invalid(1, "file", "请选择单工作表文件")
            sheet = wb.worksheets[0]
            sheet.reset_dimensions()

            def values() -> Iterator[list[Any]]:
                for i, r in enumerate(sheet.rows, 1):
                    if len(r) > 32 or any(c.data_type == "f" for c in r):
                        raise invalid(i, "row", "列数超限或包含公式")
                    yield [c.value for c in r]

            yield from table_rows(values(), mapping)
        finally:
            wb.close()
        return
    text = data.decode("utf-8-sig")
    if fmt == "csv":
        yield from table_rows(csv.reader(io.StringIO(text, newline=""), strict=True), mapping)
    elif fmt == "json":
        content = json.loads(text)
        if not isinstance(content, list):
            raise invalid(1, "file", "需要 JSON 数组")
        yield from enumerate(content, 1)
    else:
        for i, line in enumerate(io.StringIO(text), 1):
            try:
                yield i, json.loads(line)
            except ValueError as exc:
                raise invalid(i, "row", "JSONL 格式不正确") from exc


def parse(data: bytes, fmt: Format, mapping: dict[str, str]) -> list[Case]:
    if len(data) > MAX_BYTES:
        raise Fault(413, "FILE_LIMIT", "文件超过 4 MiB")
    if mapping and fmt in ("json", "jsonl"):
        raise invalid(1, "mapping", "仅表格支持列映射")
    result: list[Case] = []
    seen = set()
    try:
        for i, raw in source(data, fmt, mapping):
            if len(result) >= MAX_ROWS:
                raise Fault(413, "ROW_LIMIT", f"第 {i} 行 · row · 单次最多 5000 条")
            try:
                case = Case.model_validate(raw)
            except ValidationError as exc:
                err = exc.errors(include_input=False)[0]
                name = str(err["loc"][0]) if err["loc"] and err["loc"][0] in FIELDS else "row"
                raise invalid(i, name, "字段缺失、格式错误或内容超限") from exc
            if case.case_key in seen:
                raise invalid(i, "case_key", "文件内有重复标识")
            seen.add(case.case_key)
            result.append(case)
    except Fault:
        raise
    except (
        ValueError,
        KeyError,
        TypeError,
        csv.Error,
        RecursionError,
        BadZipFile,
        ParseError,
        DefusedXmlException,
        OSError,
        IndexError,
        OverflowError,
    ) as exc:
        raise invalid(1, "file", "无法解析文件") from exc
    if not result:
        raise invalid(1, "file", "没有样本")
    return result


class ExportWriter:
    def __init__(self, stream: BinaryIO, fmt: Format) -> None:
        self.stream, self.fmt, self.count = stream, fmt, 0
        self.workbook = Workbook(write_only=True) if fmt == "xlsx" else None
        self.sheet = self.workbook.create_sheet("Cases") if self.workbook else None
        if fmt == "json":
            stream.write(b"[")
        elif fmt in ("csv", "xlsx"):
            self.table(list(FIELDS))

    def table(self, cells: list[str]) -> None:
        if self.sheet is not None:
            self.sheet.append(cells)
        else:
            line = io.StringIO(newline="")
            csv.writer(line).writerow(cells)
            self.stream.write(line.getvalue().encode())

    def append(self, case: dict[str, Any]) -> None:
        case = public_case(case)
        if self.fmt in ("csv", "xlsx"):
            cells = [compact(case[f]) for f in FIELDS]
            if self.sheet is not None and any(len(c) > 32767 for c in cells):
                raise Fault(422, "XLSX_CELL_LIMIT", "样本超过 Excel 单元格容量，请选择 JSONL")
            self.table(cells)
        else:
            if self.fmt == "json" and self.count:
                self.stream.write(b",")
            self.stream.write(compact(case).encode())
            if self.fmt == "jsonl":
                self.stream.write(b"\n")
        self.count += 1

    def finish(self) -> None:
        if self.workbook:
            self.workbook.save(self.stream)
            self.workbook.close()
        if self.fmt == "json":
            self.stream.write(b"]")
        self.stream.seek(0)
