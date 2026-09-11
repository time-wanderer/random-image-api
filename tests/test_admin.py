from __future__ import annotations

import hashlib
import hmac
import io
import os
import re
import tempfile
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image
from starlette.datastructures import FormData, UploadFile

from app import db
from app.admin import _stage_spooled_upload, create_admin_router
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
        admin_multipart_archive_max_bytes=2 * 1024 * 1024,
        admin_chunked_upload_enabled=True,
        admin_chunk_recommended_bytes=8,
        admin_chunk_min_bytes=1,
        admin_chunk_max_bytes=16,
        admin_chunked_max_upload_bytes=2 * 1024 * 1024,
        admin_chunked_upload_ttl_seconds=60,
        admin_chunked_max_active_tasks=2,
        admin_chunked_max_inflight_patches=2,
        admin_chunked_min_free_bytes=1,
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


def pending_archive_files(settings) -> list[Path]:
    """Exclude persistent upload infrastructure from ordinary preview residue checks."""
    root = settings.upload_tmp_dir
    return [item for item in root.iterdir() if item.name not in {"chunked", ".import.lock"}]


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
        assert client.post(f"{ADMIN}/images/{image_id}/delete", data={"csrf": csrf, "confirm": "DELETE"}).status_code == 400
        assert (settings.images_dir / rel_path).is_file()
        confirmation = client.get(f"{ADMIN}/images/{image_id}/delete-confirm")
        assert confirmation.status_code == 200
        assert "DELETE LOCAL IMAGE" in confirmation.text
        assert client.post(
            f"{ADMIN}/images/{image_id}/delete",
            data={"csrf": csrf, "confirm_phrase": "DELETE LOCAL IMAGE", "confirm_image_id": str(image_id), "expected_path": rel_path},
            follow_redirects=False,
        ).status_code == 303
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
        assert all(fragment in response.text for fragment in (
            "导入摘要",
            "<dt>归档内容</dt><dd>2</dd>",
            "<dt>检查图片</dt><dd>2</dd>",
            "<dt>可导入</dt><dd>2</dd>",
            "<dt>重复</dt><dd>0</dd>",
            "<dt>跳过</dt><dd>0</dd>",
            "<dt>横屏</dt><dd>1</dd>",
            "<dt>竖屏</dt><dd>1</dd>",
            "<dt>方形</dt><dd>0</dd>",
        ))
        assert "<pre>" not in response.text
        assert all(field not in response.text for field in (
            "output_dir", "dry_run", "square_policy", "square_both_storage",
            "files_examined", "bytes_read", "tag_targets", "created_paths",
        ))
        assert "{'archive':" not in response.text
        assert "photos.zip" not in response.text
        assert str(settings.upload_tmp_dir) not in response.text
        assert str(settings.images_dir) not in response.text
        assert "追加标签（可多选）" in response.text
        assert 'name="tags" value="featured"' in response.text
        assert 'name="tags" value="seasonal"' in response.text
        assert list(settings.images_dir.rglob("*")) == before_files
        with db.get_conn(settings.database_path) as conn:
            assert int(conn.execute("SELECT COUNT(*) FROM images").fetchone()[0]) == before_rows
        temporary = pending_archive_files(settings)
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
        assert not pending_archive_files(settings)
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
        assert len(pending_archive_files(settings)) == 1
        forbidden = other.post(f"{ADMIN}/archives/confirm", data={"csrf": other_csrf, "token": token})
        assert forbidden.status_code == 403
        assert not list(settings.images_dir.rglob("*.*"))
        assert owner.post(f"{ADMIN}/logout", data={"csrf": owner_csrf}, follow_redirects=False).status_code == 303
        assert not pending_archive_files(settings)
        assert owner.post(f"{ADMIN}/archives/confirm", data={"csrf": owner_csrf, "token": token}).status_code == 401


def test_archive_preview_expiry_removes_temp_and_cannot_confirm(admin_env) -> None:
    settings, app = admin_env
    with new_client(app) as client:
        csrf = login(client)
        token, _response = preview(client, csrf, archive_bytes({"safe.png": image_bytes()}))
        assert pending_archive_files(settings)
        time.sleep(1.1)
        expired = client.post(f"{ADMIN}/archives/confirm", data={"csrf": csrf, "token": token})
        assert expired.status_code in {404, 410}
        assert not pending_archive_files(settings)
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
        assert not pending_archive_files(settings)
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
        assert not pending_archive_files(settings)
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
        pending = pending_archive_files(settings)[0]
        pending.write_bytes(pending.read_bytes() + b"tampered")
        response = client.post(f"{ADMIN}/archives/confirm", data={"csrf": csrf, "token": token})
        assert response.status_code == 400
        assert not pending_archive_files(settings)
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
        assert 'action="/manage-images/images/' + str(image_id) + '/delete"' not in page.text
        assert "危险操作</summary>" not in page.text
        assert f"/images/{image_id}/detail" in page.text
        assert f"/images/{image_id}/delete-confirm" not in page.text
        confirmation = client.get(f"{ADMIN}/images/{image_id}/delete-confirm")
        assert confirmation.status_code == 200
        assert client.post(
            f"{ADMIN}/images/{image_id}/delete",
            data={"csrf": csrf, "confirm_phrase": "DELETE LOCAL IMAGE", "confirm_image_id": str(image_id), "expected_path": str(updated["rel_path"])},
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
        assert "批量删除确认" in images
        assert "危险操作会要求确认" in images
        assert "name=\"confirm\" value=\"1\"" not in images
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
        assert 'action="/manage-images/images/delete"' not in images
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


def test_upload_forms_expose_client_validation_progress_and_fallback(admin_env) -> None:
    settings, app = admin_env
    with new_client(app) as client:
        csrf = login(client)
        assert client.post(
            f"{ADMIN}/tags",
            data={"csrf": csrf, "slug": "upload-test", "display_name": "上传测试"},
            follow_redirects=False,
        ).status_code == 303
        page = client.get(ADMIN).text
        upload_script = client.get(f"{ADMIN}/admin-upload.js")

    assert upload_script.status_code == 200
    assert upload_script.headers["content-type"].startswith("text/javascript")
    assert f'data-upload-kind="image" data-max-bytes="{settings.admin_max_upload_bytes}"' in page
    assert f'data-upload-kind="archive" data-max-bytes="{settings.admin_multipart_archive_max_bytes}"' in page
    assert 'accept=".jpg,.jpeg,.png,.webp,image/jpeg,image/png,image/webp"' in page
    assert 'accept=".zip,.tar.gz,.tgz"' in page
    assert 'method="post"' in page and 'enctype="multipart/form-data"' in page
    assert f'<script src="{ADMIN}/admin-upload.js" defer></script>' in page
    assert "XMLHttpRequest" not in page and "xhr.upload.onprogress" not in page
    assert all(marker in page for marker in (
        "data-upload-error", "data-upload-status", "data-chunked-enabled",
        "data-chunked-url", "data-chunked-max-bytes", "data-chunk-threshold-bytes",
    ))
    assert all(text in page for text in (
        "支持 JPG、JPEG、PNG、WebP", "支持 ZIP、TAR.GZ、TGZ",
        "普通上传上限 2.00 MiB", "上传前所选标签将应用于本次全部图片",
        "归档确认页可调整",
    ))
    assert all(text not in page for text in (
        "UPLOAD_TMP_DIR", "进程内存", "TTL", "浏览器", "边缘临时存储",
        "Cloudflare", "2.56 GiB", "代理限制", "CLI",
    ))


def test_archive_preview_preserves_selected_tags_and_confirm_revalidates(admin_env) -> None:
    settings, app = admin_env
    payload = archive_bytes({"safe.png": image_bytes()})
    with new_client(app) as client:
        csrf = login(client)
        for slug in ("primary", "extra"):
            assert client.post(
                f"{ADMIN}/tags",
                data={"csrf": csrf, "slug": slug, "display_name": slug.title()},
                follow_redirects=False,
            ).status_code == 303
        response = client.post(
            f"{ADMIN}/archives/preview",
            data={"csrf": csrf, "default_tag": "primary", "tags": "extra"},
            files={"archive": ("selected.zip", payload, "application/zip")},
        )
        assert response.status_code == 200
        assert re.search(r'<option value="primary" selected>', response.text)
        assert re.search(r'name="tags" value="extra" checked', response.text)
        assert "确认前仍可调整本次全部图片的标签" in response.text
        token_match = re.search(r'name="token" value="([^"]+)"', response.text)
        assert token_match
        token = token_match.group(1)

        with db.get_conn(settings.database_path) as conn:
            conn.execute("UPDATE tags SET enabled=0 WHERE slug='extra'")
        rejected = client.post(
            f"{ADMIN}/archives/confirm",
            data={"csrf": csrf, "token": token, "default_tag": "primary", "tags": "extra", "tags_present": "1"},
        )
        assert rejected.status_code == 400
        assert not pending_archive_files(settings)
        assert not list(settings.images_dir.rglob("*.*"))


def test_archive_confirm_uses_tags_saved_in_preview_state(admin_env) -> None:
    settings, app = admin_env
    with new_client(app) as client:
        csrf = login(client)
        for slug in ("primary", "extra"):
            assert client.post(
                f"{ADMIN}/tags",
                data={"csrf": csrf, "slug": slug, "display_name": slug.title()},
                follow_redirects=False,
            ).status_code == 303
        response = client.post(
            f"{ADMIN}/archives/preview",
            data={"csrf": csrf, "default_tag": "primary", "tags": "extra"},
            files={"archive": ("saved-tags.zip", archive_bytes({"safe.png": image_bytes()}), "application/zip")},
        )
        token_match = re.search(r'name="token" value="([^"]+)"', response.text)
        assert response.status_code == 200 and token_match
        confirmed = client.post(
            f"{ADMIN}/archives/confirm",
            data={"csrf": csrf, "token": token_match.group(1)},
            follow_redirects=False,
        )
        assert confirmed.status_code == 303
        with db.get_conn(settings.database_path) as conn:
            slugs = {
                str(row[0])
                for row in conn.execute(
                    "SELECT t.slug FROM image_tags it JOIN tags t ON t.id=it.tag_id"
                )
            }
        assert {"primary", "extra"} <= slugs
        assert not pending_archive_files(settings)


@pytest.mark.parametrize("slug", ["unknown", "disabled"])
def test_archive_preview_rejects_unknown_or_disabled_tag_without_temp(admin_env, slug: str) -> None:
    settings, app = admin_env
    with new_client(app) as client:
        csrf = login(client)
        if slug == "disabled":
            with db.get_conn(settings.database_path) as conn:
                tag_id = db.ensure_tag(conn, slug, "Disabled")
                conn.execute("UPDATE tags SET enabled=0 WHERE id=?", (tag_id,))
        response = client.post(
            f"{ADMIN}/archives/preview",
            data={"csrf": csrf, "tags": slug},
            files={"archive": ("tagged.zip", archive_bytes({"safe.png": image_bytes()}), "application/zip")},
        )
        assert response.status_code == 400
        assert not pending_archive_files(settings)
        assert not list(settings.images_dir.rglob("*.*"))


def chunked_create(client: TestClient, csrf: str, payload: bytes, name: str = "large.zip") -> tuple[str, object]:
    response = client.post(
        f"{ADMIN}/archives/uploads",
        headers={"X-CSRF-Token": csrf},
        json={"filename": name, "size": len(payload), "fingerprint": hashlib.sha256(payload[:65536] + payload[-65536:]).hexdigest(), "default_tag": "", "tags": []},
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"]), response


def test_chunked_upload_migration_normal_offset_conflict_complete_confirm_and_recovery(admin_env) -> None:
    settings, app = admin_env
    settings.admin_chunk_min_bytes = 1
    settings.admin_chunk_recommended_bytes = 8
    settings.admin_chunk_max_bytes = 16
    settings.admin_chunked_max_upload_bytes = 2 * 1024 * 1024
    settings.admin_chunked_min_free_bytes = 1
    payload = archive_bytes({"topic/safe.png": image_bytes()})
    with new_client(app) as client:
        csrf = login(client)
        task_id, created = chunked_create(client, csrf, payload)
        assert created.headers["upload-offset"] == "0"
        task_dir = settings.upload_tmp_dir / "chunked" / task_id
        assert [item.name for item in task_dir.iterdir()] == ["upload.bin"]
        first = client.patch(
            f"{ADMIN}/archives/uploads/{task_id}",
            headers={"X-CSRF-Token": csrf, "Upload-Offset": "0", "Content-Type": "application/offset+octet-stream"},
            content=payload[:8],
        )
        assert first.status_code == 204 and first.headers["upload-offset"] == "8"
        conflict = client.patch(
            f"{ADMIN}/archives/uploads/{task_id}",
            headers={"X-CSRF-Token": csrf, "Upload-Offset": "0", "Content-Type": "application/offset+octet-stream"},
            content=payload[8:16],
        )
        assert conflict.status_code == 409
        assert conflict.headers["upload-offset"] == "8"
        assert conflict.json()["error"]["code"] == "offset_mismatch"
        offset = 8
        while offset < len(payload):
            part = payload[offset:offset + 16]
            response = client.patch(
                f"{ADMIN}/archives/uploads/{task_id}",
                headers={"X-CSRF-Token": csrf, "Upload-Offset": str(offset), "Content-Type": "application/offset+octet-stream"},
                content=part,
            )
            assert response.status_code == 204, response.text
            offset = int(response.headers["upload-offset"])
        assert client.head(f"{ADMIN}/archives/uploads/{task_id}").headers["upload-offset"] == str(len(payload))
        completed = client.post(f"{ADMIN}/archives/uploads/{task_id}/complete", headers={"X-CSRF-Token": csrf})
        assert completed.status_code == 200, completed.text
        preview_page = client.get(completed.json()["preview_url"])
        assert preview_page.status_code == 200 and "导入摘要" in preview_page.text
        owner_cookie = client.cookies.get("ria_admin_session_upload_owner")
        assert owner_cookie

    # Router reconstruction simulates a restart. The same browser retains its
    # signed owner cookie, then obtains a fresh in-memory admin Session.
    restarted = FastAPI()
    restarted.state.catalog = Catalog(settings)
    restarted.state.catalog.scan()
    restarted.include_router(create_admin_router(settings))
    with new_client(restarted) as client:
        client.cookies.set("ria_admin_session_upload_owner", owner_cookie, path=ADMIN)
        csrf = login(client)
        status = client.head(f"{ADMIN}/archives/uploads/{task_id}")
        assert status.status_code == 204 and status.headers["upload-state"] == "preview_ready"
        page = client.get(f"{ADMIN}/archives/uploads/{task_id}/preview")
        token = re.search(r'name="token" value="([^"]+)"', page.text).group(1)  # type: ignore[union-attr]
        confirmed = client.post(f"{ADMIN}/archives/confirm", data={"csrf": csrf, "token": token}, follow_redirects=False)
        assert confirmed.status_code == 303, confirmed.text
        repeated = client.post(f"{ADMIN}/archives/confirm", data={"csrf": csrf, "token": token}, follow_redirects=False)
        assert repeated.status_code == 404
        assert not task_dir.exists()
        with db.get_conn(settings.database_path) as conn:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
            assert conn.execute("SELECT 1 FROM upload_tasks WHERE id=?", (task_id,)).fetchone() is None


def test_chunked_upload_auth_csrf_quota_limits_cancel_ttl_and_orphan_cleanup(admin_env, monkeypatch) -> None:
    settings, app = admin_env
    settings.admin_chunk_min_bytes = 1
    settings.admin_chunk_recommended_bytes = 4
    settings.admin_chunk_max_bytes = 8
    settings.admin_chunked_max_upload_bytes = 32
    settings.admin_chunked_max_active_tasks = 1
    settings.admin_chunked_upload_ttl_seconds = 1
    settings.admin_chunked_min_free_bytes = 1
    app = FastAPI()
    app.state.catalog = Catalog(settings)
    app.state.catalog.scan()
    app.include_router(create_admin_router(settings))
    with new_client(app) as client:
        assert client.post(f"{ADMIN}/archives/uploads", json={"filename": "a.zip", "size": 4}).status_code == 401
        csrf = login(client)
        assert client.post(f"{ADMIN}/archives/uploads", json={"filename": "a.zip", "size": 4}).status_code == 403
        too_large = client.post(f"{ADMIN}/archives/uploads", headers={"X-CSRF-Token": csrf}, json={"filename": "a.zip", "size": 33})
        assert too_large.status_code == 413 and too_large.json()["error"]["code"] == "upload_too_large"
        task_id, _ = chunked_create(client, csrf, b"1234")
        full = client.post(f"{ADMIN}/archives/uploads", headers={"X-CSRF-Token": csrf}, json={"filename": "b.zip", "size": 4})
        assert full.status_code == 429 and full.json()["error"]["code"] == "too_many_active_uploads"
        oversized = client.patch(
            f"{ADMIN}/archives/uploads/{task_id}",
            headers={"X-CSRF-Token": csrf, "Upload-Offset": "0", "Content-Type": "application/offset+octet-stream"},
            content=b"123456789",
        )
        assert oversized.status_code == 413 and oversized.json()["error"]["code"] == "chunk_too_large"
        assert client.head(f"{ADMIN}/archives/uploads/{task_id}").headers["upload-offset"] == "0"
        assert client.delete(f"{ADMIN}/archives/uploads/{task_id}").status_code == 403
        assert client.delete(f"{ADMIN}/archives/uploads/{task_id}", headers={"X-CSRF-Token": csrf}).status_code == 204
        assert client.head(f"{ADMIN}/archives/uploads/{task_id}").status_code == 404

        task_id, _ = chunked_create(client, csrf, b"1234")
        orphan = settings.upload_tmp_dir / "chunked" / ("a" * 48)
        orphan.mkdir(); (orphan / "upload.bin").write_bytes(b"orphan")
        old = time.time() - 120
        os.utime(orphan, (old, old))
        with db.get_conn(settings.database_path) as conn:
            conn.execute("UPDATE upload_tasks SET expires_at=? WHERE id=?", (time.time() - 1, task_id))
        expired = client.head(f"{ADMIN}/archives/uploads/{task_id}")
        assert expired.status_code in {404, 410}
        assert not orphan.exists()


def test_chunked_upload_rejects_non_object_json_and_fingerprint_mismatch(admin_env) -> None:
    _settings, app = admin_env
    with new_client(app) as client:
        csrf = login(client)
        for body in ([], "archive"):
            response = client.post(
                f"{ADMIN}/archives/uploads",
                headers={"X-CSRF-Token": csrf},
                json=body,
            )
            assert response.status_code == 400
            assert response.json()["error"]["code"] == "invalid_request"
        null_response = client.post(
            f"{ADMIN}/archives/uploads",
            headers={
                "X-CSRF-Token": csrf,
                "Content-Type": "application/json",
            },
            content=b"null",
        )
        assert null_response.status_code == 400
        assert null_response.json()["error"]["code"] == "invalid_request"
        payload = b"1234"
        task_id, created = chunked_create(client, csrf, payload)
        assert created.headers["upload-fingerprint"] == hashlib.sha256(payload + payload).hexdigest()
        assert client.head(f"{ADMIN}/archives/uploads/{task_id}").headers["upload-fingerprint"] == created.headers["upload-fingerprint"]


def test_persistent_preview_owner_survives_relogin_but_isolated_from_other_browser(admin_env) -> None:
    _settings, app = admin_env
    payload = archive_bytes({"topic/safe.png": image_bytes()})
    with new_client(app) as first:
        csrf = login(first)
        task_id, _ = chunked_create(first, csrf, payload)
        offset = 0
        while offset < len(payload):
            part = payload[offset:offset + 16]
            response = first.patch(
                f"{ADMIN}/archives/uploads/{task_id}",
                headers={"X-CSRF-Token": csrf, "Upload-Offset": str(offset), "Content-Type": "application/offset+octet-stream"},
                content=part,
            )
            assert response.status_code == 204
            offset = int(response.headers["upload-offset"])
        assert first.post(f"{ADMIN}/archives/uploads/{task_id}/complete", headers={"X-CSRF-Token": csrf}).status_code == 200
        first.post(f"{ADMIN}/logout", data={"csrf": csrf}, follow_redirects=False)
        csrf = login(first)
        assert first.get(f"{ADMIN}/archives/uploads/{task_id}/preview").status_code == 200

    with new_client(app) as second:
        login(second)
        assert second.head(f"{ADMIN}/archives/uploads/{task_id}").status_code == 404
        assert second.get(f"{ADMIN}/archives/uploads/{task_id}/preview").status_code == 404

def test_chunked_upload_low_disk_and_file_ahead_rolls_back(admin_env, monkeypatch) -> None:
    settings, app = admin_env
    settings.admin_chunk_min_bytes = 1
    settings.admin_chunk_recommended_bytes = 4
    settings.admin_chunk_max_bytes = 8
    settings.admin_chunked_max_upload_bytes = 32
    settings.admin_chunked_min_free_bytes = 10
    from app import chunked_upload
    real_usage = chunked_upload.shutil.disk_usage
    monkeypatch.setattr(chunked_upload.shutil, "disk_usage", lambda _path: type("Usage", (), {"free": 12})())
    app = FastAPI()
    app.state.catalog = Catalog(settings)
    app.state.catalog.scan()
    app.include_router(create_admin_router(settings))
    with new_client(app) as client:
        csrf = login(client)
        low = client.post(f"{ADMIN}/archives/uploads", headers={"X-CSRF-Token": csrf}, json={"filename": "a.zip", "size": 4})
        assert low.status_code == 507 and low.json()["error"]["code"] == "insufficient_storage"
    monkeypatch.setattr(chunked_upload.shutil, "disk_usage", real_usage)


def test_chunked_patch_rejects_invalid_headers_with_machine_errors(admin_env) -> None:
    _settings, app = admin_env
    with new_client(app) as client:
        csrf = login(client)
        task_id, _ = chunked_create(client, csrf, b"1234")
        invalid = client.patch(
            f"{ADMIN}/archives/uploads/{task_id}",
            headers={"X-CSRF-Token": csrf, "Upload-Offset": "bad", "Content-Length": "4", "Content-Type": "application/offset+octet-stream"},
            content=b"1234",
        )
        assert invalid.status_code == 400
        assert invalid.json()["error"]["code"] == "invalid_headers"
        chunked = client.patch(
            f"{ADMIN}/archives/uploads/{task_id}",
            headers={"X-CSRF-Token": csrf, "Upload-Offset": "0", "Transfer-Encoding": "chunked", "Content-Type": "application/offset+octet-stream"},
            content=b"1234",
        )
        assert chunked.status_code == 400
        assert chunked.json()["error"]["code"] == "chunked_transfer_forbidden"
        assert client.head(f"{ADMIN}/archives/uploads/{task_id}").headers["upload-offset"] == "0"


def test_invalid_archive_complete_keeps_retryable_task_until_cancel(admin_env) -> None:
    settings, app = admin_env
    with new_client(app) as client:
        csrf = login(client)
        payload = b"not-a-zip"
        task_id, _ = chunked_create(client, csrf, payload)
        sent = client.patch(
            f"{ADMIN}/archives/uploads/{task_id}",
            headers={"X-CSRF-Token": csrf, "Upload-Offset": "0", "Content-Type": "application/offset+octet-stream"},
            content=payload,
        )
        assert sent.status_code == 204
        for _ in range(2):
            failed = client.post(f"{ADMIN}/archives/uploads/{task_id}/complete", headers={"X-CSRF-Token": csrf})
            assert failed.status_code == 400
            assert failed.json()["error"]["code"] == "invalid_archive"
        with db.get_conn(settings.database_path) as conn:
            row = conn.execute("SELECT state,error_code FROM upload_tasks WHERE id=?", (task_id,)).fetchone()
            assert tuple(row) == ("receiving", "invalid_archive")
        assert client.delete(f"{ADMIN}/archives/uploads/{task_id}", headers={"X-CSRF-Token": csrf}).status_code == 204


def test_chunked_patch_requires_content_length_at_asgi_boundary(admin_env) -> None:
    _settings, app = admin_env

    class StripLength:
        def __init__(self, wrapped):
            self.wrapped = wrapped

        async def __call__(self, scope, receive, send):
            if scope.get("type") == "http" and scope.get("method") == "PATCH":
                scope = dict(scope)
                scope["headers"] = [
                    (name, value)
                    for name, value in scope.get("headers", [])
                    if name.lower() not in {b"content-length", b"transfer-encoding"}
                ]
            await self.wrapped(scope, receive, send)

    with TestClient(StripLength(app), base_url="https://testserver") as client:
        csrf = login(client)
        task_id, _ = chunked_create(client, csrf, b"1234")
        response = client.patch(
            f"{ADMIN}/archives/uploads/{task_id}",
            headers={
                "X-CSRF-Token": csrf,
                "Upload-Offset": "0",
                "Content-Type": "application/offset+octet-stream",
            },
            content=b"1234",
        )
        assert response.status_code == 411
        assert response.json()["error"]["code"] == "content_length_required"


def test_persistent_confirm_failure_rolls_back_only_importer_created_files(admin_env, monkeypatch) -> None:
    settings, app = admin_env
    payload = archive_bytes({"safe.png": image_bytes()})
    unrelated = settings.images_dir / "desktop" / "unrelated.bin"
    unrelated.write_bytes(b"belongs-to-another-operation")

    with new_client(app) as client:
        csrf = login(client)
        task_id, _ = chunked_create(client, csrf, payload)
        offset = 0
        while offset < len(payload):
            part = payload[offset:offset + 16]
            response = client.patch(
                f"{ADMIN}/archives/uploads/{task_id}",
                headers={
                    "X-CSRF-Token": csrf,
                    "Upload-Offset": str(offset),
                    "Content-Type": "application/offset+octet-stream",
                },
                content=part,
            )
            assert response.status_code == 204
            offset = int(response.headers["upload-offset"])
        completed = client.post(
            f"{ADMIN}/archives/uploads/{task_id}/complete",
            headers={"X-CSRF-Token": csrf},
        )
        assert completed.status_code == 200
        preview = client.get(completed.json()["preview_url"])
        match = re.search(r'name="token" value="([^"]+)"', preview.text)
        assert match
        token = match.group(1)

        original_scan = app.state.catalog.scan
        calls = 0

        def fail_once():
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("simulated post-import scan failure")
            return original_scan()

        monkeypatch.setattr(app.state.catalog, "scan", fail_once)
        with pytest.raises(RuntimeError, match="simulated post-import scan failure"):
            client.post(
                f"{ADMIN}/archives/confirm",
                data={"csrf": csrf, "token": token},
                follow_redirects=False,
            )

        assert unrelated.read_bytes() == b"belongs-to-another-operation"
        imported = [item for item in settings.images_dir.rglob("*.*") if item != unrelated]
        assert imported == []
        with db.get_conn(settings.database_path) as conn:
            assert conn.execute("SELECT 1 FROM upload_tasks WHERE id=?", (task_id,)).fetchone()



def test_cookie_secure_setting_scheme_and_trusted_forwarded_proto(admin_env) -> None:
    settings, _app = admin_env

    def login_cookie(*, forced: bool, base_url: str, forwarded: str = "") -> str:
        settings.admin_cookie_secure = forced
        app = FastAPI()
        app.state.catalog = Catalog(settings)
        app.state.catalog.scan()
        app.include_router(create_admin_router(settings))
        headers = {"x-forwarded-proto": forwarded} if forwarded else {}
        with TestClient(app, base_url=base_url) as client:
            response = client.post(f"{ADMIN}/login", data={"token": "correct horse"}, headers=headers, follow_redirects=False)
            return response.headers["set-cookie"].lower()

    assert "secure" in login_cookie(forced=True, base_url="http://testserver")
    assert "secure" in login_cookie(forced=False, base_url="https://testserver")
    assert "secure" in login_cookie(forced=False, base_url="http://testserver", forwarded="https")
    assert "secure" not in login_cookie(forced=False, base_url="http://testserver")


def test_logout_and_clear_removes_owner_tasks_but_normal_logout_keeps_them(admin_env) -> None:
    settings, app = admin_env
    with new_client(app) as client:
        csrf = login(client)
        task_id, _ = chunked_create(client, csrf, b"1234")
        owner_cookie = client.cookies.get("ria_admin_session_upload_owner")
        normal = client.post(f"{ADMIN}/logout", data={"csrf": csrf}, follow_redirects=False)
        assert normal.status_code == 303
        assert client.cookies.get("ria_admin_session_upload_owner") == owner_cookie
        with db.get_conn(settings.database_path) as conn:
            assert conn.execute("SELECT 1 FROM upload_tasks WHERE id=?", (task_id,)).fetchone()
        csrf = login(client)
        cleared = client.post(f"{ADMIN}/logout-and-clear", data={"csrf": csrf}, follow_redirects=False)
        assert cleared.status_code == 303
        assert client.cookies.get("ria_admin_session_upload_owner") is None
        with db.get_conn(settings.database_path) as conn:
            assert conn.execute("SELECT 1 FROM upload_tasks WHERE id=?", (task_id,)).fetchone() is None
        assert not (settings.upload_tmp_dir / "chunked" / task_id).exists()


def test_chunked_create_raw_body_limit_and_duplicate_headers(admin_env) -> None:
    _settings, app = admin_env

    class RewriteHeaders:
        def __init__(self, wrapped, mode: str):
            self.wrapped = wrapped
            self.mode = mode

        async def __call__(self, scope, receive, send):
            if scope.get("type") == "http" and scope.get("path", "").endswith("/archives/uploads"):
                scope = dict(scope)
                headers = list(scope.get("headers", []))
                if self.mode == "duplicate":
                    current = next(value for name, value in headers if name.lower() == b"content-length")
                    headers.append((b"content-length", current))
                elif self.mode == "transfer":
                    headers.append((b"transfer-encoding", b"chunked"))
                elif self.mode == "oversize":
                    headers = [(name, b"65537" if name.lower() == b"content-length" else value) for name, value in headers]
                scope["headers"] = headers
            await self.wrapped(scope, receive, send)

    for mode, status, code in (
        ("duplicate", 400, "invalid_content_length"),
        ("transfer", 400, "chunked_transfer_forbidden"),
        ("oversize", 413, "request_too_large"),
    ):
        with TestClient(RewriteHeaders(app, mode), base_url="https://testserver") as client:
            csrf = login(client)
            response = client.post(
                f"{ADMIN}/archives/uploads",
                headers={"X-CSRF-Token": csrf},
                json={"filename": "a.zip", "size": 4, "tags": []},
            )
            assert response.status_code == status
            assert response.json()["error"]["code"] == code


def test_chunked_create_field_bounds_are_structured_400(admin_env) -> None:
    _settings, app = admin_env
    invalid_payloads = (
        {"filename": "a" * 256 + ".zip", "size": 4},
        {"filename": "bad\x00name.zip", "size": 4},
        {"filename": "a.zip", "size": 4, "fingerprint": "x" * 129},
        {"filename": "a.zip", "size": 4, "default_tag": "x" * 64},
        {"filename": "a.zip", "size": 4, "tags": ["a"] * 65},
        {"filename": "a.zip", "size": 4, "tags": [1]},
        {"filename": "a.zip", "size": 4, "unknown": "x"},
    )
    with new_client(app) as client:
        csrf = login(client)
        for payload in invalid_payloads:
            response = client.post(f"{ADMIN}/archives/uploads", headers={"X-CSRF-Token": csrf}, json=payload)
            assert response.status_code == 400
            assert response.json()["error"]["code"] == "invalid_fields"

        literal_escape = client.post(
            f"{ADMIN}/archives/uploads",
            headers={"X-CSRF-Token": csrf},
            json={"filename": r"literal\x00name.zip", "size": 4},
        )
        assert literal_escape.status_code == 201


def test_two_ordinary_previews_same_digest_and_same_preview_confirm_once(admin_env, monkeypatch) -> None:
    settings, app = admin_env
    payload = archive_bytes({"same.png": image_bytes()})
    entered = Event()
    release = Event()
    real_import = __import__("app.importer", fromlist=["import_archive"]).import_archive
    upload_store_type = __import__("app.chunked_upload", fromlist=["UploadStore"]).UploadStore
    real_capacity_check = upload_store_type.ensure_import_capacity
    required_checks: list[int] = []

    def recording_capacity_check(store, task_id, required_bytes):
        required_checks.append(required_bytes)
        return real_capacity_check(store, task_id, required_bytes)

    def paused_import(*args, **kwargs):
        if not kwargs.get("dry_run") and not entered.is_set():
            entered.set()
            assert release.wait(5)
        return real_import(*args, **kwargs)

    monkeypatch.setattr("app.admin.importer.import_archive", paused_import)
    monkeypatch.setattr(upload_store_type, "ensure_import_capacity", recording_capacity_check)
    with new_client(app) as owner:
        csrf = login(owner)
        first, _ = preview(owner, csrf, payload)
        second, _ = preview(owner, csrf, payload)
        cookies = dict(owner.cookies)
    barrier = Barrier(3)

    def confirm(token: str):
        with new_client(app) as client:
            client.cookies.update(cookies)
            barrier.wait(timeout=2)
            return client.post(f"{ADMIN}/archives/confirm", data={"csrf": csrf, "token": token}, follow_redirects=False)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(confirm, first), pool.submit(confirm, second)]
        barrier.wait(timeout=2)
        assert entered.wait(5)
        release.set()
        results = [future.result(timeout=10) for future in futures]
    assert [response.status_code for response in results] == [303, 303]
    assert len(list(settings.images_dir.rglob("*.png"))) == 1
    assert sorted(required_checks) == [0, len(image_bytes())]

    with new_client(app) as owner:
        owner.cookies.update(cookies)
        token, _ = preview(owner, csrf, payload)
    barrier = Barrier(3)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(confirm, token), pool.submit(confirm, token)]
        barrier.wait(timeout=2)
        statuses = sorted(future.result(timeout=10).status_code for future in futures)
    assert statuses in ([303, 404], [303, 409])


def test_chunked_patch_rejects_duplicate_content_length_and_transfer_encoding(admin_env) -> None:
    _settings, app = admin_env

    class RewritePatchHeaders:
        def __init__(self, wrapped, transfer: bool):
            self.wrapped = wrapped
            self.transfer = transfer

        async def __call__(self, scope, receive, send):
            if scope.get("type") == "http" and scope.get("method") == "PATCH":
                scope = dict(scope)
                headers = list(scope.get("headers", []))
                if self.transfer:
                    headers.append((b"transfer-encoding", b"chunked"))
                else:
                    length = next(value for name, value in headers if name.lower() == b"content-length")
                    headers.append((b"content-length", length))
                scope["headers"] = headers
            await self.wrapped(scope, receive, send)

    for transfer, code in ((False, "invalid_content_length"), (True, "chunked_transfer_forbidden")):
        with TestClient(RewritePatchHeaders(app, transfer), base_url="https://testserver") as client:
            csrf = login(client)
            task_id, _ = chunked_create(client, csrf, b"1234")
            response = client.patch(
                f"{ADMIN}/archives/uploads/{task_id}",
                headers={"X-CSRF-Token": csrf, "Upload-Offset": "0", "Content-Type": "application/offset+octet-stream"},
                content=b"1234",
            )
            assert response.status_code == 400
            assert response.json()["error"]["code"] == code


def test_multipart_spool_staging_checks_peak_space_and_cleans_failures(tmp_path: Path, monkeypatch) -> None:
    payload = b"server-side-spool"
    spool = tempfile.SpooledTemporaryFile(max_size=1, mode="w+b")
    spool.write(payload)
    uploaded = UploadFile(file=spool, filename="client-name-is-not-a-path.zip")
    destination = tmp_path / "controlled" / "archive.zip"

    monkeypatch.setattr(
        "app.admin.shutil.disk_usage",
        lambda _path: type("Usage", (), {"free": len(payload) + 20})(),
    )
    size, digest = _stage_spooled_upload(
        uploaded,
        destination,
        limit=1024,
        min_free=10,
    )
    assert size == len(payload)
    assert digest == hashlib.sha256(payload).hexdigest()
    assert destination.read_bytes() == payload

    second_spool = tempfile.SpooledTemporaryFile(max_size=1, mode="w+b")
    second_spool.write(payload)
    second = UploadFile(file=second_spool, filename="ignored.zip")
    rejected = tmp_path / "controlled" / "rejected.zip"
    monkeypatch.setattr(
        "app.admin.shutil.disk_usage",
        lambda _path: type("Usage", (), {"free": len(payload) + 10})(),
    )
    with pytest.raises(Exception) as caught:
        _stage_spooled_upload(
            second,
            rejected,
            limit=1024,
            min_free=10,
            reserved_bytes=1,
        )
    assert getattr(caught.value, "status_code", None) == 507
    assert not rejected.exists()
    spool.close()
    second_spool.close()


def test_upload_owner_cookie_rolls_near_expiry_without_losing_task(admin_env) -> None:
    settings, _app = admin_env
    settings.admin_upload_owner_ttl_seconds = 120
    app = FastAPI()
    app.state.catalog = Catalog(settings)
    app.state.catalog.scan()
    app.include_router(create_admin_router(settings))

    cookie_name = "ria_admin_session_upload_owner"
    with new_client(app) as client:
        csrf = login(client)
        task_id, _created = chunked_create(client, csrf, b"1234")
        current = client.cookies.get(cookie_name)
        assert current
        owner_id = current.split(".", 1)[0]
        expiry = int(time.time()) + 1
        payload = f"upload-owner.{owner_id}.{expiry}"
        signature = hmac.new(
            settings.admin_session_secret.encode(),
            payload.encode(),
            hashlib.sha256,
        ).hexdigest()
        existing_owner = next(cookie for cookie in client.cookies.jar if cookie.name == cookie_name)
        client.cookies.set(
            cookie_name,
            f"{owner_id}.{expiry}.{signature}",
            domain=existing_owner.domain,
            path=existing_owner.path,
        )

        response = client.head(f"{ADMIN}/archives/uploads/{task_id}")
        assert response.status_code == 204
        set_cookie = response.headers.get("set-cookie", "").lower()
        assert cookie_name in set_cookie
        assert "httponly" in set_cookie
        assert "samesite=strict" in set_cookie
        assert "secure" in set_cookie
        assert f"path={ADMIN}" in set_cookie
        renewed = client.cookies.get(cookie_name)
        assert renewed and renewed.split(".", 1)[0] == owner_id
        assert int(renewed.split(".", 2)[1]) > expiry


def test_same_persistent_preview_concurrent_confirm_imports_once(admin_env) -> None:
    settings, app = admin_env
    payload = archive_bytes({"same.png": image_bytes()})
    with new_client(app) as owner:
        csrf = login(owner)
        task_id, _ = chunked_create(owner, csrf, payload)
        offset = 0
        while offset < len(payload):
            part = payload[offset:offset + 16]
            response = owner.patch(
                f"{ADMIN}/archives/uploads/{task_id}",
                headers={"X-CSRF-Token": csrf, "Upload-Offset": str(offset), "Content-Type": "application/offset+octet-stream"},
                content=part,
            )
            assert response.status_code == 204
            offset = int(response.headers["upload-offset"])
        assert owner.post(f"{ADMIN}/archives/uploads/{task_id}/complete", headers={"X-CSRF-Token": csrf}).status_code == 200
        cookies = dict(owner.cookies)
    barrier = Barrier(3)

    def confirm():
        with new_client(app) as client:
            client.cookies.update(cookies)
            barrier.wait(timeout=2)
            return client.post(f"{ADMIN}/archives/confirm", data={"csrf": csrf, "token": task_id}, follow_redirects=False)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(confirm), pool.submit(confirm)]
        barrier.wait(timeout=2)
        statuses = sorted(future.result(timeout=10).status_code for future in futures)
    assert statuses == [303, 404]
    assert len(list(settings.images_dir.rglob("*.png"))) == 1


def test_batch_tags_apply_to_all_filtered_images_across_pages(admin_env) -> None:
    settings, app = admin_env
    with new_client(app) as client:
        csrf = login(client)
        for slug in ("source", "game"):
            assert client.post(
                f"{ADMIN}/tags",
                data={"csrf": csrf, "slug": slug, "display_name": slug},
                follow_redirects=False,
            ).status_code == 303
        for index in range(3):
            assert client.post(
                f"{ADMIN}/upload",
                data={"csrf": csrf, "tags": "source"},
                files={"file": (f"image-{index}.png", image_bytes(size=(40 + index, 10)), "image/png")},
                follow_redirects=False,
            ).status_code == 303
        response = client.post(
            f"{ADMIN}/images/tags",
            data={
                "csrf": csrf,
                "all_filtered": "1",
                "orientation": "desktop",
                "enabled": "1",
                "storage": "",
                "tag": "source",
                "q": "",
                "tag_id": "2",
                "action": "add",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303, response.text
        with db.get_conn(settings.database_path) as conn:
            tagged = conn.execute(
                "SELECT COUNT(*) FROM image_tags it JOIN tags t ON t.id=it.tag_id WHERE t.slug='game'"
            ).fetchone()[0]
        assert int(tagged) == 3


def test_batch_delete_cascades_image_tags_and_removes_files(admin_env) -> None:
    settings, app = admin_env
    with new_client(app) as client:
        csrf = login(client)
        assert client.post(
            f"{ADMIN}/tags",
            data={"csrf": csrf, "slug": "cleanup", "display_name": "cleanup"},
            follow_redirects=False,
        ).status_code == 303
        for index in range(2):
            assert client.post(
                f"{ADMIN}/upload",
                data={"csrf": csrf, "tags": "cleanup"},
                files={"file": (f"delete-{index}.png", image_bytes(size=(50 + index, 10)), "image/png")},
                follow_redirects=False,
            ).status_code == 303
        with db.get_conn(settings.database_path) as conn:
            assert int(conn.execute("SELECT COUNT(*) FROM images").fetchone()[0]) == 2
            assert int(conn.execute("SELECT COUNT(*) FROM image_tags").fetchone()[0]) == 2
        confirmation = client.get(
            f"{ADMIN}/images/delete-confirm?source=local&orientation=desktop&enabled=1&tag=cleanup"
        )
        assert confirmation.status_code == 200
        assert "当前数量" in confirmation.text
        assert client.post(
            f"{ADMIN}/images/delete",
            data={
                "csrf": csrf,
                "confirm_phrase": "not the phrase",
                "confirm_count": "2",
                "confirm_count_check": "2",
                "all_filtered": "1",
                "orientation": "desktop",
                "enabled": "1",
                "storage": "",
                "tag": "cleanup",
                "q": "",
            },
            follow_redirects=False,
        ).status_code == 400
        response = client.post(
            f"{ADMIN}/images/delete",
            data={
                "csrf": csrf,
                "confirm_phrase": "DELETE LOCAL IMAGES",
                "confirm_count": "2",
                "confirm_count_check": "2",
                "all_filtered": "1",
                "orientation": "desktop",
                "enabled": "1",
                "storage": "",
                "tag": "cleanup",
                "q": "",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303, response.text
        assert not list(settings.images_dir.rglob("*.png"))
        with db.get_conn(settings.database_path) as conn:
            assert int(conn.execute("SELECT COUNT(*) FROM images").fetchone()[0]) == 0
            assert int(conn.execute("SELECT COUNT(*) FROM image_tags").fetchone()[0]) == 0


def test_archive_confirm_excludes_selected_member(admin_env) -> None:
    settings, app = admin_env
    payload = archive_bytes({
        "keep.png": image_bytes("PNG", (40, 10)),
        "remove.png": image_bytes("PNG", (10, 40)),
    })
    with new_client(app) as client:
        csrf = login(client)
        token, response = preview(client, csrf, payload)
        member_ids = re.findall(r'name="exclude_member_ids" value="([^"]+)"', response.text)
        assert len(member_ids) == 2
        remove_id = member_ids[1]
        confirmed = client.post(
            f"{ADMIN}/archives/confirm",
            data={"csrf": csrf, "token": token, "exclude_member_ids": remove_id},
            follow_redirects=False,
        )
        assert confirmed.status_code == 303, confirmed.text
        with db.get_conn(settings.database_path) as conn:
            assert int(conn.execute("SELECT COUNT(*) FROM images").fetchone()[0]) == 1
        assert len(list(settings.images_dir.rglob("*.png"))) == 1
