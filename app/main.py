from __future__ import annotations

import logging
import threading
import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse

from app import __version__
from app.catalog import Catalog, NoImageAvailable
from app.config import Settings, get_settings
from app.ua import detect_client_type, describe_user_agent

logger = logging.getLogger("random_image_api")

VALID_TYPES = {"desktop", "mobile"}


def configure_logging(settings: Settings) -> None:
    settings.ensure_directories()
    level = getattr(logging, settings.log_level, logging.INFO)
    log_file = settings.log_dir / "access.log"
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    root = logging.getLogger()
    root.setLevel(level)
    if not any(isinstance(h, logging.StreamHandler) for h in root.handlers):
        stream = logging.StreamHandler()
        stream.setFormatter(formatter)
        root.addHandler(stream)
    if not any(isinstance(h, logging.FileHandler) for h in root.handlers):
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)


def client_ip(request: Request, trusted_proxy_headers: bool) -> str:
    if trusted_proxy_headers:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
        real_ip = request.headers.get("x-real-ip")
        if real_ip:
            return real_ip.strip()
    return request.client.host if request.client else "unknown"


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings: Settings = app.state.settings
    configure_logging(settings)
    catalog = Catalog(settings)
    app.state.catalog = catalog
    app.state.stop_scanner = threading.Event()

    if settings.scan_on_startup:
        try:
            catalog.scan()
        except Exception as exc:
            catalog.last_scan_error = str(exc)
            logger.exception("startup scan failed: %s", exc)

    def scanner_loop() -> None:
        interval = max(5, settings.scan_interval_seconds)
        while not app.state.stop_scanner.wait(interval):
            try:
                catalog.scan()
            except Exception as exc:
                catalog.last_scan_error = str(exc)
                logger.exception("periodic scan failed: %s", exc)

    thread = threading.Thread(target=scanner_loop, name="image-scanner", daemon=True)
    thread.start()
    app.state.scanner_thread = thread
    yield
    app.state.stop_scanner.set()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    application = FastAPI(
        title="Random Image API",
        version=__version__,
        lifespan=lifespan,
    )
    application.state.settings = settings

    @application.middleware("http")
    async def access_log(request: Request, call_next):
        started = time.perf_counter()
        response = await call_next(request)
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        user_agent = request.headers.get("user-agent")
        logger.info(
            "method=%s path=%s status=%s client=%s ua=%s type=%s image=%s duration_ms=%s ip=%s",
            request.method,
            request.url.path,
            response.status_code,
            describe_user_agent(user_agent),
            (user_agent or "-")[:180],
            response.headers.get("x-client-type", "-"),
            response.headers.get("x-image-file", "-"),
            duration_ms,
            client_ip(request, settings.trusted_proxy_headers),
        )
        return response

    @application.exception_handler(RequestValidationError)
    async def validation_handler(_request: Request, exc: RequestValidationError):
        return JSONResponse(
            status_code=400,
            content={"detail": "invalid request", "errors": exc.errors()},
        )

    @application.get("/health")
    def health(request: Request) -> dict[str, Any]:
        catalog: Catalog = request.app.state.catalog
        db_ok = catalog.database_ok()
        counts = catalog.counts()
        status = "ok" if db_ok else "degraded"
        return {
            "status": status,
            "service": "random-image-api",
            "version": __version__,
            "images": counts,
            "database": "ok" if db_ok else "error",
            "fallback_enabled": settings.fallback_enabled,
            "square_policy": settings.square_policy,
            "last_scan_at": catalog.last_scan_at,
            "last_scan_error": catalog.last_scan_error,
        }

    @application.get("/random")
    def random_image(
        request: Request,
        type: str | None = Query(default=None, alias="type"),
    ):
        catalog: Catalog = request.app.state.catalog
        if type is not None:
            normalized = type.strip().lower()
            if normalized not in VALID_TYPES:
                raise HTTPException(
                    status_code=400,
                    detail="type must be desktop or mobile",
                )
            preferred = normalized
        else:
            preferred = detect_client_type(request.headers.get("user-agent"))

        if not catalog.database_ok():
            raise HTTPException(status_code=503, detail="database unavailable")

        try:
            record, fallback_used = catalog.pick(preferred)
        except NoImageAvailable as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        headers = {
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "X-Client-Type": preferred,
            "X-Image-File": record.rel_path,
            "X-Image-Orientation": record.orientation,
            "X-Image-Width": str(record.width),
            "X-Image-Height": str(record.height),
            "X-Fallback-Used": "true" if fallback_used else "false",
        }
        return FileResponse(
            path=record.abs_path,
            media_type=record.content_type,
            filename=record.filename,
            content_disposition_type="inline",
            headers=headers,
        )

    @application.post("/admin/rescan")
    def rescan(request: Request):
        if not settings.admin_token:
            raise HTTPException(status_code=404, detail="not found")
        provided = request.headers.get("x-admin-token", "")
        if provided != settings.admin_token:
            raise HTTPException(status_code=401, detail="invalid admin token")
        catalog: Catalog = request.app.state.catalog
        try:
            result = catalog.scan()
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return {"status": "ok", **result, "images": catalog.counts()}

    return application


app = create_app()
