import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.core.config import settings
from app.core.errors import AppError
from app.db.session import SessionLocal
from app.features import (
    auth,
    cash_statements,
    farm,
    finance,
    lele_biomass,
    lenders,
    loans,
    main_cash_account,
    people,
)
from app.features import settings as settings_feature

logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings.validate_runtime_secrets()
    yield


app = FastAPI(
    title=settings.app_name,
    version="1.0.0",
    summary="Transactional farm-management API",
    docs_url="/docs",
    redoc_url=None,
    openapi_url="/openapi.json",
    lifespan=lifespan,
)

if settings.cors_allowed_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_allowed_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "OPTIONS"],
        allow_headers=["Authorization", "X-PBB-API-Key", "Content-Type"],
    )


@app.exception_handler(AppError)
def app_error_handler(_: Request, exc: AppError) -> JSONResponse:
    body: dict[str, Any] = {"code": exc.code, "detail": exc.detail}
    if exc.errors:
        body["errors"] = exc.errors
    body.update(exc.extra)
    return JSONResponse(
        status_code=exc.status_code,
        content=body,
        headers={"WWW-Authenticate": "Bearer"} if exc.status_code == 401 else None,
    )


@app.exception_handler(RequestValidationError)
def request_validation_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
    errors: dict[str, list[str]] = {}
    for item in exc.errors():
        location = ".".join(str(part) for part in item["loc"] if part not in ("body", "query", "path")) or "request"
        errors.setdefault(location, []).append(item["msg"])
    return JSONResponse(
        status_code=422, content={"code": "VALIDATION_ERROR", "detail": "The request is invalid.", "errors": errors}
    )


@app.exception_handler(IntegrityError)
def integrity_error_handler(_: Request, exc: IntegrityError) -> JSONResponse:
    logging.getLogger(__name__).info("Database constraint rejected request", exc_info=exc)
    return JSONResponse(
        status_code=409, content={"code": "DATABASE_CONFLICT", "detail": "The operation conflicts with existing data."}
    )


@app.get("/health/live", tags=["System"])
def health_live() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/health/ready", tags=["System"])
def health_ready() -> dict[str, str]:
    with SessionLocal() as db:
        db.execute(text("select 1"))
    return {"status": "ready"}


for feature_router in (
    auth.router,
    people.router,
    farm.router,
    lele_biomass.router,
    settings_feature.router,
    finance.router,
    main_cash_account.router,
    cash_statements.router,
    lenders.router,
    loans.router,
):
    app.include_router(feature_router, prefix="/api/v1")


def custom_openapi() -> dict[str, Any]:
    if app.openapi_schema:
        return app.openapi_schema
    schema = get_openapi(title=app.title, version=app.version, summary=app.summary, routes=app.routes)
    security_schemes = schema.setdefault("components", {}).setdefault("securitySchemes", {})
    security_schemes["ApiKeyAuth"] = {"type": "apiKey", "in": "header", "name": "X-PBB-API-Key"}
    security_schemes["BearerAuth"] = {"type": "http", "scheme": "bearer", "bearerFormat": "JWT"}
    for path, operations in schema.get("paths", {}).items():
        for operation in operations.values():
            if not isinstance(operation, dict):
                continue
            if path == "/api/v1/auth/login":
                operation["security"] = [{"ApiKeyAuth": []}]
            elif path.startswith("/api/v1/"):
                operation["security"] = [{"ApiKeyAuth": [], "BearerAuth": []}]
            else:
                operation.pop("security", None)
    app.openapi_schema = schema
    return schema


app.openapi = custom_openapi
