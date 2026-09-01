from __future__ import annotations

import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
RESERVED_SLUGS = {"random", "health", "admin", "manage-images", "login", "logout", "static", "api"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS images (
 id INTEGER PRIMARY KEY AUTOINCREMENT, rel_path TEXT NOT NULL UNIQUE,
 width INTEGER NOT NULL, height INTEGER NOT NULL, orientation TEXT NOT NULL,
 format TEXT NOT NULL, content_type TEXT NOT NULL, file_size INTEGER NOT NULL,
 mtime_ns INTEGER NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_images_orientation ON images(orientation);
CREATE TABLE IF NOT EXISTS tags (
 id INTEGER PRIMARY KEY AUTOINCREMENT, slug TEXT NOT NULL UNIQUE COLLATE NOCASE,
 display_name TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS image_tags (
 image_id INTEGER NOT NULL REFERENCES images(id) ON DELETE CASCADE,
 tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
 created_at TEXT NOT NULL, PRIMARY KEY(image_id, tag_id)
);
CREATE INDEX IF NOT EXISTS idx_image_tags_tag ON image_tags(tag_id, image_id);
CREATE TABLE IF NOT EXISTS webdav_objects (
 href TEXT PRIMARY KEY, orientation TEXT NOT NULL, etag TEXT, last_modified TEXT,
 content_length INTEGER, content_type TEXT, updated_at TEXT NOT NULL,
 selected_mark INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_webdav_objects_orientation ON webdav_objects(orientation);
CREATE TABLE IF NOT EXISTS webdav_object_tags (
 href TEXT NOT NULL REFERENCES webdav_objects(href) ON DELETE CASCADE,
 tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
 origin TEXT NOT NULL DEFAULT 'admin' CHECK(origin IN ('admin','remote')),
 created_at TEXT NOT NULL, PRIMARY KEY(href, tag_id)
);
CREATE INDEX IF NOT EXISTS idx_webdav_object_tags_tag ON webdav_object_tags(tag_id, href);
CREATE TABLE IF NOT EXISTS webdav_cache (
 href TEXT PRIMARY KEY, cache_name TEXT NOT NULL UNIQUE, orientation TEXT NOT NULL,
 width INTEGER NOT NULL, height INTEGER NOT NULL, content_type TEXT NOT NULL,
 file_size INTEGER NOT NULL, etag TEXT, last_modified TEXT, fetched_at REAL NOT NULL,
 accessed_at REAL NOT NULL, maintenance_mark INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_webdav_cache_lru ON webdav_cache(accessed_at);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def validate_slug(value: str) -> str:
    slug = value.strip().lower()
    if not SLUG_RE.fullmatch(slug) or slug in RESERVED_SLUGS:
        raise ValueError("slug must be 1-63 lowercase letters, digits or single hyphens and not reserved")
    return slug


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def migrate(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    image_additions = {
        "content_hash": "TEXT",
        "enabled": "INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1))",
        "source": "TEXT NOT NULL DEFAULT 'local'",
    }
    remote_additions = {
        "enabled": "INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1))",
        "remote_present": "INTEGER NOT NULL DEFAULT 1 CHECK(remote_present IN (0,1))",
        "tag_hint": "TEXT",
    }
    for name, definition in image_additions.items():
        if name not in _columns(conn, "images"):
            conn.execute(f"ALTER TABLE images ADD COLUMN {name} {definition}")
    for name, definition in remote_additions.items():
        if name not in _columns(conn, "webdav_objects"):
            conn.execute(f"ALTER TABLE webdav_objects ADD COLUMN {name} {definition}")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_images_content_hash ON images(content_hash) WHERE content_hash IS NOT NULL")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_images_enabled_orientation ON images(enabled, orientation)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_webdav_enabled_orientation ON webdav_objects(enabled, remote_present, orientation)")
    conn.execute("PRAGMA user_version=2")


def connect(database_path: Path) -> sqlite3.Connection:
    database_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(database_path), timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    migrate(conn)
    return conn


@contextmanager
def get_conn(database_path: Path) -> Iterator[sqlite3.Connection]:
    conn = connect(database_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback(); raise
    finally:
        conn.close()


def ping(database_path: Path) -> bool:
    try:
        with get_conn(database_path) as conn: conn.execute("SELECT 1").fetchone()
        return True
    except sqlite3.Error: return False


def upsert_image(conn: sqlite3.Connection, record: dict[str, Any]) -> int:
    values = {**record, "updated_at": utc_now(), "content_hash": record.get("content_hash")}
    conn.execute("""INSERT INTO images
      (rel_path,width,height,orientation,format,content_type,file_size,mtime_ns,updated_at,content_hash)
      VALUES (:rel_path,:width,:height,:orientation,:format,:content_type,:file_size,:mtime_ns,:updated_at,:content_hash)
      ON CONFLICT(rel_path) DO UPDATE SET width=excluded.width,height=excluded.height,
      orientation=excluded.orientation,format=excluded.format,content_type=excluded.content_type,
      file_size=excluded.file_size,mtime_ns=excluded.mtime_ns,updated_at=excluded.updated_at,
      content_hash=COALESCE(excluded.content_hash,images.content_hash)""", values)
    return int(conn.execute("SELECT id FROM images WHERE rel_path=?", (values["rel_path"],)).fetchone()[0])


def delete_missing(conn: sqlite3.Connection, keep_rel_paths: set[str]) -> int:
    rows = conn.execute("SELECT id,rel_path FROM images WHERE source='local'").fetchall(); deleted=0
    for row in rows:
        if row["rel_path"] not in keep_rel_paths:
            conn.execute("DELETE FROM images WHERE id=?", (row["id"],)); deleted += 1
    return deleted


def delete_by_rel_path(conn: sqlite3.Connection, rel_path: str) -> None:
    conn.execute("DELETE FROM images WHERE rel_path=?", (rel_path,))


def list_images(conn: sqlite3.Connection, *, enabled_only: bool = False) -> list[sqlite3.Row]:
    sql = "SELECT * FROM images" + (" WHERE enabled=1" if enabled_only else "") + " ORDER BY id"
    return list(conn.execute(sql))


def ensure_tag(conn: sqlite3.Connection, slug: str, display_name: str | None = None) -> int:
    slug = validate_slug(slug); name = (display_name or slug).strip()
    if not name or len(name) > 100: raise ValueError("display_name must be 1-100 characters")
    now=utc_now()
    conn.execute("INSERT INTO tags(slug,display_name,enabled,created_at,updated_at) VALUES(?,?,1,?,?) ON CONFLICT(slug) DO NOTHING", (slug,name,now,now))
    return int(conn.execute("SELECT id FROM tags WHERE slug=? COLLATE NOCASE", (slug,)).fetchone()[0])


def vacuum_into(database_path: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists(): destination.unlink()
    with sqlite3.connect(str(database_path), timeout=30) as conn:
        conn.execute("PRAGMA busy_timeout=5000"); conn.execute("VACUUM INTO ?", (str(destination),))
