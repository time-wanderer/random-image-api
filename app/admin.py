"""Server-rendered administration UI for Random Image API V2.

Integration::

    app.include_router(create_admin_router(settings))

Sessions, login throttles and ordinary multipart previews remain in process
memory. Chunked archive task state and preview metadata are persisted in SQLite
so an administrator can resume them after restarting and logging in again.
"""
from __future__ import annotations

import hashlib
import hmac
import html
import io
import errno
import json
import logging
import os
import re
import secrets
import shutil
import sqlite3
import stat
import tarfile
import tempfile
import threading
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote, urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.routing import APIRoute
from PIL import Image, ImageOps, UnidentifiedImageError
from starlette.datastructures import FormData
from starlette.formparsers import FormParser, MultiPartException, MultiPartParser
from starlette.concurrency import run_in_threadpool

from app import db, importer
from app.chunked_upload import UploadError, UploadStore
from app.catalog import classify_orientation

_FORMAT_EXTENSIONS = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp"}
_CONTENT_TYPES = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}
_SLUG_PART = re.compile(r"[^a-z0-9]+")
_UNTAGGED_FILTER = "__untagged__"
_UPLOAD_JS = Path(__file__).with_name("admin_upload.js").read_text(encoding="utf-8")
logger = logging.getLogger("random_image_api.admin")


def _stage_spooled_upload(
    uploaded: Any,
    destination: Path,
    *,
    limit: int,
    min_free: int,
    reserved_bytes: int = 0,
) -> tuple[int, str]:
    """Move a server spool when possible, otherwise copy it once with peak checks."""
    spool = getattr(uploaded, "file", None)
    if not isinstance(spool, tempfile.SpooledTemporaryFile):
        raise HTTPException(400, "上传临时文件不可用，请重新选择归档。")
    spool.rollover()
    spool.seek(0, os.SEEK_END)
    size = spool.tell()
    spool.seek(0)
    if size <= 0:
        raise HTTPException(400, "归档为空，请选择包含图片的归档。")
    if size > limit:
        raise HTTPException(413, "归档超过普通上传上限，请使用分片上传或减小文件。")

    source: Path | None = None
    source_name = getattr(getattr(spool, "_file", None), "name", None)
    if isinstance(source_name, (str, os.PathLike)):
        candidate = Path(source_name)
        try:
            source_stat = candidate.lstat()
            if stat.S_ISREG(source_stat.st_mode) and not candidate.is_symlink():
                source = candidate
        except OSError:
            source = None

    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    free = shutil.disk_usage(destination.parent).free
    can_move = False
    if source is not None:
        try:
            can_move = source.stat().st_dev == destination.parent.stat().st_dev
        except OSError:
            can_move = False
    required_extra = 0 if can_move else size
    if free - required_extra - reserved_bytes < min_free:
        raise HTTPException(507, "上传临时空间不足，请清理空间后重试。")

    digest = hashlib.sha256()
    try:
        if can_move and source is not None:
            for chunk in iter(lambda: spool.read(1024 * 1024), b""):
                digest.update(chunk)
            spool.seek(0)
            os.replace(source, destination)
            os.chmod(destination, 0o600)
        else:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(destination, flags, 0o600)
            with os.fdopen(fd, "wb") as output:
                os.fchmod(output.fileno(), 0o600)
                copied = 0
                for chunk in iter(lambda: spool.read(1024 * 1024), b""):
                    copied += len(chunk)
                    if copied > size or output.write(chunk) != len(chunk):
                        raise OSError(errno.ENOSPC, "short write")
                    digest.update(chunk)
                if copied != size:
                    raise OSError(errno.EIO, "spool size changed")
                output.flush()
                os.fsync(output.fileno())
        return size, digest.hexdigest()
    except HTTPException:
        destination.unlink(missing_ok=True)
        raise
    except OSError as exc:
        destination.unlink(missing_ok=True)
        if exc.errno in {errno.ENOSPC, getattr(errno, "EDQUOT", errno.ENOSPC)}:
            raise HTTPException(507, "上传临时空间不足，请清理空间后重试。") from exc
        raise HTTPException(400, "无法保存上传归档，请重新选择文件后重试。") from exc


class _ClosingFormRoute(APIRoute):
    """Close parsed form uploads after every administration request."""

    def get_route_handler(self):
        handler = super().get_route_handler()

        async def closing_handler(request: Request):
            try:
                response = await handler(request)
                renewal = getattr(request.state, "upload_owner_renewal", None)
                if renewal is not None:
                    name, value, max_age, secure, path = renewal
                    response.set_cookie(
                        name,
                        value,
                        max_age=max_age,
                        httponly=True,
                        secure=secure,
                        samesite="strict",
                        path=path,
                    )
                return response
            finally:
                await request.close()

        return closing_handler


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
    selected_default_tag: str
    selected_tags: tuple[str, ...]
    persistent: bool = False
    owner_key: str = ""


def _escape(value: object) -> str:
    return html.escape(str(value), quote=True)


def _page(title: str, body: str, csrf: str | None = None, nav: str = "") -> HTMLResponse:
    token = "" if csrf is None else f'<meta name="csrf-token" content="{_escape(csrf)}">'
    style = """<style>
:root {
  color-scheme: light;
  --bg: #eef3f8;
  --surface: #fff;
  --surface-soft: #f7f9fc;
  --text: #172033;
  --muted: #667085;
  --line: #d9e2ec;
  --primary: #2563eb;
  --primary-dark: #1d4ed8;
  --danger: #b42318;
  --ok: #067647;
  --warning: #b54708;
  --shadow: 0 8px 24px #17203312;
}
* { box-sizing: border-box; }
html, body { overflow-x: hidden; }
body { margin: 0; background: linear-gradient(180deg, #e9f0f8 0, #f7f9fc 280px); color: var(--text); font: 15px/1.55 system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
header, main { width: min(1180px, calc(100% - 32px)); margin: auto; }
header { padding: 24px 0 0; }
.appbar { display: flex; align-items: center; justify-content: space-between; gap: 18px; }
.brand { display: flex; align-items: center; gap: 12px; min-width: 0; }
.brand-mark { display: grid; place-items: center; width: 42px; height: 42px; border-radius: 12px; background: var(--primary); color: #fff; font-weight: 800; font-size: 20px; }
.brand h1 { margin: 0; font-size: 1.35rem; }
.version, .muted, .stat small, .path { color: var(--muted); }
.user-actions, .actions, .card-action-group { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
.user-actions form, .actions form { margin: 0; }
nav.main-nav { display: flex; flex-wrap: wrap; gap: 8px; margin: 22px 0; min-width: 0; }
nav.main-nav a { border: 1px solid transparent; border-radius: 9px; padding: 9px 14px; color: #344054; text-decoration: none; font-weight: 650; white-space: nowrap; }
nav.main-nav a:hover, nav.main-nav a.active { background: var(--surface); border-color: var(--line); box-shadow: 0 2px 7px #1720330d; color: var(--primary); }
main { padding-bottom: 48px; }
h1, h2, h3 { line-height: 1.2; }
h2 { margin: 0 0 8px; }
h3 { margin: 0 0 6px; }
.eyebrow { margin: 0 0 7px; color: var(--primary); font-size: .78rem; font-weight: 800; letter-spacing: .08em; text-transform: uppercase; }
.lead { font-size: 1.05rem; }
.panel, .card, .stat, .step { background: var(--surface); border: 1px solid var(--line); border-radius: 14px; box-shadow: var(--shadow); }
.panel { padding: 20px; margin: 16px 0; }
.hero { padding: 28px; background: linear-gradient(135deg, #fff 0, #eff6ff 100%); }
.section-heading, .result-toolbar { display: flex; justify-content: space-between; align-items: end; gap: 16px; margin: 28px 0 12px; }
.stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(0, 1fr)); gap: 12px; min-width: 0; }
.stat { padding: 16px; }
.stat strong { display: block; font-size: 1.8rem; }
.steps { display: grid; grid-template-columns: repeat(3, 1fr); gap: 14px; }
.step { padding: 18px; min-width: 0; overflow-wrap: anywhere; }
.step-number { display: inline-grid; place-items: center; width: 30px; height: 30px; border-radius: 50%; background: #dbeafe; color: var(--primary); font-weight: 800; }
.management-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 16px; }
.management-grid .panel { margin: 0; min-width: 0; }
.button, button { display: inline-flex; align-items: center; justify-content: center; border: 0; border-radius: 9px; padding: 10px 14px; background: var(--primary); color: #fff; text-decoration: none; cursor: pointer; font: inherit; font-weight: 650; }
button:hover, .button:hover { background: var(--primary-dark); }
button:disabled, .button[aria-disabled="true"] { opacity: .55; cursor: not-allowed; }
button.secondary, .button.secondary { background: #475467; }
button.danger { background: var(--danger); }
button.warning { background: var(--warning); }
button.link { background: transparent; color: var(--primary); padding: 4px 0; }
button:focus-visible, a:focus-visible, input:focus-visible, select:focus-visible, summary:focus-visible { outline: 3px solid #93c5fd; outline-offset: 3px; }
form { margin: 0; }
form.filters, .form-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 14px; align-items: end; }
.toolbar { display: flex; justify-content: space-between; align-items: center; gap: 12px; padding: 14px 16px; border: 1px solid var(--line); border-radius: 11px; background: var(--surface-soft); }
.toolbar h3 { margin: 0; }
.file-picker { display: grid; gap: 7px; padding: 12px; border: 1px dashed #9bb5d1; border-radius: 10px; background: var(--surface-soft); }
.help-text { color: var(--muted); font-size: .84rem; }
label { display: block; font-weight: 650; }
label span.label-note { display: block; color: var(--muted); font-size: .82rem; font-weight: 400; }
input, select { width: 100%; min-width: 0; padding: 10px; border: 1px solid #cbd5e1; border-radius: 8px; background: #fff; color: var(--text); font: inherit; }
input[type=file] { padding: 8px; background: var(--surface-soft); }
input[type=checkbox] { width: auto; }
.check { display: inline-flex; gap: 6px; align-items: center; font-weight: 400; }
.actions form { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; min-width: 0; }
.actions select { width: auto; max-width: 100%; }
.message, .alert { padding: 12px 14px; border-radius: 10px; background: #eff6ff; border: 1px solid #bfdbfe; }
.alert.success { background: #ecfdf3; border-color: #a7f3d0; color: var(--ok); }
.alert.error { background: #fef3f2; border-color: #fecdca; color: var(--danger); }
.card-body { padding: 14px; overflow-wrap: anywhere; }
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 16px; align-items: start; min-width: 0; }
.card { min-width: 0; overflow: hidden; }
.preview-frame { position: relative; display: grid; place-items: center; width: 100%; aspect-ratio: 4 / 3; background: #e9eef5; overflow: hidden; }
.card img, .preview-frame .placeholder { display: block; width: 100%; height: 100%; object-fit: contain; background: #e9eef5; }
.placeholder { display: grid; place-items: center; color: var(--muted); }
.preview-overlay { position: absolute; inset: 10px 10px auto; display: flex; justify-content: space-between; align-items: flex-start; gap: 8px; pointer-events: none; }
.preview-overlay .badge { margin: 0; box-shadow: 0 1px 4px #17203326; }
.path { color: var(--muted); font-size: .82rem; overflow-wrap: anywhere; word-break: break-word; }
.tags { margin: 10px 0; }
.tags-label { display: block; margin-bottom: 4px; font-weight: 750; }
.badge { display: inline-block; margin: 2px; padding: 4px 10px; border-radius: 999px; background: #e0e7ff; color: #273b8f; font-size: .82rem; font-weight: 700; }
.badge.ok { background: #ecfdf3; color: var(--ok); }
.badge.off { background: #fef3f2; color: var(--danger); }
.card-meta { display: flex; flex-wrap: wrap; gap: 2px; }
.manage-entry { width: 100%; margin-top: 12px; }
.card-action-group { align-items: stretch; margin-top: 10px; padding: 10px; border: 1px solid var(--line); border-radius: 10px; background: var(--surface-soft); }
.card-action-group details { flex: 1 1 100%; min-width: 0; }
.card-action-group summary { cursor: pointer; color: var(--primary); font-weight: 750; }
.card-action-group.danger-zone { border-color: #fecdca; background: #fff8f7; }
.card-action-group.danger-zone summary { color: var(--danger); }
.table-wrap { overflow-x: auto; }
.tag-table { width: 100%; border-collapse: collapse; }
.tag-table th, .tag-table td { padding: 12px 10px; border-bottom: 1px solid var(--line); text-align: left; vertical-align: top; }
.tag-table th { color: var(--muted); font-size: .82rem; }
.danger-zone { border-color: #fecdca; background: #fff8f7; }
.filter-group { min-width: 0; padding: 16px; border: 1px solid var(--line); border-radius: 11px; background: var(--surface-soft); }
.filter-group h3 { font-size: 1rem; }
.clear-filter { color: var(--muted); font-weight: 650; text-decoration: none; }
.card-action-group { align-items: stretch; margin-top: 12px; }
.card-action-group details { flex: 1 1 100%; min-width: 0; }
.card-action-group summary { cursor: pointer; color: var(--primary); font-weight: 700; }
.pager { display: flex; justify-content: center; gap: 12px; margin: 20px 0; }
.pager a { color: var(--primary); font-weight: 650; }
.empty-state { text-align: center; padding: 34px 18px; }
.empty-icon { font-size: 2rem; }
.form-error { grid-column: 1 / -1; }
.tag-color-0{background:#e0f2fe;color:#075985}.tag-color-1{background:#ede9fe;color:#5b21b6}.tag-color-2{background:#fce7f3;color:#9d174d}.tag-color-3{background:#dcfce7;color:#166534}.tag-color-4{background:#fef3c7;color:#92400e}.tag-color-5{background:#ffedd5;color:#9a3412}.tag-color-6{background:#cffafe;color:#155e75}.tag-color-7{background:#f3e8ff;color:#7e22ce}.tag-color-8{background:#dbeafe;color:#1e40af}.tag-color-9{background:#ccfbf1;color:#115e59}.tag-color-10{background:#fee2e2;color:#991b1b}.tag-color-11{background:#f1f5f9;color:#334155}
.preview-frame img{cursor:zoom-in}.floating-toolbar{position:sticky;top:8px;z-index:5;display:flex;align-items:center;justify-content:space-between;gap:12px;margin:12px 0;padding:12px 16px;background:#172033f2;color:#fff;border-radius:12px;box-shadow:0 8px 24px #17203340}.floating-toolbar[hidden]{display:none}.drawer,.lightbox{position:fixed;inset:0;z-index:20;background:#17203399;display:flex;justify-content:flex-end}.drawer[hidden],.lightbox[hidden]{display:none}.drawer-panel{width:min(520px,100%);height:100%;overflow:auto;padding:24px;background:#fff;box-shadow:-8px 0 28px #17203340}.lightbox{align-items:center;justify-content:center;padding:18px}.lightbox img{max-width:96vw;max-height:90vh;width:auto;height:auto;object-fit:contain}.dialog-close{float:right;background:#475467}.detail-grid{display:grid;grid-template-columns:max-content 1fr;gap:8px 16px}.detail-grid dt{font-weight:700;color:var(--muted)}
@media(max-width:760px){
  header, main { width: min(100% - 18px, 1180px); }
  .appbar { display: block; }
  .brand { align-items: flex-start; }
  .brand > div { min-width: 0; }
  .brand h1, .version { overflow-wrap: anywhere; }
  .user-actions { display: grid; grid-template-columns: minmax(0, 1fr) auto; align-items: start; min-width: 0; }
  .user-actions form { justify-self: end; }
  .user-actions form button { white-space: nowrap; }
  .stats, .steps, .management-grid, .workspace-grid, form.filters, .form-grid { grid-template-columns: 1fr; min-width: 0; }
  .stats,.steps,.workspace-grid,form.filters,.form-grid{grid-template-columns:1fr;min-width:0}
  nav.main-nav a{white-space:nowrap}
  nav.main-nav a{word-break:keep-all}
  nav.main-nav{overflow-x:auto}
  nav.main-nav{flex-wrap:nowrap;overflow-x:auto;overflow-y:hidden}
  html,body{overflow-x:hidden}
  .grid { grid-template-columns: 1fr; min-width: 0; }
  .card, .card-body, .card-action-group, .result-toolbar, .toolbar { min-width: 0; }
  .section-heading, .result-toolbar, .toolbar { align-items: flex-start; flex-direction: column; }
  .result-toolbar strong { align-self: stretch; }
  .card-action-group .actions, .card-action-group .actions form { width: 100%; min-width: 0; }
  .card-action-group .actions form > * { max-width: 100%; }
  code, .path { overflow-wrap: anywhere; word-break: break-word; }
  .actions form, .actions button, .actions .button, .card-action-group button, .card-action-group select { width: 100%; }
  .actions form select, .actions form input:not([type=hidden]) { width: 100%; min-width: 0; }
  nav.main-nav { flex-wrap: nowrap; overflow-x: auto; overflow-y: hidden; scrollbar-width: thin; margin: 12px 0; min-width: 0; padding-bottom: 2px; }
  nav.main-nav a { flex: 0 0 auto; text-align: center; white-space: nowrap; }
  .tag-table { min-width: 680px; }
}
@media(max-width:390px){
  header, main { width: calc(100% - 18px); }
  .grid { grid-template-columns: 1fr; }
  .card-action-group { padding: 8px; }
  .card-action-group .actions form { display: grid; grid-template-columns: 1fr; }
  .card-action-group .actions form > * { width: 100%; min-width: 0; }
  .result-toolbar strong { width: 100%; }
}
</style><script>(function(){document.addEventListener('DOMContentLoaded',function(){
function closeLayer(layer){if(!layer)return;layer.hidden=true;document.body.style.overflow='';if(layer._opener)layer._opener.focus();}
document.addEventListener('submit',function(e){var f=e.target;if(!f.matches('form')||f.matches('[data-upload-form]'))return;var sel=f.querySelector('select[name="tag_id"]');if(sel&&sel.required&&!sel.value){e.preventDefault();var h=f.querySelector('.form-error')||document.createElement('p');h.className='alert error form-error';h.setAttribute('role','alert');h.textContent='请先选择一个标签，再提交此操作。';if(!h.parentNode)f.prepend(h);sel.focus();return;}var btn=f.querySelector('button[type="submit"],button:not([type])');if(btn){btn.disabled=true;btn.dataset.originalText=btn.textContent;btn.textContent='处理中…';}});
function refreshBatchSelection(){var count=document.querySelectorAll('[data-batch-image]:checked').length;var h=document.getElementById('batch-count');var bar=document.querySelector('[data-batch-toolbar]');if(h)h.textContent=count?'已选择 '+count+' 张图片':'请选择图片';if(bar)bar.hidden=!count;return count;}document.addEventListener('change',function(e){if(e.target.matches('[data-batch-image]'))refreshBatchSelection();if(e.target.matches('[data-drop-input]')){var z=e.target.closest('[data-drop-zone]'),h=z&&z.querySelector('[data-file-summary]');if(h)h.textContent=e.target.files.length+' 个文件：'+Array.from(e.target.files).map(function(x){return x.name;}).join('、');}});
document.addEventListener('click',function(e){var layer=e.target.closest('.lightbox,.drawer');if(layer&&e.target===layer){closeLayer(layer);return;}var batch=e.target.closest('[data-batch-select]');if(batch){e.preventDefault();var checked=batch.dataset.batchSelect==='all';document.querySelectorAll('[data-batch-image]').forEach(function(x){x.checked=checked;});refreshBatchSelection();return;}var c=e.target.closest('[data-close-layer]');if(c){e.preventDefault();closeLayer(document.getElementById(c.dataset.closeLayer));return;}var l=e.target.closest('[data-lightbox-src]');if(l){e.preventDefault();var b=document.getElementById('image-lightbox');b.querySelector('img').src=l.dataset.lightboxSrc;b.querySelector('img').alt=l.dataset.lightboxAlt||'';b._opener=l;b.hidden=false;document.body.style.overflow='hidden';return;}var d=e.target.closest('[data-detail-url]');if(d){e.preventDefault();var x=document.getElementById('image-detail-drawer');x.querySelector('.drawer-content').innerHTML='<p class="muted">正在加载…</p>';x._opener=d;x.hidden=false;document.body.style.overflow='hidden';fetch(d.dataset.detailUrl,{credentials:'same-origin'}).then(function(r){if(r.redirected&&new URL(r.url,window.location.href).pathname.endsWith('/login')){window.location.assign(r.url);return null;}if(!r.ok)throw Error();return r.text();}).then(function(t){if(t!==null)x.querySelector('.drawer-content').innerHTML=t;}).catch(function(){x.querySelector('.drawer-content').innerHTML='<p class="alert error" role="alert">详情加载失败，请重试。</p>';});}});
document.addEventListener('keydown',function(e){if(e.key==='Escape'){closeLayer(document.getElementById('image-lightbox'));closeLayer(document.getElementById('image-detail-drawer'));}});
var initialBatch=document.querySelector('[data-batch-toolbar]');if(initialBatch)initialBatch.hidden=true;var z=document.querySelector('[data-drop-zone]'),i=document.querySelector('[data-drop-input]');if(z&&i){['dragenter','dragover'].forEach(function(n){z.addEventListener(n,function(e){e.preventDefault();z.classList.add('is-dragging');});});['dragleave','drop'].forEach(function(n){z.addEventListener(n,function(e){e.preventDefault();z.classList.remove('is-dragging');});});z.addEventListener('drop',function(e){if(e.dataTransfer.files.length){i.files=e.dataTransfer.files;i.dispatchEvent(new Event('change',{bubbles:true}));}});}
});})();</script>"""
    return HTMLResponse("<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"+f"{token}<title>{_escape(title)}</title>{style}</head><body><header><div class=\"appbar\"><div class=\"brand\"><span class=\"brand-mark\" aria-hidden=\"true\">R</span><div><h1>{_escape(title)}</h1><span class=\"version\">Random Image API · 管理端 · V2</span></div></div><div class=\"user-actions\">{nav}</div></div></header><main>{body}</main></body></html>")


def _error_page(title: str, message: str, status_code: int = 400) -> HTMLResponse:
    """Render expected administrator/input failures without exposing internals."""
    page = _page(title, f'<p class="alert error" role="alert">{_escape(message)}</p>'
                 '<p><a class="button" href="javascript:history.back()">返回上一页</a></p>', None)
    page.status_code = status_code
    return page


def _redirect(path: str, message: str | None = None, error: str | None = None) -> RedirectResponse:
    separator = "&" if "?" in path else "?"
    query = []
    if message:
        query.append(("message", message))
    if error:
        query.append(("error", error))
    target = path if not query else f"{path}{separator}{urlencode(query)}"
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


def _archive_entries(
    path: Path,
    limits: importer.ImportLimits,
    archive_suffix: str | None = None,
) -> list[tuple[str, str]]:
    """Return image hashes/tag hints after importer-owned archive validation."""
    archive_size = path.stat().st_size
    archive, kind = importer._open_archive(path, archive_suffix)
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
        row = conn.execute("SELECT id FROM tags WHERE slug=? COLLATE NOCASE AND enabled=1", (slug,)).fetchone()
        if row is None:
            raise ValueError(f"tag does not exist or is disabled: {slug}")
        result[slug] = int(row["id"])
    return result


def _safe_db_file(root_value: Path | str, stored_name: str) -> Path:
    """安全解析数据库持有的相对路径，并拒绝越界和符号链接。"""
    root = Path(root_value).resolve()
    relative = Path(stored_name)
    if relative.is_absolute() or ".." in relative.parts:
        raise HTTPException(400, "文件位置无效")
    lexical = root / relative
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise HTTPException(404, "文件不可用")
    resolved = lexical.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise HTTPException(400, "文件位置无效") from exc
    if not resolved.is_file():
        raise HTTPException(404, "文件不可用")
    return resolved


def _tag_color_class(slug: str) -> str:
    """Return a stable finite CSS class; user input never becomes CSS."""
    digest = hashlib.sha256(slug.strip().lower().encode("utf-8", "replace")).hexdigest()
    return f"tag-color-{int(digest[:8], 16) % 12}"


_SORT_COLUMNS = {
    "added_at": "i.updated_at",
    "filename": "i.rel_path",
    "direction": "i.orientation",
    "source": "i.source",
}
_WEBDAV_SORT_COLUMNS = {
    "added_at": "o.updated_at",
    "filename": "o.href",
    "direction": "o.orientation",
    "source": "o.href",
}


def _storage_group(rel_path: str) -> str:
    parts = PurePosixPath(rel_path).parts
    first = parts[0] if parts else "root"
    return first if first in {"desktop", "mobile", "square"} else "root"


def _like_pattern(value: str) -> str:
    return "%" + value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def create_admin_router(settings: Any, upload_store: UploadStore | None = None) -> APIRouter:
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
    multipart_archive_max = max(1, int(getattr(
        settings, "admin_multipart_archive_max_bytes",
        min(int(getattr(settings, "admin_max_archive_bytes", importer.ImportLimits().max_archive_bytes)), 512 * 1024 * 1024),
    )))
    request_max = max(upload_max, multipart_archive_max) + 1024 * 1024
    upload_tmp_dir = Path(getattr(settings, "upload_tmp_dir", Path(settings.data_dir) / "tmp" / "admin"))
    configured_secret = str(getattr(settings, "admin_session_secret", ""))
    signing_key = configured_secret.encode()
    upload_store = upload_store or UploadStore(settings)
    owner_cookie_name = f"{cookie_name}_upload_owner"
    owner_ttl = max(
        int(getattr(settings, "admin_upload_owner_ttl_seconds", 30 * 86_400)),
        int(getattr(settings, "admin_chunked_upload_ttl_seconds", 86_400)),
    )
    sessions: dict[str, _Session] = {}
    previews: dict[str, _Preview] = {}
    previews_lock = threading.RLock()
    confirming_tokens: set[str] = set()
    failures: dict[str, list[float]] = {}
    router = APIRouter(
        prefix=admin_path,
        tags=["admin-v2"],
        route_class=_ClosingFormRoute,
    )

    def clean(now: float | None = None) -> None:
        current = time.time() if now is None else now
        upload_store.clean(current)
        for sid in [key for key, value in sessions.items() if value.expires_at <= current]:
            sessions.pop(sid, None)
            for token in [key for key, value in previews.items() if value.session_id == sid]:
                pending = previews.pop(token)
                if not pending.persistent:
                    pending.archive_path.unlink(missing_ok=True)
        for token in [key for key, value in previews.items() if value.expires_at <= current]:
            pending = previews.pop(token)
            if pending.persistent:
                try:
                    upload_store.delete(token, pending.owner_key, missing_ok=True)
                except UploadError:
                    pass
            else:
                pending.archive_path.unlink(missing_ok=True)

    def discard_preview(token: str, pending: _Preview) -> None:
        previews.pop(token, None)
        if pending.persistent:
            try:
                upload_store.delete(token, pending.owner_key, missing_ok=True)
            except UploadError:
                pass
        else:
            pending.archive_path.unlink(missing_ok=True)

    def discard_failed_preview(token: str, pending: _Preview) -> None:
        # Persistent previews remain available for a corrected/retried confirm.
        if not pending.persistent:
            discard_preview(token, pending)

    def sign(sid: str, expires: int) -> str:
        payload = f"{sid}.{expires}"
        signature = hmac.new(signing_key, payload.encode(), hashlib.sha256).hexdigest()
        return f"{payload}.{signature}"

    def sign_owner(owner_id: str, expires: int) -> str:
        payload = f"upload-owner.{owner_id}.{expires}"
        signature = hmac.new(signing_key, payload.encode(), hashlib.sha256).hexdigest()
        return f"{owner_id}.{expires}.{signature}"

    def parse_owner(raw: str) -> tuple[str, int] | None:
        try:
            owner_id, expiry_text, supplied = raw.split(".", 2)
            expiry = int(expiry_text)
        except (ValueError, TypeError):
            return None
        if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", owner_id) or expiry <= int(time.time()):
            return None
        expected = hmac.new(
            signing_key, f"upload-owner.{owner_id}.{expiry}".encode(), hashlib.sha256
        ).hexdigest()
        return (owner_id, expiry) if hmac.compare_digest(supplied, expected) else None

    def upload_owner(request: Request) -> str:
        parsed = parse_owner(request.cookies.get(owner_cookie_name, ""))
        if parsed is None:
            raise HTTPException(401, "请重新登录后继续上传。")
        owner_id, expiry = parsed
        now = int(time.time())
        if expiry - now <= max(60, owner_ttl // 2):
            renewed_expiry = now + owner_ttl
            forwarded_proto = request.headers.get("x-forwarded-proto", "").split(",", 1)[0].strip().lower()
            secure = cookie_secure or request.url.scheme == "https" or (
                bool(getattr(settings, "trusted_proxy_headers", True)) and forwarded_proto == "https"
            )
            request.state.upload_owner_renewal = (
                owner_cookie_name,
                sign_owner(owner_id, renewed_expiry),
                owner_ttl,
                secure,
                admin_path,
            )
        return hmac.new(signing_key, f"upload-task:{owner_id}".encode(), hashlib.sha256).hexdigest()

    def authenticate(request: Request, *, api: bool = False) -> tuple[str, _Session]:
        clean()

        def authentication_failed() -> None:
            if request.method == "GET" and not api:
                raise HTTPException(
                    status_code=303,
                    detail="请先登录管理端",
                    headers={"Location": f"{admin_path}/login"},
                )
            raise HTTPException(401, "请先登录管理端")

        raw = request.cookies.get(cookie_name, "")
        try:
            sid, expiry_text, supplied = raw.split(".", 2)
            expiry = int(expiry_text)
        except (ValueError, TypeError):
            authentication_failed()
            raise AssertionError("unreachable")
        expected = hmac.new(signing_key, f"{sid}.{expiry}".encode(), hashlib.sha256).hexdigest()
        if expiry <= int(time.time()) or not hmac.compare_digest(supplied, expected):
            authentication_failed()
        session = sessions.get(sid)
        if session is None or session.expires_at <= time.time():
            authentication_failed()
        return sid, session

    async def form_data(request: Request) -> Any:
        """Stream form data with a hard total-size limit and disk-spooled files."""
        content_type = request.headers.get("content-type", "")
        media_type = content_type.partition(";")[0].strip().lower()
        try:
            declared = int(request.headers.get("content-length", "0"))
        except ValueError:
            raise HTTPException(400, "请求大小信息无效")
        if declared < 0:
            raise HTTPException(400, "请求大小信息无效")
        if declared > request_max:
            raise HTTPException(413, "上传内容超过网页上限，请减小文件或使用命令行导入。")
        if not content_type:
            return FormData()

        stream_limit_message = "上传内容超过网页上限，请减小文件或使用命令行导入。"

        async def limited_stream():
            received = 0
            async for chunk in request.stream():
                received += len(chunk)
                if received > request_max:
                    raise MultiPartException(stream_limit_message)
                yield chunk

        try:
            if media_type == "multipart/form-data":
                parsed = await MultiPartParser(
                    request.headers,
                    limited_stream(),
                    max_files=1000,
                    max_fields=1000,
                    max_part_size=request_max,
                ).parse()
            elif media_type == "application/x-www-form-urlencoded":
                parsed = await FormParser(
                    request.headers,
                    limited_stream(),
                    max_fields=1000,
                    max_part_size=upload_max,
                ).parse()
            else:
                raise HTTPException(415, "提交格式不受支持，请刷新页面后重试。")
            request._form = parsed
            return parsed
        except HTTPException:
            raise
        except MultiPartException as exc:
            status = 413 if exc.message == stream_limit_message else 400
            raise HTTPException(status, exc.message) from exc
        except Exception as exc:
            raise HTTPException(400, "无法读取提交内容，请重新选择文件后重试。") from exc
        raise HTTPException(415, "提交格式不受支持，请刷新页面后重试。")

    async def write_auth(request: Request) -> tuple[str, _Session, Any]:
        sid, session = authenticate(request)
        form = await form_data(request)
        supplied = str(form.get("csrf", ""))
        if not hmac.compare_digest(supplied, session.csrf):
            raise HTTPException(403, "页面已失效，请刷新后重试。")
        return sid, session, form

    def upload_api_auth(request: Request, *, write: bool = False) -> tuple[str, _Session, str]:
        try:
            sid, session = authenticate(request, api=True)
            owner_key = upload_owner(request)
        except HTTPException as exc:
            raise UploadError(401, "authentication_required", "请重新登录后继续上传。") from exc
        if write:
            supplied = request.headers.get("x-csrf-token", "")
            if not hmac.compare_digest(supplied, session.csrf):
                raise UploadError(403, "csrf_failed", "页面已失效，请刷新后重试。")
        return sid, session, owner_key

    def upload_error(exc: UploadError) -> JSONResponse:
        headers = {"Cache-Control": "no-store"}
        if exc.offset is not None:
            headers["Upload-Offset"] = str(exc.offset)
        return JSONResponse(
            status_code=exc.status,
            content={"error": {"code": exc.code, "message": exc.message}},
            headers=headers,
        )

    def upload_headers(row: Any) -> dict[str, str]:
        return {
            "Cache-Control": "no-store",
            "Upload-Offset": str(int(row["committed_offset"])),
            "Upload-Length": str(int(row["expected_size"])),
            "Upload-State": str(row["state"]),
            "Upload-Chunk-Recommended": str(upload_store.recommended),
            "Upload-Chunk-Min": str(upload_store.minimum),
            "Upload-Chunk-Max": str(upload_store.maximum),
            "Upload-Fingerprint": str(row["client_fingerprint"] or ""),
        }

    @router.get("/admin-upload.js", include_in_schema=False)
    def admin_upload_script() -> Response:
        return Response(
            _UPLOAD_JS,
            media_type="text/javascript; charset=utf-8",
            headers={"Cache-Control": "public, max-age=3600"},
        )

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

    def page_nav(active: str, csrf: str | None = None) -> str:
        links = (("overview", "总览", admin_path), ("images", "图片库", f"{admin_path}/images"), ("tags", "标签工作台", f"{admin_path}/tags"))
        items = "".join(
            f'<a class="{"active" if key == active else ""}" href="{_escape(url)}">{_escape(label)}</a>'
            for key, label, url in links
        )
        logout = ""
        if csrf is not None:
            logout = (f'<form method="post" action="{_escape(admin_path)}/logout">{hidden(csrf)}<button class="secondary">退出（保留上传）</button></form>' f'<form method="post" action="{_escape(admin_path)}/logout-and-clear" onsubmit="return confirm(\'确定退出并清除所有未完成上传？\')">{hidden(csrf)}<button class="danger">退出并清除上传</button></form>')
        return f'<nav class="main-nav" aria-label="主导航">{items}</nav>{logout}'

    def tag_inputs(rows: list[Any], *, adjustable_on_confirm: bool = False) -> str:
        if not rows:
            return '<p class="muted">当前还没有标签，请先创建标签。<a class="button" href="' + _escape(admin_path) + '/tags">创建第一个标签</a></p>'
        options = '<option value="">不添加标签（可选）</option>' + "".join(
            f'<option value="{_escape(row["slug"])}">{_escape(row["slug"])} — {_escape(row["display_name"])}</option>'
            for row in rows
        )
        checks = "".join(
            f'<label><input type="checkbox" name="tags" value="{_escape(row["slug"])}"> {_escape(row["display_name"])} — {_escape(row["slug"])}</label> '
            for row in rows
        )
        adjustment = "归档确认页可调整。" if adjustable_on_confirm else ""
        return f'<p class="muted">上传前所选标签将应用于本次全部图片；不选择则不添加标签。{adjustment}</p><label>主标签（可选） <select name="default_tag">{options}</select></label><fieldset><legend>附加标签（可选）</legend>{checks}</fieldset>'

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
            raise HTTPException(429, "登录尝试过多，请稍后重试。", headers={"Retry-After": str(login_window)})
        form = await form_data(request)
        supplied = str(form.get("token", ""))
        if not hmac.compare_digest(supplied, expected_token):
            recent.append(now)
            raise HTTPException(401, "登录凭据无效。")
        failures.pop(ip, None)
        sid = secrets.token_urlsafe(32)
        expires = int(now + session_ttl)
        sessions[sid] = _Session(secrets.token_urlsafe(32), float(expires))
        response = _redirect(admin_path)
        parsed_owner = parse_owner(request.cookies.get(owner_cookie_name, ""))
        owner_id = parsed_owner[0] if parsed_owner is not None else secrets.token_urlsafe(32)
        owner_expires = int(now + owner_ttl)
        forwarded_proto = request.headers.get("x-forwarded-proto", "").split(",", 1)[0].strip().lower()
        secure_cookie = cookie_secure or request.url.scheme == "https" or (
            bool(getattr(settings, "trusted_proxy_headers", True)) and forwarded_proto == "https"
        )
        response.set_cookie(cookie_name, sign(sid, expires), max_age=session_ttl, httponly=True, secure=secure_cookie, samesite="strict", path=admin_path)
        response.set_cookie(owner_cookie_name, sign_owner(owner_id, owner_expires), max_age=owner_ttl, httponly=True, secure=secure_cookie, samesite="strict", path=admin_path)
        return response

    @router.post("/logout")
    async def logout(request: Request):
        sid, _session, _form = await write_auth(request)
        sessions.pop(sid, None)
        with previews_lock:
            ordinary = [
                previews.pop(token)
                for token in list(previews)
                if previews[token].session_id == sid and not previews[token].persistent
            ]
        for pending in ordinary:
            pending.archive_path.unlink(missing_ok=True)
        response = _redirect(f"{admin_path}/login")
        response.delete_cookie(cookie_name, path=admin_path)
        return response

    @router.post("/logout-and-clear")
    async def logout_and_clear(request: Request):
        sid, _session, _form = await write_auth(request)
        owner_key = upload_owner(request)
        sessions.pop(sid, None)
        with previews_lock:
            ordinary = [previews.pop(token) for token in list(previews) if previews[token].session_id == sid and not previews[token].persistent]
        for pending in ordinary:
            pending.archive_path.unlink(missing_ok=True)
        await run_in_threadpool(upload_store.delete_owner_tasks, owner_key)
        response = _redirect(f"{admin_path}/login")
        response.delete_cookie(cookie_name, path=admin_path)
        response.delete_cookie(owner_cookie_name, path=admin_path)
        return response

    @router.get("", response_class=HTMLResponse)
    def overview(request: Request) -> HTMLResponse:
        _sid, session = authenticate(request)
        with db.get_conn(settings.database_path) as conn:
            local, local_enabled = conn.execute("SELECT COUNT(*),COALESCE(SUM(enabled),0) FROM images").fetchone()
            remote, remote_enabled = conn.execute("SELECT COUNT(*),COALESCE(SUM(enabled),0) FROM webdav_objects").fetchone()
            cached = int(conn.execute("SELECT COUNT(*) FROM webdav_cache").fetchone()[0])
            tags = int(conn.execute("SELECT COUNT(*) FROM tags").fetchone()[0])
            tag_rows = conn.execute("SELECT slug,display_name FROM tags WHERE enabled=1 ORDER BY slug").fetchall()
        message = _escape(request.query_params.get("message", ""))
        error = _escape(request.query_params.get("error", ""))
        notices = ((f'<p class="alert success" role="status">{message}</p>' if message else "") +
                   (f'<p class="alert error" role="alert">{error}</p>' if error else ""))
        body = (
            notices + f'<section class="panel hero"><p class="eyebrow">图库管理 · 快速开始</p><h2>把图片变成可调用的主题 API</h2>'
            '<p class="lead">先创建标签，再上传或导入图片，最后在图片库检查标签并使用主题接口。</p></section>'
            '<section class="section-heading"><div><p class="eyebrow">上手路径</p><h2>开始使用</h2></div></section>'
            '<section class="steps">'
            f'<article class="step"><span class="step-number">1</span><h3>创建标签</h3><p class="muted">用显示名称给人看，用唯一 slug 供 API 稳定调用。</p><a class="button" href="{_escape(admin_path)}/tags">创建标签</a></article>'
            '<article class="step"><span class="step-number">2</span><h3>上传 / 导入</h3><p class="muted">上传单张或多张图片，也可以先预览 ZIP / TAR 归档。</p><a class="button secondary" href="#upload">选择文件</a></article>'
            f'<article class="step"><span class="step-number">3</span><h3>浏览并调用 API</h3><p class="muted">在图片库确认真实方向和标签，再将 slug 用于主题 API。</p><div class="actions"><a class="button secondary" href="{_escape(admin_path)}/images?source=local">浏览本地图片</a><a class="button secondary" href="{_escape(admin_path)}/images?source=webdav">浏览 WebDAV</a></div></article>'
            '</section>'
            '<div class="section-heading"><div><p class="eyebrow">服务状态</p><h2>当前统计</h2></div></div>'
            f'<section class="stats"><article class="stat"><span>本地图片</span><strong>{int(local)}</strong><small>已启用 {int(local_enabled)}</small></article>'
            f'<article class="stat"><span>WebDAV 图片</span><strong>{int(remote)}</strong><small>已启用 {int(remote_enabled)}</small></article>'
            f'<article class="stat"><span>WebDAV 缓存</span><strong>{cached}</strong><small>仅展示已缓存预览</small></article>'
            f'<article class="stat"><span>主题标签</span><strong>{tags}</strong><small>可用于主题 API</small></article></section>'
            '<div class="section-heading"><div><p class="eyebrow">管理操作</p><h2>导入与维护</h2></div></div>'
            '<section class="management-grid">'
            f'<section class="panel" id="upload"><h3>上传图片</h3><p class="muted">支持 JPG、JPEG、PNG、WebP；可多选。标签为空时，图片仍可先上传，之后在图片库补充。</p>'
            f'<form data-upload-form="1" data-upload-kind="image" data-max-bytes="{upload_max}" method="post" action="{_escape(admin_path)}/upload" enctype="multipart/form-data">{hidden(session.csrf)}{tag_inputs(tag_rows)}<label class="file-picker" data-drop-zone="1">选择图片<span class="help-text" data-file-summary>拖拽文件到此处，或点击选择；每个文件网页上限 {upload_max / 1024 / 1024:.2f} MiB；无 JS 时仍可普通上传</span><input type="file" data-upload-input="1" name="files" accept=".jpg,.jpeg,.png,.webp,image/jpeg,image/png,image/webp" multiple required></label><p class="alert error" role="alert" data-upload-error hidden></p><button type="submit">上传图片</button></form></section>'
            f'<section class="panel"><h3>归档导入</h3><p class="muted">上传后先预览，确认后才会导入。</p><form data-upload-form="1" data-upload-kind="archive" data-max-bytes="{multipart_archive_max}" data-chunked-enabled="{str(upload_store.enabled).lower()}" data-chunked-url="{_escape(admin_path)}/archives/uploads" data-chunked-max-bytes="{upload_store.max_upload}" data-chunk-threshold-bytes="{upload_store.recommended}" data-login-url="{_escape(admin_path)}/login" method="post" action="{_escape(admin_path)}/archives/preview" enctype="multipart/form-data">{hidden(session.csrf)}{tag_inputs(tag_rows, adjustable_on_confirm=True)}<label class="file-picker" data-drop-zone="1">选择 ZIP / TAR 归档<span class="help-text" data-file-summary>支持 ZIP、TAR.GZ、TGZ；普通上传上限 {multipart_archive_max / 1024 / 1024:.2f} MiB；应用总上传上限 {upload_store.max_upload / 1024 / 1024:.2f} MiB；建议每片 {upload_store.recommended / 1024 / 1024:.2f} MiB。刷新后请重新选择同一文件继续</span><input type="file" data-upload-input="1" name="archive" accept=".zip,.tar.gz,.tgz" required></label><p class="alert error" role="alert" data-upload-error hidden></p><p class="message" role="status" aria-live="polite" data-upload-status hidden></p><div class="actions"><button type="submit">预览归档</button><button type="button" class="warning" data-upload-cancel hidden>取消上传</button></div></form></section>'
            f'<section class="panel"><h3>WebDAV 与缓存</h3><p class="muted">WebDAV 只管理已索引对象；预览仅使用已有缓存。</p><div class="actions"><a class="button secondary" href="{_escape(admin_path)}/images?source=webdav">管理 WebDAV 图片</a>'
            f'<form method="post" action="{_escape(admin_path)}/cache/clear" onsubmit="return confirm(\'确定清理全部 WebDAV 缓存？\')">{hidden(session.csrf)}<button class="warning">清理 WebDAV 缓存</button></form></div></section>'
            f'</section><script src="{_escape(admin_path)}/admin-upload.js" defer></script>'
        )
        return _page("管理总览", body, session.csrf, page_nav("overview", session.csrf))

    @router.get("/images/{image_id}/detail", response_class=HTMLResponse)
    def local_detail(image_id: int, request: Request):
        authenticate(request)
        with db.get_conn(settings.database_path) as conn:
            row = conn.execute(
                "SELECT id,rel_path,width,height,orientation,format,file_size,updated_at,source,enabled FROM images WHERE id=?",
                (image_id,),
            ).fetchone()
            if row is None or row["source"] != "local":
                raise HTTPException(404, "本地图片不存在")
            tag_rows = conn.execute(
                "SELECT t.slug,t.display_name,t.enabled FROM image_tags x JOIN tags t ON t.id=x.tag_id WHERE x.image_id=? ORDER BY t.slug",
                (image_id,),
            ).fetchall()
        tags_html = "".join(
            f'<span class="badge {_tag_color_class(str(tag["slug"]))}{("" if tag["enabled"] else " off")}">{_escape(tag["display_name"])} <small>({_escape(tag["slug"])})</small></span>'
            for tag in tag_rows
        ) or '<span class="muted">无标签</span>'
        rel_path = str(row["rel_path"])
        group = _storage_group(rel_path)
        return HTMLResponse(
            f'<h2>图片详情 #{int(row["id"])}</h2>'
            f'<img src="{_escape(admin_path)}/images/{int(row["id"])}/preview" alt="图片详情预览" style="max-width:100%;max-height:280px;object-fit:contain">'
            f'<dl class="detail-grid"><dt>ID</dt><dd>{int(row["id"])}</dd><dt>文件名</dt><dd>{_escape(Path(rel_path).name)}</dd>'
            f'<dt>真实方向</dt><dd>{_escape(row["orientation"])}</dd><dt>尺寸</dt><dd>{int(row["width"])} × {int(row["height"])} px</dd>'
            f'<dt>文件大小</dt><dd>{int(row["file_size"])} bytes</dd><dt>格式</dt><dd>{_escape(row["format"])}</dd>'
            f'<dt>存放目录</dt><dd>{_escape(group)}</dd><dt>文件位置</dt><dd><code>{_escape(rel_path)}</code></dd>'
            f'<dt>来源</dt><dd>{_escape(row["source"])}</dd><dt>启用状态</dt><dd>{"启用" if row["enabled"] else "禁用"}</dd>'
            f'<dt>缓存状态</dt><dd>管理员受保护预览</dd><dt>更新时间</dt><dd>{_escape(row["updated_at"])}</dd></dl>'
            f'<section class="tags"><strong>完整标签</strong><div>{tags_html}</div></section>'
        )

    @router.get("/images/{image_id}/preview")
    def local_preview(image_id: int, request: Request):
        authenticate(request)
        with db.get_conn(settings.database_path) as conn:
            row = conn.execute("SELECT rel_path,content_type,source FROM images WHERE id=?", (image_id,)).fetchone()
        if row is None or row["source"] != "local":
            raise HTTPException(404, "本地图片不存在")
        target = _safe_db_file(settings.images_dir, str(row["rel_path"]))
        return FileResponse(target, media_type=str(row["content_type"]), headers={"Cache-Control": "private, max-age=300", "X-Content-Type-Options": "nosniff"})

    @router.get("/webdav/preview")
    def webdav_preview(request: Request, href: str):
        authenticate(request)
        with db.get_conn(settings.database_path) as conn:
            row = conn.execute("SELECT c.cache_name,c.content_type FROM webdav_cache c JOIN webdav_objects o ON o.href=c.href WHERE c.href=?", (href,)).fetchone()
        if row is None:
            raise HTTPException(404, "缓存预览不存在")
        target = _safe_db_file(settings.cache_dir, str(row["cache_name"]))
        return FileResponse(target, media_type=str(row["content_type"]), headers={"Cache-Control": "private, max-age=300", "X-Content-Type-Options": "nosniff"})

    @router.get("/images", response_class=HTMLResponse)
    def images(request: Request, page: int = 1, per_page: int = 0, orientation: str = "", source: str = "local", cached: str = "", enabled: str = "", tag: str = "", storage: str = "", q: str = "", sort: str = "added_at", direction: str = "desc") -> HTMLResponse:
        _sid, session = authenticate(request)
        source = source if source in {"local", "webdav"} else "local"
        page = max(1, page)
        per_page = int(getattr(settings, "admin_page_size", 20)) if per_page <= 0 else min(100, max(1, per_page))
        orientation = orientation if orientation in {"desktop", "mobile", "square"} else ""
        enabled = enabled if enabled in {"0", "1"} else ""
        cached = cached if cached in {"0", "1"} else ""
        storage = storage if storage in {"root", "desktop", "mobile", "square"} else ""
        q = q.strip()[:100]
        sort = sort if sort in _SORT_COLUMNS else "added_at"
        direction = direction.lower() if direction.lower() in {"asc", "desc"} else "desc"
        sort_sql = _SORT_COLUMNS[sort]
        order_sql = f"{sort_sql} {direction.upper()}, i.id DESC"
        filters = {"source": source, "per_page": per_page, "orientation": orientation, "cached": cached, "enabled": enabled, "tag": tag, "storage": storage, "q": q, "sort": sort, "direction": direction}
        with db.get_conn(settings.database_path) as conn:
            all_tags = conn.execute("SELECT id,slug,display_name,enabled FROM tags ORDER BY slug").fetchall()
            selected_tag = None
            if tag and tag != _UNTAGGED_FILTER:
                selected_tag = conn.execute(
                    "SELECT slug,display_name FROM tags WHERE slug=? COLLATE NOCASE", (tag,)
                ).fetchone()
            clauses: list[str] = []
            args: list[object] = []
            if source == "webdav":
                clauses.append("1=1")
                if orientation in {"desktop", "mobile", "square"}: clauses.append("o.orientation=?"); args.append(orientation)
                if enabled: clauses.append("o.enabled=?"); args.append(int(enabled))
                if cached: clauses.append("c.href IS NOT NULL" if cached == "1" else "c.href IS NULL")
                if tag == _UNTAGGED_FILTER: clauses.append("NOT EXISTS(SELECT 1 FROM webdav_object_tags x WHERE x.href=o.href)")
                elif selected_tag is not None: clauses.append("EXISTS(SELECT 1 FROM webdav_object_tags x JOIN tags t ON t.id=x.tag_id WHERE x.href=o.href AND t.slug=? COLLATE NOCASE)"); args.append(tag)
                elif tag: clauses.append("0=1")
                if q: clauses.append("o.href LIKE ? ESCAPE '\\'"); args.append(_like_pattern(q))
                where = " WHERE " + " AND ".join(clauses)
                total = int(conn.execute("SELECT COUNT(*) FROM webdav_objects o LEFT JOIN webdav_cache c ON c.href=o.href" + where, args).fetchone()[0])
                webdav_order_sql = _WEBDAV_SORT_COLUMNS[sort]
                rows = conn.execute("SELECT o.href,o.orientation,o.enabled,c.href IS NOT NULL cached FROM webdav_objects o LEFT JOIN webdav_cache c ON c.href=o.href" + where + " ORDER BY " + webdav_order_sql + " " + direction.upper() + ", o.href LIMIT ? OFFSET ?", (*args, per_page, (page-1)*per_page)).fetchall()
                cards = []
                for row in rows:
                    href_raw = str(row["href"]); href = _escape(href_raw)
                    related = conn.execute("SELECT t.id,t.slug,t.enabled FROM webdav_object_tags x JOIN tags t ON t.id=x.tag_id WHERE x.href=? ORDER BY t.slug", (href_raw,)).fetchall()
                    badges = "".join(f'<span class="badge {_tag_color_class(str(x["slug"]))}{("" if x["enabled"] else " off")}">{_escape(x["slug"])}</span>' for x in related) or '<span class="muted">无标签</span>'
                    choices = "".join(f'<option value="{int(x["id"])}">{_escape(x["display_name"])} — {_escape(x["slug"])}</option>' for x in all_tags if x["enabled"])
                    media = (f'<div class="preview-frame"><img loading="lazy" src="{admin_path}/webdav/preview?{_escape(urlencode({"href": href_raw}))}" alt="WebDAV 缓存预览"><div class="preview-overlay"><span class="badge">方向：{_escape(row["orientation"])}</span><span class="badge {"ok" if row["enabled"] else "off"}">{"启用" if row["enabled"] else "禁用"}</span></div></div>' if row["cached"] else '<div class="preview-frame"><div class="placeholder">未缓存，不自动下载</div><div class="preview-overlay"><span class="badge">WebDAV</span><span class="badge off">未缓存</span></div></div>')
                    forms = f'<form method="post" action="{admin_path}/webdav/enabled">{hidden(session.csrf)}<input type="hidden" name="href" value="{href}"><input type="hidden" name="enabled" value="{1-int(row["enabled"])}"><button class="secondary">{"禁用" if row["enabled"] else "启用"}</button></form>'
                    forms += f'<form method="post" action="{admin_path}/webdav/tags"><span class="muted">添加或移除标签（可选）</span>{hidden(session.csrf)}<input type="hidden" name="href" value="{href}"><select name="tag_id" required><option value="">选择标签</option>{choices}</select><select name="action"><option value="add">添加标签</option><option value="remove">移除标签</option></select><button>更新标签</button></form>'
                    cards.append(f'<article class="card">{media}<div class="card-body"><div class="tags"><span class="tags-label">标签</span>{badges}</div><p class="path"><code>{href}</code></p><div class="card-meta"><span class="badge">真实方向：{_escape(row["orientation"])}</span><span class="badge">存放目录：WebDAV</span><span class="badge {"ok" if row["enabled"] else "off"}">{"启用" if row["enabled"] else "禁用"}</span></div><div class="card-action-group"><strong>管理此图片</strong><div class="actions">{forms}</div></div></div></article>')
                heading = "WebDAV 图片"; listing = "".join(cards)
            else:
                clauses.append("i.source='local'")
                if orientation: clauses.append("i.orientation=?"); args.append(orientation)
                if enabled: clauses.append("i.enabled=?"); args.append(int(enabled))
                if tag == _UNTAGGED_FILTER: clauses.append("NOT EXISTS(SELECT 1 FROM image_tags x WHERE x.image_id=i.id)")
                elif selected_tag is not None: clauses.append("EXISTS(SELECT 1 FROM image_tags x JOIN tags t ON t.id=x.tag_id WHERE x.image_id=i.id AND t.slug=? COLLATE NOCASE)"); args.append(tag)
                elif tag: clauses.append("0=1")
                if storage == "root": clauses.append("instr(i.rel_path,'/')=0")
                elif storage: clauses.append("i.rel_path LIKE ? ESCAPE '\\'"); args.append(storage + "/%")
                if q: clauses.append("i.rel_path LIKE ? ESCAPE '\\'"); args.append(_like_pattern(q))
                where = " WHERE " + " AND ".join(clauses)
                total = int(conn.execute("SELECT COUNT(*) FROM images i" + where, args).fetchone()[0])
                rows = conn.execute("SELECT i.id,i.rel_path,i.orientation,i.enabled FROM images i" + where + " ORDER BY " + order_sql + " LIMIT ? OFFSET ?", (*args, per_page, (page-1)*per_page)).fetchall()
                cards = []
                choices = "".join(f'<option value="{int(x["id"])}">{_escape(x["display_name"])} — {_escape(x["slug"])}</option>' for x in all_tags if x["enabled"])
                for row in rows:
                    iid=int(row["id"]); rel=_escape(row["rel_path"]); group=_storage_group(str(row["rel_path"]))
                    related=conn.execute("SELECT t.slug,t.enabled FROM image_tags x JOIN tags t ON t.id=x.tag_id WHERE x.image_id=? ORDER BY t.slug",(iid,)).fetchall()
                    badges="".join(f'<span class="badge {_tag_color_class(str(x["slug"]))}{("" if x["enabled"] else " off")}">{_escape(x["slug"])}</span>' for x in related) or '<span class="muted">无标签</span>'
                    toggle=f'<form method="post" action="{admin_path}/images/{iid}/enabled">{hidden(session.csrf)}<input type="hidden" name="enabled" value="{1-int(row["enabled"])}"><button class="secondary">{"禁用" if row["enabled"] else "启用"}</button></form>'
                    delete=f'<form method="post" action="{admin_path}/images/{iid}/delete" onsubmit="return confirm(\'确定删除本地原图？此操作不可撤销。\')">{hidden(session.csrf)}<input type="hidden" name="confirm" value="1"><button class="danger">删除本地原图</button></form>'
                    move=f'<form method="post" action="{admin_path}/images/{iid}/move">{hidden(session.csrf)}<select name="target" required><option value="desktop">desktop</option><option value="mobile">mobile</option><option value="square">square</option></select><button>移动归档</button></form>'
                    tags_form=f'<form method="post" action="{admin_path}/images/{iid}/tags"><span class="muted">添加或移除标签（可选）</span>{hidden(session.csrf)}<select name="tag_id" required><option value="">选择标签</option>{choices}</select><select name="action"><option value="add">添加标签</option><option value="remove">移除标签</option></select><button>更新标签</button></form>'
                    orientation_label = _escape(str(row["orientation"]))
                    status_class = "ok" if row["enabled"] else "off"
                    status_label = "启用" if row["enabled"] else "禁用"
                    cards.append(f'<article class="card"><div class="preview-frame"><img loading="lazy" data-lightbox-src="{admin_path}/images/{iid}/preview" data-lightbox-alt="本地图片 #{iid}" src="{admin_path}/images/{iid}/preview" alt="本地图片 #{iid}"><div class="preview-overlay"><span class="badge">{orientation_label}</span><span class="badge {status_class}">{status_label}</span></div></div><div class="card-body"><label class="check"><input type="checkbox" data-batch-image form="batch-tags" name="image_ids" value="{iid}">选择 #{iid}</label><button type="button" class="link" data-detail-url="{admin_path}/images/{iid}/detail" aria-label="查看图片 {iid} 详情">查看详情</button><p class="path"><code>{rel}</code></p><div class="card-meta"><span class="badge">真实方向：{orientation_label}</span><span class="badge">存放目录：{_escape(group)}</span><span class="badge {status_class}">{status_label}</span></div><div class="tags"><span class="tags-label">标签</span>{badges}</div><div class="card-action-group shortcut-menu"><details><summary class="button secondary manage-entry" aria-label="打开图片快捷菜单">管理此图片</summary><div class="actions"><span class="muted">状态 / 标签 / 归档</span>{toggle}{move}{tags_form}</div></details></div><div class="card-action-group danger-zone"><details><summary>危险操作</summary><div class="actions"><span class="muted">危险操作</span>{delete}</div></details></div></div></article>')
                heading="本地图片"; listing="".join(cards)
        def opts(values: list[tuple[str,str]], current: str) -> str:
            return '<option value="">全部</option>'+''.join(f'<option value="{_escape(v)}"{" selected" if v==current else ""}>{_escape(label)}</option>' for v,label in values)
        tag_opts=(f'<option value="">全部标签</option><option value="{_UNTAGGED_FILTER}"{" selected" if tag == _UNTAGGED_FILTER else ""}>无标签</option>'
                  + ''.join(f'<option value="{_escape(x["slug"])}"{" selected" if x["slug"]==tag else ""}>{_escape(x["slug"])}</option>' for x in all_tags))
        controls=(f'<section class="panel toolbar"><div><p class="eyebrow">图片库</p><h3>筛选工具栏</h3><span class="help-text">先用快速筛选缩小范围，再展开高级筛选。</span></div><a class="clear-filter" href="{_escape(admin_path)}/images?source={_escape(source)}" title="保留当前数据来源并清除其他筛选">清除筛选</a></section><section class="panel"><div class="filter-group"><h3>快速筛选</h3><form class="filters" method="get"><label>数据来源<select name="source">{opts([("local","本地图片"),("webdav","WebDAV 图片")],source)}</select></label><label>真实方向<select name="orientation">{opts([("desktop","横屏 Desktop"),("mobile","竖屏 Mobile"),("square","方形 Square")],orientation)}</select></label><label>标签<span class="label-note">“无标签”表示没有任何标签关系</span><select name="tag" aria-describedby="tag-filter-help">{tag_opts}</select></label><button>应用快速筛选</button><span id="tag-filter-help" class="help-text">全部标签不会限制结果；无标签与用户创建标签互斥。</span></form></div><details class="filter-group"><summary><h3>高级筛选</h3></summary><form class="filters" method="get"><input type="hidden" name="source" value="{_escape(source)}"><input type="hidden" name="orientation" value="{_escape(orientation)}"><input type="hidden" name="tag" value="{_escape(tag)}"><label>存放目录<select name="storage">{opts([("root","根目录"),("desktop","desktop 目录"),("mobile","mobile 目录"),("square","square 目录")],storage)}</select></label><label>启用状态<select name="enabled">{opts([("1","仅启用"),("0","仅停用")],enabled)}</select></label><label>缓存状态<select name="cached">{opts([("1","已缓存"),("0","未缓存")],cached)}</select></label><label>文件名 / HREF<input name="q" value="{_escape(q)}" placeholder="输入关键词"></label><label>排序字段<select name="sort">{opts([("added_at","添加时间"),("filename","文件名"),("direction","真实方向"),("source","来源")],sort)}</select></label><label>排序方向<select name="direction">{opts([("asc","升序"),("desc","降序")],direction)}</select></label><label>每页数量<select name="per_page">{opts([("12","12 条"),("20","20 条"),("40","40 条"),("80","80 条")],str(per_page))}</select></label><button class="secondary">应用高级筛选</button></form></details></section>')

        pager=[]
        if page>1: pager.append(f'<a rel="prev" href="{admin_path}/images?{_escape(urlencode({**filters,"page":page-1}))}">上一页</a>')
        if page*per_page<total: pager.append(f'<a rel="next" href="{admin_path}/images?{_escape(urlencode({**filters,"page":page+1}))}">下一页</a>')
        batch=""
        if source=="local":
            candidates=''.join(f'<option value="{int(x["id"])}">{_escape(x["display_name"])} — {_escape(x["slug"])}</option>' for x in all_tags if x["enabled"])
            batch=f'<form class="panel form-grid floating-toolbar" id="batch-tags" data-batch-toolbar="1" method="post" action="{admin_path}/images/tags"><p class="muted">批量标签：先勾选图片，再选择标签和操作。<span id="batch-count">请选择图片</span> <button type="button" class="secondary" data-batch-select="all">全选</button> <button type="button" class="secondary" data-batch-select="none">取消全选</button></p>{hidden(session.csrf)}<label>批量标签<select name="tag_id" required><option value="">选择标签</option>{candidates}</select></label><label>操作<select name="action"><option value="add">添加标签</option><option value="remove">移除标签</option></select></label><button type="submit">应用到选中图片</button></form>'
        tag_summary = "无标签" if tag == _UNTAGGED_FILTER else (str(selected_tag["display_name"]) if selected_tag is not None else "全部标签")
        result = f'<section class="result-toolbar"><div><p class="eyebrow">图片库</p><h2>{_escape(heading)}</h2><p class="muted">当前条件：标签：{_escape(tag_summary)}</p><p class="muted">真实方向来自图片内容；存放目录只是文件所在的归档目录。</p><p class="muted">操作按状态、标签、归档分组；危险操作会要求确认。删除本地原图前会提示：确定删除本地原图？此操作不可撤销。</p></div><strong>筛选结果：{int(total)} 张</strong></section>'
        if not listing:
            if tag == _UNTAGGED_FILTER:
                listing = f'<section class="panel empty-state"><div class="empty-icon" aria-hidden="true">▧</div><h3>当前筛选条件下没有无标签图片</h3><p class="muted">可调整其他筛选条件，或上传尚未添加标签的图片。</p><div class="actions"><a class="button secondary" href="{_escape(admin_path)}#upload">前往上传</a><a class="button secondary" href="{_escape(admin_path)}/tags">管理标签</a></div></section>'
            else:
                listing = f'<section class="panel empty-state"><div class="empty-icon" aria-hidden="true">▧</div><h3>没有匹配的图片</h3><p class="muted">调整筛选条件，或先从总览上传图片。</p><a class="button secondary" href="{_escape(admin_path)}#upload">前往上传</a></section>'
        layers='<section id="image-lightbox" class="lightbox" role="dialog" aria-modal="true" aria-label="大图预览" hidden><button type="button" class="dialog-close" data-close-layer="image-lightbox" aria-label="关闭大图">关闭</button><img alt="大图预览"></section><section id="image-detail-drawer" class="drawer" role="dialog" aria-modal="true" aria-label="图片详情" hidden><aside class="drawer-panel"><button type="button" class="dialog-close" data-close-layer="image-detail-drawer" aria-label="关闭详情">关闭</button><div class="drawer-content"></div></aside></section>'
        return _page("图片库",f'{controls}{result}<section class="grid">{listing}</section>{batch}<nav class="pager">{" ".join(pager)}</nav>{layers}',session.csrf,page_nav("images", session.csrf))

    @router.post("/images/tags")
    async def batch_tags(request: Request):
        _sid, _session, form = await write_auth(request)
        try:
            ids = {int(v) for v in form.getlist("image_ids") if str(v).isdigit()}
            if not ids:
                raise ValueError("请先选择至少一张图片")
            action = str(form.get("action", ""))
            if action not in {"add", "remove"}:
                raise ValueError("无效的标签操作")
            raw = str(form.get("tag_id", form.get("tag", ""))).strip()
            if not raw:
                raise ValueError("未选择标签")
            with db.get_conn(settings.database_path) as conn:
                row = (conn.execute("SELECT id FROM tags WHERE id=? AND enabled=1", (int(raw),)).fetchone()
                       if raw.isdigit() else conn.execute("SELECT id FROM tags WHERE slug=? COLLATE NOCASE AND enabled=1", (db.validate_slug(raw),)).fetchone())
                if row is None:
                    raise LookupError("标签不存在或已禁用")
                tag_id = int(row["id"])
                valid = {int(x[0]) for x in conn.execute(f"SELECT id FROM images WHERE source='local' AND id IN ({','.join('?' for _ in ids)})", tuple(ids))}
                if valid != ids:
                    raise LookupError("图片不存在")
                for iid in ids:
                    if action == "add":
                        conn.execute("INSERT OR IGNORE INTO image_tags(image_id,tag_id,created_at) VALUES(?,?,?)", (iid, tag_id, db.utc_now()))
                    else:
                        conn.execute("DELETE FROM image_tags WHERE image_id=? AND tag_id=?", (iid, tag_id))
            scan(request)
        except HTTPException:
            raise
        except (ValueError, LookupError, sqlite3.IntegrityError, OSError) as exc:
            return _error_page("批量标签操作失败", str(exc) if isinstance(exc, (ValueError, LookupError)) else "标签操作无法完成")
        return _redirect(f"{admin_path}/images?source=local", "标签已更新")

    @router.post("/images/{image_id}/tags")
    async def image_tags(image_id: int, request: Request):
        _sid, _session, form = await write_auth(request)
        action = str(form.get("action", ""))
        if action not in {"add", "remove"}:
            return _error_page("图片标签操作失败", "无效的标签操作", 400)
        raw_tag_id = str(form.get("tag_id", "")).strip()
        if not raw_tag_id.isdigit():
            return _error_page("图片标签操作失败", "未选择标签", 400)
        tag_id = int(raw_tag_id)
        try:
            with db.get_conn(settings.database_path) as conn:
                if conn.execute("SELECT 1 FROM images WHERE id=? AND source='local'", (image_id,)).fetchone() is None:
                    return _error_page("图片标签操作失败", "本地图片不存在", 404)
                if conn.execute("SELECT 1 FROM tags WHERE id=? AND enabled=1", (tag_id,)).fetchone() is None:
                    return _error_page("图片标签操作失败", "标签不存在或已禁用", 404)
                if action == "add":
                    conn.execute("INSERT OR IGNORE INTO image_tags(image_id,tag_id,created_at) VALUES(?,?,?)", (image_id, tag_id, db.utc_now()))
                else:
                    conn.execute("DELETE FROM image_tags WHERE image_id=? AND tag_id=?", (image_id, tag_id))
            scan(request)
        except (sqlite3.IntegrityError, OSError):
            return _error_page("图片标签操作失败", "标签操作无法完成，请稍后重试。", 503)
        return _redirect(f"{admin_path}/images?source=local", "图片标签已更新")

    @router.get("/tags", response_class=HTMLResponse)
    def tags(request: Request) -> HTMLResponse:
        _sid, session = authenticate(request)
        with db.get_conn(settings.database_path) as conn:
            rows = conn.execute("SELECT * FROM tags ORDER BY slug").fetchall()
            counts = {int(row["id"]): (int(row["local_count"]), int(row["remote_count"])) for row in conn.execute(
                "SELECT t.id, COUNT(DISTINCT it.image_id) local_count, COUNT(DISTINCT wt.href) remote_count "
                "FROM tags t LEFT JOIN image_tags it ON it.tag_id=t.id LEFT JOIN webdav_object_tags wt ON wt.tag_id=t.id GROUP BY t.id"
            )}
        items = []
        for row in rows:
            tag_id = int(row["id"]); slug = _escape(row["slug"]); name = _escape(row["display_name"])
            local_count, remote_count = counts.get(tag_id, (0, 0))
            edit = f'<form method="post" action="{_escape(admin_path)}/tags/{tag_id}/edit">{hidden(session.csrf)}<label>显示名称<input name="display_name" value="{name}" required maxlength="100"></label><label>Slug<input name="slug" value="{slug}" required maxlength="63"></label><button>保存编辑</button></form>'
            toggle = f'<form method="post" action="{_escape(admin_path)}/tags/{tag_id}/disable" onsubmit="return confirm(\'确定{"启用" if not row["enabled"] else "停用"}此标签？\')">{hidden(session.csrf)}<input type="hidden" name="enabled" value="{1-int(row["enabled"])}"><button class="secondary">{"启用标签" if not row["enabled"] else "停用标签"}</button></form>'
            merge = f'<form method="post" action="{_escape(admin_path)}/tags/{tag_id}/merge" onsubmit="return confirm(\'合并后源标签将被删除，确定继续？\')">{hidden(session.csrf)}<label>合并到<select name="target_id" required><option value="">选择目标标签</option>' + "".join(f'<option value="{int(target["id"])}">{_escape(target["display_name"])} — {_escape(target["slug"])}</option>' for target in rows if int(target["id"]) != tag_id) + '</select></label><button class="warning">合并并删除源标签</button></form>'
            status = "已启用" if row["enabled"] else "已停用"; status_class = "ok" if row["enabled"] else "off"
            items.append(f'<article class="panel"><div class="section-heading"><div><h3>{name}</h3><p class="muted">slug：<code>{slug}</code></p></div><span class="badge {status_class}">{status}</span></div><div class="stats"><div class="stat"><span>本地关联</span><strong>{local_count}</strong></div><div class="stat"><span>WebDAV 关联</span><strong>{remote_count}</strong></div></div><details><summary>编辑、启停或合并</summary><div class="workspace-grid tag-actions">{edit}{toggle}{merge}</div></details></article>')
        listing = "".join(items) or f'<section class="panel empty-state"><div class="empty-icon" aria-hidden="true">#</div><h3>还没有标签</h3><p class="muted">先创建第一个标签，上传时就能选择；标签 slug 之后可用于主题 API。</p><a class="button" href="#create-tag">创建第一个标签</a></section>'
        body = (f'<section class="panel"><p class="eyebrow">标签工作台</p><h2>显示名称给人看，slug 给 API 用</h2><p class="lead">显示名称可以是中文；slug 是唯一、稳定、仅含小写字母、数字和连字符的 API 标识。</p></section>'
                f'<section class="panel" id="create-tag"><h2>创建标签</h2><p class="muted">创建后可在上传和图片库中给图片打标签。标签为空时不能提交标签操作。</p><form method="post" class="form-grid">{hidden(session.csrf)}<label>显示名称<span class="label-note">例如：夏日风景</span><input name="display_name" required maxlength="100"></label><label>Slug<span class="label-note">例如：summer-landscape</span><input name="slug" required maxlength="63" pattern="[a-z0-9](?:[a-z0-9-]{{0,61}}[a-z0-9])?"></label><button>创建标签</button></form></section><div class="section-heading"><div><p class="eyebrow">已有标签</p><h2>标签列表</h2></div><span class="muted">共 {len(rows)} 个</span></div><section>{listing}</section>')
        return _page("标签工作台", body, session.csrf, page_nav("tags", session.csrf))

    @router.post("/tags")
    async def create_tag(request: Request):
        _sid, _session, form = await write_auth(request)
        try:
            with db.get_conn(settings.database_path) as conn:
                db.ensure_tag(conn, str(form.get("slug", "")), str(form.get("display_name", "")))
        except ValueError as exc:
            return _error_page("创建标签失败", "标签格式或名称无效，请检查后重试。")
        except (sqlite3.IntegrityError, OSError):
            return _error_page("创建标签失败", "标签无法保存，名称可能已存在，请检查后重试。")
        return _redirect(f"{admin_path}/tags", "标签已创建")

    @router.post("/tags/{tag_id}/edit")
    async def edit_tag(tag_id: int, request: Request):
        _sid, _session, form = await write_auth(request)
        try:
            slug = db.validate_slug(str(form.get("slug", "")))
            name = str(form.get("display_name", "")).strip()
            if not name or len(name) > 100:
                raise ValueError("invalid display name")
            with db.get_conn(settings.database_path) as conn:
                cursor = conn.execute("UPDATE tags SET slug=?,display_name=?,updated_at=? WHERE id=?", (slug, name, db.utc_now(), tag_id))
                if not cursor.rowcount:
                    return _error_page("编辑标签失败", "标签不存在", 404)
            scan(request)
        except ValueError:
            return _error_page("编辑标签失败", "标签格式或名称无效，请检查后重试。", 400)
        except sqlite3.IntegrityError:
            return _error_page("编辑标签失败", "标签无法保存，Slug 可能已存在。", 409)
        except (sqlite3.Error, OSError):
            return _error_page("编辑标签失败", "标签无法保存，请稍后重试。", 503)
        except (importer.ImportErrorBase, RuntimeError):
            return _error_page("编辑标签失败", "图库扫描失败，请稍后重试。", 503)
        return _redirect(f"{admin_path}/tags", "标签已编辑")

    @router.post("/tags/{tag_id}/disable")
    async def disable_tag(tag_id: int, request: Request):
        _sid, _session, form = await write_auth(request)
        enabled = 1 if str(form.get("enabled", "0")) == "1" else 0
        try:
            with db.get_conn(settings.database_path) as conn:
                if not conn.execute("UPDATE tags SET enabled=?,updated_at=? WHERE id=?", (enabled, db.utc_now(), tag_id)).rowcount:
                    return _error_page("更新标签失败", "标签不存在", 404)
            scan(request)
        except (sqlite3.Error, OSError):
            return _error_page("更新标签失败", "标签状态无法保存，请稍后重试。", 503)
        except (importer.ImportErrorBase, RuntimeError):
            return _error_page("更新标签失败", "图库扫描失败，请稍后重试。", 503)
        return _redirect(f"{admin_path}/tags", "标签状态已更新")

    @router.post("/tags/{tag_id}/merge")
    async def merge_tag(tag_id: int, request: Request):
        _sid, _session, form = await write_auth(request)
        try:
            target_id = int(str(form.get("target_id", "0")))
        except ValueError:
            return _error_page("合并标签失败", "请选择有效的目标标签", 400)
        if target_id <= 0 or target_id == tag_id:
            return _error_page("合并标签失败", "请选择不同的目标标签", 400)
        try:
            with db.get_conn(settings.database_path) as conn:
                if conn.execute("SELECT 1 FROM tags WHERE id=?", (target_id,)).fetchone() is None:
                    return _error_page("合并标签失败", "目标标签不存在", 404)
                conn.execute("INSERT OR IGNORE INTO image_tags(image_id,tag_id,created_at) SELECT image_id,?,created_at FROM image_tags WHERE tag_id=?", (target_id, tag_id))
                conn.execute("INSERT OR IGNORE INTO webdav_object_tags(href,tag_id,origin,created_at) SELECT href,?,origin,created_at FROM webdav_object_tags WHERE tag_id=?", (target_id, tag_id))
                if not conn.execute("DELETE FROM tags WHERE id=?", (tag_id,)).rowcount:
                    return _error_page("合并标签失败", "源标签不存在", 404)
            scan(request)
        except sqlite3.IntegrityError:
            return _error_page("合并标签失败", "标签无法合并，请检查目标标签。", 409)
        except (sqlite3.Error, OSError):
            return _error_page("合并标签失败", "标签无法合并，请稍后重试。", 503)
        except (importer.ImportErrorBase, RuntimeError):
            return _error_page("合并标签失败", "图库扫描失败，请稍后重试。", 503)
        return _redirect(f"{admin_path}/tags", "标签已合并")

    @router.post("/upload")
    async def upload(request: Request):
        _sid, _session, form = await write_auth(request)
        files = [item for item in form.getlist("files") if hasattr(item, "read")]
        if not files:
            one = form.get("file")
            files = [one] if hasattr(one, "read") else []
        if not files:
            raise HTTPException(400, "请选择至少一个图片文件。")
        try:
            selected_slugs = _selected_tag_slugs(form)
            with db.get_conn(settings.database_path) as conn:
                selected_tags = _require_existing_tags(conn, selected_slugs)
        except ValueError as exc:
            raise HTTPException(400, "所选标签无效或已停用，请重新选择。") from exc
        limits = _limits(settings)
        written: list[Path] = []
        digests: list[str] = []
        duplicates = 0
        try:
            for uploaded in files:
                payload = await uploaded.read(upload_max + 1)
                if len(payload) > upload_max:
                    raise HTTPException(413, "图片超过网页单文件上限，请减小文件或使用命令行导入。")
                try:
                    detected, width, height = importer._inspect_image(payload, limits.max_image_pixels)
                except importer.ArchiveSecurityError as exc:
                    raise HTTPException(400, "图片不符合安全限制，请减小图片或更换文件。") from exc
                except (UnidentifiedImageError, OSError, ValueError, SyntaxError) as exc:
                    raise HTTPException(400, "图片无效或格式不受支持。") from exc
                # Persist the visual orientation.  Catalog.read_image_meta reads
                # pixel dimensions directly, so retaining only the EXIF flag
                # would make a later scan reverse the admin upload classification.
                try:
                    with importer.pillow_guard(limits.max_image_pixels), Image.open(io.BytesIO(payload)) as source:
                        visual = ImageOps.exif_transpose(source)
                        visual.load()
                        normalized = io.BytesIO()
                        save_options = {"quality": 95} if detected == "JPEG" else {}
                        visual.save(normalized, format=detected, **save_options)
                    payload = normalized.getvalue()
                    width, height = visual.size
                except (UnidentifiedImageError, OSError, ValueError, SyntaxError) as exc:
                    raise HTTPException(400, "图片处理失败，请检查文件后重试。") from exc
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
            raise HTTPException(400, "请选择有效的目标目录。")
        images_root = Path(settings.images_dir).resolve()
        target_dir = images_root / target_group
        target_dir.mkdir(parents=True, exist_ok=True)
        if target_dir.is_symlink() or target_dir.resolve().parent != images_root:
            raise HTTPException(400, "目标目录无效。")
        with db.get_conn(settings.database_path) as conn:
            row = conn.execute(
                "SELECT rel_path,source FROM images WHERE id=?", (image_id,)
            ).fetchone()
            if row is None or row["source"] != "local":
                raise HTTPException(404, "本地图片不存在")
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
            raise HTTPException(400, "请确认后再删除图片。")
        quarantined: Path | None = None
        target: Path | None = None
        try:
            with db.get_conn(settings.database_path) as conn:
                row = conn.execute(
                    "SELECT rel_path,source FROM images WHERE id=?", (image_id,)
                ).fetchone()
                if row is None:
                    raise HTTPException(404, "图片不存在")
                if row["source"] != "local":
                    raise HTTPException(400, "只能删除本地图片。")
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
                raise HTTPException(404, "本地图片不存在")
        scan(request)
        return _redirect(f"{admin_path}/images?source=local", "本地图片状态已更新")

    @router.post("/webdav/enabled")
    async def webdav_enabled(request: Request):
        _sid, _session, form = await write_auth(request)
        href = str(form.get("href", ""))
        enabled = 1 if str(form.get("enabled", "0")) == "1" else 0
        with db.get_conn(settings.database_path) as conn:
            if not conn.execute("UPDATE webdav_objects SET enabled=? WHERE href=?", (enabled, href)).rowcount:
                raise HTTPException(404, "WebDAV 图片不存在")
        return _redirect(f"{admin_path}/images?source=webdav", "WebDAV 状态已更新")

    async def update_webdav_tag(request: Request, force_remove: bool = False):
        _sid, _session, form = await write_auth(request)
        href = str(form.get("href", ""))
        action = "remove" if force_remove else str(form.get("action", ""))
        if action not in {"add", "remove"}:
            return _error_page("WebDAV 标签操作失败", "无效的标签操作", 400)
        raw_tag_id = str(form.get("tag_id", "")).strip()
        if not raw_tag_id.isdigit():
            return _error_page("WebDAV 标签操作失败", "未选择标签", 400)
        tag_id = int(raw_tag_id)
        try:
            with db.get_conn(settings.database_path) as conn:
                if conn.execute("SELECT 1 FROM webdav_objects WHERE href=?", (href,)).fetchone() is None:
                    return _error_page("WebDAV 标签操作失败", "WebDAV 图片不存在", 404)
                if conn.execute("SELECT 1 FROM tags WHERE id=? AND enabled=1", (tag_id,)).fetchone() is None:
                    return _error_page("WebDAV 标签操作失败", "标签不存在或已禁用", 404)
                if action == "add":
                    conn.execute("INSERT OR IGNORE INTO webdav_object_tags(href,tag_id,origin,created_at) VALUES(?,?,'admin',?)", (href, tag_id, db.utc_now()))
                else:
                    conn.execute("DELETE FROM webdav_object_tags WHERE href=? AND tag_id=?", (href, tag_id))
        except (sqlite3.IntegrityError, OSError):
            return _error_page("WebDAV 标签操作失败", "标签操作无法完成，请稍后重试。", 503)
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
                try:
                    candidate = _safe_db_file(cache_root, str(row["cache_name"]))
                except HTTPException:
                    continue
                if candidate.is_file():
                    candidate.unlink(); removed += 1
            conn.execute("DELETE FROM webdav_cache")
        return _redirect(admin_path, f"缓存已清理 {removed}")

    def render_archive_preview(token: str, pending: _Preview, session: _Session) -> HTMLResponse:
        summary = pending.summary
        hints = sorted({hint for _digest, hint in pending.entries if hint})
        choices = "".join(
            f'<label><input type="checkbox" name="map_dirs" value="{_escape(hint)}" checked> 映射目录 {_escape(hint)}</label><br>'
            for hint in hints
        )
        default_value = pending.selected_default_tag
        with db.get_conn(settings.database_path) as conn:
            tag_rows = conn.execute("SELECT slug,display_name FROM tags WHERE enabled=1 ORDER BY slug").fetchall()
        default_options = '<option value="">不添加标签（可选）</option>' + "".join(
            f'<option value="{_escape(row["slug"])}"{" selected" if str(row["slug"]).lower() == default_value.lower() else ""}>{_escape(row["display_name"])} — {_escape(row["slug"])}</option>'
            for row in tag_rows
        )
        tag_choices = "".join(
            f'<label><input type="checkbox" name="tags" value="{_escape(row["slug"])}"'
            f'{" checked" if str(row["slug"]) in pending.selected_tags else ""}> '
            f'{_escape(row["display_name"])} — {_escape(row["slug"])}</label><br>'
            for row in tag_rows if str(row["slug"]).lower() != default_value.lower()
        ) or f'<p class="muted">没有其他可选标签，可先到 <a href="{admin_path}/tags">标签工作台</a> 创建。</p>'
        summary_items = (
            ("归档内容", summary["members"]), ("检查图片", summary["files_examined"]),
            ("可导入", summary["imported"]), ("重复", summary["duplicates"]),
            ("跳过", summary["skipped"]), ("横屏", summary["desktop"]),
            ("竖屏", summary["mobile"]), ("方形", summary["square"]),
        )
        summary_html = "".join(f"<dt>{_escape(label)}</dt><dd>{int(value)}</dd>" for label, value in summary_items)
        body = (
            '<p class="muted">请检查导入内容和标签；确认前仍可调整本次全部图片的标签。</p>'
            f'<section aria-labelledby="archive-summary-title"><h2 id="archive-summary-title">导入摘要</h2><dl class="detail-grid">{summary_html}</dl></section>'
            f'<form method="post" action="{admin_path}/archives/confirm">{hidden(session.csrf)}'
            f'<input type="hidden" name="token" value="{_escape(token)}">'
            f'<input type="hidden" name="map_dirs_present" value="1">'
            f'<label>本次所有图片的主标签（可选） <select name="default_tag">{default_options}</select></label>'
            '<input type="hidden" name="tags_present" value="1"><br>'
            f'<fieldset><legend>追加标签（可多选）</legend>{tag_choices}</fieldset>'
            f'{choices}<button>确认导入</button></form>'
        )
        return _page("归档预览", body, session.csrf)

    def restore_persistent_preview(task_id: str, sid: str, owner_key: str) -> _Preview:
        row = upload_store.status(task_id, owner_key)
        if row["state"] != "preview_ready":
            raise UploadError(409, "preview_not_ready", "归档尚未完成校验。", int(row["committed_offset"]))
        _directory, path = upload_store._paths(task_id)
        pending = _Preview(
            sid, path, float(row["expires_at"]), int(row["expected_size"]), str(row["archive_sha256"]),
            json.loads(str(row["summary_json"])), [tuple(item) for item in json.loads(str(row["entries_json"]))],
            str(row["selected_default_tag"]), tuple(json.loads(str(row["selected_tags_json"]))), True, owner_key,
        )
        return pending

    @router.get("/archives/uploads/capabilities")
    def chunked_capabilities(request: Request):
        try:
            upload_api_auth(request)
            return JSONResponse(upload_store.capabilities(), headers={"Cache-Control": "no-store"})
        except UploadError as exc:
            return upload_error(exc)

    def raw_content_length(
        request: Request,
        *,
        limit: int,
        too_large_code: str = "request_too_large",
        too_large_message: str = "请求内容超过上限。",
    ) -> int:
        raw = list(request.scope.get("headers", []))
        lengths = [value for name, value in raw if name.lower() == b"content-length"]
        transfers = [value for name, value in raw if name.lower() == b"transfer-encoding"]
        if transfers:
            raise UploadError(400, "chunked_transfer_forbidden", "请求必须提供确定大小。")
        if not lengths:
            raise UploadError(411, "content_length_required", "请求缺少大小信息。")
        if len(lengths) != 1:
            raise UploadError(400, "invalid_content_length", "请求大小信息无效。")
        value = lengths[0]
        if len(value) > 20 or not value.isascii() or not value.isdigit():
            raise UploadError(400, "invalid_content_length", "请求大小信息无效。")
        declared = int(value)
        if declared > limit:
            raise UploadError(413, too_large_code, too_large_message)
        return declared

    async def bounded_body(
        request: Request,
        declared: int,
        limit: int,
        *,
        too_large_code: str = "request_too_large",
        too_large_message: str = "请求内容超过上限。",
    ) -> bytes:
        chunks: list[bytes] = []
        received = 0
        async for chunk in request.stream():
            received += len(chunk)
            if received > declared or received > limit:
                raise UploadError(413, too_large_code, too_large_message)
            chunks.append(chunk)
        if received != declared:
            raise UploadError(400, "content_length_mismatch", "请求内容长度不一致。")
        return b"".join(chunks)

    @router.post("/archives/uploads")
    async def chunked_create(request: Request):
        try:
            _sid, _session, owner_key = upload_api_auth(request, write=True)
            declared = raw_content_length(request, limit=64 * 1024)
            raw_body = await bounded_body(request, declared, 64 * 1024)
            try:
                payload = json.loads(raw_body)
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise UploadError(400, "invalid_json", "JSON 请求无效。") from exc
            if not isinstance(payload, dict):
                raise UploadError(400, "invalid_request", "上传信息无效，请重新选择归档。")
            if not set(payload) <= {"filename", "size", "fingerprint", "default_tag", "tags"}:
                raise UploadError(400, "invalid_fields", "上传字段无效。")
            filename = payload.get("filename")
            fingerprint = payload.get("fingerprint", "")
            selected_default = payload.get("default_tag", "")
            selected_values = payload.get("tags", [])
            length = payload.get("size")
            if not isinstance(filename, str) or not filename or len(filename) > 255 or "\x00" in filename:
                raise UploadError(400, "invalid_fields", "文件名无效。")
            if isinstance(length, bool) or not isinstance(length, int):
                raise UploadError(400, "invalid_fields", "归档大小无效。")
            if not isinstance(fingerprint, str) or len(fingerprint) > 128 or (fingerprint and not re.fullmatch(r"[0-9a-f]{64}", fingerprint)):
                raise UploadError(400, "invalid_fields", "文件指纹无效。")
            if not isinstance(selected_default, str) or len(selected_default) > 63:
                raise UploadError(400, "invalid_fields", "主标签无效。")
            if not isinstance(selected_values, list) or len(selected_values) > 64 or any(not isinstance(value, str) or len(value) > 63 for value in selected_values):
                raise UploadError(400, "invalid_fields", "附加标签无效。")
            selected_default = selected_default.strip()
            selected_tags = tuple(dict.fromkeys(value.strip() for value in selected_values if value.strip()))
            lower = filename.lower()
            suffix = ".tar.gz" if lower.endswith(".tar.gz") else ".tgz" if lower.endswith(".tgz") else ".zip" if lower.endswith(".zip") else ""
            with db.get_conn(settings.database_path) as conn:
                _require_existing_tags(conn, [value for value in (selected_default, *selected_tags) if value])
            row = await run_in_threadpool(upload_store.create, owner_key, filename, suffix, length, selected_default, selected_tags, fingerprint)
            location = f"{admin_path}/archives/uploads/{row['id']}"
            return JSONResponse({"id": row["id"], "location": location, **upload_store.capabilities()}, status_code=201, headers={**upload_headers(row), "Location": location})
        except UploadError as exc:
            return upload_error(exc)
        except (ValueError, TypeError):
            return upload_error(UploadError(400, "invalid_fields", "上传字段无效，请重新选择归档。"))

    @router.head("/archives/uploads/{task_id}")
    def chunked_status(task_id: str, request: Request):
        try:
            _sid, _session, owner_key = upload_api_auth(request)
            row = upload_store.status(task_id, owner_key)
            return Response(status_code=204, headers=upload_headers(row))
        except UploadError as exc:
            return upload_error(exc)

    @router.patch("/archives/uploads/{task_id}")
    async def chunked_patch(task_id: str, request: Request):
        try:
            _sid, _session, owner_key = upload_api_auth(request, write=True)
            declared = raw_content_length(
                request,
                limit=upload_store.maximum,
                too_large_code="chunk_too_large",
                too_large_message="当前分片过大，请使用更小分片重试。",
            )
            raw_offsets = [value for name, value in request.scope.get("headers", []) if name.lower() == b"upload-offset"]
            if len(raw_offsets) != 1 or len(raw_offsets[0]) > 20 or not raw_offsets[0].isascii() or not raw_offsets[0].isdigit():
                raise UploadError(400, "invalid_headers", "分片上传信息无效，请刷新后重试。")
            if request.headers.get("content-type", "").partition(";")[0].strip().lower() != "application/offset+octet-stream":
                raise UploadError(415, "unsupported_chunk_type", "分片格式不受支持，请刷新页面后重试。")
            row = await upload_store.append(
                task_id,
                owner_key,
                request,
                int(raw_offsets[0]),
                declared,
            )
            return Response(status_code=204, headers=upload_headers(row))
        except UploadError as exc:
            return upload_error(exc)

    def validate_chunked_archive(task_id: str, owner_key: str) -> Any:
        with upload_store.locked(task_id, owner_key) as (row, _directory, path, handle):
            row = upload_store._reconcile_locked(row, handle)
            if row["state"] == "preview_ready":
                return row
            if row["state"] != "receiving" or int(row["committed_offset"]) != int(row["expected_size"]):
                raise UploadError(409, "upload_incomplete", "归档尚未上传完成。", int(row["committed_offset"]))
            digest = hashlib.sha256()
            total = 0
            handle.seek(0)
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                total += len(chunk)
                digest.update(chunk)
            if total != int(row["expected_size"]):
                raise UploadError(409, "upload_length_mismatch", "归档长度校验失败，请重新上传。", int(row["committed_offset"]))
            limits = _limits(settings)
            if total > limits.max_archive_bytes:
                raise UploadError(413, "archive_limit_exceeded", "归档超过导入校验上限，请调整配置后重试。")
            with importer.global_import_lock(upload_tmp_dir):
                archive_suffix = str(row["suffix"])
                result = importer.import_archive(
                    path,
                    settings.images_dir,
                    dry_run=True,
                    square_policy=settings.square_policy,
                    limits=limits,
                    archive_suffix=archive_suffix,
                )
                entries = _archive_entries(path, limits, archive_suffix)
                return upload_store.mark_preview(
                    task_id,
                    owner_key,
                    digest.hexdigest(),
                    result.to_dict(),
                    entries,
                    result.import_required_bytes,
                )

    @router.post("/archives/uploads/{task_id}/complete")
    async def chunked_complete(task_id: str, request: Request):
        owner_key: str | None = None

        def record_completion_error(code: str) -> None:
            if owner_key is None:
                return
            try:
                upload_store.record_error(task_id, owner_key, code)
            except Exception:
                logger.exception(
                    "chunked upload error state could not be recorded",
                    extra={"upload_task_id": task_id, "upload_error_code": code},
                )

        try:
            _sid, _session, owner_key = upload_api_auth(request, write=True)
            row = await run_in_threadpool(validate_chunked_archive, task_id, owner_key)
            json.loads(str(row["summary_json"]))
            [tuple(item) for item in json.loads(str(row["entries_json"]))]
            return JSONResponse({"status": "preview_ready", "preview_url": f"{admin_path}/archives/uploads/{task_id}/preview"}, headers=upload_headers(row))
        except UploadError as exc:
            return upload_error(exc)
        except importer.ImportStorageError as exc:
            logger.warning(
                "chunked archive validation lacked storage",
                extra={"upload_task_id": task_id, "error_type": type(exc).__name__},
                exc_info=True,
            )
            record_completion_error("insufficient_storage")
            return upload_error(UploadError(507, "insufficient_storage", "存储空间不足，请清理空间后重试；上传任务已保留。"))
        except (importer.ImportErrorBase, ValueError, zipfile.BadZipFile, tarfile.TarError, OSError, json.JSONDecodeError) as exc:
            logger.warning(
                "chunked archive validation failed",
                extra={"upload_task_id": task_id, "error_type": type(exc).__name__},
                exc_info=True,
            )
            record_completion_error("invalid_archive")
            return upload_error(UploadError(400, "invalid_archive", "无法读取归档，请检查格式和内容后重试或取消任务。"))

    @router.get("/archives/uploads/{task_id}/preview")
    def chunked_preview(task_id: str, request: Request):
        sid, session = authenticate(request)
        owner_key = upload_owner(request)
        try:
            pending = restore_persistent_preview(task_id, sid, owner_key)
            return render_archive_preview(task_id, pending, session)
        except UploadError as exc:
            raise HTTPException(exc.status, exc.message) from exc

    @router.delete("/archives/uploads/{task_id}")
    def chunked_cancel(task_id: str, request: Request):
        try:
            _sid, _session, owner_key = upload_api_auth(request, write=True)
            previews.pop(task_id, None)
            upload_store.delete(task_id, owner_key)
            return Response(status_code=204, headers={"Cache-Control": "no-store"})
        except UploadError as exc:
            return upload_error(exc)

    @router.post("/archives/preview")
    async def archive_preview(request: Request):
        sid, _session, form = await write_auth(request)
        uploaded = form.get("archive")
        if not hasattr(uploaded, "read"):
            raise HTTPException(400, "请选择一个归档文件。")
        filename = str(getattr(uploaded, "filename", "")).lower()
        suffix = ".tar.gz" if filename.endswith(".tar.gz") else ".tgz" if filename.endswith(".tgz") else ".zip" if filename.endswith(".zip") else ""
        if not suffix:
            raise HTTPException(400, "仅支持 ZIP、TAR.GZ、TGZ 归档。")
        try:
            selected_slugs = _selected_tag_slugs(form)
            selected_default_tag = str(form.get("default_tag", "")).strip()
            selected_extra_tags = tuple(
                slug for slug in selected_slugs if slug != selected_default_tag
            )
            with db.get_conn(settings.database_path) as conn:
                _require_existing_tags(conn, selected_slugs)
        except ValueError as exc:
            raise HTTPException(400, "所选标签无效或已停用，请重新选择。") from exc
        limits = _limits(settings)
        upload_tmp_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if upload_tmp_dir.is_symlink() or not upload_tmp_dir.is_dir():
            raise HTTPException(400, "上传临时目录不可用。")
        os.chmod(upload_tmp_dir, 0o700)
        token = secrets.token_urlsafe(32)
        path = upload_tmp_dir / f"{token}{suffix}"
        try:
            archive_size, archive_digest = await run_in_threadpool(
                _stage_spooled_upload,
                uploaded,
                path,
                limit=multipart_archive_max,
                min_free=upload_store.min_free,
                reserved_bytes=upload_store.receiving_reserved_bytes(),
            )
            with importer.global_import_lock(upload_tmp_dir):
                summary = importer.import_archive(path, settings.images_dir, dry_run=True, square_policy=settings.square_policy, limits=limits)
                entries = _archive_entries(path, limits)
            previews[token] = _Preview(
                sid,
                path,
                time.time() + preview_ttl,
                archive_size,
                archive_digest,
                summary.to_dict(),
                entries,
                selected_default_tag,
                selected_extra_tags,
            )
        except HTTPException:
            path.unlink(missing_ok=True)
            raise
        except (importer.ImportErrorBase, ValueError, zipfile.BadZipFile, tarfile.TarError, OSError) as exc:
            path.unlink(missing_ok=True)
            raise HTTPException(400, "无法读取归档，请检查格式和内容后重试。") from exc
        return render_archive_preview(token, previews[token], _session)

    def perform_archive_confirm(
        request: Request,
        token: str,
        pending: _Preview,
        selected_slugs: list[str],
        selected_hints: set[str],
        owner_key: str,
    ) -> Any:
        def import_locked(
            path: Path,
            handle: Any | None = None,
            archive_suffix: str | None = None,
        ) -> Any:
            if handle is None:
                current_size = path.stat().st_size
                current_digest = hashlib.sha256()
                with path.open("rb") as archive_handle:
                    for chunk in iter(lambda: archive_handle.read(1024 * 1024), b""):
                        current_digest.update(chunk)
            else:
                current_size = os.fstat(handle.fileno()).st_size
                current_digest = hashlib.sha256()
                handle.seek(0)
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    current_digest.update(chunk)
            if current_size != pending.archive_size or not hmac.compare_digest(
                current_digest.hexdigest(), pending.archive_sha256
            ):
                raise UploadError(400, "preview_changed", "预览内容已变化，请重新上传归档。")
            with db.get_conn(settings.database_path) as conn:
                try:
                    selected_tag_ids = list(_require_existing_tags(conn, selected_slugs).values())
                except ValueError as exc:
                    raise UploadError(400, "invalid_tags", "所选标签无效或已停用，请重新上传归档。") from exc
            summary = None
            try:
                limits = _limits(settings)
                # The preview reservation can become stale while another task imports
                # the same digest. Refresh it under the global import lock so capacity
                # accounts only for bytes that this confirmation would add now.
                current_preview = importer.import_archive(
                    path,
                    settings.images_dir,
                    dry_run=True,
                    square_policy=settings.square_policy,
                    limits=limits,
                    archive_suffix=archive_suffix,
                )
                upload_store.ensure_import_capacity(token, current_preview.import_required_bytes)
                summary = importer.import_archive(
                    path,
                    settings.images_dir,
                    dry_run=False,
                    square_policy=settings.square_policy,
                    limits=limits,
                    archive_suffix=archive_suffix,
                )
                scan(request)
                with db.get_conn(settings.database_path) as conn:
                    for digest, hint in pending.entries:
                        row = conn.execute("SELECT id FROM images WHERE content_hash=?", (digest,)).fetchone()
                        if row is None:
                            continue
                        for tag_id in selected_tag_ids:
                            conn.execute(
                                "INSERT OR IGNORE INTO image_tags(image_id,tag_id,created_at) VALUES(?,?,?)",
                                (row["id"], tag_id, db.utc_now()),
                            )
                        if hint and hint in selected_hints:
                            hint_id = db.ensure_tag(conn, hint)
                            conn.execute(
                                "INSERT OR IGNORE INTO image_tags(image_id,tag_id,created_at) VALUES(?,?,?)",
                                (row["id"], hint_id, db.utc_now()),
                            )
                scan(request)
                return summary
            except Exception:
                images_root = Path(settings.images_dir).resolve()
                if summary is not None:
                    for raw_path in summary.created_paths:
                        candidate = Path(raw_path)
                        try:
                            resolved = candidate.resolve(strict=False)
                            resolved.relative_to(images_root)
                        except (OSError, ValueError):
                            continue
                        if candidate.is_symlink():
                            continue
                        candidate.unlink(missing_ok=True)
                try:
                    scan(request)
                except Exception:
                    pass
                raise

        if pending.persistent:
            with upload_store.locked(token, owner_key) as (row, directory, path, handle):
                if row["state"] != "preview_ready":
                    raise UploadError(409, "preview_not_ready", "归档尚未完成校验。")
                upload_store._reconcile_locked(row, handle)
                with importer.global_import_lock(upload_tmp_dir):
                    summary = import_locked(path, handle, str(row["suffix"]))
                    upload_store._delete_locked(token, owner_key, directory)
                    return summary
        with importer.global_import_lock(upload_tmp_dir):
            summary = import_locked(pending.archive_path)
            pending.archive_path.unlink(missing_ok=True)
            return summary

    @router.post("/archives/confirm")
    async def archive_confirm(request: Request):
        sid, _session, form = await write_auth(request)
        token = str(form.get("token", ""))
        owner_key = ""
        pending = previews.get(token)
        if pending is None or pending.persistent:
            try:
                owner_key = upload_owner(request)
                pending = await run_in_threadpool(restore_persistent_preview, token, sid, owner_key)
            except UploadError as exc:
                raise HTTPException(exc.status, exc.message) from exc
        if pending.expires_at <= time.time():
            discard_preview(token, pending)
            raise HTTPException(410, "预览已失效，请重新上传归档。")
        if not pending.persistent and not hmac.compare_digest(pending.session_id, sid):
            raise HTTPException(403, "无法确认此预览，请重新上传归档。")
        if not pending.persistent:
            with previews_lock:
                if token in confirming_tokens or previews.get(token) is not pending:
                    raise HTTPException(409, "此预览正在确认或已被处理。")
                confirming_tokens.add(token)
        try:
            if "default_tag" not in form and "tags_present" not in form:
                form = FormData([
                    *form.multi_items(),
                    ("default_tag", pending.selected_default_tag),
                    *(("tags", slug) for slug in pending.selected_tags),
                ])
            selected_slugs = _selected_tag_slugs(form)
            available_hints = {hint for _digest, hint in pending.entries if hint}
            if str(form.get("map_dirs_present", "")) == "1":
                selected_hints = {db.validate_slug(str(value)) for value in form.getlist("map_dirs")}
                if not selected_hints <= available_hints:
                    raise ValueError("invalid directory tag")
            else:
                selected_hints = available_hints
        except ValueError as exc:
            with previews_lock:
                confirming_tokens.discard(token)
            discard_failed_preview(token, pending)
            raise HTTPException(400, "归档选项无效，请重新上传并预览。") from exc
        succeeded = False
        try:
            try:
                summary = await run_in_threadpool(
                    perform_archive_confirm, request, token, pending, selected_slugs, selected_hints, owner_key
                )
                succeeded = True
            except importer.ImportStorageError as exc:
                discard_failed_preview(token, pending)
                raise HTTPException(507, "存储空间不足，请清理空间后重试；上传任务已保留。") from exc
            except UploadError as exc:
                discard_failed_preview(token, pending)
                raise HTTPException(exc.status, exc.message) from exc
            except Exception:
                discard_failed_preview(token, pending)
                raise
        finally:
            with previews_lock:
                if succeeded:
                    previews.pop(token, None)
                confirming_tokens.discard(token)
        if not pending.persistent:
            pending.archive_path.unlink(missing_ok=True)
        return _redirect(admin_path, f"归档导入 {summary.imported}")

    return router
