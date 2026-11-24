from collections.abc import AsyncGenerator, Iterator
from tempfile import TemporaryFile
from typing import Any, BinaryIO, Literal, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response
from pydantic import TypeAdapter, ValidationError
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser
from starlette.responses import StreamingResponse

from app.api.access import Cat, Db, User, record
from app.api.projects import ListArgs, reply
from app.application.catalog import datasets as service
from app.application.catalog.dataset_files import MAX_BYTES, parse
from app.application.catalog.store import Fault, page, row
from app.contracts.datasets import (
    Case,
    CaseCreate,
    CasePatch,
    DatasetCreate,
    DatasetPatch,
    Format,
    Split,
    VersionAction,
)


async def public_dataset(db: Db, d: UUID) -> None:
    await row(
        db,
        "SELECT id FROM datasets d WHERE id=:d AND to_jsonb(d)->>'shadow_project_id' IS NULL",
        d=d,
    )


async def check_dataset(req: Request, db: Db) -> None:
    if "d" in req.path_params:
        try:
            d = UUID(req.path_params["d"])
        except ValueError as exc:
            raise Fault(422, "INVALID_INPUT", "数据集编号无效") from exc
        await public_dataset(db, d)


router = APIRouter(
    prefix="/api/v1/datasets", tags=["datasets"], dependencies=[Depends(check_dataset)]
)


@router.get("")
async def listing(
    req: Request, db: Db, who: User, args: ListArgs, archived: bool | None = None
) -> dict[str, object]:
    params: dict[str, Any] = {"q": "%" + args.q + "%"}
    sql = service.CATALOG + " WHERE (d.name ILIKE :q OR d.slug ILIKE :q)"
    sql += " AND to_jsonb(d)->>'shadow_project_id' IS NULL"
    if archived is not None:
        sql += " AND d.archived=:archived"
        params["archived"] = archived
    return reply(req, await page(db, sql, params, args.cursor, args.limit, str(who), args.sort))


@router.post("", status_code=201)
async def create(req: Request, db: Db, who: Cat, body: DatasetCreate) -> dict[str, object]:
    data = await service.create(db, body)
    await record(db, req, who, "create", "dataset", data["id"], list(body.model_fields_set))
    return reply(req, data)


class UploadLimit(MultiPartException):
    pass


@router.post(
    "/import",
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "multipart/form-data": {
                    "schema": {
                        "type": "object",
                        "required": ["file", "dataset_id", "draft_id", "format"],
                        "properties": {
                            "file": {"type": "string", "format": "binary"},
                            "dataset_id": {"type": "string", "format": "uuid"},
                            "draft_id": {"type": "string", "format": "uuid"},
                            "format": {"enum": ["json", "jsonl", "csv", "xlsx"]},
                            "mapping": {"type": "string"},
                            "preview": {"type": "boolean"},
                        },
                    }
                }
            },
        }
    },
)
async def import_file(req: Request, db: Db, who: Cat) -> dict[str, object]:
    async def bounded_body() -> AsyncGenerator[bytes]:
        size = 0
        async for chunk in req.stream():
            size += len(chunk)
            if size > MAX_BYTES + 65536:
                raise UploadLimit("文件过大")
            yield chunk

    try:
        form = await MultiPartParser(
            req.headers, bounded_body(), max_files=1, max_fields=6, max_part_size=8192
        ).parse()
    except UploadLimit as exc:
        raise Fault(413, "FILE_LIMIT", "文件超过 4 MiB") from exc
    except (MultiPartException, ValueError) as exc:
        raise Fault(422, "INVALID_FORM", "上传格式无效") from exc
    try:
        d = UUID(str(form.get("dataset_id")))
        v = UUID(str(form.get("draft_id")))
        value = form.get("format")
        if value not in ("json", "jsonl", "csv", "xlsx"):
            raise ValueError
        fmt = cast(Format, value)
        mapping = TypeAdapter(dict[str, str]).validate_json(str(form.get("mapping", "{}")))
        preview = TypeAdapter(bool).validate_python(form.get("preview", False))
        file = form.get("file")
        if not isinstance(file, UploadFile):
            raise ValueError
        data = await file.read(MAX_BYTES + 1)
    except (ValueError, ValidationError) as exc:
        raise Fault(422, "INVALID_FORM", "文件、版本、格式或映射参数无效") from exc
    finally:
        await form.close()
    cases = await run_in_threadpool(parse, data, fmt, mapping)
    await public_dataset(db, d)
    result = await service.import_cases(db, d, v, cases, preview, int(fmt in ("csv", "xlsx")))
    if not preview:
        await record(db, req, who, "import", "dataset_version", v, ["cases"])
    return reply(req, result)


@router.get("/{d}")
async def detail(req: Request, d: UUID, db: Db, who: User) -> dict[str, object]:
    return reply(req, await service.detail(db, d))


@router.patch("/{d}")
async def patch(req: Request, d: UUID, db: Db, who: Cat, body: DatasetPatch) -> dict[str, object]:
    result = await service.edit_catalog(db, d, body)
    await record(db, req, who, "edit", "dataset", d, list(body.model_fields_set))
    return reply(req, result)


@router.get("/{d}/versions")
async def versions(
    req: Request,
    d: UUID,
    db: Db,
    who: User,
    args: ListArgs,
    state: Literal["DRAFT", "PUBLISHED"] | None = None,
) -> dict[str, object]:
    await row(db, "SELECT id FROM datasets WHERE id=:d", d=d)
    params: dict[str, Any] = {"d": d}
    sql = service.VERSION + " WHERE v.dataset_id=:d"
    if state:
        sql += " AND v.state=:state"
        params["state"] = state
    return reply(req, await page(db, sql, params, args.cursor, args.limit, str(who), args.sort))


@router.post("/{d}/versions", status_code=201)
async def change_version(
    req: Request, d: UUID, db: Db, who: Cat, body: VersionAction
) -> dict[str, object]:
    result = await service.version_action(db, d, body)
    await record(db, req, who, body.action, "dataset_version", result["id"], ["state"])
    return reply(req, result)


@router.get("/{d}/cases")
async def cases(
    req: Request,
    d: UUID,
    version_id: UUID,
    db: Db,
    who: User,
    args: ListArgs,
    category: str | None = None,
    difficulty: str | None = None,
    tag: str | None = None,
    split: Split | None = None,
) -> dict[str, object]:
    await row(
        db, "SELECT id FROM dataset_versions WHERE dataset_id=:d AND id=:v", d=d, v=version_id
    )
    sql = """SELECT * FROM evaluation_cases WHERE dataset_version_id=:v
        AND (case_key ILIKE :q OR input->>'query' ILIKE :q)"""
    params: dict[str, Any] = {"v": version_id, "q": "%" + args.q + "%"}
    for key, value in (("category", category), ("difficulty", difficulty), ("split", split)):
        if value is not None:
            sql += f" AND {key}=:{key}"
            params[key] = value
    if tag is not None:
        sql += " AND tags @> ARRAY[CAST(:tag AS text)]"
        params["tag"] = tag
    return reply(req, await page(db, sql, params, args.cursor, args.limit, str(who), args.sort))


@router.post("/{d}/cases", status_code=201)
async def add(req: Request, d: UUID, db: Db, who: Cat, body: CaseCreate) -> dict[str, object]:
    data = await service.add(
        db, d, body.draft_id, Case.model_validate(body.model_dump(exclude={"draft_id"}))
    )
    await record(db, req, who, "add", "evaluation_case", data["id"], ["case_key"])
    return reply(req, data)


@router.patch("/{d}/cases/{c}")
async def edit(
    req: Request, d: UUID, c: UUID, db: Db, who: Cat, body: CasePatch
) -> dict[str, object]:
    data = await service.edit(db, d, c, body)
    await record(db, req, who, "edit", "evaluation_case", c, list(body.model_fields_set))
    return reply(req, data)


@router.delete("/{d}/cases/{c}", status_code=204)
async def remove(req: Request, d: UUID, c: UUID, draft_id: UUID, db: Db, who: Cat) -> Response:
    await service.remove(db, d, draft_id, c)
    await record(db, req, who, "remove", "evaluation_case", c, [])
    return Response(status_code=204)


@router.get("/{d}/export")
async def export(d: UUID, version_id: UUID, format: Format, db: Db, who: User) -> StreamingResponse:
    tmp = cast(BinaryIO, TemporaryFile("w+b"))
    try:
        await service.export(db, d, version_id, format, tmp)
    except BaseException:
        tmp.close()
        raise

    def blocks() -> Iterator[bytes]:
        while chunk := tmp.read(65536):
            yield chunk

    return StreamingResponse(
        blocks(),
        media_type={
            "csv": "text/csv",
            "json": "application/json",
            "jsonl": "application/x-ndjson",
            "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        }[format],
        headers={"Content-Disposition": f'attachment; filename="cases-{version_id}.{format}"'},
        background=BackgroundTask(tmp.close),
    )
