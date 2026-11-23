import hashlib
import json
from typing import Any, BinaryIO
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.application.catalog.dataset_files import FIELDS, ExportWriter, compact
from app.application.catalog.store import Fault, row
from app.contracts.datasets import (
    Case,
    CasePatch,
    DatasetCreate,
    DatasetPatch,
    Format,
    VersionAction,
)

COLS = ", ".join(FIELDS)
VERSION = """SELECT v.*,(SELECT count(*) FROM evaluation_cases c WHERE c.dataset_version_id=v.id)
 AS sample_count FROM dataset_versions v"""
CATALOG = """SELECT d.*,(SELECT count(*) FROM dataset_versions WHERE dataset_id=d.id)
 AS version_count,
 (SELECT count(*) FROM evaluation_cases WHERE dataset_version_id=(SELECT id FROM dataset_versions
 WHERE dataset_id=d.id ORDER BY version DESC LIMIT 1)) AS sample_count FROM datasets d"""


async def create(db: AsyncSession, data: DatasetCreate) -> dict[str, Any]:
    d = await row(
        db,
        """INSERT INTO datasets(slug,name,description)
        VALUES (:slug,:name,:description) RETURNING *""",
        **data.model_dump(),
    )
    d["draft_id"] = (
        await row(
            db,
            """INSERT INTO dataset_versions(dataset_id,version,state)
        VALUES (:d,1,'DRAFT') RETURNING id""",
            d=d["id"],
        )
    )["id"]
    return d


async def lock_catalog(db: AsyncSession, d: UUID) -> dict[str, Any]:
    return await row(db, "SELECT * FROM datasets WHERE id=:d FOR UPDATE", d=d)


async def edit_catalog(db: AsyncSession, d: UUID, patch: DatasetPatch) -> dict[str, Any]:
    current = await lock_catalog(db, d)
    fields = patch.model_dump(exclude_unset=True)
    if not fields or any(fields[k] is None for k in fields if k != "description"):
        raise Fault(422, "INVALID_INPUT", "缺少有效变更")
    if current["archived"] and fields != {"archived": False}:
        raise Fault(409, "ARCHIVED", "请先解除归档")
    changes = ",".join(f"{k}=:{k}" for k in fields)
    return await row(
        db,
        f"UPDATE datasets SET {changes}, updated_at=now() WHERE id=:d RETURNING *",
        d=d,
        **fields,
    )


async def active(db: AsyncSession, d: UUID) -> None:
    if (await lock_catalog(db, d))["archived"]:
        raise Fault(409, "ARCHIVED", "数据集已归档")


async def writable(db: AsyncSession, d: UUID, v: UUID) -> dict[str, Any]:
    await active(db, d)
    version = await row(
        db, "SELECT * FROM dataset_versions WHERE dataset_id=:d AND id=:v FOR UPDATE", d=d, v=v
    )
    if version["state"] != "DRAFT":
        raise Fault(409, "VERSION_FROZEN", "只能修改草稿")
    return version


async def checksum(db: AsyncSession, v: UUID, *, validate: bool = False) -> str:
    result = await db.stream(
        text(f"""SELECT {COLS} FROM evaluation_cases
        WHERE dataset_version_id=:v ORDER BY case_key COLLATE "C" """),
        {"v": v},
    )
    hash_ = hashlib.sha256(b"[")
    first = True
    async for item in result.mappings():
        if validate:
            try:
                Case.model_validate(dict(item))
            except ValueError as exc:
                raise Fault(422, "INVALID_CASE", "存在无效样本，请修订后发布") from exc
        if not first:
            hash_.update(b",")
        hash_.update(compact(dict(item)).encode())
        first = False
    hash_.update(b"]")
    return hash_.hexdigest()


async def detail(db: AsyncSession, d: UUID) -> dict[str, Any]:
    dataset = await row(db, "SELECT * FROM datasets WHERE id=:d FOR SHARE", d=d)
    stmt = text(VERSION + " WHERE v.dataset_id=:d ORDER BY version DESC")
    versions = [dict(v) for v in (await db.execute(stmt, {"d": d})).mappings()]
    draft = next((v for v in versions if v["state"] == "DRAFT"), None)
    if draft:
        draft["digest"] = await checksum(db, draft["id"])
        draft["changes"] = await row(
            db,
            """WITH old AS (
            SELECT case_key,to_jsonb(c)-ARRAY['id','created_at','dataset_version_id'] AS content
            FROM evaluation_cases c WHERE dataset_version_id=:base), new AS (
            SELECT case_key,to_jsonb(c)-ARRAY['id','created_at','dataset_version_id'] AS content
            FROM evaluation_cases c WHERE dataset_version_id=:v)
            SELECT count(*) FILTER(WHERE old.case_key IS NULL) AS added,
            count(*) FILTER(WHERE new.case_key IS NULL) AS removed,
            count(*) FILTER(WHERE old.content<>new.content) AS modified
            FROM old FULL JOIN new USING(case_key)""",
            base=draft["base_version_id"],
            v=draft["id"],
        )
    return {**dataset, "draft": draft, "versions": versions}


async def version_action(db: AsyncSession, d: UUID, data: VersionAction) -> dict[str, Any]:
    await active(db, d)
    if data.action == "publish":
        assert data.draft_id
        await writable(db, d, data.draft_id)
        if not await db.scalar(
            text("SELECT 1 FROM evaluation_cases WHERE dataset_version_id=:v LIMIT 1"),
            {"v": data.draft_id},
        ):
            raise Fault(422, "EMPTY_DATASET", "空草稿不能发布")
        value = await checksum(db, data.draft_id, validate=True)
        if value != data.reviewed_digest:
            raise Fault(409, "DIGEST_CHANGED", "草稿已变化，请重新核对")
        version = await row(
            db,
            """UPDATE dataset_versions SET state='PUBLISHED',digest=:h,
            published_at=now() WHERE id=:v RETURNING *""",
            h=value,
            v=data.draft_id,
        )
    else:
        if data.base_version_id:
            await row(
                db,
                """SELECT id FROM dataset_versions
                WHERE dataset_id=:d AND id=:base AND state='PUBLISHED'""",
                d=d,
                base=data.base_version_id,
            )
        version = await row(
            db,
            """INSERT INTO dataset_versions(dataset_id,version,base_version_id,state)
            SELECT :d,COALESCE(max(version),0)+1,:base,'DRAFT' FROM dataset_versions
            WHERE dataset_id=:d RETURNING *""",
            d=d,
            base=data.base_version_id,
        )
        if data.base_version_id:
            await db.execute(
                text(f"""INSERT INTO evaluation_cases(dataset_version_id,{COLS})
                SELECT :v,{COLS} FROM evaluation_cases WHERE dataset_version_id=:base"""),
                {"v": version["id"], "base": data.base_version_id},
            )
    await touch(db, d)
    return version


async def touch(db: AsyncSession, d: UUID) -> None:
    await db.execute(text("UPDATE datasets SET updated_at=now() WHERE id=:d"), {"d": d})


def params(case: Case, v: UUID) -> dict[str, Any]:
    values = case.model_dump()
    for key in ("input", "expected", "extensions"):
        values[key] = json.dumps(values[key], allow_nan=False)
    return {**values, "v": v}


BIND = ", ".join(
    f"CAST(:{k} AS jsonb)" if k in ("input", "expected", "extensions") else ":" + k for k in FIELDS
)
INSERT = f"INSERT INTO evaluation_cases(dataset_version_id,{COLS}) VALUES (:v,{BIND})"


async def add(db: AsyncSession, d: UUID, v: UUID, case: Case) -> dict[str, Any]:
    await writable(db, d, v)
    await touch(db, d)
    return await row(db, INSERT + " RETURNING *", **params(case, v))


async def edit(db: AsyncSession, d: UUID, c: UUID, patch: CasePatch) -> dict[str, Any]:
    await writable(db, d, patch.draft_id)
    before = await row(
        db,
        f"SELECT {COLS} FROM evaluation_cases WHERE id=:c AND dataset_version_id=:v",
        c=c,
        v=patch.draft_id,
    )
    changes = patch.model_dump(exclude_unset=True, exclude={"draft_id"})
    try:
        case = Case.model_validate({**before, **changes})
    except ValueError as exc:
        raise Fault(422, "INVALID_CASE", "样本字段无效") from exc
    await touch(db, d)
    return await row(
        db,
        f"""UPDATE evaluation_cases SET ({COLS})=({BIND})
        WHERE id=:c AND dataset_version_id=:v RETURNING *""",
        c=c,
        **params(case, patch.draft_id),
    )


async def remove(db: AsyncSession, d: UUID, v: UUID, c: UUID) -> None:
    await writable(db, d, v)
    await row(
        db,
        "DELETE FROM evaluation_cases WHERE dataset_version_id=:v AND id=:c RETURNING id",
        v=v,
        c=c,
    )
    await touch(db, d)


async def import_cases(
    db: AsyncSession, d: UUID, v: UUID, cases: list[Case], preview: bool, header_rows: int
) -> dict[str, Any]:
    await writable(db, d, v)
    keys = [c.case_key for c in cases]
    existing = set(
        (
            await db.scalars(
                text("""SELECT case_key FROM evaluation_cases
        WHERE dataset_version_id=:v AND case_key=ANY(CAST(:keys AS text[]))"""),
                {"v": v, "keys": keys},
            )
        ).all()
    )
    for i, key in enumerate(keys, 1 + header_rows):
        if key in existing:
            raise Fault(409, "DUPLICATE_CASE", f"第 {i} 行 · case_key · 草稿中已存在")
    if preview:
        return {"draft_id": v, "count": len(cases), "preview": [c.model_dump() for c in cases[:5]]}
    await db.execute(text(INSERT), [params(c, v) for c in cases])
    await touch(db, d)
    return {"draft_id": v, "inserted": len(cases)}


async def export(db: AsyncSession, d: UUID, v: UUID, fmt: Format, target: BinaryIO) -> None:
    await row(db, "SELECT id FROM dataset_versions WHERE dataset_id=:d AND id=:v", d=d, v=v)
    result = await db.stream(
        text(f"""SELECT {COLS} FROM evaluation_cases
        WHERE dataset_version_id=:v ORDER BY case_key COLLATE "C" """),
        {"v": v},
    )
    writer = ExportWriter(target, fmt)
    async for batch in result.mappings().partitions(128):

        def write_batch(items: list[dict[str, Any]]) -> None:
            for item in items:
                writer.append(item)

        await run_in_threadpool(write_batch, [dict(item) for item in batch])
    await run_in_threadpool(writer.finish)
