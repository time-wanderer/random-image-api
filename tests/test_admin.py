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
from starlette.datastructures import FormData

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
        (settings.images_dir / "square").mkdir(exist_ok=True)
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
        unauthenticated = client.get(f"{ADMIN}/images", follow_redirects=False)
        assert unauthenticated.status_code == 303
        assert unauthenticated.headers["location"] == f"{ADMIN}/login"
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
        expired = client.get(f"{ADMIN}/images", follow_redirects=False)
        assert expired.status_code == 303
        assert expired.headers["location"] == f"{ADMIN}/login"
        assert client.post(f"{ADMIN}/tags", data={"csrf": csrf}).status_code == 401

    with new_client(app) as attacker:
        attacker.cookies.set(settings.admin_cookie_name, "fake.9999999999.bad", path=ADMIN)
        tampered = attacker.get(f"{ADMIN}/images", follow_redirects=False)
        assert tampered.status_code == 303
        assert tampered.headers["location"] == f"{ADMIN}/login"


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
        for slug in ("imported", "featured", "seasonal"):
            assert client.post(
                f"{ADMIN}/tags",
                data={"csrf": csrf, "slug": slug, "display_name": slug.title()},
                follow_redirects=False,
            ).status_code == 303
        token, response = preview(client, csrf, payload)
        assert "dry_run" in response.text
        assert "追加标签（可多选）" in response.text
        assert 'name="tags" value="featured"' in response.text
        assert 'name="tags" value="seasonal"' in response.text
        assert list(settings.images_dir.rglob("*")) == before_files
        with db.get_conn(settings.database_path) as conn:
            assert int(conn.execute("SELECT COUNT(*) FROM images").fetchone()[0]) == before_rows
        temporary = list(settings.upload_tmp_dir.iterdir())
        assert len(temporary) == 1

        confirmed = client.post(
            f"{ADMIN}/archives/confirm",
            data={
                "csrf": csrf,
                "token": token,
                "default_tag": "imported",
                "tags": ["featured", "seasonal", "featured"],
            },
            follow_redirects=False,
        )
        assert confirmed.status_code == 303, confirmed.text
        assert not list(settings.upload_tmp_dir.iterdir())
        with db.get_conn(settings.database_path) as conn:
            assert int(conn.execute("SELECT COUNT(*) FROM images").fetchone()[0]) == 2
            slugs = {row[0] for row in conn.execute("SELECT slug FROM tags")}
            assert {"imported", "featured", "seasonal", "animals"} <= slugs
            rows = conn.execute(
                "SELECT i.orientation,t.slug FROM images i "
                "JOIN image_tags it ON it.image_id=i.id JOIN tags t ON t.id=it.tag_id"
            ).fetchall()
            tags_by_orientation: dict[str, set[str]] = {}
            for row in rows:
                tags_by_orientation.setdefault(str(row[0]), set()).add(str(row[1]))
            assert {"imported", "featured", "seasonal", "animals"} <= tags_by_orientation["desktop"]
            assert {"imported", "featured", "seasonal"} <= tags_by_orientation["mobile"]


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


def test_multipart_form_is_closed_after_upload_request(
    admin_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _settings, app = admin_env
    closed: list[bool] = []
    original_close = FormData.close

    async def recording_close(form: FormData) -> None:
        closed.append(True)
        await original_close(form)

    monkeypatch.setattr(FormData, "close", recording_close)
    with new_client(app) as client:
        csrf = login(client)
        response = client.post(
            f"{ADMIN}/upload",
            data={"csrf": csrf},
            files={"file": ("closed.png", image_bytes(), "image/png")},
            follow_redirects=False,
        )
        assert response.status_code == 303
    assert closed


def test_oversized_archive_is_rejected_without_temp_or_data_residue(admin_env) -> None:
    settings, app = admin_env
    oversized = b"x" * (settings.admin_max_archive_bytes + 1)
    with new_client(app) as client:
        csrf = login(client)
        response = client.post(
            f"{ADMIN}/archives/preview",
            data={"csrf": csrf},
            files={"archive": ("oversized.zip", oversized, "application/zip")},
        )
        assert response.status_code == 413
        assert not list(settings.upload_tmp_dir.glob("*"))
        assert not list(settings.images_dir.rglob("*.*"))
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


def test_v21_masonry_preview_auth_square_upload_and_symlink_rejection(admin_env) -> None:
    settings, app = admin_env
    square = image_bytes("PNG", (32, 32))
    with new_client(app) as client:
        unauthenticated_preview = client.get(
            f"{ADMIN}/images/1/preview",
            follow_redirects=False,
        )
        assert unauthenticated_preview.status_code == 303
        assert unauthenticated_preview.headers["location"] == f"{ADMIN}/login"
        csrf = login(client)
        uploaded = client.post(
            f"{ADMIN}/upload",
            data={"csrf": csrf},
            files={"file": ("square.png", square, "image/png")},
            follow_redirects=False,
        )
        assert uploaded.status_code == 303
        with db.get_conn(settings.database_path) as conn:
            row = conn.execute("SELECT id,rel_path,orientation FROM images").fetchone()
            image_id = int(row["id"])
            assert row["orientation"] == "square"
            assert str(row["rel_path"]).startswith("square/")
        page = client.get(f"{ADMIN}/images?source=local")
        assert page.status_code == 200
        assert 'class="grid"' in page.text
        assert 'class="card"' in page.text
        assert f"/images/{image_id}/preview" in page.text
        assert "真实方向：square" in page.text
        assert "存放目录：square" in page.text
        rendered = client.get(f"{ADMIN}/images/{image_id}/preview")
        assert rendered.status_code == 200
        assert rendered.headers["content-type"].startswith("image/png")
        assert rendered.headers["cache-control"] == "private, max-age=300"
        assert rendered.headers["x-content-type-options"] == "nosniff"

        outside = settings.data_dir / "outside.png"
        outside.write_bytes(square)
        stored = settings.images_dir / str(row["rel_path"])
        stored.unlink()
        stored.symlink_to(outside)
        assert client.get(f"{ADMIN}/images/{image_id}/preview").status_code == 404


def test_v21_move_conflict_single_tags_and_friendly_delete(admin_env) -> None:
    settings, app = admin_env
    with new_client(app) as client:
        csrf = login(client)
        for slug in ("genshin", "wallpaper"):
            assert client.post(
                f"{ADMIN}/tags",
                data={"csrf": csrf, "slug": slug, "display_name": slug.title()},
                follow_redirects=False,
            ).status_code == 303
        assert client.post(
            f"{ADMIN}/upload",
            data={"csrf": csrf},
            files={"file": ("wide.png", image_bytes("PNG", (40, 10)), "image/png")},
            follow_redirects=False,
        ).status_code == 303
        with db.get_conn(settings.database_path) as conn:
            image = conn.execute("SELECT id,rel_path FROM images").fetchone()
            image_id = int(image["id"])
            tag_ids = {row["slug"]: int(row["id"]) for row in conn.execute("SELECT id,slug FROM tags")}
        original = settings.images_dir / str(image["rel_path"])
        conflict = settings.images_dir / "square" / original.name
        conflict.write_bytes(b"occupied")
        moved = client.post(
            f"{ADMIN}/images/{image_id}/move",
            data={"csrf": csrf, "target": "square"},
            follow_redirects=False,
        )
        assert moved.status_code == 303, moved.text
        with db.get_conn(settings.database_path) as conn:
            updated = conn.execute("SELECT rel_path,orientation FROM images WHERE id=?", (image_id,)).fetchone()
        assert str(updated["rel_path"]).startswith("square/")
        assert str(updated["rel_path"]) != f"square/{original.name}"
        assert updated["orientation"] == "desktop"
        assert (settings.images_dir / str(updated["rel_path"])).is_file()
        assert conflict.read_bytes() == b"occupied"

        for slug in ("genshin", "wallpaper"):
            assert client.post(
                f"{ADMIN}/images/{image_id}/tags",
                data={"csrf": csrf, "tag_id": str(tag_ids[slug]), "action": "add"},
                follow_redirects=False,
            ).status_code == 303
        with db.get_conn(settings.database_path) as conn:
            assert {row[0] for row in conn.execute(
                "SELECT t.slug FROM image_tags x JOIN tags t ON t.id=x.tag_id WHERE x.image_id=?",
                (image_id,),
            )} == {"genshin", "wallpaper"}
        record, fallback = app.state.catalog.pick("desktop", tag="genshin")
        assert record.id == image_id and not fallback
        assert client.post(
            f"{ADMIN}/images/{image_id}/tags",
            data={"csrf": csrf, "tag_id": str(tag_ids["wallpaper"]), "action": "remove"},
            follow_redirects=False,
        ).status_code == 303

        page = client.get(f"{ADMIN}/images?source=local")
        assert 'onsubmit="return confirm(' in page.text
        assert 'name="confirm" value="1"' in page.text
        assert "输入 DELETE" not in page.text
        assert client.post(
            f"{ADMIN}/images/{image_id}/delete",
            data={"csrf": csrf, "confirm": "1"},
            follow_redirects=False,
        ).status_code == 303
        assert not (settings.images_dir / str(updated["rel_path"])).exists()


def test_v21_webdav_tag_edit_cached_preview_and_uncached_placeholder(admin_env) -> None:
    settings, app = admin_env
    cached_href = "https://dav.example.test/root/cached%2Fone.png"
    uncached_href = "https://dav.example.test/root/uncached.png"
    payload = image_bytes("PNG", (25, 10))
    with new_client(app) as client:
        csrf = login(client)
        assert client.post(
            f"{ADMIN}/tags",
            data={"csrf": csrf, "slug": "remote-topic", "display_name": "Remote Topic"},
            follow_redirects=False,
        ).status_code == 303
        cache_name = "cached-preview.png"
        (settings.cache_dir / cache_name).write_bytes(payload)
        with db.get_conn(settings.database_path) as conn:
            tag_id = int(conn.execute("SELECT id FROM tags WHERE slug='remote-topic'").fetchone()[0])
            now = db.utc_now()
            for href in (cached_href, uncached_href):
                conn.execute(
                    "INSERT INTO webdav_objects(href,orientation,updated_at) VALUES(?,?,?)",
                    (href, "desktop", now),
                )
            conn.execute(
                "INSERT INTO webdav_cache(href,cache_name,orientation,width,height,content_type,file_size,fetched_at,accessed_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (cached_href, cache_name, "desktop", 25, 10, "image/png", len(payload), 1, 1),
            )
        page = client.get(f"{ADMIN}/images?source=webdav")
        assert page.status_code == 200
        assert "未缓存，不自动下载" in page.text
        assert "/webdav/preview?href=" in page.text
        cached_preview = client.get(f"{ADMIN}/webdav/preview", params={"href": cached_href})
        assert cached_preview.status_code == 200
        assert cached_preview.headers["content-type"].startswith("image/png")
        assert client.get(f"{ADMIN}/webdav/preview", params={"href": uncached_href}).status_code == 404
        assert client.post(
            f"{ADMIN}/webdav/tags",
            data={"csrf": csrf, "href": cached_href, "tag_id": str(tag_id), "action": "add"},
            follow_redirects=False,
        ).status_code == 303
        with db.get_conn(settings.database_path) as conn:
            assert conn.execute(
                "SELECT origin FROM webdav_object_tags WHERE href=? AND tag_id=?",
                (cached_href, tag_id),
            ).fetchone()[0] == "admin"
        assert client.post(
            f"{ADMIN}/webdav/tags",
            data={"csrf": csrf, "href": cached_href, "tag_id": str(tag_id), "action": "remove"},
            follow_redirects=False,
        ).status_code == 303
        with db.get_conn(settings.database_path) as conn:
            assert conn.execute(
                "SELECT 1 FROM webdav_object_tags WHERE href=? AND tag_id=?",
                (cached_href, tag_id),
            ).fetchone() is None


def test_v21_tag_errors_and_empty_state_are_safe(admin_env) -> None:
    settings, app = admin_env
    with new_client(app) as client:
        csrf = login(client)
        overview = client.get(ADMIN)
        assert "当前还没有标签，请先创建标签" in overview.text
        assert "创建第一个标签" in overview.text

        invalid = client.post(
            f"{ADMIN}/tags",
            data={"csrf": csrf, "slug": "BAD SLUG", "display_name": "坏标签"},
        )
        assert invalid.status_code == 400
        assert "创建标签失败" in invalid.text
        assert "Internal Server Error" not in invalid.text

        assert client.post(
            f"{ADMIN}/tags",
            data={"csrf": csrf, "slug": "safe-tag", "display_name": "安全标签"},
            follow_redirects=False,
        ).status_code == 303
        page = client.get(f"{ADMIN}/images?source=local")
        assert "安全标签 — safe-tag" in page.text

        empty_single = client.post(
            f"{ADMIN}/images/999/tags",
            data={"csrf": csrf, "tag_id": "", "action": "add"},
        )
        assert empty_single.status_code == 400
        assert "未选择标签" in empty_single.text
        assert "Traceback" not in empty_single.text

        empty_batch = client.post(
            f"{ADMIN}/images/tags",
            data={"csrf": csrf, "image_ids": "999", "tag_id": "", "action": "add"},
        )
        assert empty_batch.status_code == 400
        assert "未选择标签" in empty_batch.text

        uploaded = client.post(
            f"{ADMIN}/upload",
            data={"csrf": csrf},
            files={"file": ("safe.png", image_bytes(), "image/png")},
            follow_redirects=False,
        )
        assert uploaded.status_code == 303
        with db.get_conn(settings.database_path) as conn:
            image_id = int(conn.execute("SELECT id FROM images").fetchone()[0])

        disabled = client.post(
            f"{ADMIN}/tags/1/disable",
            data={"csrf": csrf, "enabled": "0"},
            follow_redirects=False,
        )
        assert disabled.status_code == 303
        disabled_tag = client.post(
            f"{ADMIN}/images/{image_id}/tags",
            data={"csrf": csrf, "tag_id": "1", "action": "add"},
        )
        assert disabled_tag.status_code == 404
        assert "标签不存在或已禁用" in disabled_tag.text

        empty_webdav = client.post(
            f"{ADMIN}/webdav/tags",
            data={"csrf": csrf, "href": "/missing", "tag_id": "", "action": "add"},
        )
        assert empty_webdav.status_code == 400
        assert "未选择标签" in empty_webdav.text


def test_admin_ux_information_architecture_and_responsive_contract(admin_env) -> None:
    settings, app = admin_env
    with new_client(app) as client:
        csrf = login(client)
        overview = client.get(ADMIN).text
        assert all(text in overview for text in ("开始使用", "创建标签", "上传 / 导入", "浏览并调用 API", "上传图片", "归档导入", "WebDAV 与缓存"))
        assert "source=local" in overview and "source=webdav" in overview
        assert "input type=\"file\"" in overview

        assert client.post(f"{ADMIN}/tags", data={"csrf": csrf, "slug": "summer", "display_name": "夏日"}, follow_redirects=False).status_code == 303
        tags = client.get(f"{ADMIN}/tags").text
        assert all(text in tags for text in ("标签工作台", "显示名称给人看，slug 给 API 用", "本地关联", "WebDAV 关联", "编辑、启停或合并", "保存编辑", "停用标签", "合并并删除源标签"))
        assert "enabled=1" not in tags

        uploaded = client.post(
            f"{ADMIN}/upload",
            data={"csrf": csrf},
            files={"file": ("ux-contract.png", image_bytes(), "image/png")},
            follow_redirects=False,
        )
        assert uploaded.status_code == 303

        images = client.get(f"{ADMIN}/images?source=local").text
        assert all(text in images for text in ("快速筛选", "高级筛选", "真实方向", "存放目录", "筛选结果", "危险"))
        assert "@media(max-width:760px)" in images
        assert ".stats,.steps,.workspace-grid,form.filters,.form-grid{grid-template-columns:1fr;min-width:0}" in images
        assert "white-space:nowrap" in images
        assert "word-break:keep-all" in images
        assert "overflow-x:auto" in images
        assert "overflow-x:hidden" in images
        assert "nav.main-nav{flex-wrap:nowrap;overflow-x:auto" in images
        assert all(text in images for text in ("主导航", "总览", "图片库", "标签工作台", "退出", "快速筛选", "高级筛选", "batch-tags", "name=\"tag_id\"", "name=\"q\""))
        assert "确定删除本地原图？此操作不可撤销。" in images
        assert all(text in overview for text in ("主导航", "总览", "图片库", "标签工作台", "退出", "id=\"upload\"", "name=\"files\"", "name=\"archive\""))
        assert all(text in tags for text in ("主导航", "总览", "图片库", "标签工作台", "退出", "id=\"create-tag\"", "name=\"display_name\"", "name=\"slug\""))

        # 图片库产品化布局契约：宽屏网格、完整比例预览、管理入口与危险区。
        assert "grid-template-columns: repeat(auto-fill, minmax(260px, 1fr))" in images
        assert "aspect-ratio: 4 / 3" in images
        assert "object-fit: contain" in images
        assert ".preview-frame" in images and ".preview-overlay" in images
        assert ".card-action-group" in images and "管理此图片" in images
        assert ".tags-label" in images and ".badge" in images
        assert ".card-action-group.danger-zone" in images
        assert "@media(max-width:390px)" in images
        assert ".grid { grid-template-columns: 1fr; }" in images
        assert "请选择图片" in images
        assert "count?'已选择 '+count+' 张图片':'请选择图片'" in images
        assert 'onsubmit="return confirm(' in images
        assert 'name="confirm" value="1"' in images
        assert 'name="target" required' in images
        assert 'data-batch-image form="batch-tags" name="image_ids"' in images
        assert "function refreshBatchSelection()" in images
        assert "document.querySelectorAll('[data-batch-image]:checked')" in images
        assert "document.addEventListener('DOMContentLoaded',function(){" in images
        assert "if(e.target.matches('[data-batch-image]'))refreshBatchSelection()" in images
        assert "document.querySelectorAll('[data-batch-image]').forEach(function(x){x.checked=checked;});refreshBatchSelection();" in images
        assert "if(bar)bar.hidden=!count" in images


def test_untagged_filter_local_webdav_combinations_injection_and_pagination(admin_env) -> None:
    settings, app = admin_env
    with new_client(app) as client:
        login(client)
        now = db.utc_now()
        with db.get_conn(settings.database_path) as conn:
            tag_id = db.ensure_tag(conn, "topic", "主题")
            disabled_id = db.ensure_tag(conn, "disabled-topic", "停用主题")
            conn.execute("UPDATE tags SET enabled=0 WHERE id=?", (disabled_id,))
            local_rows = (
                ("desktop/untagged-a.png", "desktop", 1),
                ("desktop/untagged-b.png", "desktop", 1),
                ("desktop/tagged.png", "desktop", 1),
                ("desktop/disabled-tagged.png", "desktop", 1),
                ("mobile/untagged-mobile.png", "mobile", 1),
                ("desktop/untagged-off.png", "desktop", 0),
            )
            for index, (rel_path, orientation, enabled) in enumerate(local_rows, 1):
                conn.execute(
                    "INSERT INTO images(rel_path,width,height,orientation,format,content_type,file_size,mtime_ns,updated_at,enabled,source) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (rel_path, 30, 10, orientation, "PNG", "image/png", 10, index, now, enabled, "local"),
                )
            ids = {row["rel_path"]: int(row["id"]) for row in conn.execute("SELECT id,rel_path FROM images")}
            conn.execute("INSERT INTO image_tags(image_id,tag_id,created_at) VALUES(?,?,?)", (ids["desktop/tagged.png"], tag_id, now))
            conn.execute("INSERT INTO image_tags(image_id,tag_id,created_at) VALUES(?,?,?)", (ids["desktop/disabled-tagged.png"], disabled_id, now))

            webdav_rows = (
                ("/remote/untagged-a.png", "desktop", 1),
                ("/remote/untagged-b.png", "desktop", 1),
                ("/remote/tagged.png", "desktop", 1),
                ("/remote/untagged-mobile.png", "mobile", 1),
                ("/remote/untagged-off.png", "desktop", 0),
            )
            for href, orientation, enabled in webdav_rows:
                conn.execute(
                    "INSERT INTO webdav_objects(href,orientation,updated_at,enabled) VALUES(?,?,?,?)",
                    (href, orientation, now, enabled),
                )
            conn.execute("INSERT INTO webdav_object_tags(href,tag_id,origin,created_at) VALUES(?,?,?,?)", ("/remote/tagged.png", tag_id, "admin", now))
            conn.execute(
                "INSERT INTO webdav_cache(href,cache_name,orientation,width,height,content_type,file_size,fetched_at,accessed_at) VALUES(?,?,?,?,?,?,?,?,?)",
                ("/remote/untagged-a.png", "untagged-a.png", "desktop", 30, 10, "image/png", 10, 1, 1),
            )

        local = client.get(
            f"{ADMIN}/images",
            params={"source": "local", "tag": "__untagged__", "orientation": "desktop", "storage": "desktop", "enabled": "1", "q": "untagged", "sort": "filename", "direction": "asc", "per_page": 1},
        )
        assert local.status_code == 200
        assert '<option value="__untagged__" selected>无标签</option>' in local.text
        assert "当前条件：标签：无标签" in local.text
        assert "desktop/untagged-a.png" in local.text and "desktop/tagged.png" not in local.text
        assert "disabled-tagged.png" not in local.text
        assert "下一页" in local.text and "tag=__untagged__" in local.text
        assert all(piece in local.text for piece in ("orientation=desktop", "storage=desktop", "enabled=1", "q=untagged", "sort=filename", "direction=asc"))

        second = client.get(
            f"{ADMIN}/images",
            params={"source": "local", "tag": "__untagged__", "orientation": "desktop", "storage": "desktop", "enabled": "1", "q": "untagged", "sort": "filename", "direction": "asc", "per_page": 1, "page": 2},
        )
        assert '<option value="__untagged__" selected>无标签</option>' in second.text
        assert "上一页" in second.text and "tag=__untagged__" in second.text

        remote = client.get(
            f"{ADMIN}/images",
            params={"source": "webdav", "tag": "__untagged__", "orientation": "desktop", "enabled": "1", "cached": "1", "q": "untagged-a", "sort": "filename", "direction": "asc"},
        )
        assert remote.status_code == 200
        assert "/remote/untagged-a.png" in remote.text
        assert "/remote/untagged-b.png" not in remote.text and "/remote/tagged.png" not in remote.text
        assert "当前条件：标签：无标签" in remote.text

        injection = client.get(f"{ADMIN}/images", params={"source": "local", "tag": "' OR 1=1--"})
        assert injection.status_code == 200
        assert "筛选结果：0 张" in injection.text
        assert "untagged-a.png" not in injection.text and "tagged.png" not in injection.text
        with db.get_conn(settings.database_path) as conn:
            assert int(conn.execute("SELECT COUNT(*) FROM images").fetchone()[0]) == len(local_rows)
            assert int(conn.execute("SELECT COUNT(*) FROM tags").fetchone()[0]) == 2


def test_untagged_filter_empty_state_and_reserved_value_contract(admin_env) -> None:
    _settings, app = admin_env
    with pytest.raises(ValueError):
        db.validate_slug("__untagged__")
    with new_client(app) as client:
        login(client)
        page = client.get(f"{ADMIN}/images", params={"source": "local", "tag": "__untagged__"})
        assert page.status_code == 200
        assert '<option value="">全部标签</option>' in page.text
        assert '<option value="__untagged__" selected>无标签</option>' in page.text
        assert "当前筛选条件下没有无标签图片" in page.text
        assert "前往上传" in page.text and "管理标签" in page.text
        assert "没有匹配的图片" not in page.text
