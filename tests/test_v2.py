from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from urllib.parse import unquote

import httpx
import pytest
from fastapi.testclient import TestClient

from app import db
from app.catalog import Catalog
from app.config import Settings
from app.main import create_app
from app.webdav import WebDAVManager
from tests.conftest import make_image


def test_v1_schema_migrates_idempotently_and_keeps_untagged(tmp_path: Path) -> None:
    database = tmp_path / "legacy.db"
    with sqlite3.connect(database) as conn:
        conn.executescript("""
        CREATE TABLE images(id INTEGER PRIMARY KEY AUTOINCREMENT,rel_path TEXT NOT NULL UNIQUE,
          width INTEGER NOT NULL,height INTEGER NOT NULL,orientation TEXT NOT NULL,format TEXT NOT NULL,
          content_type TEXT NOT NULL,file_size INTEGER NOT NULL,mtime_ns INTEGER NOT NULL,updated_at TEXT NOT NULL);
        CREATE TABLE webdav_objects(href TEXT PRIMARY KEY,orientation TEXT NOT NULL,etag TEXT,last_modified TEXT,
          content_length INTEGER,content_type TEXT,updated_at TEXT NOT NULL,selected_mark INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE webdav_cache(href TEXT PRIMARY KEY,cache_name TEXT NOT NULL UNIQUE,orientation TEXT NOT NULL,
          width INTEGER NOT NULL,height INTEGER NOT NULL,content_type TEXT NOT NULL,file_size INTEGER NOT NULL,
          etag TEXT,last_modified TEXT,fetched_at REAL NOT NULL,accessed_at REAL NOT NULL,maintenance_mark INTEGER NOT NULL DEFAULT 0);
        INSERT INTO images(rel_path,width,height,orientation,format,content_type,file_size,mtime_ns,updated_at)
          VALUES('desktop/old.jpg',2,1,'desktop','jpeg','image/jpeg',10,1,'old');
        """)
    for _ in range(2):
        with db.get_conn(database) as conn:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
    with db.get_conn(database) as conn:
        row = conn.execute("SELECT enabled,source,content_hash FROM images").fetchone()
        assert (row["enabled"], row["source"], row["content_hash"]) == (1, "local", None)
        assert conn.execute("SELECT COUNT(*) FROM image_tags").fetchone()[0] == 0


def test_topic_api_is_strict_and_many_tags_share_one_file(settings: Settings) -> None:
    picture = settings.images_dir / "desktop" / "only.png"
    other = settings.images_dir / "mobile" / "other.png"
    make_image(picture, (80, 40))
    make_image(other, (40, 80))
    app = create_app(settings)
    with TestClient(app) as client:
        with db.get_conn(settings.database_path) as conn:
            image_id = conn.execute("SELECT id FROM images WHERE rel_path='desktop/only.png'").fetchone()[0]
            for slug, display_name in (("genshin", "原神"), ("anime", "Anime")):
                tag_id = db.ensure_tag(conn, slug, display_name)
                conn.execute("INSERT INTO image_tags(image_id,tag_id,created_at) VALUES(?,?,?)", (image_id, tag_id, db.utc_now()))
        client.app.state.catalog.scan()
        assert len([p for p in settings.images_dir.rglob("*") if p.is_file()]) == 2
        for endpoint in ("/random/genshin", "/random?tag=anime"):
            response = client.get(endpoint, params={"type": "mobile"} if "?" not in endpoint else None)
            assert response.status_code == 200
            assert response.headers["x-image-file"] == "desktop/only.png"
            assert response.headers["x-image-tag"] in {"genshin", "anime"}
            if response.headers["x-image-tag"] == "genshin":
                assert unquote(response.headers["x-image-tag-name"]) == "原神"
        with db.get_conn(settings.database_path) as conn:
            empty = db.ensure_tag(conn, "empty", "Empty")
            conn.execute("UPDATE tags SET enabled=0 WHERE slug='anime'")
        assert client.get("/random/empty").status_code == 404
        assert client.get("/random/anime").status_code == 404
        assert client.get("/random/missing").status_code == 404
        assert client.get("/random/genshin", params={"tag": "empty"}).status_code == 400
        assert picture.is_file() and other.is_file()


def test_admin_path_validation_and_empty_secret_disables_ui(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        Settings(admin_path="/random/manage")
    settings = Settings(data_dir=tmp_path/"d", images_dir=tmp_path/"d/images",
        database_path=tmp_path/"d/db/images.db", log_dir=tmp_path/"d/logs",
        cache_dir=tmp_path/"d/cache", admin_token="token", admin_session_secret="",
        scan_interval_seconds=3600)
    with TestClient(create_app(settings)) as client:
        assert client.get("/manage-images/login").status_code == 404
        assert client.post("/manage-images/login", data={"token": "token"}).status_code == 404


def _multistatus(entries: str) -> bytes:
    return ("<?xml version='1.0'?><d:multistatus xmlns:d='DAV:'>" + entries + "</d:multistatus>").encode()


def test_webdav_first_level_tag_and_disabled_object_not_revived(tmp_path: Path) -> None:
    data = tmp_path / "runtime"
    settings = Settings(data_dir=data, images_dir=data/"images", database_path=data/"db/images.db",
        log_dir=data/"logs", cache_dir=data/"cache", storage_mode="hybrid",
        webdav_base_url="https://dav.test/", webdav_allowed_hosts="dav.test",
        scan_on_startup=False, scan_interval_seconds=3600, webdav_sync_interval_seconds=3600)
    collection = """<d:response><d:href>/desktop/genshin/</d:href><d:propstat><d:prop><d:resourcetype><d:collection/></d:resourcetype></d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"""
    image = """<d:response><d:href>/desktop/genshin/a.png</d:href><d:propstat><d:prop><d:getcontentlength>100</d:getcontentlength><d:getcontenttype>image/png</d:getcontenttype><d:resourcetype/></d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/desktop/": return httpx.Response(207, content=_multistatus(collection))
        if request.url.path == "/desktop/genshin/": return httpx.Response(207, content=_multistatus(image))
        return httpx.Response(207, content=_multistatus(""))
    manager = WebDAVManager(settings, transport=httpx.MockTransport(handler))
    try:
        manager.sync()
        with db.get_conn(settings.database_path) as conn:
            row = conn.execute("SELECT enabled,tag_hint FROM webdav_objects").fetchone()
            assert (row["enabled"], row["tag_hint"]) == (1, "genshin")
            assert conn.execute("SELECT COUNT(*) FROM webdav_object_tags").fetchone()[0] == 1
            conn.execute("UPDATE webdav_objects SET enabled=0")
        manager.sync()
        with db.get_conn(settings.database_path) as conn:
            assert conn.execute("SELECT enabled FROM webdav_objects").fetchone()[0] == 0
    finally:
        manager.close()


def test_catalog_removes_stale_row_when_file_mutates_to_duplicate(settings: Settings) -> None:
    canonical = settings.images_dir / "desktop" / "canonical.png"
    changed = settings.images_dir / "desktop" / "changed.png"
    make_image(canonical, (80, 40), "#112233")
    make_image(changed, (81, 40), "#445566")
    catalog = Catalog(settings)
    assert catalog.scan()["total"] == 2

    with db.get_conn(settings.database_path) as conn:
        stale_id = int(conn.execute("SELECT id FROM images WHERE rel_path='desktop/changed.png'").fetchone()[0])
        tag_id = db.ensure_tag(conn, "stale-tag", "Stale Tag")
        conn.execute(
            "INSERT INTO image_tags(image_id,tag_id,created_at) VALUES(?,?,?)",
            (stale_id, tag_id, db.utc_now()),
        )

    previous_mtime = changed.stat().st_mtime_ns
    changed.write_bytes(canonical.read_bytes())
    os.utime(changed, ns=(previous_mtime + 1_000_000_000, previous_mtime + 1_000_000_000))
    result = catalog.scan()

    assert result["total"] == 1
    assert canonical.is_file() and changed.is_file()
    with db.get_conn(settings.database_path) as conn:
        rows = conn.execute("SELECT id,rel_path FROM images ORDER BY rel_path").fetchall()
        assert [(int(row["id"]), row["rel_path"]) for row in rows] == [
            (int(rows[0]["id"]), "desktop/canonical.png")
        ]
        assert int(rows[0]["id"]) != stale_id
        assert conn.execute("SELECT 1 FROM image_tags WHERE image_id=?", (stale_id,)).fetchone() is None
