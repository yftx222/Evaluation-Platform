import asyncio
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from uuid import UUID, uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from starlette.exceptions import HTTPException
from starlette.responses import Response

from app.api import datasets, evaluations, evaluators, members, projects, run_progress
from app.application.access.identity import (
    DevelopmentAuthProvider,
    DevelopmentSecretResolver,
    IdentityProvider,
    SecretResolver,
)
from app.application.catalog.store import Fault
from app.contracts.adapters import AdapterError
from app.infrastructure.resources import Resources, Settings

logger = logging.getLogger("uvicorn.error")


def error(request: Request, status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        {
            "error": {"code": code, "message": message, "retryable": status == 503},
            "request_id": request.state.request_id,
        },
        status_code=status,
    )


def create_app(
    settings: Settings | None = None,
    *,
    identities: IdentityProvider | None = None,
    secrets: SecretResolver | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        config = settings or Settings()
        logging.getLogger("httpx").setLevel(logging.WARNING)
        if config.mode == "production" and (identities is None or secrets is None):
            raise RuntimeError("生产环境需要正式身份与密钥提供方")
        application.state.settings = config
        application.state.identities = identities or DevelopmentAuthProvider(config.identities)
        application.state.secrets = secrets or DevelopmentSecretResolver(config.secrets)
        resources = Resources(config)
        application.state.resources = resources
        try:
            yield
        finally:
            await resources.close()

    api = FastAPI(title="AI Evaluation Platform", version="0.1.0", lifespan=lifespan)

    api.include_router(projects.router)
    api.include_router(members.router)
    api.include_router(datasets.router)
    api.include_router(evaluators.router)
    api.include_router(evaluations.router)
    api.include_router(run_progress.router)

    @api.exception_handler(Fault)
    async def domain_fault(request: Request, exc: Fault) -> JSONResponse:
        return error(request, exc.status, exc.code, exc.message)

    @api.exception_handler(AdapterError)
    async def adapter_fault(request: Request, exc: AdapterError) -> JSONResponse:
        return error(request, 422, exc.code, "接入配置不可用")

    @api.exception_handler(TimeoutError)
    async def timeout(request: Request, exc: TimeoutError) -> JSONResponse:
        return error(request, 504, "TARGET_TIMEOUT", "探测超时")

    @api.exception_handler(IntegrityError)
    async def conflict(request: Request, exc: IntegrityError) -> JSONResponse:
        return error(request, 409, "CONFLICT", "记录重复或违反引用约束")

    @api.middleware("http")
    async def observe(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        try:
            request_id = str(UUID(request.headers.get("x-request-id", "")))
        except ValueError:
            request_id = str(uuid4())
        request.state.request_id = request_id
        try:
            response = await call_next(request)
        except Exception:
            response = error(request, 500, "INTERNAL_ERROR", "服务内部错误")
        response.headers["X-Request-ID"] = request_id
        # Only allow server-generated identifiers and status in access records.
        logger.info(
            json.dumps(
                {"event": "http_request", "request_id": request_id, "status": response.status_code}
            )
        )
        return response

    @api.exception_handler(HTTPException)
    async def handle_http(request: Request, exc: HTTPException) -> JSONResponse:
        return error(request, exc.status_code, f"HTTP_{exc.status_code}", "请求未完成")

    @api.exception_handler(RequestValidationError)
    async def handle_validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        if request.url.path.startswith("/api/v1/evaluators"):
            first = exc.errors()[0]
            field = ".".join(str(part) for part in first["loc"] if part != "body")
            description = {
                "missing": "此项必填",
                "dict_type": "需要 JSON 对象",
                "list_type": "需要数组",
                "string_type": "需要文本",
                "extra_forbidden": "包含未支持的字段",
                "string_pattern_mismatch": "格式错误，请检查标识或版本号",
            }.get(first["type"], "请检查此字段的类型、长度或取值")
            return error(request, 422, "INVALID_INPUT", f"{field or '请求体'}：{description}")
        return error(request, 422, "INVALID_INPUT", "请求字段不符合要求")

    @api.get("/health")
    async def health(request: Request) -> dict[str, object]:
        return {"data": {"status": "ok"}, "request_id": request.state.request_id}

    @api.get("/ready")
    async def ready(request: Request) -> JSONResponse:
        resources: Resources = request.app.state.resources

        async def postgres() -> None:
            async with resources.engine.connect() as conn:
                await conn.execute(text("SELECT 1"))

        checks = await asyncio.gather(
            asyncio.wait_for(postgres(), 4),
            asyncio.wait_for(resources.redis.ping(), 4),
            return_exceptions=True,
        )
        failed = [
            name
            for name, result in zip(("postgresql", "redis"), checks, strict=True)
            if isinstance(result, BaseException)
        ]
        if failed:
            return error(request, 503, "DEPENDENCY_UNAVAILABLE", ", ".join(failed))
        return JSONResponse(
            {"data": {"postgresql": "ok", "redis": "ok"}, "request_id": request.state.request_id}
        )

    return api


app = create_app()
