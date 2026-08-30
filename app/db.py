from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS images (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rel_path TEXT NOT NULL UNIQUE,
    width INTEGER NOT NULL,
    height INTEGER NOT NULL,
    orientation TEXT NOT NULL,
    format TEXT NOT NULL,
    content_type TEXT NOT NULL,
    file_size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_images_orientation ON images(orientation);

CREATE TABLE IF NOT EXISTS webdav_objects (
    href TEXT PRIMARY KEY,
    orientation TEXT NOT NULL,
    etag TEXT,
    last_modified TEXT,
    content_length INTEGER,
    content_type TEXT,
    updated_at TEXT NOT NULL,
    selected_mark INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_webdav_objects_orientation
    ON webdav_objects(orientation);

CREATE TABLE IF NOT EXISTS webdav_cache (
    href TEXT PRIMARY KEY,
    cache_name TEXT NOT NULL UNIQUE,
    orientation TEXT NOT NULL,
    width INTEGER NOT NULL,
    height INTEGER NOT NULL,
    content_type TEXT NOT NULL,
    file_size INTEGER NOT NULL,
    etag TEXT,
    last_modified TEXT,
    fetched_at REAL NOT NULL,
    accessed_at REAL NOT NULL,
    maintenance_mark INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_webdav_cache_lru
    ON webdav_cache(accessed_at);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def connect(database_path: Path) -> sqlite3.Connection:
    database_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(database_path), timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(SCHEMA)
    return conn


@contextmanager
def get_conn(database_path: Path) -> Iterator[sqlite3.Connection]:
    conn = connect(database_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def ping(database_path: Path) -> bool:
    try:
        with get_conn(database_path) as conn:
            conn.execute("SELECT 1").fetchone()
        return True
    except sqlite3.Error:
        return False


def upsert_image(conn: sqlite3.Connection, record: dict[str, Any]) -> int:
    record = {**record, "updated_at": utc_now()}
    conn.execute(
        """
        INSERT INTO images (
            rel_path, width, height, orientation, format, content_type,
            file_size, mtime_ns, updated_at
        ) VALUES (
            :rel_path, :width, :height, :orientation, :format, :content_type,
            :file_size, :mtime_ns, :updated_at
        )
        ON CONFLICT(rel_path) DO UPDATE SET
            width=excluded.width,
            height=excluded.height,
            orientation=excluded.orientation,
            format=excluded.format,
            content_type=excluded.content_type,
            file_size=excluded.file_size,
            mtime_ns=excluded.mtime_ns,
            updated_at=excluded.updated_at
        """,
        record,
    )
    row = conn.execute(
        "SELECT id FROM images WHERE rel_path = ?", (record["rel_path"],)
    ).fetchone()
    return int(row["id"])


def delete_missing(conn: sqlite3.Connection, keep_rel_paths: set[str]) -> int:
    rows = conn.execute("SELECT id, rel_path FROM images").fetchall()
    deleted = 0
    for row in rows:
        if row["rel_path"] not in keep_rel_paths:
            conn.execute("DELETE FROM images WHERE id = ?", (row["id"],))
            deleted += 1
    return deleted


def delete_by_rel_path(conn: sqlite3.Connection, rel_path: str) -> None:
    conn.execute("DELETE FROM images WHERE rel_path = ?", (rel_path,))


def list_images(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM images ORDER BY id").fetchall())


def vacuum_into(database_path: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        destination.unlink()
    with sqlite3.connect(str(database_path), timeout=30) as conn:
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("VACUUM INTO ?", (str(destination),))
