import csv
import io
import json
from collections.abc import Iterator
from tempfile import SpooledTemporaryFile
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import StreamingResponse
from sqlalchemy import text

from app.api.access import Actor, Db, ProjectKey, User, authorize
from app.api.projects import reply as envelope
from app.application.catalog.dataset_files import csv_cell
from app.application.catalog.store import Fault, row
from app.application.report import analysis, metrics
from app.contracts.reports import (
    CasePage,
    Dimension,
    GroupPage,
    ReportResponse,
    RunComparison,
    RunReport,
    Signal,
)
from trace_sdk.privacy import PRIVATE_VALUE

router = APIRouter(prefix="/api/v1/reports", tags=["reports"])


def safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [safe(v) for v in value]
    if isinstance(value, str):
        return PRIVATE_VALUE.sub("[REDACTED]", value)
    return value


def reply(req: Request, data: object) -> dict[str, object]:
    return envelope(req, jsonable_encoder(safe(data)))


async def snapshot(db: Db, who: Actor, r: UUID) -> dict[str, Any]:
    owner = await row(db, "SELECT project_id,status FROM evaluation_runs WHERE id=:r", r=r)
    await authorize(db, who, owner["project_id"], "R", key_read=True, key_scope="results:read")
    if owner["status"] not in analysis.TERMINAL:
        raise Fault(409, "RUN_NOT_FINISHED", "运行结束后可以查看报告")
    record = await row(db, "SELECT config_snapshot FROM evaluation_runs WHERE id=:r", r=r)
    return dict(record["config_snapshot"])


async def report(db: Db, r: UUID, snap: dict[str, Any]) -> dict[str, Any]:
    summary = await metrics.summary(db, r, snap)
    process = await metrics.process(db, r, snap)
    usage = await analysis.model_usage(db, r, snap)
    if sum(m["products"] for m in usage["models"]) != summary["total_cases"] * (
        1 + metrics.params(r, snap)["judges"]
    ):
        usage["estimated_cost"] = None
    return {
        "summary": summary,
        "process": process,
        "usage": usage,
        "conditions": analysis.comparison_conditions(snap, usage),
        "limits": {
            k: snap.get("report_policy", {}).get(k) for k in ("max_case_tokens", "max_case_ms")
        },
    }


@router.get("/runs/{r}", response_model=ReportResponse[RunReport])
async def overview(req: Request, db: Db, who: Actor, r: UUID) -> dict[str, object]:
    return reply(req, await report(db, r, await snapshot(db, who, r)))


@router.get("/runs/{r}/metrics", response_model=ReportResponse[GroupPage])
async def breakdown(
    req: Request,
    db: Db,
    who: Actor,
    r: UUID,
    dimension: Dimension = "category",
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
    evaluator_version_id: UUID | None = None,
) -> dict[str, object]:
    snap = await snapshot(db, who, r)
    if evaluator_version_id and not any(
        n["id"] == str(evaluator_version_id) for n in snap["evaluator_plan"]
    ):
        raise Fault(422, "VERSION_OUTSIDE_RUN", "评分器版本不在本次运行中")
    return reply(
        req, await metrics.groups(db, r, snap, dimension, offset, limit, evaluator_version_id)
    )


def delta(a: Any, b: Any) -> float | None:
    return None if a is None or b is None else float(b) - float(a)


@router.get("/compare", response_model=ReportResponse[RunComparison])
async def compare(
    req: Request,
    db: Db,
    who: User,
    run_id: Annotated[list[UUID], Query(min_length=2, max_length=5)],
    dimension: Dimension = "category",
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
) -> dict[str, object]:
    if len(set(run_id)) != len(run_id):
        raise Fault(422, "DUPLICATE_RUN", "请选择不同运行，第一项为基线")
    snaps = [await snapshot(db, who, r) for r in run_id]
    data = [await report(db, r, s) for r, s in zip(run_id, snaps, strict=True)]
    group_pages = [
        await metrics.groups(db, r, s, dimension, offset, limit)
        for r, s in zip(run_id, snaps, strict=True)
    ]
    base = data[0]
    results = []
    for i, candidate in enumerate(data[1:], 1):
        mismatch = analysis.differences(
            base["conditions"],
            candidate["conditions"],
            base["usage"]["truncated"] or candidate["usage"]["truncated"],
        )
        entry: dict[str, Any] = {
            "run_id": run_id[i],
            "differences": mismatch,
            "comparable": not mismatch,
            "deltas": None,
            "changes": None,
            "group_deltas": None,
        }
        if not mismatch:
            a, b = base["summary"], candidate["summary"]
            ds = {
                k: delta(a["metrics"][k]["value"], b["metrics"][k]["value"])
                for k in (
                    "case_success_rate",
                    "score_execution_success_rate",
                    "score_coverage",
                    "pass_rate",
                )
            }
            ds.update(
                {
                    k: delta(a[k], b[k])
                    for k in (
                        "latency_ms_p50",
                        "latency_ms_p95",
                        "total_tokens",
                        "complete",
                        "partial",
                        "missing",
                    )
                }
            )
            ds["trace_complete_rate"] = delta(
                a["trace_complete_rate"]["value"], b["trace_complete_rate"]["value"]
            )
            entry["deltas"] = ds
            entry["changes"] = await analysis.changes(db, run_id[i], snaps[i], run_id[0])
            before = {g["label"]: g for g in group_pages[0]["items"]}
            entry["group_deltas"] = [
                {
                    "label": g["label"],
                    "cases": g["cases"],
                    "pass_rate": delta(
                        before[g["label"]]["pass_rate"]["value"], g["pass_rate"]["value"]
                    ),
                    "mean_score": delta(before[g["label"]]["mean_score"], g["mean_score"]),
                }
                for g in group_pages[i]["items"]
                if g["label"] in before
            ]
        results.append(entry)
    return reply(
        req, {"baseline": run_id[0], "reports": data, "groups": group_pages, "comparisons": results}
    )


class Filter:
    def __init__(
        self,
        signal: Signal = "any",
        threshold: Annotated[float, Query(ge=0, le=1)] = 0.5,
        baseline_id: UUID | None = None,
    ) -> None:
        self.signal, self.threshold, self.baseline = signal, threshold, baseline_id


Filters = Annotated[Filter, Depends()]


async def query_for(
    db: Db, who: Actor, r: UUID, snap: dict[str, Any], f: Filters
) -> tuple[str, dict[str, Any]]:
    if f.baseline:
        if isinstance(who, ProjectKey):
            raise Fault(403, "FORBIDDEN", "跨运行比较需要组织用户身份")
        other = await snapshot(db, who, f.baseline)
        info = await analysis.model_usage(db, r, snap)
        prior = await analysis.model_usage(db, f.baseline, other)
        if f.baseline == r or analysis.differences(
            analysis.comparison_conditions(snap, info),
            analysis.comparison_conditions(other, prior),
            info["truncated"] or prior["truncated"],
        ):
            raise Fault(409, "INCOMPARABLE_RUNS", "基线与当前运行不满足直接比较条件")
    return analysis.case_query(r, snap, f.signal, f.threshold, f.baseline)


def public_case(item: dict[str, Any]) -> dict[str, Any]:
    item["case_key"] = PRIVATE_VALUE.sub("[REDACTED]", item["case_key"])
    item["evidence_url"] = f"/api/v1/case-executions/{item['execution_id']}/evidence"
    return item


@router.get("/runs/{r}/bad-cases", response_model=ReportResponse[CasePage])
async def bad_cases(
    req: Request,
    db: Db,
    who: Actor,
    r: UUID,
    f: Filters,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
) -> dict[str, object]:
    sql, args = await query_for(db, who, r, await snapshot(db, who, r), f)
    items = await metrics.rows(
        db, sql + " LIMIT :n OFFSET :start", {**args, "n": limit + 1, "start": offset}
    )
    return envelope(
        req,
        {
            "items": [public_case(v) for v in items[:limit]],
            "next_offset": offset + limit if len(items) > limit else None,
        },
    )


def chunks(fp: SpooledTemporaryFile[bytes]) -> Iterator[bytes]:
    try:
        while data := fp.read(65536):
            yield data
    finally:
        fp.close()


def dump(value: Any) -> bytes:
    return json.dumps(jsonable_encoder(value), ensure_ascii=False, allow_nan=False).encode()


@router.get("/runs/{r}/export")
async def export(
    req: Request, db: Db, who: User, r: UUID, f: Filters, format: Literal["csv", "json"] = "json"
) -> StreamingResponse:
    snap = await snapshot(db, who, r)
    if "signal" not in req.query_params:
        f.signal = "all"
    sql, args = await query_for(db, who, r, snap, f)
    info = await report(db, r, snap)
    fp = SpooledTemporaryFile(max_size=1024 * 1024, mode="w+b")
    try:
        fields = (
            "execution_id",
            "case_key",
            "status",
            "trace_id",
            "trace_status",
            "case_ms",
            "input_tokens",
            "output_tokens",
            "judge_tokens",
            "total_tokens",
            "min_score",
            "errors",
            "failed_scores",
            "attempt_count",
            "reasons",
            "evidence_url",
        )

        def write_csv(cells: list[str]) -> None:
            buffer = io.StringIO(newline="")
            csv.writer(buffer).writerow(csv_cell(v) for v in cells)
            fp.write(buffer.getvalue().encode())

        first = True
        if format == "json":
            fp.write(b'{"report":' + dump(safe(info)) + b',"cases":[')
        else:
            fp.write(b"\xef\xbb\xbf")
            write_csv(
                ["record_type", "run_id", "metric", "value", "numerator", "denominator", *fields]
            )
            summary = info["summary"]
            for name in (
                "case_success_rate",
                "score_execution_success_rate",
                "score_coverage",
                "pass_rate",
            ):
                rate = summary["metrics"][name]
                write_csv(
                    [
                        "metric",
                        str(r),
                        name,
                        "" if rate["value"] is None else str(rate["value"]),
                        str(rate["numerator"]),
                        str(rate["denominator"]),
                        *([""] * len(fields)),
                    ]
                )
            for name in (
                "total_cases",
                "latency_ms_p50",
                "latency_ms_p95",
                "complete",
                "partial",
                "missing",
                "not_collected",
                "total_tokens",
            ):
                value = summary[name]
                write_csv(
                    [
                        "metric",
                        str(r),
                        name,
                        "" if value is None else str(value),
                        "",
                        "",
                        *([""] * len(fields)),
                    ]
                )
        cursor = await db.stream(text(sql).execution_options(yield_per=128), args)
        try:
            async for rec in cursor.mappings():
                case = public_case(dict(rec))
                if format == "json":
                    fp.write((b"" if first else b",") + dump(case))
                    first = False
                else:
                    write_csv(
                        [
                            "case",
                            str(r),
                            "",
                            "",
                            "",
                            "",
                            *[
                                ""
                                if case[k] is None
                                else ",".join(case[k])
                                if k == "reasons"
                                else str(case[k])
                                for k in fields
                            ],
                        ]
                    )
        finally:
            await cursor.close()
        if format == "json":
            fp.write(b"]}")
        fp.seek(0)
        return StreamingResponse(
            chunks(fp),
            media_type="text/csv" if format == "csv" else "application/json",
            headers={"Content-Disposition": f'attachment; filename="report-{r}.{format}"'},
        )
    except BaseException:
        fp.close()
        raise
