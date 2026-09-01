from __future__ import annotations

import logging
import random
import secrets
import threading
import time
from urllib.parse import quote
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse

from app import __version__, db
from app.admin import create_admin_router
from app.catalog import Catalog, NoImageAvailable
from app.config import Settings, get_settings
from app.ua import detect_client_type, describe_user_agent
from app.webdav import NoRemoteImageAvailable, WebDAVError, WebDAVManager

logger = logging.getLogger("random_image_api")
VALID_TYPES = {"desktop", "mobile"}


def configure_logging(settings: Settings) -> None:
    settings.ensure_directories()
    level = getattr(logging, settings.log_level, logging.INFO)
    log_file = settings.log_dir / "access.log"
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
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
    webdav = WebDAVManager(
        settings,
        transport=app.state.webdav_transport,
        rng=app.state.rng,
    )
    app.state.catalog = catalog
    app.state.webdav = webdav
    app.state.stop_scanner = threading.Event()

    if settings.scan_on_startup:
        try:
            catalog.scan()
        except Exception as exc:
            catalog.last_scan_error = str(exc)
            logger.exception("startup scan failed: %s", exc)
    if webdav.enabled:
        try:
            webdav.sync()
            webdav.maintain()
        except WebDAVError as exc:
            logger.warning("startup WebDAV sync failed: %s", type(exc).__name__)

    def scanner_loop() -> None:
        local_interval = max(5, settings.scan_interval_seconds)
        webdav_interval = max(5, settings.webdav_sync_interval_seconds)
        next_local_scan = time.monotonic() + local_interval
        next_webdav_sync = time.monotonic() + webdav_interval
        while True:
            now = time.monotonic()
            next_run = next_local_scan
            if webdav.enabled:
                next_run = min(next_run, next_webdav_sync)
            if app.state.stop_scanner.wait(max(0.1, next_run - now)):
                break
            now = time.monotonic()
            if now >= next_local_scan:
                try:
                    catalog.scan()
                except Exception as exc:
                    catalog.last_scan_error = str(exc)
                    logger.exception("periodic scan failed: %s", exc)
                next_local_scan = now + local_interval
            if webdav.enabled and now >= next_webdav_sync:
                try:
                    webdav.sync()
                    webdav.maintain()
                except WebDAVError as exc:
                    logger.warning("periodic WebDAV sync failed: %s", type(exc).__name__)
                next_webdav_sync = now + webdav_interval

    thread = threading.Thread(target=scanner_loop, name="image-scanner", daemon=True)
    thread.start()
    app.state.scanner_thread = thread
    try:
        yield
    finally:
        app.state.stop_scanner.set()
        thread.join(timeout=1)
        webdav.close()


def create_app(
    settings: Settings | None = None,
    *,
    webdav_transport: httpx.BaseTransport | None = None,
    rng: random.Random | object | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    application = FastAPI(
        title="Random Image API",
        version=__version__,
        lifespan=lifespan,
    )
    application.state.settings = settings
    application.state.webdav_transport = webdav_transport
    application.state.rng = rng or random
    application.include_router(create_admin_router(settings))

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
        webdav_health = request.app.state.webdav.health()
        return {
            "status": "ok" if db_ok else "degraded",
            "service": "random-image-api",
            "version": __version__,
            "images": catalog.counts(),
            "database": "ok" if db_ok else "error",
            "fallback_enabled": settings.fallback_enabled,
            "square_policy": settings.square_policy,
            "last_scan_at": catalog.last_scan_at,
            "last_scan_error": catalog.last_scan_error,
            "webdav": {k: v for k, v in webdav_health.items() if k != "cache"},
            "cache": webdav_health.get("cache", {"files": 0, "bytes": 0}),
        }

    @application.get("/random")
    @application.get("/random/{tag_slug}")
    def random_image(
        request: Request,
        tag_slug: str | None = None,
        type: str | None = Query(default=None, alias="type"),
        tag: str | None = Query(default=None, alias="tag"),
    ):
        catalog: Catalog = request.app.state.catalog
        if tag_slug is not None and tag is not None and tag_slug.strip().lower() != tag.strip().lower():
            raise HTTPException(400, "path tag and query tag must match")
        requested_tag = tag_slug if tag_slug is not None else tag
        if requested_tag is not None:
            try:
                requested_tag = db.validate_slug(requested_tag)
            except ValueError as exc:
                raise HTTPException(404, "tag not found or disabled") from exc
            with db.get_conn(settings.database_path) as conn:
                tag_row = conn.execute(
                    "SELECT display_name FROM tags WHERE slug=? COLLATE NOCASE AND enabled=1",
                    (requested_tag,),
                ).fetchone()
            if tag_row is None:
                raise HTTPException(404, "tag not found or disabled")
            tag_display_name = str(tag_row["display_name"])
        else:
            tag_display_name = None
        if type is not None:
            normalized = type.strip().lower()
            if normalized not in VALID_TYPES:
                raise HTTPException(400, "type must be desktop or mobile")
            preferred = normalized
        else:
            preferred = detect_client_type(request.headers.get("user-agent"))
        if not catalog.database_ok():
            raise HTTPException(503, "database unavailable")

        webdav: WebDAVManager = request.app.state.webdav
        remote_attempted = (
            webdav.enabled
            and request.app.state.rng.random() < settings.hybrid_remote_probability
        )
        source = "local"
        fallback_used = False
        remote_fallback_used = False

        def local_only(orientation: str):
            try:
                item, _ = catalog.pick(orientation, allow_fallback=False, tag=requested_tag)
                return item
            except NoImageAvailable:
                return None

        def union_pick(orientation: str):
            candidates: list[tuple[object, str]] = [
                (item, "webdav-cache")
                for item in webdav.cached_candidates(orientation, requested_tag)
            ]
            local = local_only(orientation)
            if local is not None:
                candidates.append((local, "local"))
            return request.app.state.rng.choice(candidates) if candidates else None

        if remote_attempted:
            try:
                record = webdav.fetch(preferred, requested_tag)
                source = record.source
            except WebDAVError as exc:
                remote_fallback_used = True
                selected = union_pick(preferred)
                if selected is None and settings.fallback_enabled:
                    other = "mobile" if preferred == "desktop" else "desktop"
                    selected = union_pick(other)
                    fallback_used = selected is not None
                if selected is None:
                    if (
                        isinstance(exc, NoRemoteImageAvailable)
                        and webdav.last_sync_error is None
                    ):
                        raise HTTPException(404, "no images available")
                    raise HTTPException(503, "remote source unavailable")
                record, source = selected
        else:
            try:
                record, fallback_used = catalog.pick(preferred, tag=requested_tag)
            except NoImageAvailable as local_error:
                if not webdav.enabled:
                    raise HTTPException(404, str(local_error)) from local_error
                try:
                    record = webdav.fetch(preferred, requested_tag)
                    source = record.source
                except WebDAVError as remote_error:
                    remote_fallback_used = True
                    selected = union_pick(preferred)
                    if selected is None and settings.fallback_enabled:
                        other = "mobile" if preferred == "desktop" else "desktop"
                        selected = union_pick(other)
                        fallback_used = selected is not None
                    if selected is None:
                        if (
                            isinstance(remote_error, NoRemoteImageAvailable)
                            and webdav.last_sync_error is None
                        ):
                            raise HTTPException(404, "no images available") from remote_error
                        raise HTTPException(503, "remote source unavailable") from remote_error
                    record, source = selected

        headers = {
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "X-Client-Type": preferred,
            "X-Image-File": record.rel_path,
            "X-Image-Orientation": record.orientation,
            "X-Image-Width": str(record.width),
            "X-Image-Height": str(record.height),
            "X-Fallback-Used": "true" if fallback_used else "false",
            "X-Image-Source": source,
            "X-Remote-Fallback-Used": "true" if remote_fallback_used else "false",
            "X-Image-Tag": requested_tag or "untagged",
        }
        if tag_display_name is not None:
            headers["X-Image-Tag-Name"] = quote(tag_display_name, safe="")
        return FileResponse(
            path=record.abs_path,
            media_type=record.content_type,
            filename=record.filename,
            content_disposition_type="inline",
            headers=headers,
        )

    def require_admin(request: Request) -> None:
        if not settings.admin_token:
            raise HTTPException(404, "not found")
        provided = request.headers.get("x-admin-token", "")
        if not secrets.compare_digest(provided, settings.admin_token):
            raise HTTPException(401, "invalid admin token")

    @application.post("/admin/rescan")
    def rescan(request: Request):
        require_admin(request)
        catalog: Catalog = request.app.state.catalog
        try:
            result = catalog.scan()
        except Exception as exc:
            raise HTTPException(500, str(exc)) from exc
        return {"status": "ok", **result, "images": catalog.counts()}

    @application.post("/admin/webdav/sync")
    def webdav_sync(request: Request):
        require_admin(request)
        webdav: WebDAVManager = request.app.state.webdav
        if not webdav.enabled:
            raise HTTPException(409, "WebDAV is disabled")
        try:
            return {"status": "ok", **webdav.sync()}
        except WebDAVError as exc:
            raise HTTPException(502, "WebDAV synchronization failed") from exc

    @application.post("/admin/cache/maintain")
    def cache_maintain(request: Request):
        require_admin(request)
        return {"status": "ok", **request.app.state.webdav.maintain()}

    return application


app = create_app()
