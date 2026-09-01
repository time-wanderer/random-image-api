from __future__ import annotations

import hashlib
import os
import random
import sqlite3
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit
from xml.etree import ElementTree

import httpx
from PIL import Image, UnidentifiedImageError

from app import db
from app.catalog import classify_orientation
from app.config import CONTENT_TYPES, SUPPORTED_EXTENSIONS, Settings

Orientation = Literal["desktop", "mobile"]


class WebDAVError(Exception):
    """Remote storage is unavailable or returned unsafe/invalid data."""


class NoRemoteImageAvailable(WebDAVError):
    """The remote index is healthy but has no image for the requested orientation."""


class WebDAVConfigurationError(WebDAVError):
    pass


class UnsafeHrefError(WebDAVError):
    pass


@dataclass(frozen=True, slots=True)
class CachedImage:
    href: str
    abs_path: Path
    width: int
    height: int
    orientation: str
    content_type: str
    file_size: int
    source: str = "webdav-cache"

    @property
    def filename(self) -> str:
        return Path(urlsplit(self.href).path).name or self.abs_path.name

    @property
    def rel_path(self) -> str:
        return self.filename


class WebDAVManager:
    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.BaseTransport | None = None,
        rng: random.Random | object | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.settings = settings
        self.rng = rng or random
        self.clock = clock
        self._lock = threading.RLock()
        self.last_sync_at: str | None = None
        self.last_sync_error: str | None = None
        self._enabled = settings.storage_mode == "hybrid"
        self._base_url = ""
        self._roots: dict[str, str] = {}
        self._client: httpx.Client | None = None

        if not self._enabled:
            return
        self._base_url, self._roots = self._validated_configuration()
        auth = None
        if settings.webdav_username or settings.webdav_password:
            auth = (settings.webdav_username, settings.webdav_password)
        self._client = httpx.Client(
            auth=auth,
            timeout=settings.webdav_timeout_seconds,
            follow_redirects=False,
            transport=transport,
        )

    @property
    def enabled(self) -> bool:
        return self._enabled

    def close(self) -> None:
        if self._client is not None:
            self._client.close()

    def _validated_configuration(self) -> tuple[str, dict[str, str]]:
        raw = self.settings.webdav_base_url.strip()
        parsed = urlsplit(raw)
        if parsed.scheme.lower() != "https" or not parsed.hostname:
            raise WebDAVConfigurationError("WebDAV base URL must use HTTPS")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise WebDAVConfigurationError("invalid WebDAV base URL")
        host = parsed.hostname.lower()
        allowed = self.settings.webdav_hosts or {host}
        if host not in allowed:
            raise WebDAVConfigurationError("WebDAV base host is not allowed")
        base = urlunsplit(("https", parsed.netloc, parsed.path.rstrip("/") + "/", "", ""))
        roots = {
            "desktop": self._root_url(base, self.settings.webdav_desktop_root),
            "mobile": self._root_url(base, self.settings.webdav_mobile_root),
        }
        return base, roots

    def _root_url(self, base: str, root: str) -> str:
        # A leading slash is interpreted below the configured host, never as a new host.
        base_parts = urlsplit(base)
        path = root.strip()
        if not path:
            raise WebDAVConfigurationError("WebDAV root cannot be empty")
        if path.startswith("//") or urlsplit(path).scheme or urlsplit(path).netloc:
            raise WebDAVConfigurationError("invalid WebDAV root")
        if path.startswith("/"):
            candidate = urlunsplit((base_parts.scheme, base_parts.netloc, path, "", ""))
        else:
            candidate = urljoin(base, path)
        if not candidate.endswith("/"):
            candidate += "/"
        self._validate_absolute(candidate, expected_root=None)
        return candidate

    def _validate_absolute(self, href: str, expected_root: str | None) -> str:
        parsed = urlsplit(href)
        base = urlsplit(self._base_url or href)
        if parsed.scheme.lower() != "https" or not parsed.hostname:
            raise UnsafeHrefError("unsafe WebDAV href")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise UnsafeHrefError("unsafe WebDAV href")
        if parsed.hostname.lower() != base.hostname.lower() or parsed.port != base.port:
            raise UnsafeHrefError("cross-host WebDAV href")
        allowed = self.settings.webdav_hosts or {base.hostname.lower()}
        if parsed.hostname.lower() not in allowed:
            raise UnsafeHrefError("WebDAV href host is not allowed")
        decoded = unquote(parsed.path)
        if chr(0) in decoded or any(
            part in {".", ".."} for part in decoded.split("/")
        ):
            raise UnsafeHrefError("unsafe WebDAV href path")
        normalized = urlunsplit(("https", parsed.netloc, parsed.path, "", ""))
        if expected_root is not None:
            root_path = urlsplit(expected_root).path.rstrip("/") + "/"
            item_path = parsed.path
            if not item_path.startswith(root_path) or item_path.rstrip("/") == root_path.rstrip("/"):
                raise UnsafeHrefError("WebDAV href is outside configured root")
        return normalized

    def validate_href(self, href: str, orientation: Orientation) -> str:
        root = self._roots[orientation]
        absolute = urljoin(root, href)
        return self._validate_absolute(absolute, root)

    def _read_limited(self, response: httpx.Response, limit: int) -> bytes:
        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_bytes():
            total += len(chunk)
            if total > limit:
                raise WebDAVError("remote response exceeds configured limit")
            chunks.append(chunk)
        return b"".join(chunks)

    def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        if self._client is None:
            raise WebDAVError("WebDAV is disabled")
        try:
            response = self._client.request(method, url, **kwargs)
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise WebDAVError("remote storage unavailable") from exc
        if 300 <= response.status_code < 400 and response.status_code != 304:
            response.close()
            raise WebDAVError("remote redirect refused")
        if response.status_code in {401, 403} or response.status_code >= 500:
            response.close()
            raise WebDAVError("remote storage unavailable")
        return response

    def sync(self) -> dict[str, int]:
        if not self.enabled:
            return {"objects": 0, "desktop": 0, "mobile": 0}
        collected: list[dict[str, object]] = []
        try:
            for orientation in ("desktop", "mobile"):
                root_items, collections = self._propfind_url(
                    self._roots[orientation], orientation, tag_hint=None
                )
                collected.extend(root_items)
                for collection_url, hint in collections:
                    nested, _ignored = self._propfind_url(
                        collection_url, orientation, tag_hint=hint
                    )
                    collected.extend(nested)
                if len(collected) > self.settings.webdav_max_objects:
                    raise WebDAVError("PROPFIND object limit exceeded")
            # Upsert instead of replacement: administrator enabled state and
            # administrator-added tags must survive later synchronizations.
            with self._lock, db.get_conn(self.settings.database_path) as conn:
                conn.execute("UPDATE webdav_objects SET remote_present=0")
                for item in collected:
                    conn.execute(
                        """INSERT INTO webdav_objects
                        (href,orientation,etag,last_modified,content_length,
                         content_type,updated_at,selected_mark,enabled,remote_present,tag_hint)
                        VALUES (?,?,?,?,?,?,?,0,1,1,?)
                        ON CONFLICT(href) DO UPDATE SET
                          orientation=excluded.orientation,etag=excluded.etag,
                          last_modified=excluded.last_modified,
                          content_length=excluded.content_length,
                          content_type=excluded.content_type,
                          updated_at=excluded.updated_at,remote_present=1,
                          tag_hint=excluded.tag_hint""",
                        (item["href"], item["orientation"], item["etag"],
                         item["last_modified"], item["content_length"],
                         item["content_type"], db.utc_now(), item["tag_hint"]),
                    )
                    hint = item.get("tag_hint")
                    if hint:
                        try:
                            tag_id = db.ensure_tag(conn, str(hint), str(hint))
                        except ValueError:
                            continue
                        conn.execute(
                            """INSERT OR IGNORE INTO webdav_object_tags
                            (href,tag_id,origin,created_at) VALUES(?,?,'remote',?)""",
                            (item["href"], tag_id, db.utc_now()),
                        )
                # A changed/removed remote hint removes only remote-owned links;
                # admin-owned links are never overwritten by synchronization.
                conn.execute("""DELETE FROM webdav_object_tags
                    WHERE origin='remote' AND NOT EXISTS (
                      SELECT 1 FROM webdav_objects o JOIN tags t ON t.slug=o.tag_hint
                      WHERE o.href=webdav_object_tags.href
                        AND t.id=webdav_object_tags.tag_id AND o.remote_present=1)""")
            self.last_sync_at = db.utc_now()
            self.last_sync_error = None
        except Exception as exc:
            self.last_sync_error = type(exc).__name__
            if isinstance(exc, WebDAVError):
                raise
            raise WebDAVError("WebDAV synchronization failed") from exc
        return {
            "objects": len(collected),
            "desktop": sum(item["orientation"] == "desktop" for item in collected),
            "mobile": sum(item["orientation"] == "mobile" for item in collected),
        }

    def _propfind(self, orientation: Orientation) -> list[dict[str, object]]:
        items, _collections = self._propfind_url(
            self._roots[orientation], orientation, tag_hint=None
        )
        return items

    def _propfind_url(
        self, url: str, orientation: Orientation, tag_hint: str | None
    ) -> tuple[list[dict[str, object]], list[tuple[str, str]]]:
        root = self._roots[orientation]
        response = self._request(
            "PROPFIND", url,
            headers={"Depth": "1", "Content-Type": "application/xml; charset=utf-8"},
            content=(b'<?xml version="1.0" encoding="utf-8"?>'
                     b'<d:propfind xmlns:d="DAV:"><d:prop><d:getetag/>'
                     b'<d:getlastmodified/><d:getcontentlength/><d:getcontenttype/>'
                     b'<d:resourcetype/></d:prop></d:propfind>'),
        )
        try:
            if response.status_code != 207:
                raise WebDAVError("invalid PROPFIND response")
            body = self._read_limited(response, self.settings.webdav_max_xml_bytes)
        finally:
            response.close()
        upper = body.upper()
        if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
            raise WebDAVError("unsafe XML response")
        try:
            document = ElementTree.fromstring(body)
        except ElementTree.ParseError as exc:
            raise WebDAVError("invalid XML response") from exc
        result: list[dict[str, object]] = []
        collections: list[tuple[str, str]] = []
        base_path = urlsplit(url).path.rstrip("/") + "/"
        for node in document.findall("{DAV:}response"):
            href_node = node.find("{DAV:}href")
            if href_node is None or not href_node.text:
                continue
            href = self.validate_href(href_node.text.strip(), orientation)
            prop = None
            for propstat in node.findall("{DAV:}propstat"):
                if " 200 " in (propstat.findtext("{DAV:}status") or ""):
                    prop = propstat.find("{DAV:}prop"); break
            if prop is None:
                continue
            is_collection = prop.find("{DAV:}resourcetype/{DAV:}collection") is not None
            if is_collection:
                path = urlsplit(href).path.rstrip("/") + "/"
                relative = unquote(path[len(base_path):]).strip("/") if path.startswith(base_path) else ""
                if tag_hint is None and relative and "/" not in relative:
                    try:
                        hint = db.validate_slug(relative)
                    except ValueError:
                        continue
                    collections.append((href.rstrip("/") + "/", hint))
                continue
            suffix = Path(unquote(urlsplit(href).path)).suffix.lower()
            if suffix not in SUPPORTED_EXTENSIONS:
                continue
            length_text = prop.findtext("{DAV:}getcontentlength")
            try:
                content_length = int(length_text) if length_text else None
            except ValueError as exc:
                raise WebDAVError("invalid remote object size") from exc
            if content_length is not None and (content_length < 0 or content_length > self.settings.webdav_max_download_bytes):
                continue
            result.append({"href": href, "orientation": orientation,
                "etag": prop.findtext("{DAV:}getetag"),
                "last_modified": prop.findtext("{DAV:}getlastmodified"),
                "content_length": content_length,
                "content_type": prop.findtext("{DAV:}getcontenttype"),
                "tag_hint": tag_hint})
        return result, collections

    def _choose_object(self, orientation: Orientation, tag: str | None = None) -> sqlite3.Row:
        with self._lock, db.get_conn(self.settings.database_path) as conn:
            rows = list(
                conn.execute(
                    """SELECT o.* FROM webdav_objects o
                    WHERE o.orientation=? AND o.enabled=1 AND o.remote_present=1
                      AND (? IS NULL OR EXISTS (
                        SELECT 1 FROM webdav_object_tags ot JOIN tags t ON t.id=ot.tag_id
                        WHERE ot.href=o.href AND t.slug=? AND t.enabled=1))
                    ORDER BY o.href""",
                    (orientation, tag, tag),
                )
            )
            if not rows:
                raise NoRemoteImageAvailable("no remote images available")
            unselected = [row for row in rows if int(row["selected_mark"]) == 0]
            if not unselected:
                conn.execute(
                    "UPDATE webdav_objects SET selected_mark=0 WHERE orientation=? AND enabled=1 AND remote_present=1",
                    (orientation,),
                )
                unselected = rows
            row = self.rng.choice(unselected)
            conn.execute("UPDATE webdav_objects SET selected_mark=1 WHERE href=?", (row["href"],))
            return row

    def fetch(self, orientation: Orientation, tag: str | None = None) -> CachedImage:
        row = self._choose_object(orientation, tag)
        return self.fetch_href(row["href"], orientation)

    def fetch_href(self, href: str, orientation: Orientation) -> CachedImage:
        safe_href = self.validate_href(href, orientation)
        cached = self._cache_row(safe_href)
        now = self.clock()
        fetched_at = float(cached["fetched_at"]) if cached is not None else 0.0
        if (
            cached is not None
            and fetched_at > 0
            and now - fetched_at < self.settings.cache_refresh_after_seconds
        ):
            image = self._cached_image(cached)
            if image is not None:
                self._touch(safe_href, now)
                return image
        headers: dict[str, str] = {}
        if cached is not None:
            if cached["etag"]:
                headers["If-None-Match"] = cached["etag"]
            if cached["last_modified"]:
                headers["If-Modified-Since"] = cached["last_modified"]
        response = self._request("GET", safe_href, headers=headers)
        try:
            if response.status_code == 304 and cached is not None:
                image = self._cached_image(cached)
                if image is None:
                    raise WebDAVError("cached image is missing")
                with db.get_conn(self.settings.database_path) as conn:
                    conn.execute(
                        "UPDATE webdav_cache SET fetched_at=?, accessed_at=? WHERE href=?",
                        (now, now, safe_href),
                    )
                return image
            if response.status_code != 200:
                raise WebDAVError("remote image download failed")
            return self._store_response(response, safe_href, orientation, now)
        finally:
            response.close()

    def _store_response(
        self, response: httpx.Response, href: str, orientation: Orientation, now: float
    ) -> CachedImage:
        assert self.settings.cache_dir is not None
        declared = response.headers.get("content-length")
        if declared:
            try:
                if int(declared) > self.settings.webdav_max_download_bytes:
                    raise WebDAVError("remote image exceeds configured limit")
            except ValueError as exc:
                raise WebDAVError("invalid remote content length") from exc
        digest = hashlib.sha256(href.encode("utf-8")).hexdigest()
        suffix = Path(unquote(urlsplit(href).path)).suffix.lower()
        cache_name = digest + suffix
        final_path = self.settings.cache_dir / cache_name
        temp_dir = self.settings.cache_dir / "tmp"
        temp_dir.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=".download-", dir=temp_dir)
        temp_path = Path(temp_name)
        total = 0
        try:
            with os.fdopen(fd, "wb") as output:
                for chunk in response.iter_bytes():
                    total += len(chunk)
                    if total > self.settings.webdav_max_download_bytes:
                        raise WebDAVError("remote image exceeds configured limit")
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            if total == 0:
                raise WebDAVError("empty remote image")
            try:
                with Image.open(temp_path) as image:
                    image.verify()
                with Image.open(temp_path) as image:
                    width, height = image.size
                    fmt = (image.format or "").lower()
            except (UnidentifiedImageError, OSError, ValueError) as exc:
                raise WebDAVError("remote object is not a valid image") from exc
            actual = classify_orientation(width, height)
            if actual != orientation and not (
                actual == "square" and self.settings.square_policy in {"both", orientation}
            ):
                raise WebDAVError("remote image orientation mismatch")
            expected_formats = {"jpg": "jpeg", "jpeg": "jpeg", "png": "png", "webp": "webp"}
            if expected_formats.get(suffix.lstrip(".")) != fmt:
                raise WebDAVError("remote image format mismatch")
            os.replace(temp_path, final_path)
            content_type = CONTENT_TYPES[suffix]
            with db.get_conn(self.settings.database_path) as conn:
                conn.execute(
                    """INSERT INTO webdav_cache
                    (href, cache_name, orientation, width, height, content_type,
                     file_size, etag, last_modified, fetched_at, accessed_at, maintenance_mark)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                    ON CONFLICT(href) DO UPDATE SET
                    cache_name=excluded.cache_name, orientation=excluded.orientation,
                    width=excluded.width, height=excluded.height,
                    content_type=excluded.content_type, file_size=excluded.file_size,
                    etag=excluded.etag, last_modified=excluded.last_modified,
                    fetched_at=excluded.fetched_at, accessed_at=excluded.accessed_at""",
                    (
                        href, cache_name, actual, width, height, content_type, total,
                        response.headers.get("etag"), response.headers.get("last-modified"), now, now,
                    ),
                )
            image = CachedImage(
                href, final_path, width, height, actual, content_type, total, "webdav-live"
            )
            self.maintain(rotate=False)
            return image
        finally:
            temp_path.unlink(missing_ok=True)

    def _cache_row(self, href: str) -> sqlite3.Row | None:
        with db.get_conn(self.settings.database_path) as conn:
            return conn.execute("SELECT * FROM webdav_cache WHERE href=?", (href,)).fetchone()

    def _cached_image(self, row: sqlite3.Row) -> CachedImage | None:
        assert self.settings.cache_dir is not None
        path = self.settings.cache_dir / row["cache_name"]
        try:
            path.resolve().relative_to(self.settings.cache_dir.resolve())
        except ValueError:
            return None
        if not path.is_file() or path.stat().st_size != int(row["file_size"]):
            return None
        return CachedImage(
            row["href"], path, int(row["width"]), int(row["height"]),
            row["orientation"], row["content_type"], int(row["file_size"]),
        )

    def _touch(self, href: str, now: float) -> None:
        with db.get_conn(self.settings.database_path) as conn:
            conn.execute("UPDATE webdav_cache SET accessed_at=? WHERE href=?", (now, href))

    def cached_candidates(self, orientation: Orientation, tag: str | None = None) -> list[CachedImage]:
        with db.get_conn(self.settings.database_path) as conn:
            rows = list(
                conn.execute(
                    """SELECT c.* FROM webdav_cache c JOIN webdav_objects o ON o.href=c.href
                    WHERE c.orientation=? AND o.enabled=1
                      AND (? IS NULL OR EXISTS (
                        SELECT 1 FROM webdav_object_tags ot JOIN tags t ON t.id=ot.tag_id
                        WHERE ot.href=c.href AND t.slug=? AND t.enabled=1))
                    ORDER BY c.accessed_at,c.href""",
                    (orientation, tag, tag),
                )
            )
        return [image for row in rows if (image := self._cached_image(row)) is not None]

    def pick_cached(self, orientation: Orientation) -> CachedImage:
        candidates = self.cached_candidates(orientation)
        if not candidates:
            raise WebDAVError("no cached images available")
        image = self.rng.choice(candidates)
        self._touch(image.href, self.clock())
        return image

    def maintain(self, *, rotate: bool = True) -> dict[str, int]:
        assert self.settings.cache_dir is not None
        removed = 0
        marked_for_refresh = 0
        with self._lock, db.get_conn(self.settings.database_path) as conn:
            rows = list(
                conn.execute(
                    "SELECT * FROM webdav_cache ORDER BY accessed_at ASC, href ASC"
                )
            )
            valid: list[sqlite3.Row] = []
            for row in rows:
                if self._cached_image(row) is None:
                    conn.execute("DELETE FROM webdav_cache WHERE href=?", (row["href"],))
                    removed += 1
                else:
                    valid.append(row)
            total_files = len(valid)
            total_bytes = sum(int(row["file_size"]) for row in valid)
            evicted: set[str] = set()
            for row in valid:
                if (
                    total_files <= self.settings.cache_max_files
                    and total_bytes <= self.settings.cache_max_bytes
                ):
                    break
                path = self.settings.cache_dir / row["cache_name"]
                path.unlink(missing_ok=True)
                conn.execute("DELETE FROM webdav_cache WHERE href=?", (row["href"],))
                evicted.add(str(row["href"]))
                total_files -= 1
                total_bytes -= int(row["file_size"])
                removed += 1

            survivors = [row for row in valid if str(row["href"]) not in evicted]
            conn.execute("UPDATE webdav_cache SET maintenance_mark=0")
            if rotate and survivors and self.settings.cache_rotate_percent > 0:
                count = max(
                    1,
                    (len(survivors) * self.settings.cache_rotate_percent + 99) // 100,
                )
                count = min(count, len(survivors))
                selected = self.rng.sample(survivors, count)
                conn.executemany(
                    """UPDATE webdav_cache
                    SET fetched_at=0, maintenance_mark=1
                    WHERE href=?""",
                    [(row["href"],) for row in selected],
                )
                marked_for_refresh = len(selected)
        return {
            "removed": removed,
            "files": total_files,
            "bytes": total_bytes,
            "marked_for_refresh": marked_for_refresh,
        }

    def health(self) -> dict[str, object]:
        if not self.enabled:
            return {"enabled": False, "status": "disabled", "last_sync_at": None, "last_sync_error": None}
        try:
            with db.get_conn(self.settings.database_path) as conn:
                objects = int(conn.execute("SELECT COUNT(*) FROM webdav_objects").fetchone()[0])
                cache_row = conn.execute(
                    "SELECT COUNT(*), COALESCE(SUM(file_size), 0) FROM webdav_cache"
                ).fetchone()
            return {
                "enabled": True,
                "status": "ok" if self.last_sync_error is None else "degraded",
                "objects": objects,
                "last_sync_at": self.last_sync_at,
                "last_sync_error": self.last_sync_error,
                "cache": {
                    "files": int(cache_row[0]),
                    "bytes": int(cache_row[1]),
                    "refresh_after_seconds": self.settings.cache_refresh_after_seconds,
                    "rotate_percent": self.settings.cache_rotate_percent,
                },
            }
        except sqlite3.Error:
            return {"enabled": True, "status": "error", "last_sync_at": self.last_sync_at, "last_sync_error": "database"}
