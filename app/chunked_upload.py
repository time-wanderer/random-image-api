"""Persistent, sequential archive upload tasks backed by SQLite and one file."""
from __future__ import annotations

import errno
import fcntl
import json
import os
import re
import secrets
import shutil
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app import db

_TASK_ID = re.compile(r"^[0-9a-f]{48}$")
_ACTIVE_STATES = ("receiving", "preview_ready")
_ORPHAN_GRACE_SECONDS = 60


@dataclass(slots=True)
class UploadError(Exception):
    status: int
    code: str
    message: str
    offset: int | None = None


class UploadStore:
    def __init__(self, settings: Any):
        self.database_path = Path(settings.database_path)
        self.root = Path(settings.upload_tmp_dir) / "chunked"
        self.images_root = Path(getattr(settings, "images_dir", self.root))
        self.enabled = bool(getattr(settings, "admin_chunked_upload_enabled", True))
        self.recommended = int(getattr(settings, "admin_chunk_recommended_bytes", 8 * 1024 * 1024))
        self.minimum = int(getattr(settings, "admin_chunk_min_bytes", 1024 * 1024))
        self.maximum = int(getattr(settings, "admin_chunk_max_bytes", 16 * 1024 * 1024))
        self.max_upload = int(getattr(settings, "admin_chunked_max_upload_bytes", 8 * 1024**3))
        self.ttl = int(getattr(settings, "admin_chunked_upload_ttl_seconds", 86_400))
        self.max_active = int(getattr(settings, "admin_chunked_max_active_tasks", 2))
        self.max_inflight_patches = int(getattr(settings, "admin_chunked_max_inflight_patches", 2))
        self.min_free = int(getattr(settings, "admin_chunked_min_free_bytes", 256 * 1024 * 1024))
        self._inflight_lock = threading.Lock()
        self._inflight_tasks: set[str] = set()
        self._inflight_count = 0
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.root.is_symlink() or not self.root.is_dir():
            raise RuntimeError("unsafe upload temporary directory")
        os.chmod(self.root, 0o700)
        self.images_root.mkdir(parents=True, exist_ok=True)
        # Ensure migration exists even when the catalog startup scan is disabled.
        with db.get_conn(self.database_path):
            pass
        self.clean()

    def capabilities(self) -> dict[str, int | bool]:
        return {
            "enabled": self.enabled,
            "recommended_chunk_bytes": self.recommended,
            "min_chunk_bytes": self.minimum,
            "max_chunk_bytes": self.maximum,
            "max_upload_bytes": self.max_upload,
            "ttl_seconds": self.ttl,
            "max_inflight_patches": self.max_inflight_patches,
        }

    def receiving_reserved_bytes(self) -> int:
        """Return uncommitted bytes promised to active receiving tasks."""
        with db.get_conn(self.database_path) as conn:
            return int(conn.execute(
                "SELECT COALESCE(SUM(expected_size-committed_offset),0) FROM upload_tasks "
                "WHERE state='receiving' AND expires_at>?",
                (time.time(),),
            ).fetchone()[0])

    @contextmanager
    def patch_slot(self, task_id: str):
        """Reject duplicate/global excess PATCH requests before reading their body."""
        with self._inflight_lock:
            if task_id in self._inflight_tasks:
                raise UploadError(409, "upload_in_progress", "此上传任务正在接收另一个分片，请稍后重试。")
            if self._inflight_count >= self.max_inflight_patches:
                raise UploadError(429, "too_many_inflight_chunks", "当前正在接收的分片过多，请稍后重试。")
            self._inflight_tasks.add(task_id)
            self._inflight_count += 1
        try:
            yield
        finally:
            with self._inflight_lock:
                self._inflight_tasks.discard(task_id)
                self._inflight_count -= 1

    def _paths(self, task_id: str) -> tuple[Path, Path]:
        if not _TASK_ID.fullmatch(task_id):
            raise UploadError(404, "upload_not_found", "上传任务不存在或已失效。")
        root = self.root.resolve()
        directory = root / task_id
        # Keep the final component lexical: resolving it could follow a malicious
        # symlink into another task directory before containment is checked.
        if directory.parent != root or directory.is_symlink():
            raise UploadError(404, "upload_not_found", "上传任务不存在或已失效。")
        return directory, directory / "upload.bin"

    def _row(self, task_id: str, owner_key: str) -> Any:
        with db.get_conn(self.database_path) as conn:
            row = conn.execute(
                "SELECT * FROM upload_tasks WHERE id=? AND owner_key=?", (task_id, owner_key)
            ).fetchone()
        if row is None:
            raise UploadError(404, "upload_not_found", "上传任务不存在或已失效。")
        if float(row["expires_at"]) <= time.time():
            self.delete(task_id, owner_key, missing_ok=True)
            raise UploadError(410, "upload_expired", "上传任务已过期，请重新开始。")
        return row

    def create(
        self,
        owner_key: str,
        original_name: str,
        suffix: str,
        expected_size: int,
        selected_default_tag: str = "",
        selected_tags: tuple[str, ...] = (),
        client_fingerprint: str = "",
    ) -> Any:
        self.clean()
        if not self.enabled:
            raise UploadError(404, "chunked_upload_disabled", "分片上传未启用。")
        if expected_size <= 0:
            raise UploadError(400, "invalid_upload_length", "归档大小必须大于零。")
        if expected_size > self.max_upload:
            raise UploadError(413, "upload_too_large", "归档超过应用总上传上限。")
        if suffix not in {".zip", ".tar.gz", ".tgz"}:
            raise UploadError(400, "unsupported_archive", "仅支持 ZIP、TAR.GZ、TGZ 归档。")
        now = time.time()
        task_id = secrets.token_hex(24)
        directory, path = self._paths(task_id)
        try:
            with db.get_conn(self.database_path) as conn:
                # connect() may have run idempotent migration statements; close that
                # transaction before taking the quota reservation write lock.
                conn.commit()
                # Serialize quota reservation across processes before observing counts.
                conn.execute("BEGIN IMMEDIATE")
                active = int(conn.execute(
                    "SELECT COUNT(*) FROM upload_tasks WHERE state IN ('receiving','preview_ready') AND expires_at>?",
                    (now,),
                ).fetchone()[0])
                if active >= self.max_active:
                    raise UploadError(429, "too_many_active_uploads", "当前上传任务已满，请稍后重试或取消旧任务。")
                reserved = int(conn.execute(
                    "SELECT COALESCE(SUM(expected_size-committed_offset),0) FROM upload_tasks "
                    "WHERE state IN ('receiving','preview_ready') AND expires_at>?",
                    (now,),
                ).fetchone()[0])
                if shutil.disk_usage(self.root).free - reserved - expected_size < self.min_free:
                    raise UploadError(507, "insufficient_storage", "可用空间不足，请清理空间后重试。")
                directory.mkdir(mode=0o700)
                os.chmod(directory, 0o700)
                file_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
                fd = os.open(path, file_flags, 0o600)
                try:
                    os.fchmod(fd, 0o600)
                    os.fsync(fd)
                finally:
                    os.close(fd)
                conn.execute(
                    """INSERT INTO upload_tasks
                    (id,owner_key,original_name,suffix,expected_size,committed_offset,state,
                     selected_default_tag,selected_tags_json,client_fingerprint,created_at,updated_at,expires_at)
                    VALUES(?,?,?,?,?,0,'receiving',?,?,?,?,?,?)""",
                    (task_id, owner_key, original_name[:255], suffix, expected_size,
                     selected_default_tag, json.dumps(selected_tags), client_fingerprint[:128], now, now, now + self.ttl),
                )
        except Exception as exc:
            if directory.exists() and directory.parent == self.root.resolve() and not directory.is_symlink():
                shutil.rmtree(directory, ignore_errors=True)
            if isinstance(exc, OSError) and exc.errno in {errno.ENOSPC, getattr(errno, "EDQUOT", errno.ENOSPC)}:
                raise UploadError(507, "insufficient_storage", "可用空间不足，请清理空间后重试。") from exc
            raise
        return self._row(task_id, owner_key)

    @contextmanager
    def locked(self, task_id: str, owner_key: str, *, nonblocking: bool = False):
        directory, path = self._paths(task_id)
        try:
            flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(path, flags)
            try:
                handle = os.fdopen(fd, "r+b", closefd=True)
            except BaseException:
                os.close(fd)
                raise
        except (FileNotFoundError, NotADirectoryError):
            with db.get_conn(self.database_path) as conn:
                row = conn.execute(
                    "SELECT 1 FROM upload_tasks WHERE id=? AND owner_key=?", (task_id, owner_key)
                ).fetchone()
                if row is not None:
                    conn.execute("DELETE FROM upload_tasks WHERE id=? AND owner_key=?", (task_id, owner_key))
            if row is None:
                raise UploadError(404, "upload_not_found", "上传任务不存在或已失效。")
            raise UploadError(410, "upload_data_lost", "上传数据不可用，请重新开始。")
        except OSError as exc:
            raise UploadError(410, "upload_data_lost", "上传数据不可用，请重新开始。") from exc
        try:
            flags = fcntl.LOCK_EX | (fcntl.LOCK_NB if nonblocking else 0)
            fcntl.flock(handle.fileno(), flags)
            with db.get_conn(self.database_path) as conn:
                row = conn.execute(
                    "SELECT * FROM upload_tasks WHERE id=? AND owner_key=?", (task_id, owner_key)
                ).fetchone()
            if row is None:
                raise UploadError(404, "upload_not_found", "上传任务不存在或已失效。")
            yield row, directory, path, handle
        finally:
            handle.close()

    def _reconcile_locked(self, row: Any, handle: Any) -> Any:
        expected_offset = int(row["committed_offset"])
        actual = os.fstat(handle.fileno()).st_size
        if actual < expected_offset:
            raise UploadError(410, "upload_data_lost", "上传数据不可用，请重新开始。")
        if actual > expected_offset:
            handle.truncate(expected_offset)
            handle.flush()
            os.fsync(handle.fileno())
        return row

    def status(self, task_id: str, owner_key: str) -> Any:
        try:
            with self.locked(task_id, owner_key) as (row, _directory, _path, handle):
                if float(row["expires_at"]) <= time.time():
                    expired = True
                else:
                    expired = False
                    row = self._reconcile_locked(row, handle)
        except UploadError as exc:
            if exc.code == "upload_data_lost":
                self.delete(task_id, owner_key, missing_ok=True)
            raise
        if expired:
            self.delete(task_id, owner_key, missing_ok=True)
            raise UploadError(410, "upload_expired", "上传任务已过期，请重新开始。")
        return row

    def append_bytes(self, task_id: str, owner_key: str, payload: bytes, supplied_offset: int, declared: int) -> Any:
        """Commit an already-bounded chunk while holding only synchronous locks."""
        if declared <= 0:
            raise UploadError(400, "invalid_chunk_length", "分片大小信息无效。")
        if declared > self.maximum or len(payload) > self.maximum:
            raise UploadError(413, "chunk_too_large", "当前分片过大，请使用更小分片重试。")
        if len(payload) != declared:
            raise UploadError(400, "chunk_length_mismatch", "分片接收不完整，请重试。", supplied_offset)
        try:
            with self.locked(task_id, owner_key) as (row, _directory, _path, handle):
                if float(row["expires_at"]) <= time.time():
                    raise UploadError(410, "upload_expired", "上传任务已过期，请重新开始。")
                if row["state"] != "receiving":
                    raise UploadError(409, "invalid_upload_state", "此上传任务不能继续接收分片。", int(row["committed_offset"]))
                offset = int(row["committed_offset"])
                if supplied_offset != offset:
                    raise UploadError(409, "offset_mismatch", "上传位置已变化，正在从已确认位置继续。", offset)
                self._reconcile_locked(row, handle)
                remaining = int(row["expected_size"]) - offset
                if declared > remaining:
                    raise UploadError(413, "chunk_exceeds_upload", "分片超过归档剩余大小。", offset)
                if declared < self.minimum and declared != remaining:
                    raise UploadError(400, "chunk_too_small", "当前分片过小，请按服务端建议大小重试。", offset)
                with db.get_conn(self.database_path) as conn:
                    reserved = int(conn.execute(
                        "SELECT COALESCE(SUM(expected_size-committed_offset),0) FROM upload_tasks "
                        "WHERE state='receiving' AND expires_at>?", (time.time(),),
                    ).fetchone()[0])
                if shutil.disk_usage(self.root).free - reserved < self.min_free:
                    raise UploadError(507, "insufficient_storage", "可用空间不足，请清理空间后继续。", offset)
                handle.seek(offset)
                try:
                    written = handle.write(payload)
                    if written != declared:
                        raise OSError(errno.ENOSPC, "short write")
                    handle.flush()
                    os.fsync(handle.fileno())
                    new_offset = offset + declared
                    now = time.time()
                    with db.get_conn(self.database_path) as conn:
                        changed = conn.execute(
                            """UPDATE upload_tasks SET committed_offset=?,updated_at=?,expires_at=?
                            WHERE id=? AND owner_key=? AND state='receiving' AND committed_offset=?""",
                            (new_offset, now, now + self.ttl, task_id, owner_key, offset),
                        ).rowcount
                        if changed != 1:
                            raise UploadError(409, "offset_mismatch", "上传位置已变化，请查询后继续。", offset)
                        result = conn.execute(
                            "SELECT * FROM upload_tasks WHERE id=? AND owner_key=?", (task_id, owner_key)
                        ).fetchone()
                except BaseException as original:
                    try:
                        handle.truncate(offset)
                        handle.flush()
                        os.fsync(handle.fileno())
                    except OSError:
                        pass
                    if isinstance(original, OSError) and original.errno in {errno.ENOSPC, getattr(errno, "EDQUOT", errno.ENOSPC)}:
                        raise UploadError(507, "insufficient_storage", "可用空间不足，请清理空间后重试。", offset) from original
                    raise
            return result
        except OSError as exc:
            if exc.errno in {errno.ENOSPC, getattr(errno, "EDQUOT", errno.ENOSPC)}:
                raise UploadError(507, "insufficient_storage", "可用空间不足，请清理空间后重试。", supplied_offset) from exc
            raise

    async def append(self, task_id: str, owner_key: str, request: Any, supplied_offset: int, declared: int) -> Any:
        """Stream one bounded chunk into its task file after pre-body admission."""
        if request.headers.get("content-type", "").partition(";")[0].strip().lower() != "application/offset+octet-stream":
            raise UploadError(415, "unsupported_chunk_type", "分片格式不受支持，请刷新页面后重试。")
        if declared <= 0:
            raise UploadError(400, "invalid_chunk_length", "分片大小信息无效。")
        if declared > self.maximum:
            raise UploadError(413, "chunk_too_large", "当前分片过大，请使用更小分片重试。", supplied_offset)
        with self.patch_slot(task_id):
            try:
                with self.locked(task_id, owner_key, nonblocking=True) as (row, _directory, _path, handle):
                    if float(row["expires_at"]) <= time.time():
                        raise UploadError(410, "upload_expired", "上传任务已过期，请重新开始。")
                    if row["state"] != "receiving":
                        raise UploadError(409, "invalid_upload_state", "此上传任务不能继续接收分片。", int(row["committed_offset"]))
                    offset = int(row["committed_offset"])
                    if supplied_offset != offset:
                        raise UploadError(409, "offset_mismatch", "上传位置已变化，正在从已确认位置继续。", offset)
                    self._reconcile_locked(row, handle)
                    remaining = int(row["expected_size"]) - offset
                    if declared > remaining:
                        raise UploadError(413, "chunk_exceeds_upload", "分片超过归档剩余大小。", offset)
                    if declared < self.minimum and declared != remaining:
                        raise UploadError(400, "chunk_too_small", "当前分片过小，请按服务端建议大小重试。", offset)
                    with db.get_conn(self.database_path) as conn:
                        reserved = int(conn.execute(
                            "SELECT COALESCE(SUM(expected_size-committed_offset),0) FROM upload_tasks "
                            "WHERE state='receiving' AND expires_at>?", (time.time(),),
                        ).fetchone()[0])
                    if shutil.disk_usage(self.root).free - reserved < self.min_free:
                        raise UploadError(507, "insufficient_storage", "可用空间不足，请清理空间后继续。", offset)
                    received = 0
                    handle.seek(offset)
                    try:
                        async for chunk in request.stream():
                            received += len(chunk)
                            if received > declared or received > self.maximum:
                                raise UploadError(413, "chunk_too_large", "当前分片过大，请使用更小分片重试。", offset)
                            if chunk and handle.write(chunk) != len(chunk):
                                raise OSError(errno.ENOSPC, "short write")
                        if received != declared:
                            raise UploadError(400, "chunk_length_mismatch", "分片接收不完整，请重试。", offset)
                        handle.flush()
                        os.fsync(handle.fileno())
                        new_offset = offset + declared
                        now = time.time()
                        with db.get_conn(self.database_path) as conn:
                            changed = conn.execute(
                                """UPDATE upload_tasks SET committed_offset=?,updated_at=?,expires_at=?
                                WHERE id=? AND owner_key=? AND state='receiving' AND committed_offset=?""",
                                (new_offset, now, now + self.ttl, task_id, owner_key, offset),
                            ).rowcount
                            if changed != 1:
                                raise UploadError(409, "offset_mismatch", "上传位置已变化，请查询后继续。", offset)
                            result = conn.execute(
                                "SELECT * FROM upload_tasks WHERE id=? AND owner_key=?", (task_id, owner_key)
                            ).fetchone()
                    except BaseException as original:
                        try:
                            handle.truncate(offset)
                            handle.flush()
                            os.fsync(handle.fileno())
                        except OSError:
                            pass
                        if isinstance(original, OSError) and original.errno in {errno.ENOSPC, getattr(errno, "EDQUOT", errno.ENOSPC)}:
                            raise UploadError(507, "insufficient_storage", "可用空间不足，请清理空间后重试。", offset) from original
                        raise
                    return result
            except BlockingIOError as exc:
                raise UploadError(409, "upload_in_progress", "此上传任务正在处理另一个请求，请稍后重试。") from exc

    def mark_preview(self, task_id: str, owner_key: str, digest: str, summary: dict[str, Any], entries: list[tuple[str, str]], import_required_bytes: int) -> Any:
        now = time.time()
        with db.get_conn(self.database_path) as conn:
            changed = conn.execute(
                """UPDATE upload_tasks SET state='preview_ready',archive_sha256=?,summary_json=?,entries_json=?,
                import_required_bytes=?,updated_at=?,expires_at=?,error_code=NULL WHERE id=? AND owner_key=? AND state='receiving' AND committed_offset=expected_size""",
                (digest, json.dumps(summary), json.dumps(entries), import_required_bytes, now, now + self.ttl, task_id, owner_key),
            ).rowcount
        if changed != 1:
            row = self._row(task_id, owner_key)
            if row["state"] != "preview_ready":
                raise UploadError(409, "upload_incomplete", "归档尚未上传完成。", int(row["committed_offset"]))
        return self._row(task_id, owner_key)

    def ensure_import_capacity(self, task_id: str, required_bytes: int) -> None:
        """Reserve import bytes on IMAGES_DIR, including shared-device upload promises."""
        now = time.time()
        with db.get_conn(self.database_path) as conn:
            reserved = int(conn.execute(
                "SELECT COALESCE(SUM(import_required_bytes),0) FROM upload_tasks "
                "WHERE state='preview_ready' AND expires_at>? AND id<>?",
                (now, task_id),
            ).fetchone()[0])
            reserved += int(conn.execute(
                "SELECT COALESCE(SUM(expected_size-committed_offset),0) FROM upload_tasks "
                "WHERE state='receiving' AND expires_at>? AND id<>?",
                (now, task_id),
            ).fetchone()[0])
        if shutil.disk_usage(self.images_root).free - reserved - required_bytes < self.min_free:
            raise UploadError(507, "insufficient_storage", "可用空间不足，请清理空间后重试。")

    def delete_owner_tasks(self, owner_key: str) -> None:
        with db.get_conn(self.database_path) as conn:
            rows = list(conn.execute(
                "SELECT id FROM upload_tasks WHERE owner_key=? AND state IN ('receiving','preview_ready','failed')",
                (owner_key,),
            ))
        for row in rows:
            self.delete(str(row["id"]), owner_key, missing_ok=True)

    def record_error(self, task_id: str, owner_key: str, code: str) -> None:
        """Record a retryable completion error without discarding uploaded bytes."""
        with db.get_conn(self.database_path) as conn:
            conn.execute(
                "UPDATE upload_tasks SET error_code=?,updated_at=? WHERE id=? AND owner_key=?",
                (code[:64], time.time(), task_id, owner_key),
            )

    def _delete_locked(self, task_id: str, owner_key: str, directory: Path) -> None:
        with db.get_conn(self.database_path) as conn:
            conn.execute("DELETE FROM upload_tasks WHERE id=? AND owner_key=?", (task_id, owner_key))
        if directory.parent == self.root.resolve() and not directory.is_symlink():
            shutil.rmtree(directory, ignore_errors=True)

    def delete(self, task_id: str, owner_key: str, *, missing_ok: bool = False) -> None:
        try:
            with self.locked(task_id, owner_key) as (_row, directory, _path, _handle):
                self._delete_locked(task_id, owner_key, directory)
        except UploadError as exc:
            if not missing_ok or exc.code not in {"upload_not_found", "upload_data_lost"}:
                raise
            directory, _path = self._paths(task_id)
            with db.get_conn(self.database_path) as conn:
                conn.execute("DELETE FROM upload_tasks WHERE id=? AND owner_key=?", (task_id, owner_key))
            if directory.parent == self.root.resolve() and not directory.is_symlink():
                shutil.rmtree(directory, ignore_errors=True)

    def clean(self, now: float | None = None) -> None:
        current = time.time() if now is None else now
        with db.get_conn(self.database_path) as conn:
            rows = list(conn.execute("SELECT id,owner_key,expires_at FROM upload_tasks"))
        known: set[str] = set()
        for row in rows:
            task_id, owner_key = str(row["id"]), str(row["owner_key"])
            try:
                with self.locked(task_id, owner_key, nonblocking=True) as (fresh, directory, _path, _handle):
                    if float(fresh["expires_at"]) <= current:
                        self._delete_locked(task_id, owner_key, directory)
                    else:
                        known.add(task_id)
            except BlockingIOError:
                known.add(task_id)  # An active PATCH/complete/confirm owns this task.
            except UploadError:
                # Invalid/symlink/missing task storage can never be resumed. Remove
                # only its database row; never follow or delete an unsafe path.
                with db.get_conn(self.database_path) as conn:
                    conn.execute("DELETE FROM upload_tasks WHERE id=?", (task_id,))
                directory = self.root.resolve() / task_id
                if directory.parent == self.root.resolve() and directory.exists() and not directory.is_symlink():
                    shutil.rmtree(directory, ignore_errors=True)
        for child in self.root.iterdir():
            if (
                child.name not in known
                and _TASK_ID.fullmatch(child.name)
                and child.is_dir()
                and not child.is_symlink()
                and child.stat().st_mtime <= current - _ORPHAN_GRACE_SECONDS
            ):
                try:
                    with (child / "upload.bin").open("r+b") as handle:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        shutil.rmtree(child, ignore_errors=True)
                except (FileNotFoundError, BlockingIOError):
                    pass
