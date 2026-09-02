"""Server-rendered administration UI for Random Image API V2.

Integration::

    app.include_router(create_admin_router(settings))

The router intentionally keeps sessions, login throttles and pending archive
previews in process memory.  Restarting the process logs administrators out and
removes stale previews on the next router construction/request.
"""
from __future__ import annotations

import hashlib
import hmac
import html
import io
import os
import re
import secrets
import tarfile
import time
import zipfile
from dataclasses import dataclass
from email.parser import BytesParser
from email.policy import default as email_policy
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import parse_qsl, quote, urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from PIL import Image, ImageOps, UnidentifiedImageError
from starlette.datastructures import FormData, UploadFile

from app import db, importer
from app.catalog import classify_orientation

_FORMAT_EXTENSIONS = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp"}
_CONTENT_TYPES = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}
_SLUG_PART = re.compile(r"[^a-z0-9]+")


@dataclass(slots=True)
class _Session:
    csrf: str
    expires_at: float


@dataclass(slots=True)
class _Preview:
    session_id: str
    archive_path: Path
    expires_at: float
    archive_size: int
    archive_sha256: str
    summary: dict[str, object]
    entries: list[tuple[str, str]]  # (sha256, directory-derived slug)


def _escape(value: object) -> str:
    return html.escape(str(value), quote=True)


def _page(title: str, body: str, csrf: str | None = None) -> HTMLResponse:
    token = "" if csrf is None else f'<meta name="csrf-token" content="{_escape(csrf)}">'
    style = """<style>
:root{color-scheme:light;--bg:#f4f7fb;--card:#fff;--text:#172033;--muted:#667085;--line:#dbe2ea;--primary:#2563eb;--danger:#b42318;--ok:#067647}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px/1.5 system-ui,-apple-system,sans-serif}header,main{width:min(1180px,calc(100% - 28px));margin:auto}header{padding:24px 0 12px}h1,h2,h3{line-height:1.2}nav{display:flex;flex-wrap:wrap;gap:8px;margin:12px 0 20px}nav a,.button,button{border:0;border-radius:8px;padding:9px 13px;background:var(--primary);color:#fff;text-decoration:none;cursor:pointer}button.danger{background:var(--danger)}button.secondary,.button.secondary{background:#475467}.panel,.card,.stat{background:var(--card);border:1px solid var(--line);border-radius:12px;box-shadow:0 1px 2px #1018280d}.panel{padding:18px;margin:16px 0}.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px}.stat{padding:16px}.stat strong{display:block;font-size:1.7rem}.grid{columns:4 230px;column-gap:14px}.card{display:inline-block;width:100%;overflow:hidden;margin:0 0 14px;break-inside:avoid}.card img,.placeholder{width:100%;height:190px;display:block;object-fit:cover;background:#e9eef5}.placeholder{display:grid;place-items:center;color:var(--muted)}.card-body{padding:13px}.actions{display:flex;flex-wrap:wrap;gap:8px;align-items:center}.actions form{margin:0}.badge{display:inline-block;border-radius:999px;padding:3px 8px;margin:2px;background:#eef2f6;color:#344054;font-size:.82rem}.badge.ok{background:#ecfdf3;color:var(--ok)}.badge.off{background:#fef3f2;color:var(--danger)}form.filters,.form-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;align-items:end}label{display:block;font-weight:600}input,select{width:100%;padding:9px;border:1px solid #cbd5e1;border-radius:7px;background:#fff}input[type=checkbox]{width:auto}.check{font-weight:400;display:inline-flex;gap:6px;align-items:center}.muted{color:var(--muted);overflow-wrap:anywhere}.message{padding:10px;border-radius:8px;background:#eff6ff}.pager{justify-content:center}@media(max-width:640px){header,main{width:min(100% - 18px,1180px)}.grid{columns:1}.card img,.placeholder{height:230px}nav a{flex:1;text-align:center}.actions button{width:100%}}
</style>"""
    return HTMLResponse(
        "<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
        f"<meta name=\"viewport\" content=\"width=device-width\">{token}"
        f"<title>{_escape(title)}</title>{style}</head><body><header><h1>{_escape(title)}</h1></header><main>{body}</main></body></html>"
    )


def _redirect(path: str, message: str | None = None) -> RedirectResponse:
    separator = "&" if "?" in path else "?"
    target = path if not message else f"{path}{separator}message={quote(message)}"
    return RedirectResponse(target, status_code=303)


def _client_ip(request: Request, trust_proxy: bool) -> str:
    if trust_proxy:
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",", 1)[0].strip()
        real = request.headers.get("x-real-ip", "")
        if real:
            return real.strip()
    return request.client.host if request.client else "unknown"


def _slugify(value: str) -> str:
    slug = _SLUG_PART.sub("-", value.strip().lower()).strip("-")[:63]
    if not slug:
        slug = "imported"
    try:
        return db.validate_slug(slug)
    except ValueError:
        digest = hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()[:12]
        return f"imported-{digest}"


def _limits(settings: Any) -> importer.ImportLimits:
    defaults = importer.ImportLimits()
    return importer.ImportLimits(
        max_archive_bytes=int(getattr(settings, "admin_max_archive_bytes", defaults.max_archive_bytes)),
        max_members=int(getattr(settings, "admin_max_archive_members", defaults.max_members)),
        max_member_bytes=int(getattr(settings, "admin_max_archive_member_bytes", defaults.max_member_bytes)),
        max_total_bytes=int(getattr(settings, "admin_max_archive_total_bytes", defaults.max_total_bytes)),
        max_compression_ratio=float(getattr(settings, "admin_max_archive_compression_ratio", defaults.max_compression_ratio)),
        max_image_pixels=int(getattr(settings, "admin_max_image_pixels", defaults.max_image_pixels)),
    )


def _archive_entries(path: Path, limits: importer.ImportLimits) -> list[tuple[str, str]]:
    """Return image hashes/tag hints after importer-owned archive validation."""
    archive_size = path.stat().st_size
    archive, kind = importer._open_archive(path)
    entries: list[tuple[str, str]] = []
    with archive:
        members = (
            importer._zip_members(archive, limits, archive_size)
            if kind == "zip"
            else importer._tar_members(archive, limits, archive_size)
        )
        total = 0
        for member in members:
            stream = archive.open(member.source, "r") if kind == "zip" else archive.extractfile(member.source)
            if stream is None:
                raise importer.ArchiveSecurityError(f"cannot read archive member: {member.name!r}")
            with stream:
                payload = importer._read_limited(stream, member, total, limits)
            total += len(payload)
            try:
                importer._inspect_image(payload, limits.max_image_pixels)
            except (UnidentifiedImageError, OSError, ValueError, SyntaxError):
                continue
            parent = PurePosixPath(member.name).parent
            hint = "" if str(parent) == "." else _slugify(parent.parts[-1])
            entries.append((hashlib.sha256(payload).hexdigest(), hint))
    return entries


def _selected_tag_slugs(form: Any) -> list[str]:
    """严格解析上传标签；空默认标签允许，重复值合并。"""
    values = [str(form.get("default_tag", "")), *(str(value) for value in form.getlist("tags"))]
    slugs: list[str] = []
    for value in values:
        if not value.strip():
            continue
        slug = db.validate_slug(value)
        if slug not in slugs:
            slugs.append(slug)
    return slugs


def _require_existing_tags(conn: Any, slugs: list[str]) -> dict[str, int]:
    result: dict[str, int] = {}
    for slug in slugs:
        row = conn.execute("SELECT id FROM tags WHERE slug=? COLLATE NOCASE", (slug,)).fetchone()
        if row is None:
            raise ValueError(f"tag does not exist: {slug}")
        result[slug] = int(row["id"])
    return result


def _safe_db_file(root_value: Path | str, stored_name: str) -> Path:
    """安全解析数据库持有的相对路径，并拒绝越界和符号链接。"""
    root = Path(root_value).resolve()
    relative = Path(stored_name)
    if relative.is_absolute() or ".." in relative.parts:
        raise HTTPException(400, "unsafe stored path")
    lexical = root / relative
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise HTTPException(404, "file unavailable")
    resolved = lexical.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise HTTPException(400, "unsafe stored path") from exc
    if not resolved.is_file():
        raise HTTPException(404, "file unavailable")
    return resolved


def _storage_group(rel_path: str) -> str:
    parts = PurePosixPath(rel_path).parts
    first = parts[0] if parts else "root"
    return first if first in {"desktop", "mobile", "square"} else "root"


def _like_pattern(value: str) -> str:
    return "%" + value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def create_admin_router(settings: Any) -> APIRouter:
    """Build a self-contained administration router.

    Optional settings are read with ``getattr`` so this module works with the
    current Settings model without changing app/config.py.
    """
    raw_path = str(getattr(settings, "admin_path", "/manage-images")).strip()
    admin_path = "/" + raw_path.strip("/") if raw_path.strip("/") else "/manage-images"
    cookie_name = str(getattr(settings, "admin_cookie_name", "ria_admin_session"))
    cookie_secure = bool(getattr(settings, "admin_cookie_secure", True))
    session_ttl = max(60, int(getattr(settings, "admin_session_ttl_seconds", 1800)))
    preview_ttl = max(1, int(getattr(settings, "admin_preview_ttl_seconds", 600)))
    login_window = max(1, int(getattr(settings, "admin_login_window_seconds", 60)))
    login_attempts = max(1, int(getattr(settings, "admin_login_max_attempts", 5)))
    upload_max = max(1, int(getattr(settings, "admin_max_upload_bytes", 25 * 1024 * 1024)))
    request_max = max(
        upload_max,
        int(getattr(settings, "admin_max_archive_bytes", importer.ImportLimits().max_archive_bytes)),
    ) + 1024 * 1024
    upload_tmp_dir = Path(getattr(settings, "upload_tmp_dir", Path(settings.data_dir) / "tmp" / "admin"))
    configured_secret = str(getattr(settings, "admin_session_secret", ""))
    signing_key = configured_secret.encode()
    sessions: dict[str, _Session] = {}
    previews: dict[str, _Preview] = {}
    failures: dict[str, list[float]] = {}
    router = APIRouter(prefix=admin_path, tags=["admin-v2"])

    def clean(now: float | None = None) -> None:
        current = time.time() if now is None else now
        for sid in [key for key, value in sessions.items() if value.expires_at <= current]:
            sessions.pop(sid, None)
            for token in [key for key, value in previews.items() if value.session_id == sid]:
                pending = previews.pop(token)
                pending.archive_path.unlink(missing_ok=True)
        for token in [key for key, value in previews.items() if value.expires_at <= current]:
            pending = previews.pop(token)
            pending.archive_path.unlink(missing_ok=True)

    def sign(sid: str, expires: int) -> str:
        payload = f"{sid}.{expires}"
        signature = hmac.new(signing_key, payload.encode(), hashlib.sha256).hexdigest()
        return f"{payload}.{signature}"

    def authenticate(request: Request) -> tuple[str, _Session]:
        clean()
        raw = request.cookies.get(cookie_name, "")
        try:
            sid, expiry_text, supplied = raw.split(".", 2)
            expiry = int(expiry_text)
        except (ValueError, TypeError):
            raise HTTPException(401, "authentication required")
        expected = hmac.new(signing_key, f"{sid}.{expiry}".encode(), hashlib.sha256).hexdigest()
        if expiry <= int(time.time()) or not hmac.compare_digest(supplied, expected):
            raise HTTPException(401, "authentication required")
        session = sessions.get(sid)
        if session is None or session.expires_at <= time.time():
            raise HTTPException(401, "authentication required")
        return sid, session

    async def form_data(request: Request) -> Any:
        content_type = request.headers.get("content-type", "")
        try:
            declared = int(request.headers.get("content-length", "0"))
        except ValueError:
            raise HTTPException(400, "invalid Content-Length")
        if declared > request_max:
            raise HTTPException(413, "request body too large")
        chunks: list[bytes] = []
        received = 0
        try:
            async for chunk in request.stream():
                received += len(chunk)
                if received > request_max:
                    raise HTTPException(413, "request body too large")
                chunks.append(chunk)
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(400, "cannot read request body") from exc
        body = b"".join(chunks)
        if not body and not content_type:
            return FormData()
        if content_type.lower().startswith("application/x-www-form-urlencoded"):
            try:
                return FormData(parse_qsl(body.decode("utf-8"), keep_blank_values=True))
            except (UnicodeDecodeError, ValueError) as exc:
                raise HTTPException(400, "invalid URL-encoded form") from exc
        if content_type.lower().startswith("multipart/form-data"):
            try:
                message = BytesParser(policy=email_policy).parsebytes(
                    b"Content-Type: " + content_type.encode("latin-1") + b"\r\nMIME-Version: 1.0\r\n\r\n" + body
                )
                if not message.is_multipart():
                    raise ValueError("multipart boundary missing")
                items: list[tuple[str, str | UploadFile]] = []
                for part in message.iter_parts():
                    if part.get_content_disposition() != "form-data":
                        continue
                    name = part.get_param("name", header="content-disposition")
                    if not name:
                        raise ValueError("form field name missing")
                    payload = part.get_payload(decode=True)
                    if payload is None:
                        payload = b""
                    filename = part.get_filename()
                    if filename is None:
                        charset = part.get_content_charset("utf-8")
                        items.append((name, payload.decode(charset)))
                    else:
                        items.append(
                            (
                                name,
                                UploadFile(
                                    file=io.BytesIO(payload),
                                    filename=filename,
                                    headers={"content-type": part.get_content_type()},
                                ),
                            )
                        )
                return FormData(items)
            except HTTPException:
                raise
            except (UnicodeDecodeError, LookupError, ValueError, TypeError) as exc:
                raise HTTPException(400, "invalid multipart form") from exc
        raise HTTPException(415, "unsupported form content type")

    async def write_auth(request: Request) -> tuple[str, _Session, Any]:
        sid, session = authenticate(request)
        form = await form_data(request)
        supplied = str(form.get("csrf", ""))
        if not hmac.compare_digest(supplied, session.csrf):
            raise HTTPException(403, "invalid CSRF token")
        return sid, session, form

    def scan(request: Request) -> None:
        catalog = getattr(request.app.state, "catalog", None)
        if catalog is None:
            return
        with db.get_conn(settings.database_path) as conn:
            disabled = {
                (str(row["content_hash"] or ""), str(row["rel_path"]))
                for row in conn.execute("SELECT content_hash,rel_path FROM images WHERE source='local' AND enabled=0")
            }
        catalog.scan()
        if disabled:
            with db.get_conn(settings.database_path) as conn:
                for digest, rel_path in disabled:
                    conn.execute(
                        "UPDATE images SET enabled=0 WHERE source='local' AND (rel_path=? OR (content_hash IS NOT NULL AND content_hash=?))",
                        (rel_path, digest),
                    )

    def hidden(csrf: str) -> str:
        return f'<input type="hidden" name="csrf" value="{_escape(csrf)}">'

    def tag_inputs(rows: list[Any]) -> str:
        options = '<option value="">无</option>' + "".join(
            f'<option value="{_escape(row["slug"])}">{_escape(row["slug"])} — {_escape(row["display_name"])}</option>'
            for row in rows
        )
        checks = "".join(
            f'<label><input type="checkbox" name="tags" value="{_escape(row["slug"])}"> {_escape(row["slug"])}</label> '
            for row in rows
        )
        return f'<label>默认标签 <select name="default_tag">{options}</select></label><fieldset><legend>附加标签</legend>{checks}</fieldset>'

    @router.get("/login", response_class=HTMLResponse)
    def login_page() -> HTMLResponse:
        if not str(getattr(settings, "admin_token", "")) or not configured_secret:
            raise HTTPException(404, "not found")
        return _page("管理员登录", '<form method="post"><label>Token <input type="password" name="token"></label><button>登录</button></form>')

    @router.post("/login")
    async def login(request: Request):
        expected_token = str(getattr(settings, "admin_token", ""))
        if not expected_token or not configured_secret:
            raise HTTPException(404, "not found")
        now = time.time()
        ip = _client_ip(request, bool(getattr(settings, "trusted_proxy_headers", True)))
        recent = [stamp for stamp in failures.get(ip, []) if stamp > now - login_window]
        failures[ip] = recent
        if len(recent) >= login_attempts:
            raise HTTPException(429, "too many login attempts", headers={"Retry-After": str(login_window)})
        form = await form_data(request)
        supplied = str(form.get("token", ""))
        if not hmac.compare_digest(supplied, expected_token):
            recent.append(now)
            raise HTTPException(401, "invalid token")
        failures.pop(ip, None)
        sid = secrets.token_urlsafe(32)
        expires = int(now + session_ttl)
        sessions[sid] = _Session(secrets.token_urlsafe(32), float(expires))
        response = _redirect(admin_path)
        forwarded_proto = request.headers.get("x-forwarded-proto", "").split(",", 1)[0].strip().lower()
        secure_cookie = request.url.scheme == "https" or (
            bool(getattr(settings, "trusted_proxy_headers", True)) and forwarded_proto == "https"
        )
        response.set_cookie(cookie_name, sign(sid, expires), max_age=session_ttl, httponly=True, secure=secure_cookie, samesite="strict", path=admin_path)
        return response

    @router.post("/logout")
    async def logout(request: Request):
        sid, _session, _form = await write_auth(request)
        sessions.pop(sid, None)
        for token in [key for key, value in previews.items() if value.session_id == sid]:
            previews.pop(token).archive_path.unlink(missing_ok=True)
        response = _redirect(f"{admin_path}/login")
        response.delete_cookie(cookie_name, path=admin_path)
        return response

    @router.get("", response_class=HTMLResponse)
    def overview(request: Request) -> HTMLResponse:
        _sid, session = authenticate(request)
        with db.get_conn(settings.database_path) as conn:
            local, local_enabled = conn.execute("SELECT COUNT(*),COALESCE(SUM(enabled),0) FROM images").fetchone()
            remote, remote_enabled = conn.execute("SELECT COUNT(*),COALESCE(SUM(enabled),0) FROM webdav_objects").fetchone()
            cached = int(conn.execute("SELECT COUNT(*) FROM webdav_cache").fetchone()[0])
            tags = int(conn.execute("SELECT COUNT(*) FROM tags").fetchone()[0])
            tag_rows = conn.execute("SELECT slug,display_name FROM tags ORDER BY slug").fetchall()
        message = _escape(request.query_params.get("message", ""))
        body = (
            f'<p class="message">{message or "管理本地图库、WebDAV 索引、标签和导入任务。"}</p>'
            f'<section class="stats"><article class="stat"><span>本地图片</span><strong>{int(local)}</strong><small>启用 {int(local_enabled)}</small></article>'
            f'<article class="stat"><span>WebDAV</span><strong>{int(remote)}</strong><small>启用 {int(remote_enabled)}</small></article>'
            f'<article class="stat"><span>WebDAV 缓存</span><strong>{cached}</strong><small>只展示已缓存预览</small></article>'
            f'<article class="stat"><span>主题标签</span><strong>{tags}</strong><small>支持一图多标签</small></article></section>'
            f'<nav><a href="{admin_path}/images?source=local">本地图片</a> '
            f'<a href="{admin_path}/images?source=webdav">WebDAV 图片</a> <a href="{admin_path}/tags">标签</a></nav>'
            f'<h2>上传图片</h2><form method="post" action="{admin_path}/upload" enctype="multipart/form-data">{hidden(session.csrf)}'
            f'{tag_inputs(tag_rows)}<input type="file" name="files" accept="image/jpeg,image/png,image/webp" multiple required><button>上传</button></form>'
            f'<h2>归档预览</h2><form method="post" action="{admin_path}/archives/preview" enctype="multipart/form-data">{hidden(session.csrf)}'
            f'<input type="file" name="archive" accept=".zip,.tar.gz,.tgz" required><button>预览归档</button></form>'
            f'<form method="post" action="{admin_path}/cache/clear">{hidden(session.csrf)}<button>清理全部 WebDAV 缓存</button></form>'
            f'<form method="post" action="{admin_path}/logout">{hidden(session.csrf)}<button>退出</button></form>'
        )
        return _page("管理总览", body, session.csrf)

    @router.get("/images/{image_id}/preview")
    def local_preview(image_id: int, request: Request):
        authenticate(request)
        with db.get_conn(settings.database_path) as conn:
            row = conn.execute("SELECT rel_path,content_type,source FROM images WHERE id=?", (image_id,)).fetchone()
        if row is None or row["source"] != "local":
            raise HTTPException(404, "local image not found")
        target = _safe_db_file(settings.images_dir, str(row["rel_path"]))
        return FileResponse(target, media_type=str(row["content_type"]), headers={"Cache-Control": "private, max-age=300", "X-Content-Type-Options": "nosniff"})

    @router.get("/webdav/preview")
    def webdav_preview(request: Request, href: str):
        authenticate(request)
        with db.get_conn(settings.database_path) as conn:
            row = conn.execute("SELECT c.cache_name,c.content_type FROM webdav_cache c JOIN webdav_objects o ON o.href=c.href WHERE c.href=?", (href,)).fetchone()
        if row is None:
            raise HTTPException(404, "cached preview not found")
        target = _safe_db_file(settings.cache_dir, str(row["cache_name"]))
        return FileResponse(target, media_type=str(row["content_type"]), headers={"Cache-Control": "private, max-age=300", "X-Content-Type-Options": "nosniff"})

    @router.get("/images", response_class=HTMLResponse)
    def images(request: Request, page: int = 1, per_page: int = 0, orientation: str = "", source: str = "local", cached: str = "", enabled: str = "", tag: str = "", storage: str = "", q: str = "") -> HTMLResponse:
        _sid, session = authenticate(request)
        source = source if source in {"local", "webdav"} else "local"
        page = max(1, page)
        per_page = int(getattr(settings, "admin_page_size", 20)) if per_page <= 0 else min(100, max(1, per_page))
        orientation = orientation if orientation in {"desktop", "mobile", "square"} else ""
        enabled = enabled if enabled in {"0", "1"} else ""
        cached = cached if cached in {"0", "1"} else ""
        storage = storage if storage in {"root", "desktop", "mobile", "square"} else ""
        q = q.strip()[:100]
        filters = {"source": source, "per_page": per_page, "orientation": orientation, "cached": cached, "enabled": enabled, "tag": tag, "storage": storage, "q": q}
        nav = f'<nav><a href="{admin_path}">总览</a><a href="{admin_path}/images?source=local">本地图片</a><a href="{admin_path}/images?source=webdav">WebDAV</a><a href="{admin_path}/tags">标签</a></nav>'
        with db.get_conn(settings.database_path) as conn:
            all_tags = conn.execute("SELECT id,slug,display_name,enabled FROM tags ORDER BY slug").fetchall()
            if tag and conn.execute("SELECT 1 FROM tags WHERE slug=? COLLATE NOCASE", (tag,)).fetchone() is None:
                tag = ""; filters["tag"] = ""
            clauses: list[str] = []
            args: list[object] = []
            if source == "webdav":
                clauses.append("1=1")
                if orientation in {"desktop", "mobile", "square"}: clauses.append("o.orientation=?"); args.append(orientation)
                if enabled: clauses.append("o.enabled=?"); args.append(int(enabled))
                if cached: clauses.append("c.href IS NOT NULL" if cached == "1" else "c.href IS NULL")
                if tag: clauses.append("EXISTS(SELECT 1 FROM webdav_object_tags x JOIN tags t ON t.id=x.tag_id WHERE x.href=o.href AND t.slug=? COLLATE NOCASE)"); args.append(tag)
                if q: clauses.append("o.href LIKE ? ESCAPE '\\'"); args.append(_like_pattern(q))
                where = " WHERE " + " AND ".join(clauses)
                total = int(conn.execute("SELECT COUNT(*) FROM webdav_objects o LEFT JOIN webdav_cache c ON c.href=o.href" + where, args).fetchone()[0])
                rows = conn.execute("SELECT o.href,o.orientation,o.enabled,c.href IS NOT NULL cached FROM webdav_objects o LEFT JOIN webdav_cache c ON c.href=o.href" + where + " ORDER BY o.href LIMIT ? OFFSET ?", (*args, per_page, (page-1)*per_page)).fetchall()
                cards = []
                for row in rows:
                    href_raw = str(row["href"]); href = _escape(href_raw)
                    related = conn.execute("SELECT t.id,t.slug,t.enabled FROM webdav_object_tags x JOIN tags t ON t.id=x.tag_id WHERE x.href=? ORDER BY t.slug", (href_raw,)).fetchall()
                    badges = "".join(f'<span class="badge{("" if x["enabled"] else " off")}">{_escape(x["slug"])}</span>' for x in related) or '<span class="muted">无标签</span>'
                    choices = "".join(f'<option value="{int(x["id"])}">{_escape(x["slug"])}</option>' for x in all_tags if x["enabled"])
                    media = f'<img loading="lazy" src="{admin_path}/webdav/preview?{_escape(urlencode({"href": href_raw}))}" alt="WebDAV 缓存预览">' if row["cached"] else '<div class="placeholder">未缓存，不自动下载</div>'
                    forms = f'<form method="post" action="{admin_path}/webdav/enabled">{hidden(session.csrf)}<input type="hidden" name="href" value="{href}"><input type="hidden" name="enabled" value="{1-int(row["enabled"])}"><button class="secondary">{"禁用" if row["enabled"] else "启用"}</button></form>'
                    forms += f'<form method="post" action="{admin_path}/webdav/tags">{hidden(session.csrf)}<input type="hidden" name="href" value="{href}"><select name="tag_id" required><option value="">选择标签</option>{choices}</select><select name="action"><option value="add">添加</option><option value="remove">移除</option></select><button>更新标签</button></form>'
                    cards.append(f'<article class="card">{media}<div class="card-body"><div>{badges}</div><p class="muted">{href}</p><span class="badge">真实方向：{_escape(row["orientation"])}</span><span class="badge {"ok" if row["enabled"] else "off"}">{"启用" if row["enabled"] else "禁用"}</span><div class="actions">{forms}</div></div></article>')
                heading = "WebDAV 图片"; listing = "".join(cards)
            else:
                clauses.append("i.source='local'")
                if orientation: clauses.append("i.orientation=?"); args.append(orientation)
                if enabled: clauses.append("i.enabled=?"); args.append(int(enabled))
                if tag: clauses.append("EXISTS(SELECT 1 FROM image_tags x JOIN tags t ON t.id=x.tag_id WHERE x.image_id=i.id AND t.slug=? COLLATE NOCASE)"); args.append(tag)
                if storage == "root": clauses.append("instr(i.rel_path,'/')=0")
                elif storage: clauses.append("i.rel_path LIKE ? ESCAPE '\\'"); args.append(storage + "/%")
                if q: clauses.append("i.rel_path LIKE ? ESCAPE '\\'"); args.append(_like_pattern(q))
                where = " WHERE " + " AND ".join(clauses)
                total = int(conn.execute("SELECT COUNT(*) FROM images i" + where, args).fetchone()[0])
                rows = conn.execute("SELECT i.id,i.rel_path,i.orientation,i.enabled FROM images i" + where + " ORDER BY i.id DESC LIMIT ? OFFSET ?", (*args, per_page, (page-1)*per_page)).fetchall()
                cards = []
                choices = "".join(f'<option value="{int(x["id"])}">{_escape(x["slug"])}</option>' for x in all_tags if x["enabled"])
                for row in rows:
                    iid=int(row["id"]); rel=_escape(row["rel_path"]); group=_storage_group(str(row["rel_path"]))
                    related=conn.execute("SELECT t.slug,t.enabled FROM image_tags x JOIN tags t ON t.id=x.tag_id WHERE x.image_id=? ORDER BY t.slug",(iid,)).fetchall()
                    badges="".join(f'<span class="badge{("" if x["enabled"] else " off")}">{_escape(x["slug"])}</span>' for x in related) or '<span class="muted">无标签</span>'
                    toggle=f'<form method="post" action="{admin_path}/images/{iid}/enabled">{hidden(session.csrf)}<input type="hidden" name="enabled" value="{1-int(row["enabled"])}"><button class="secondary">{"禁用" if row["enabled"] else "启用"}</button></form>'
                    delete=f'<form method="post" action="{admin_path}/images/{iid}/delete" onsubmit="return confirm(\'确定删除本地原图？此操作不可撤销。\')">{hidden(session.csrf)}<input type="hidden" name="confirm" value="1"><button class="danger">删除本地原图</button></form>'
                    move=f'<form method="post" action="{admin_path}/images/{iid}/move">{hidden(session.csrf)}<select name="target" required><option value="desktop">desktop</option><option value="mobile">mobile</option><option value="square">square</option></select><button>移动归档</button></form>'
                    tags_form=f'<form method="post" action="{admin_path}/images/{iid}/tags">{hidden(session.csrf)}<select name="tag_id" required><option value="">选择标签</option>{choices}</select><select name="action"><option value="add">添加</option><option value="remove">移除</option></select><button>更新标签</button></form>'
                    cards.append(f'<article class="card"><img loading="lazy" src="{admin_path}/images/{iid}/preview" alt="本地图片 #{iid}"><div class="card-body"><label class="check"><input type="checkbox" form="batch-tags" name="image_ids" value="{iid}">选择 #{iid}</label><p class="muted">{rel}</p><span class="badge">真实方向：{_escape(row["orientation"])}</span><span class="badge">存放目录：{group}</span><span class="badge {"ok" if row["enabled"] else "off"}">{"启用" if row["enabled"] else "禁用"}</span><div>{badges}</div><div class="actions">{toggle}{move}{tags_form}{delete}</div></div></article>')
                heading="本地图片"; listing="".join(cards)
        def opts(values: list[tuple[str,str]], current: str) -> str:
            return '<option value="">全部</option>'+''.join(f'<option value="{_escape(v)}"{" selected" if v==current else ""}>{_escape(label)}</option>' for v,label in values)
        tag_opts='<option value="">全部标签</option>'+''.join(f'<option value="{_escape(x["slug"])}"{" selected" if x["slug"]==tag else ""}>{_escape(x["slug"])}</option>' for x in all_tags)
        controls=f'<form class="filters panel" method="get"><label>来源<select name="source">{opts([("local","本地"),("webdav","WebDAV")],source)}</select></label><label>真实方向<select name="orientation">{opts([("desktop","desktop"),("mobile","mobile"),("square","square")],orientation)}</select></label><label>存放目录<select name="storage">{opts([("root","根目录"),("desktop","desktop"),("mobile","mobile"),("square","square")],storage)}</select></label><label>启用状态<select name="enabled">{opts([("1","启用"),("0","禁用")],enabled)}</select></label><label>缓存状态<select name="cached">{opts([("1","已缓存"),("0","未缓存")],cached)}</select></label><label>标签<select name="tag">{tag_opts}</select></label><label>文件名/HREF<input name="q" value="{_escape(q)}"></label><label>每页<select name="per_page">{opts([("12","12"),("20","20"),("40","40"),("80","80")],str(per_page))}</select></label><button>筛选</button></form>'
        pager=[]
        if page>1: pager.append(f'<a rel="prev" href="{admin_path}/images?{_escape(urlencode({**filters,"page":page-1}))}">上一页</a>')
        if page*per_page<total: pager.append(f'<a rel="next" href="{admin_path}/images?{_escape(urlencode({**filters,"page":page+1}))}">下一页</a>')
        batch=""
        if source=="local":
            candidates=''.join(f'<option value="{int(x["id"])}">{_escape(x["slug"])}</option>' for x in all_tags if x["enabled"])
            batch=f'<form class="panel form-grid" id="batch-tags" method="post" action="{admin_path}/images/tags">{hidden(session.csrf)}<label>批量标签<select name="tag_id" required>{candidates}</select></label><label>操作<select name="action"><option value="add">添加</option><option value="remove">移除</option></select></label><button>应用到选中图片</button></form>'
        return _page("图片",f'{nav}{controls}<h2>{heading}</h2><p>总计 {total}</p><section class="grid">{listing}</section>{batch}<nav class="pager">{" ".join(pager)}</nav>',session.csrf)

    @router.post("/images/tags")
    async def batch_tags(request: Request):
        _sid,_session,form=await write_auth(request)
        ids={int(v) for v in form.getlist("image_ids") if str(v).isdigit()}
        if not ids: raise HTTPException(400,"no images selected")
        action=str(form.get("action",""))
        if action not in {"add","remove"}: raise HTTPException(400,"invalid action")
        with db.get_conn(settings.database_path) as conn:
            raw=str(form.get("tag_id",form.get("tag","")))
            row=conn.execute("SELECT id FROM tags WHERE id=? AND enabled=1",(int(raw),)).fetchone() if raw.isdigit() else conn.execute("SELECT id FROM tags WHERE slug=? COLLATE NOCASE AND enabled=1",(db.validate_slug(raw),)).fetchone()
            if row is None: raise HTTPException(404,"enabled tag not found")
            tag_id=int(row["id"])
            valid={int(x[0]) for x in conn.execute(f"SELECT id FROM images WHERE source='local' AND id IN ({','.join('?' for _ in ids)})",tuple(ids))}
            if valid != ids: raise HTTPException(404,"local image not found")
            for iid in ids:
                if action=="add": conn.execute("INSERT OR IGNORE INTO image_tags(image_id,tag_id,created_at) VALUES(?,?,?)",(iid,tag_id,db.utc_now()))
                else: conn.execute("DELETE FROM image_tags WHERE image_id=? AND tag_id=?",(iid,tag_id))
        scan(request)
        return _redirect(f"{admin_path}/images?source=local", "标签已更新")

    @router.post("/images/{image_id}/tags")
    async def image_tags(image_id: int, request: Request):
        _sid, _session, form = await write_auth(request)
        action = str(form.get("action", ""))
        if action not in {"add", "remove"}:
            raise HTTPException(400, "invalid action")
        raw_tag_id = str(form.get("tag_id", ""))
        if not raw_tag_id.isdigit():
            raise HTTPException(400, "invalid tag id")
        tag_id = int(raw_tag_id)
        with db.get_conn(settings.database_path) as conn:
            if conn.execute(
                "SELECT 1 FROM images WHERE id=? AND source='local'", (image_id,)
            ).fetchone() is None:
                raise HTTPException(404, "local image not found")
            if conn.execute(
                "SELECT 1 FROM tags WHERE id=? AND enabled=1", (tag_id,)
            ).fetchone() is None:
                raise HTTPException(404, "enabled tag not found")
            if action == "add":
                conn.execute(
                    "INSERT OR IGNORE INTO image_tags(image_id,tag_id,created_at) VALUES(?,?,?)",
                    (image_id, tag_id, db.utc_now()),
                )
            else:
                conn.execute(
                    "DELETE FROM image_tags WHERE image_id=? AND tag_id=?",
                    (image_id, tag_id),
                )
        scan(request)
        return _redirect(f"{admin_path}/images?source=local", "图片标签已更新")

    @router.get("/tags", response_class=HTMLResponse)
    def tags(request: Request) -> HTMLResponse:
        _sid, session = authenticate(request)
        with db.get_conn(settings.database_path) as conn:
            rows = conn.execute("SELECT * FROM tags ORDER BY slug").fetchall()
        items = []
        for row in rows:
            tag_id = int(row["id"])
            slug = _escape(row["slug"])
            name = _escape(row["display_name"])
            edit = f'<form method="post" action="{admin_path}/tags/{tag_id}/edit">{hidden(session.csrf)}<input name="slug" value="{slug}" required><input name="display_name" value="{name}" required><button>编辑</button></form>'
            toggle = f'<form method="post" action="{admin_path}/tags/{tag_id}/disable">{hidden(session.csrf)}<input type="hidden" name="enabled" value="{1-int(row["enabled"])}"><button>{"禁用" if row["enabled"] else "启用"}</button></form>'
            merge = f'<form method="post" action="{admin_path}/tags/{tag_id}/merge">{hidden(session.csrf)}<select name="target_id">' + "".join(f'<option value="{int(target["id"])}">{_escape(target["slug"])}</option>' for target in rows if int(target["id"]) != tag_id) + '</select><button>合并</button></form>'
            items.append(f'<li>{slug} — {name} enabled={int(row["enabled"])}{edit}{toggle}{merge}</li>')
        listing = "".join(items)
        body = f'<ul>{listing}</ul><form method="post">{hidden(session.csrf)}<input name="slug" required><input name="display_name" required><button>创建</button></form>'
        return _page("标签", body, session.csrf)

    @router.post("/tags")
    async def create_tag(request: Request):
        _sid, _session, form = await write_auth(request)
        with db.get_conn(settings.database_path) as conn:
            db.ensure_tag(conn, str(form.get("slug", "")), str(form.get("display_name", "")))
        return _redirect(f"{admin_path}/tags", "标签已创建")

    @router.post("/tags/{tag_id}/edit")
    async def edit_tag(tag_id: int, request: Request):
        _sid, _session, form = await write_auth(request)
        slug = db.validate_slug(str(form.get("slug", "")))
        name = str(form.get("display_name", "")).strip()
        if not name or len(name) > 100:
            raise HTTPException(400, "invalid display name")
        with db.get_conn(settings.database_path) as conn:
            cursor = conn.execute("UPDATE tags SET slug=?,display_name=?,updated_at=? WHERE id=?", (slug, name, db.utc_now(), tag_id))
            if not cursor.rowcount:
                raise HTTPException(404, "tag not found")
        scan(request)
        return _redirect(f"{admin_path}/tags", "标签已编辑")

    @router.post("/tags/{tag_id}/disable")
    async def disable_tag(tag_id: int, request: Request):
        _sid, _session, form = await write_auth(request)
        enabled = 1 if str(form.get("enabled", "0")) == "1" else 0
        with db.get_conn(settings.database_path) as conn:
            if not conn.execute("UPDATE tags SET enabled=?,updated_at=? WHERE id=?", (enabled, db.utc_now(), tag_id)).rowcount:
                raise HTTPException(404, "tag not found")
        scan(request)
        return _redirect(f"{admin_path}/tags", "标签状态已更新")

    @router.post("/tags/{tag_id}/merge")
    async def merge_tag(tag_id: int, request: Request):
        _sid, _session, form = await write_auth(request)
        target_id = int(str(form.get("target_id", "0")))
        if target_id <= 0 or target_id == tag_id:
            raise HTTPException(400, "invalid target tag")
        with db.get_conn(settings.database_path) as conn:
            if conn.execute("SELECT 1 FROM tags WHERE id=?", (target_id,)).fetchone() is None:
                raise HTTPException(404, "target tag not found")
            conn.execute("INSERT OR IGNORE INTO image_tags(image_id,tag_id,created_at) SELECT image_id,?,created_at FROM image_tags WHERE tag_id=?", (target_id, tag_id))
            conn.execute("INSERT OR IGNORE INTO webdav_object_tags(href,tag_id,origin,created_at) SELECT href,?,origin,created_at FROM webdav_object_tags WHERE tag_id=?", (target_id, tag_id))
            if not conn.execute("DELETE FROM tags WHERE id=?", (tag_id,)).rowcount:
                raise HTTPException(404, "source tag not found")
        scan(request)
        return _redirect(f"{admin_path}/tags", "标签已合并")

    @router.post("/upload")
    async def upload(request: Request):
        _sid, _session, form = await write_auth(request)
        files = [item for item in form.getlist("files") if hasattr(item, "read")]
        if not files:
            one = form.get("file")
            files = [one] if hasattr(one, "read") else []
        if not files:
            raise HTTPException(400, "no files uploaded")
        try:
            selected_slugs = _selected_tag_slugs(form)
            with db.get_conn(settings.database_path) as conn:
                selected_tags = _require_existing_tags(conn, selected_slugs)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        limits = _limits(settings)
        written: list[Path] = []
        digests: list[str] = []
        duplicates = 0
        try:
            for uploaded in files:
                payload = await uploaded.read(upload_max + 1)
                if len(payload) > upload_max:
                    raise HTTPException(413, "upload too large")
                try:
                    detected, width, height = importer._inspect_image(payload, limits.max_image_pixels)
                except importer.ArchiveSecurityError as exc:
                    raise HTTPException(400, str(exc)) from exc
                except (UnidentifiedImageError, OSError, ValueError, SyntaxError) as exc:
                    raise HTTPException(400, "invalid or unsupported image") from exc
                # Persist the visual orientation.  Catalog.read_image_meta reads
                # pixel dimensions directly, so retaining only the EXIF flag
                # would make a later scan reverse the admin upload classification.
                try:
                    with Image.open(io.BytesIO(payload)) as source:
                        visual = ImageOps.exif_transpose(source)
                        visual.load()
                        normalized = io.BytesIO()
                        save_options = {"quality": 95} if detected == "JPEG" else {}
                        visual.save(normalized, format=detected, **save_options)
                    payload = normalized.getvalue()
                    width, height = visual.size
                except (UnidentifiedImageError, OSError, ValueError, SyntaxError) as exc:
                    raise HTTPException(400, "cannot normalize image") from exc
                digest = hashlib.sha256(payload).hexdigest()
                if digest not in digests:
                    digests.append(digest)
                with db.get_conn(settings.database_path) as conn:
                    if conn.execute("SELECT 1 FROM images WHERE content_hash=?", (digest,)).fetchone():
                        duplicates += 1; continue
                orientation = classify_orientation(width, height)
                group = orientation
                target = Path(settings.images_dir) / group / f"{digest}{_FORMAT_EXTENSIONS[detected]}"
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists():
                    duplicates += 1; continue
                temporary = target.with_name(f".{target.name}.{secrets.token_hex(8)}.tmp")
                with temporary.open("xb") as handle:
                    handle.write(payload); handle.flush(); os.fsync(handle.fileno())
                os.replace(temporary, target)
                written.append(target)
            scan(request)
            if selected_tags and digests:
                with db.get_conn(settings.database_path) as conn:
                    for digest in digests:
                        row = conn.execute("SELECT id FROM images WHERE content_hash=?", (digest,)).fetchone()
                        if row is None:
                            continue
                        for tag_id in selected_tags.values():
                            conn.execute(
                                "INSERT OR IGNORE INTO image_tags(image_id,tag_id,created_at) VALUES(?,?,?)",
                                (int(row["id"]), tag_id, db.utc_now()),
                            )
                scan(request)
        except Exception:
            for path in written:
                path.unlink(missing_ok=True)
            scan(request)
            raise
        return _redirect(admin_path, f"上传 {len(written)}，重复 {duplicates}")

    @router.post("/images/{image_id}/move")
    async def move_image(image_id: int, request: Request):
        _sid, _session, form = await write_auth(request)
        target_group = str(form.get("target", ""))
        if target_group not in {"desktop", "mobile", "square"}:
            raise HTTPException(400, "invalid target directory")
        images_root = Path(settings.images_dir).resolve()
        target_dir = images_root / target_group
        target_dir.mkdir(parents=True, exist_ok=True)
        if target_dir.is_symlink() or target_dir.resolve().parent != images_root:
            raise HTTPException(400, "unsafe target directory")
        with db.get_conn(settings.database_path) as conn:
            row = conn.execute(
                "SELECT rel_path,source FROM images WHERE id=?", (image_id,)
            ).fetchone()
            if row is None or row["source"] != "local":
                raise HTTPException(404, "local image not found")
            source = _safe_db_file(images_root, str(row["rel_path"]))
            if source.parent == target_dir:
                return _redirect(f"{admin_path}/images?source=local", "图片已在目标目录")
            candidate = target_dir / source.name
            counter = 1
            while candidate.exists():
                candidate = target_dir / f"{source.stem}-{counter}{source.suffix}"
                counter += 1
            os.replace(source, candidate)
            new_rel_path = candidate.relative_to(images_root).as_posix()
            try:
                conn.execute(
                    "UPDATE images SET rel_path=?,mtime_ns=?,updated_at=? WHERE id=? AND source='local'",
                    (new_rel_path, candidate.stat().st_mtime_ns, db.utc_now(), image_id),
                )
            except Exception:
                os.replace(candidate, source)
                raise
        scan(request)
        return _redirect(f"{admin_path}/images?source=local", "图片已移动")

    @router.post("/images/{image_id}/delete")
    async def delete_image(image_id: int, request: Request):
        _sid, _session, form = await write_auth(request)
        confirmation = str(form.get("confirm", ""))
        if confirmation not in {"1", "DELETE"}:
            raise HTTPException(400, "explicit confirmation required")
        quarantined: Path | None = None
        target: Path | None = None
        try:
            with db.get_conn(settings.database_path) as conn:
                row = conn.execute(
                    "SELECT rel_path,source FROM images WHERE id=?", (image_id,)
                ).fetchone()
                if row is None:
                    raise HTTPException(404, "image not found")
                if row["source"] != "local":
                    raise HTTPException(400, "only local images can be deleted")
                target = _safe_db_file(settings.images_dir, str(row["rel_path"]))
                quarantined = target.with_name(f".{target.name}.{secrets.token_hex(8)}.deleting")
                os.replace(target, quarantined)
                conn.execute("DELETE FROM images WHERE id=?", (image_id,))
        except Exception:
            if quarantined is not None and target is not None and quarantined.exists():
                os.replace(quarantined, target)
            raise
        if quarantined is not None:
            quarantined.unlink(missing_ok=True)
        scan(request)
        return _redirect(f"{admin_path}/images?source=local", "图片已删除")


    @router.post("/images/{image_id}/enabled")
    async def local_enabled(image_id: int, request: Request):
        _sid, _session, form = await write_auth(request)
        enabled = 1 if str(form.get("enabled", "0")) == "1" else 0
        with db.get_conn(settings.database_path) as conn:
            cursor = conn.execute("UPDATE images SET enabled=? WHERE id=? AND source='local'", (enabled, image_id))
            if not cursor.rowcount:
                raise HTTPException(404, "local image not found")
        scan(request)
        return _redirect(f"{admin_path}/images?source=local", "本地图片状态已更新")

    @router.post("/webdav/enabled")
    async def webdav_enabled(request: Request):
        _sid, _session, form = await write_auth(request)
        href = str(form.get("href", ""))
        enabled = 1 if str(form.get("enabled", "0")) == "1" else 0
        with db.get_conn(settings.database_path) as conn:
            if not conn.execute("UPDATE webdav_objects SET enabled=? WHERE href=?", (enabled, href)).rowcount:
                raise HTTPException(404, "WebDAV object not found")
        return _redirect(f"{admin_path}/images?source=webdav", "WebDAV 状态已更新")

    async def update_webdav_tag(request: Request, force_remove: bool = False):
        _sid, _session, form = await write_auth(request)
        href = str(form.get("href", ""))
        action = "remove" if force_remove else str(form.get("action", ""))
        if action not in {"add", "remove"}:
            raise HTTPException(400, "invalid action")
        raw_tag_id = str(form.get("tag_id", ""))
        if not raw_tag_id.isdigit():
            raise HTTPException(400, "invalid tag id")
        tag_id = int(raw_tag_id)
        with db.get_conn(settings.database_path) as conn:
            if conn.execute(
                "SELECT 1 FROM webdav_objects WHERE href=?", (href,)
            ).fetchone() is None:
                raise HTTPException(404, "WebDAV object not found")
            if conn.execute(
                "SELECT 1 FROM tags WHERE id=? AND enabled=1", (tag_id,)
            ).fetchone() is None:
                raise HTTPException(404, "enabled tag not found")
            if action == "add":
                conn.execute(
                    "INSERT OR IGNORE INTO webdav_object_tags(href,tag_id,origin,created_at) VALUES(?,?,'admin',?)",
                    (href, tag_id, db.utc_now()),
                )
            else:
                conn.execute(
                    "DELETE FROM webdav_object_tags WHERE href=? AND tag_id=?",
                    (href, tag_id),
                )
        return _redirect(f"{admin_path}/images?source=webdav", "WebDAV 标签已更新")

    @router.post("/webdav/tags")
    async def webdav_tags(request: Request):
        return await update_webdav_tag(request)

    @router.post("/webdav/tags/remove")
    async def webdav_remove_tag(request: Request):
        return await update_webdav_tag(request, force_remove=True)

    @router.post("/cache/clear")
    async def clear_cache(request: Request):
        _sid, _session, _form = await write_auth(request)
        cache_root = Path(settings.cache_dir).resolve()
        removed = 0
        with db.get_conn(settings.database_path) as conn:
            rows = conn.execute("SELECT cache_name FROM webdav_cache").fetchall()
            for row in rows:
                candidate = (cache_root / row["cache_name"]).resolve()
                try:
                    candidate.relative_to(cache_root)
                except ValueError:
                    continue
                if candidate.is_file():
                    candidate.unlink(); removed += 1
            conn.execute("DELETE FROM webdav_cache")
        return _redirect(admin_path, f"缓存已清理 {removed}")

    @router.post("/archives/preview")
    async def archive_preview(request: Request):
        sid, _session, form = await write_auth(request)
        uploaded = form.get("archive")
        if not hasattr(uploaded, "read"):
            raise HTTPException(400, "archive required")
        filename = str(getattr(uploaded, "filename", "")).lower()
        suffix = ".tar.gz" if filename.endswith(".tar.gz") else ".tgz" if filename.endswith(".tgz") else ".zip" if filename.endswith(".zip") else ""
        if not suffix:
            raise HTTPException(400, "supported archive types: ZIP, TAR.GZ, TGZ")
        limits = _limits(settings)
        upload_tmp_dir.mkdir(parents=True, exist_ok=True)
        token = secrets.token_urlsafe(32)
        path = upload_tmp_dir / f"{token}{suffix}"
        try:
            payload = await uploaded.read(limits.max_archive_bytes + 1)
            if len(payload) > limits.max_archive_bytes:
                raise HTTPException(413, "archive too large")
            with path.open("xb") as handle:
                handle.write(payload); handle.flush(); os.fsync(handle.fileno())
            summary = importer.import_archive(path, settings.images_dir, dry_run=True, square_policy=settings.square_policy, limits=limits)
            entries = _archive_entries(path, limits)
            previews[token] = _Preview(
                sid,
                path,
                time.time() + preview_ttl,
                len(payload),
                hashlib.sha256(payload).hexdigest(),
                summary.to_dict(),
                entries,
            )
        except HTTPException:
            path.unlink(missing_ok=True)
            raise
        except (importer.ImportErrorBase, ValueError, zipfile.BadZipFile, tarfile.TarError, OSError) as exc:
            path.unlink(missing_ok=True)
            raise HTTPException(400, str(exc)) from exc
        hints = sorted({hint for _digest, hint in entries if hint})
        choices = "".join(
            f'<label><input type="checkbox" name="map_dirs" value="{_escape(hint)}" checked> 映射目录 {_escape(hint)}</label><br>'
            for hint in hints
        )
        default_value = _slugify(str(getattr(settings, "admin_import_default_tag", "imported")))
        body = (
            f"<pre>{_escape(summary.to_dict())}</pre>"
            f'<form method="post" action="{admin_path}/archives/confirm">{hidden(_session.csrf)}'
            f'<input type="hidden" name="token" value="{_escape(token)}">'
            f'<input type="hidden" name="map_dirs_present" value="1">'
            f'<label>默认标签 <input name="default_tag" value="{_escape(default_value)}"></label><br>'
            f'{choices}<button>确认导入</button></form>'
        )
        return _page("归档预览", body, _session.csrf)

    @router.post("/archives/confirm")
    async def archive_confirm(request: Request):
        sid, _session, form = await write_auth(request)
        token = str(form.get("token", ""))
        pending = previews.get(token)
        if pending is None:
            raise HTTPException(404, "preview not found or expired")
        if pending.expires_at <= time.time():
            previews.pop(token, None); pending.archive_path.unlink(missing_ok=True)
            raise HTTPException(410, "preview expired")
        if not hmac.compare_digest(pending.session_id, sid):
            raise HTTPException(403, "preview belongs to another session")
        try:
            current_size = pending.archive_path.stat().st_size
            current_hash = hashlib.sha256(pending.archive_path.read_bytes()).hexdigest()
        except OSError as exc:
            previews.pop(token, None)
            pending.archive_path.unlink(missing_ok=True)
            raise HTTPException(400, "preview archive unavailable") from exc
        if current_size != pending.archive_size or not hmac.compare_digest(current_hash, pending.archive_sha256):
            previews.pop(token, None)
            pending.archive_path.unlink(missing_ok=True)
            raise HTTPException(400, "preview archive changed")
        try:
            default_slug = db.validate_slug(str(form.get("default_tag", getattr(settings, "admin_import_default_tag", "imported"))))
            available_hints = {hint for _digest, hint in pending.entries if hint}
            if str(form.get("map_dirs_present", "")) == "1":
                selected_hints = {db.validate_slug(str(value)) for value in form.getlist("map_dirs")}
                if not selected_hints <= available_hints:
                    raise ValueError("directory tag is not part of preview")
            else:
                selected_hints = available_hints
        except ValueError as exc:
            previews.pop(token, None)
            pending.archive_path.unlink(missing_ok=True)
            raise HTTPException(400, str(exc)) from exc
        with db.get_conn(settings.database_path) as conn:
            try:
                default_id = _require_existing_tags(conn, [default_slug])[default_slug]
            except ValueError as exc:
                previews.pop(token, None)
                pending.archive_path.unlink(missing_ok=True)
                raise HTTPException(400, str(exc)) from exc
        existing_files = {path.resolve() for path in Path(settings.images_dir).rglob("*") if path.is_file()}
        try:
            summary = importer.import_archive(pending.archive_path, settings.images_dir, dry_run=False, square_policy=settings.square_policy, limits=_limits(settings))
            scan(request)
            with db.get_conn(settings.database_path) as conn:
                for digest, hint in pending.entries:
                    row = conn.execute("SELECT id FROM images WHERE content_hash=?", (digest,)).fetchone()
                    if row is None:
                        continue
                    conn.execute("INSERT OR IGNORE INTO image_tags(image_id,tag_id,created_at) VALUES(?,?,?)", (row["id"], default_id, db.utc_now()))
                    if hint and hint in selected_hints:
                        hint_id = db.ensure_tag(conn, hint)
                        conn.execute("INSERT OR IGNORE INTO image_tags(image_id,tag_id,created_at) VALUES(?,?,?)", (row["id"], hint_id, db.utc_now()))
            scan(request)
        except Exception:
            for path in Path(settings.images_dir).rglob("*"):
                if path.is_file() and path.resolve() not in existing_files:
                    path.unlink(missing_ok=True)
            scan(request)
            raise
        finally:
            previews.pop(token, None)
            pending.archive_path.unlink(missing_ok=True)
        return _redirect(admin_path, f"归档导入 {summary.imported}")

    return router
