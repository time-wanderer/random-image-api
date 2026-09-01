from __future__ import annotations

import io
import re
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

from app import db
from app.admin import create_admin_router
from app.catalog import Catalog

ADMIN = "/manage-images"


def image_bytes(fmt: str = "PNG", size: tuple[int, int] = (30, 10), *, exif_orientation: int | None = None) -> bytes:
    output = io.BytesIO()
    image = Image.new("RGB", size, "#285577")
    kwargs = {}
    if exif_orientation is not None:
        exif = Image.Exif()
        exif[274] = exif_orientation
        kwargs["exif"] = exif
    image.save(output, format=fmt, **kwargs)
    return output.getvalue()


def archive_bytes(files: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, payload in files.items():
            archive.writestr(name, payload)
    return output.getvalue()


@pytest.fixture
def admin_env(tmp_path: Path):
    data = tmp_path / "runtime"
    settings = SimpleNamespace(
        data_dir=data,
        images_dir=data / "images",
        database_path=data / "database" / "images.db",
        log_dir=data / "logs",
        cache_dir=data / "cache" / "webdav",
        upload_tmp_dir=data / "tmp" / "admin",
        admin_path=ADMIN,
        admin_token="correct horse",
        admin_session_secret="test-only-session-secret",
        admin_cookie_secure=True,
        admin_cookie_name="ria_admin_session",
        admin_session_ttl_seconds=120,
        admin_preview_ttl_seconds=1,
        admin_login_window_seconds=60,
        admin_login_max_attempts=2,
        admin_max_upload_bytes=2 * 1024 * 1024,
        admin_max_image_pixels=1_000_000,
        admin_max_archive_bytes=2 * 1024 * 1024,
        admin_max_archive_members=30,
        admin_max_archive_member_bytes=1024 * 1024,
        admin_max_archive_total_bytes=2 * 1024 * 1024,
        admin_max_archive_compression_ratio=200.0,
        admin_import_default_tag="imported",
        trusted_proxy_headers=True,
        square_policy="both",
        fallback_enabled=True,
        max_pick_retries=3,
    )
    def ensure_directories() -> None:
        settings.images_dir.mkdir(parents=True, exist_ok=True)
        (settings.images_dir / "desktop").mkdir(exist_ok=True)
        (settings.images_dir / "mobile").mkdir(exist_ok=True)
        settings.database_path.parent.mkdir(parents=True, exist_ok=True)
        settings.cache_dir.mkdir(parents=True, exist_ok=True)
        (settings.cache_dir / "tmp").mkdir(exist_ok=True)

    settings.ensure_directories = ensure_directories
    ensure_directories()
    catalog = Catalog(settings)
    catalog.scan()
    app = FastAPI()
    app.state.catalog = catalog
    app.include_router(create_admin_router(settings))
    return settings, app


def new_client(app: FastAPI) -> TestClient:
    return TestClient(app, base_url="https://testserver")


def login(client: TestClient) -> str:
    response = client.post(f"{ADMIN}/login", data={"token": "correct horse"}, follow_redirects=False)
    assert response.status_code == 303
    page = client.get(ADMIN)
    assert page.status_code == 200
    match = re.search(r'name="csrf-token" content="([^"]+)"', page.text)
    assert match
    return match.group(1)


def preview(client: TestClient, csrf: str, payload: bytes, name: str = "photos.zip") -> tuple[str, object]:
    response = client.post(
        f"{ADMIN}/archives/preview",
        data={"csrf": csrf},
        files={"archive": (name, payload, "application/zip")},
    )
    assert response.status_code == 200, response.text
    match = re.search(r'name="token" value="([^"]+)"', response.text)
    assert match
    return match.group(1), response


def test_auth_cookie_tamper_csrf_logout_and_xss(admin_env) -> None:
    settings, app = admin_env
    with new_client(app) as client:
        assert client.get(ADMIN).status_code == 401
        login_response = client.post(f"{ADMIN}/login", data={"token": "correct horse"}, follow_redirects=False)
        cookie = login_response.headers["set-cookie"].lower()
        assert "httponly" in cookie
        assert "secure" in cookie
        assert "samesite=strict" in cookie
        assert f"path={ADMIN}" in cookie

        page = client.get(ADMIN)
        csrf = re.search(r'name="csrf-token" content="([^"]+)"', page.text).group(1)  # type: ignore[union-attr]
        assert client.post(f"{ADMIN}/tags", data={"slug": "one", "display_name": "One"}).status_code == 403
        assert client.post(f"{ADMIN}/tags", data={"csrf": "wrong", "slug": "one", "display_name": "One"}).status_code == 403
        assert client.post(f"{ADMIN}/tags", data={"csrf": csrf, "slug": "one", "display_name": "<script>alert(1)</script>"}, follow_redirects=False).status_code == 303
        tags = client.get(f"{ADMIN}/tags")
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in tags.text
        assert "<script>alert(1)</script>" not in tags.text

        assert client.post(f"{ADMIN}/logout", data={"csrf": csrf}, follow_redirects=False).status_code == 303
        assert client.get(ADMIN).status_code == 401

    with new_client(app) as attacker:
        attacker.cookies.set(settings.admin_cookie_name, "fake.9999999999.bad", path=ADMIN)
        assert attacker.get(ADMIN).status_code == 401


def test_per_ip_login_rate_limit(admin_env) -> None:
    _settings, app = admin_env
    with new_client(app) as client:
        headers = {"x-forwarded-for": "203.0.113.9"}
        assert client.post(f"{ADMIN}/login", data={"token": "bad"}, headers=headers).status_code == 401
        assert client.post(f"{ADMIN}/login", data={"token": "bad"}, headers=headers).status_code == 401
        limited = client.post(f"{ADMIN}/login", data={"token": "correct horse"}, headers=headers)
        assert limited.status_code == 429
        assert limited.headers["retry-after"] == "60"
        assert client.post(f"{ADMIN}/login", data={"token": "correct horse"}, headers={"x-forwarded-for": "203.0.113.10"}, follow_redirects=False).status_code == 303


def test_multi_upload_exif_hash_dedup_and_local_delete_confirmation(admin_env) -> None:
    settings, app = admin_env
    jpeg = image_bytes("JPEG", (40, 20), exif_orientation=6)
    png = image_bytes("PNG", (33, 11))
    with new_client(app) as client:
        csrf = login(client)
        response = client.post(
            f"{ADMIN}/upload",
            data={"csrf": csrf},
            files=[("files", ("unsafe<script>.jpg", jpeg, "image/jpeg")), ("files", ("other.png", png, "image/png"))],
            follow_redirects=False,
        )
        assert response.status_code == 303, response.text
        stored = sorted(path for path in settings.images_dir.rglob("*") if path.is_file())
        assert len(stored) == 2
        assert all(re.fullmatch(r"[0-9a-f]{64}\.(?:jpg|png|webp)", path.name) for path in stored)
        assert not any("unsafe" in path.name for path in stored)

        with db.get_conn(settings.database_path) as conn:
            rows = conn.execute("SELECT id,orientation,rel_path,content_hash FROM images ORDER BY id").fetchall()
        assert {row["orientation"] for row in rows} == {"desktop", "mobile"}
        assert all(row["content_hash"] and len(row["content_hash"]) == 64 for row in rows)

        duplicate = client.post(f"{ADMIN}/upload", data={"csrf": csrf}, files={"file": ("again.jpg", jpeg, "image/jpeg")}, follow_redirects=False)
        assert duplicate.status_code == 303
        assert len([path for path in settings.images_dir.rglob("*") if path.is_file()]) == 2

        image_id = int(rows[0]["id"])
        rel_path = str(rows[0]["rel_path"])
        assert client.post(f"{ADMIN}/images/{image_id}/delete", data={"csrf": csrf, "confirm": "delete"}).status_code == 400
        assert (settings.images_dir / rel_path).is_file()
        assert client.post(f"{ADMIN}/images/{image_id}/delete", data={"csrf": csrf, "confirm": "DELETE"}, follow_redirects=False).status_code == 303
        assert not (settings.images_dir / rel_path).exists()
        with db.get_conn(settings.database_path) as conn:
            assert conn.execute("SELECT 1 FROM images WHERE id=?", (image_id,)).fetchone() is None


def test_archive_preview_does_not_write_then_confirm_imports_and_maps_tags(admin_env) -> None:
    settings, app = admin_env
    payload = archive_bytes({"animals/cat.png": image_bytes("PNG", (31, 10)), "portrait.jpg": image_bytes("JPEG", (10, 31))})
    with new_client(app) as client:
        csrf = login(client)
        before_files = list(settings.images_dir.rglob("*"))
        with db.get_conn(settings.database_path) as conn:
            before_rows = int(conn.execute("SELECT COUNT(*) FROM images").fetchone()[0])
        assert client.post(f"{ADMIN}/tags", data={"csrf": csrf, "slug": "imported", "display_name": "Imported"}, follow_redirects=False).status_code == 303
        token, response = preview(client, csrf, payload)
        assert "dry_run" in response.text
        assert list(settings.images_dir.rglob("*")) == before_files
        with db.get_conn(settings.database_path) as conn:
            assert int(conn.execute("SELECT COUNT(*) FROM images").fetchone()[0]) == before_rows
        temporary = list(settings.upload_tmp_dir.iterdir())
        assert len(temporary) == 1

        confirmed = client.post(f"{ADMIN}/archives/confirm", data={"csrf": csrf, "token": token}, follow_redirects=False)
        assert confirmed.status_code == 303, confirmed.text
        assert not list(settings.upload_tmp_dir.iterdir())
        with db.get_conn(settings.database_path) as conn:
            assert int(conn.execute("SELECT COUNT(*) FROM images").fetchone()[0]) == 2
            slugs = {row[0] for row in conn.execute("SELECT slug FROM tags")}
            assert {"imported", "animals"} <= slugs
            cat_tags = {row[0] for row in conn.execute("SELECT t.slug FROM images i JOIN image_tags it ON it.image_id=i.id JOIN tags t ON t.id=it.tag_id WHERE i.orientation='desktop'")}
            assert {"imported", "animals"} <= cat_tags


def test_archive_preview_is_bound_to_session_and_logout_cleans_temp(admin_env) -> None:
    settings, app = admin_env
    payload = archive_bytes({"safe.png": image_bytes()})
    with new_client(app) as owner, new_client(app) as other:
        owner_csrf = login(owner)
        other_csrf = login(other)
        token, _response = preview(owner, owner_csrf, payload)
        assert len(list(settings.upload_tmp_dir.iterdir())) == 1
        forbidden = other.post(f"{ADMIN}/archives/confirm", data={"csrf": other_csrf, "token": token})
        assert forbidden.status_code == 403
        assert not list(settings.images_dir.rglob("*.*"))
        assert owner.post(f"{ADMIN}/logout", data={"csrf": owner_csrf}, follow_redirects=False).status_code == 303
        assert not list(settings.upload_tmp_dir.iterdir())
        assert owner.post(f"{ADMIN}/archives/confirm", data={"csrf": owner_csrf, "token": token}).status_code == 401


def test_archive_preview_expiry_removes_temp_and_cannot_confirm(admin_env) -> None:
    settings, app = admin_env
    with new_client(app) as client:
        csrf = login(client)
        token, _response = preview(client, csrf, archive_bytes({"safe.png": image_bytes()}))
        assert list(settings.upload_tmp_dir.iterdir())
        time.sleep(1.1)
        expired = client.post(f"{ADMIN}/archives/confirm", data={"csrf": csrf, "token": token})
        assert expired.status_code in {404, 410}
        assert not list(settings.upload_tmp_dir.iterdir())
        assert not list(settings.images_dir.rglob("*.*"))


def test_malicious_archive_rejected_without_images_db_or_temp_residue(admin_env) -> None:
    settings, app = admin_env
    malicious = archive_bytes({"good.png": image_bytes(), "../escape.png": image_bytes()})
    with new_client(app) as client:
        csrf = login(client)
        response = client.post(
            f"{ADMIN}/archives/preview",
            data={"csrf": csrf},
            files={"archive": ("evil.zip", malicious, "application/zip")},
        )
        assert response.status_code == 400
        assert not list(settings.images_dir.rglob("*.*"))
        assert not list(settings.upload_tmp_dir.glob("*"))
        with db.get_conn(settings.database_path) as conn:
            assert int(conn.execute("SELECT COUNT(*) FROM images").fetchone()[0]) == 0


def test_batch_tags_tag_lifecycle_webdav_and_cache_are_csrf_protected(admin_env) -> None:
    settings, app = admin_env
    with new_client(app) as client:
        csrf = login(client)
        assert client.post(f"{ADMIN}/upload", data={"csrf": csrf}, files={"file": ("one.png", image_bytes(), "image/png")}, follow_redirects=False).status_code == 303
        with db.get_conn(settings.database_path) as conn:
            image_id = int(conn.execute("SELECT id FROM images").fetchone()[0])
        for slug in ("source", "target"):
            assert client.post(f"{ADMIN}/tags", data={"csrf": csrf, "slug": slug, "display_name": slug.title()}, follow_redirects=False).status_code == 303
        assert client.post(f"{ADMIN}/images/tags", data={"csrf": csrf, "image_ids": str(image_id), "tag": "source", "action": "add"}, follow_redirects=False).status_code == 303
        with db.get_conn(settings.database_path) as conn:
            source = int(conn.execute("SELECT id FROM tags WHERE slug='source'").fetchone()[0])
            target = int(conn.execute("SELECT id FROM tags WHERE slug='target'").fetchone()[0])
            now = db.utc_now()
            conn.execute("INSERT INTO webdav_objects(href,orientation,updated_at) VALUES('/remote/a.jpg','desktop',?)", (now,))
            conn.execute("INSERT INTO webdav_object_tags(href,tag_id,origin,created_at) VALUES('/remote/a.jpg',?,'admin',?)", (source, now))
            cache_file = settings.cache_dir / "cache.jpg"
            cache_file.write_bytes(b"cache")
            conn.execute("INSERT INTO webdav_cache(href,cache_name,orientation,width,height,content_type,file_size,fetched_at,accessed_at) VALUES('/remote/a.jpg','cache.jpg','desktop',1,1,'image/jpeg',5,1,1)")
        assert client.post(f"{ADMIN}/tags/{source}/merge", data={"csrf": csrf, "target_id": str(target)}, follow_redirects=False).status_code == 303
        assert client.post(f"{ADMIN}/tags/{target}/disable", data={"csrf": csrf, "enabled": "0"}, follow_redirects=False).status_code == 303
        assert client.post(f"{ADMIN}/webdav/enabled", data={"href": "/remote/a.jpg", "enabled": "0"}).status_code == 403
        assert client.post(f"{ADMIN}/cache/clear", data={}).status_code == 403
        assert client.post(f"{ADMIN}/cache/clear", data={"csrf": csrf}, follow_redirects=False).status_code == 303
        assert not cache_file.exists()
        with db.get_conn(settings.database_path) as conn:
            assert int(conn.execute("SELECT COUNT(*) FROM webdav_cache").fetchone()[0]) == 0
            assert conn.execute("SELECT 1 FROM tags WHERE id=?", (source,)).fetchone() is None
            assert int(conn.execute("SELECT enabled FROM tags WHERE id=?", (target,)).fetchone()[0]) == 0


def test_overview_images_tags_ui_escape_csrf_and_pagination(admin_env) -> None:
    settings, app = admin_env
    with new_client(app) as client:
        csrf = login(client)
        assert client.post(f"{ADMIN}/tags", data={"csrf": csrf, "slug": "safe", "display_name": "<b>Safe</b>"}, follow_redirects=False).status_code == 303
        for index in range(2):
            assert client.post(f"{ADMIN}/upload", data={"csrf": csrf, "tags": "safe"}, files={"file": (f"{index}.png", image_bytes(size=(31 + index, 10)), "image/png")}, follow_redirects=False).status_code == 303
        overview = client.get(f"{ADMIN}?message=%3Cscript%3Ebad%3C/script%3E")
        assert all(text in overview.text for text in ("本地图片", "WebDAV", "WebDAV 缓存", "multiple", "archives/preview", "cache/clear", "source=local", "source=webdav"))
        assert "<script>bad</script>" not in overview.text and "&lt;script&gt;bad&lt;/script&gt;" in overview.text
        assert overview.text.count(f'name="csrf" value="{csrf}"') >= 4
        tags = client.get(f"{ADMIN}/tags")
        assert "&lt;b&gt;Safe&lt;/b&gt;" in tags.text and all(part in tags.text for part in ("/edit", "/disable", "/merge"))
        page = client.get(f"{ADMIN}/images", params={"source": "local", "per_page": 1, "orientation": "desktop", "enabled": "1", "tag": "safe"})
        assert 'name="source"' in page.text and 'action="/manage-images/images/' in page.text
        untagged = client.get(f"{ADMIN}/images", params={"source": "local", "per_page": 1, "orientation": "desktop", "enabled": "1"})
        assert "下一页" in untagged.text
        assert all(piece in untagged.text for piece in ("source=local", "per_page=1", "orientation=desktop", "enabled=1"))


def test_upload_tags_invalid_tag_no_write_and_local_disable_survives_scan(admin_env) -> None:
    settings, app = admin_env
    first = image_bytes("PNG", (41, 10))
    with new_client(app) as client:
        csrf = login(client)
        for slug in ("default", "extra"):
            assert client.post(f"{ADMIN}/tags", data={"csrf": csrf, "slug": slug, "display_name": slug}, follow_redirects=False).status_code == 303
        invalid = client.post(f"{ADMIN}/upload", data={"csrf": csrf, "default_tag": "missing"}, files={"file": ("bad.png", first, "image/png")})
        assert invalid.status_code == 400
        assert not list(settings.images_dir.rglob("*.*"))
        uploaded = client.post(
            f"{ADMIN}/upload",
            data={"csrf": csrf, "default_tag": "default", "tags": ["extra", "default"]},
            files={"file": ("first.png", first, "image/png")},
            follow_redirects=False,
        )
        assert uploaded.status_code == 303, uploaded.text
        with db.get_conn(settings.database_path) as conn:
            row = conn.execute("SELECT id FROM images WHERE content_hash IS NOT NULL").fetchone()
            image_id = int(row["id"])
            slugs = {item[0] for item in conn.execute("SELECT t.slug FROM image_tags it JOIN tags t ON t.id=it.tag_id WHERE it.image_id=?", (image_id,))}
        assert slugs == {"default", "extra"}
        for slug in ("default", "extra"):
            record, fallback_used = app.state.catalog.pick("desktop", tag=slug)
            assert record.id == image_id and not fallback_used
        assert client.post(f"{ADMIN}/images/{image_id}/enabled", data={"csrf": csrf, "enabled": "0"}, follow_redirects=False).status_code == 303
        assert client.post(f"{ADMIN}/upload", data={"csrf": csrf}, files={"file": ("second.png", image_bytes("PNG", (42, 10)), "image/png")}, follow_redirects=False).status_code == 303
        with db.get_conn(settings.database_path) as conn:
            assert int(conn.execute("SELECT enabled FROM images WHERE id=?", (image_id,)).fetchone()[0]) == 0


def test_archive_tamper_is_rejected_and_cleaned(admin_env) -> None:
    settings, app = admin_env
    with new_client(app) as client:
        csrf = login(client)
        assert client.post(f"{ADMIN}/tags", data={"csrf": csrf, "slug": "imported", "display_name": "Imported"}, follow_redirects=False).status_code == 303
        token, _ = preview(client, csrf, archive_bytes({"safe.png": image_bytes()}))
        pending = next(settings.upload_tmp_dir.iterdir())
        pending.write_bytes(pending.read_bytes() + b"tampered")
        response = client.post(f"{ADMIN}/archives/confirm", data={"csrf": csrf, "token": token})
        assert response.status_code == 400
        assert not list(settings.upload_tmp_dir.iterdir())
        assert not list(settings.images_dir.rglob("*.*"))


def test_duplicate_archive_merges_tags_and_complex_webdav_href_is_exact(admin_env) -> None:
    settings, app = admin_env
    payload = image_bytes("PNG", (51, 10))
    href = "https://dav.example.test/root/a%2Fb/c image.jpg?x=1%25"
    other_href = href + "-other"
    with new_client(app) as client:
        csrf = login(client)
        for slug in ("imported", "existing", "remote"):
            assert client.post(f"{ADMIN}/tags", data={"csrf": csrf, "slug": slug, "display_name": slug}, follow_redirects=False).status_code == 303
        assert client.post(f"{ADMIN}/upload", data={"csrf": csrf, "tags": "existing"}, files={"file": ("same.png", payload, "image/png")}, follow_redirects=False).status_code == 303
        token, _ = preview(client, csrf, archive_bytes({"folder/same.png": payload}))
        confirmed = client.post(f"{ADMIN}/archives/confirm", data={"csrf": csrf, "token": token, "default_tag": "imported", "map_dirs_present": "1", "map_dirs": "folder"}, follow_redirects=False)
        assert confirmed.status_code == 303, confirmed.text
        with db.get_conn(settings.database_path) as conn:
            assert int(conn.execute("SELECT COUNT(*) FROM images").fetchone()[0]) == 1
            image_id = int(conn.execute("SELECT id FROM images").fetchone()[0])
            slugs = {row[0] for row in conn.execute("SELECT t.slug FROM image_tags it JOIN tags t ON t.id=it.tag_id WHERE it.image_id=?", (image_id,))}
            remote_id = int(conn.execute("SELECT id FROM tags WHERE slug='remote'").fetchone()[0])
            now = db.utc_now()
            for value in (href, other_href):
                conn.execute("INSERT INTO webdav_objects(href,orientation,updated_at) VALUES(?,?,?)", (value, "desktop", now))
                conn.execute("INSERT INTO webdav_object_tags(href,tag_id,origin,created_at) VALUES(?,?,?,?)", (value, remote_id, "admin", now))
        assert {"existing", "imported", "folder"} <= slugs
        for slug in ("existing", "imported", "folder"):
            record, fallback_used = app.state.catalog.pick("desktop", tag=slug)
            assert record.id == image_id and not fallback_used
        page = client.get(f"{ADMIN}/images?source=webdav")
        assert 'action="/manage-images/webdav/enabled"' in page.text
        assert href not in re.findall(r'action="([^"]+)"', page.text)
        assert client.post(f"{ADMIN}/webdav/enabled", data={"csrf": csrf, "href": href, "enabled": "0"}, follow_redirects=False).status_code == 303
        assert client.post(f"{ADMIN}/webdav/tags/remove", data={"csrf": csrf, "href": href, "tag_id": str(remote_id)}, follow_redirects=False).status_code == 303
        with db.get_conn(settings.database_path) as conn:
            assert int(conn.execute("SELECT enabled FROM webdav_objects WHERE href=?", (href,)).fetchone()[0]) == 0
            assert int(conn.execute("SELECT enabled FROM webdav_objects WHERE href=?", (other_href,)).fetchone()[0]) == 1
            assert conn.execute("SELECT 1 FROM webdav_object_tags WHERE href=? AND tag_id=?", (href, remote_id)).fetchone() is None
            assert conn.execute("SELECT 1 FROM webdav_object_tags WHERE href=? AND tag_id=?", (other_href, remote_id)).fetchone() is not None
