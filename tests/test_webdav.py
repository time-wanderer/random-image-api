from __future__ import annotations

import sqlite3
from io import BytesIO
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app import db
from app.config import Settings
from app.main import create_app
from app.webdav import UnsafeHrefError, WebDAVManager
from tests.conftest import make_image


class FixedRng:
    def __init__(self, value: float = 0.0) -> None:
        self.value = value

    def random(self) -> float:
        return self.value

    def choice(self, items):
        return items[0]

    def sample(self, items, count):
        return list(items)[:count]


def image_bytes(size: tuple[int, int], fmt: str = "PNG") -> bytes:
    output = BytesIO()
    Image.new("RGB", size, "#285577").save(output, format=fmt)
    return output.getvalue()


def webdav_settings(tmp_path: Path, **overrides) -> Settings:
    data = tmp_path / "runtime"
    values = {
        "data_dir": data,
        "images_dir": data / "images",
        "database_path": data / "database" / "images.db",
        "log_dir": data / "logs",
        "cache_dir": data / "cache",
        "storage_mode": "hybrid",
        "hybrid_remote_probability": 0.9,
        "webdav_base_url": "https://dav.example.test/",
        "webdav_allowed_hosts": "dav.example.test",
        "webdav_desktop_root": "/desktop/",
        "webdav_mobile_root": "/mobile/",
        "scan_on_startup": True,
        "scan_interval_seconds": 3600,
        "webdav_sync_interval_seconds": 3600,
    }
    values.update(overrides)
    settings = Settings(**values)
    settings.ensure_directories()
    return settings


def multistatus(desktop: bool = True, mobile: bool = True) -> bytes:
    entries = []
    if desktop:
        entries.append(
            """<d:response><d:href>/desktop/wide.png</d:href><d:propstat>
            <d:prop><d:getetag>\"wide-v1\"</d:getetag>
            <d:getlastmodified>Sat, 01 Jan 2022 00:00:00 GMT</d:getlastmodified>
            <d:getcontentlength>256</d:getcontentlength>
            <d:getcontenttype>image/png</d:getcontenttype><d:resourcetype/>
            </d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"""
        )
    if mobile:
        entries.append(
            """<d:response><d:href>/mobile/tall.png</d:href><d:propstat>
            <d:prop><d:getetag>\"tall-v1\"</d:getetag>
            <d:getcontentlength>256</d:getcontentlength>
            <d:getcontenttype>image/png</d:getcontenttype><d:resourcetype/>
            </d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"""
        )
    return ("<?xml version='1.0'?><d:multistatus xmlns:d='DAV:'>" + "".join(entries) + "</d:multistatus>").encode()


def propfind_for_path(request: httpx.Request) -> httpx.Response:
    if request.method == "PROPFIND":
        body = multistatus(desktop="desktop" in request.url.path, mobile="mobile" in request.url.path)
        return httpx.Response(207, content=body)
    raise AssertionError(f"unexpected network request: {request.method} {request.url}")


def test_local_mode_never_uses_network(tmp_path: Path) -> None:
    calls = []

    def forbidden(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise AssertionError("local mode must not use WebDAV")

    settings = webdav_settings(tmp_path, storage_mode="local")
    make_image(settings.images_dir / "desktop" / "local.png", (80, 40))
    app = create_app(settings, webdav_transport=httpx.MockTransport(forbidden), rng=FixedRng(0.0))
    with TestClient(app) as client:
        response = client.get("/random", params={"type": "desktop"})
        assert response.status_code == 200
        assert response.headers["x-image-source"] == "local"
        assert response.headers["x-remote-fallback-used"] == "false"
        assert client.get("/health").json()["webdav"]["status"] == "disabled"
    assert calls == []


@pytest.mark.parametrize(
    ("random_value", "expected_source"),
    [(0.899999, "webdav-live"), (0.9, "local")],
)
def test_hybrid_probability_boundary(
    tmp_path: Path, random_value: float, expected_source: str
) -> None:
    wide = image_bytes((80, 40))

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PROPFIND":
            return propfind_for_path(request)
        if request.method == "GET":
            return httpx.Response(200, content=wide, headers={"ETag": '"wide-v1"'})
        raise AssertionError(request.method)

    settings = webdav_settings(tmp_path)
    make_image(settings.images_dir / "desktop" / "local.png", (90, 45))
    app = create_app(
        settings,
        webdav_transport=httpx.MockTransport(handler),
        rng=FixedRng(random_value),
    )
    with TestClient(app) as client:
        response = client.get("/random", params={"type": "desktop"})
        assert response.status_code == 200
        assert response.headers["x-image-source"] == expected_source


def test_local_probability_branch_uses_webdav_when_local_pool_is_empty(
    tmp_path: Path,
) -> None:
    wide = image_bytes((80, 40))

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PROPFIND":
            return propfind_for_path(request)
        if request.method == "GET":
            return httpx.Response(200, content=wide, headers={"ETag": '"wide-v1"'})
        raise AssertionError(request.method)

    settings = webdav_settings(tmp_path)
    app = create_app(
        settings,
        webdav_transport=httpx.MockTransport(handler),
        rng=FixedRng(0.95),
    )
    with TestClient(app) as client:
        response = client.get("/random", params={"type": "desktop"})
        assert response.status_code == 200
        assert response.headers["x-image-source"] == "webdav-live"
        assert response.headers["x-remote-fallback-used"] == "false"


def test_cache_miss_then_hit_does_one_download(tmp_path: Path) -> None:
    downloads = 0
    wide = image_bytes((100, 50))

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal downloads
        if request.method == "PROPFIND":
            return propfind_for_path(request)
        downloads += 1
        return httpx.Response(200, content=wide, headers={"ETag": '"v1"'})

    manager = WebDAVManager(
        webdav_settings(tmp_path, cache_refresh_after_seconds=3600),
        transport=httpx.MockTransport(handler),
        rng=FixedRng(),
    )
    try:
        manager.sync()
        first = manager.fetch("desktop")
        second = manager.fetch("desktop")
        assert first.source == "webdav-live"
        assert second.source == "webdav-cache"
        assert first.abs_path == second.abs_path
        assert downloads == 1
    finally:
        manager.close()


def test_403_falls_back_to_same_orientation_union_pool(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PROPFIND":
            return propfind_for_path(request)
        return httpx.Response(403)

    settings = webdav_settings(tmp_path)
    make_image(settings.images_dir / "desktop" / "local-wide.png", (120, 60))
    app = create_app(settings, webdav_transport=httpx.MockTransport(handler), rng=FixedRng())
    with TestClient(app) as client:
        response = client.get("/random", params={"type": "desktop"})
        assert response.status_code == 200
        assert response.headers["x-image-source"] == "local"
        assert response.headers["x-image-orientation"] == "desktop"
        assert response.headers["x-remote-fallback-used"] == "true"
        assert response.headers["x-fallback-used"] == "false"


def test_etag_conditional_request_and_304_refresh(tmp_path: Path) -> None:
    now = [100.0]
    requests: list[httpx.Request] = []
    wide = image_bytes((100, 50))

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PROPFIND":
            return propfind_for_path(request)
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(
                200,
                content=wide,
                headers={"ETag": '"v1"', "Last-Modified": "Sat, 01 Jan 2022 00:00:00 GMT"},
            )
        assert request.headers["if-none-match"] == '"v1"'
        assert request.headers["if-modified-since"] == "Sat, 01 Jan 2022 00:00:00 GMT"
        return httpx.Response(304)

    manager = WebDAVManager(
        webdav_settings(tmp_path, cache_refresh_after_seconds=10),
        transport=httpx.MockTransport(handler),
        rng=FixedRng(),
        clock=lambda: now[0],
    )
    try:
        manager.sync()
        manager.fetch("desktop")
        now[0] = 111.0
        refreshed = manager.fetch("desktop")
        assert refreshed.source == "webdav-cache"
        assert len(requests) == 2
        with db.get_conn(manager.settings.database_path) as conn:
            fetched_at = conn.execute("SELECT fetched_at FROM webdav_cache").fetchone()[0]
        assert fetched_at == 111.0
    finally:
        manager.close()


def test_lru_enforces_max_files_and_bytes(tmp_path: Path) -> None:
    pictures = {
        "/desktop/one.png": image_bytes((100, 50)),
        "/desktop/two.png": image_bytes((110, 55)),
    }
    now = [1.0]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=pictures[request.url.path])

    settings = webdav_settings(tmp_path, cache_max_files=1, cache_max_bytes=10_000)
    manager = WebDAVManager(
        settings,
        transport=httpx.MockTransport(handler),
        rng=FixedRng(),
        clock=lambda: now[0],
    )
    try:
        manager.fetch_href("/desktop/one.png", "desktop")
        now[0] = 2.0
        manager.fetch_href("/desktop/two.png", "desktop")
        result = manager.maintain()
        assert result["files"] == 1
        with db.get_conn(settings.database_path) as conn:
            rows = list(conn.execute("SELECT href FROM webdav_cache"))
        assert [row["href"] for row in rows] == ["https://dav.example.test/desktop/two.png"]

        settings.cache_max_bytes = 1
        result = manager.maintain()
        assert result["files"] == 0
        assert result["bytes"] == 0
    finally:
        manager.close()


def test_safe_href_requires_same_host_and_configured_root(tmp_path: Path) -> None:
    manager = WebDAVManager(
        webdav_settings(tmp_path),
        transport=httpx.MockTransport(propfind_for_path),
    )
    try:
        assert manager.validate_href("/desktop/good.png", "desktop") == (
            "https://dav.example.test/desktop/good.png"
        )
        for href in (
            "https://evil.example/desktop/a.png",
            "/mobile/wrong.png",
            "/desktop/%2e%2e/secret.png",
            "//evil.example/desktop/a.png",
        ):
            with pytest.raises(UnsafeHrefError):
                manager.validate_href(href, "desktop")
    finally:
        manager.close()


def test_webdav_base_path_is_preserved_for_roots_and_hrefs(tmp_path: Path) -> None:
    settings = webdav_settings(
        tmp_path,
        webdav_base_url="https://dav.example.test/random-image-api/",
    )
    manager = WebDAVManager(
        settings,
        transport=httpx.MockTransport(propfind_for_path),
    )
    try:
        assert manager._roots == {
            "desktop": "https://dav.example.test/random-image-api/desktop/",
            "mobile": "https://dav.example.test/random-image-api/mobile/",
        }
        assert manager.validate_href(
            "/random-image-api/mobile/tall.png", "mobile"
        ) == "https://dav.example.test/random-image-api/mobile/tall.png"
        assert manager.validate_href("tall.png", "mobile") == (
            "https://dav.example.test/random-image-api/mobile/tall.png"
        )

        for href in (
            "/mobile/outside-base.png",
            "/random-image-api/desktop/wrong.png",
            "https://evil.example/random-image-api/mobile/a.png",
        ):
            with pytest.raises(UnsafeHrefError):
                manager.validate_href(href, "mobile")
    finally:
        manager.close()


def test_failed_sync_keeps_existing_index(tmp_path: Path) -> None:
    mode = ["ok"]

    def handler(request: httpx.Request) -> httpx.Response:
        if mode[0] == "fail":
            return httpx.Response(503)
        return propfind_for_path(request)

    settings = webdav_settings(tmp_path)
    manager = WebDAVManager(settings, transport=httpx.MockTransport(handler))
    try:
        assert manager.sync()["objects"] == 2
        mode[0] = "fail"
        with pytest.raises(Exception):
            manager.sync()
        with sqlite3.connect(settings.database_path) as conn:
            assert conn.execute("SELECT COUNT(*) FROM webdav_objects").fetchone()[0] == 2
    finally:
        manager.close()


def test_default_cache_root_is_independent_and_has_tmp(tmp_path: Path) -> None:
    data_dir = tmp_path / "runtime"
    settings = Settings(
        data_dir=data_dir,
        images_dir=data_dir / "images",
        database_path=data_dir / "database" / "images.db",
        log_dir=data_dir / "logs",
    )
    settings.ensure_directories()
    assert settings.cache_dir == data_dir / "cache" / "webdav"
    assert (settings.cache_dir / "tmp").is_dir()


@pytest.mark.parametrize(
    "field",
    [
        "scan_interval_seconds",
        "max_pick_retries",
        "webdav_timeout_seconds",
        "webdav_sync_interval_seconds",
        "webdav_max_xml_bytes",
        "webdav_max_objects",
        "webdav_max_download_bytes",
        "cache_max_bytes",
        "cache_max_files",
        "cache_refresh_after_seconds",
    ],
)
def test_positive_webdav_and_cache_settings_are_validated(field: str) -> None:
    with pytest.raises(ValueError):
        Settings(**{field: 0})


@pytest.mark.parametrize("percent", [-1, 101])
def test_cache_rotate_percent_is_bounded(percent: int) -> None:
    with pytest.raises(ValueError):
        Settings(cache_rotate_percent=percent)


def test_safe_href_accepts_normal_path_and_rejects_actual_nul(tmp_path: Path) -> None:
    manager = WebDAVManager(
        webdav_settings(tmp_path),
        transport=httpx.MockTransport(propfind_for_path),
    )
    try:
        assert manager.validate_href("/desktop/normal.png", "desktop").endswith(
            "/desktop/normal.png"
        )
        with pytest.raises(UnsafeHrefError):
            manager.validate_href("/desktop/bad%00name.png", "desktop")
    finally:
        manager.close()


def test_maintenance_randomly_marks_percentage_for_conditional_refresh(
    tmp_path: Path,
) -> None:
    now = [10.0]
    requests: list[httpx.Request] = []
    pictures = {
        "/desktop/one.png": image_bytes((100, 50)),
        "/desktop/two.png": image_bytes((110, 55)),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.headers.get("if-none-match"):
            return httpx.Response(304)
        return httpx.Response(
            200,
            content=pictures[request.url.path],
            headers={"ETag": f'"{Path(request.url.path).stem}"'},
        )

    settings = webdav_settings(
        tmp_path,
        cache_rotate_percent=50,
        cache_refresh_after_seconds=3600,
    )
    manager = WebDAVManager(
        settings,
        transport=httpx.MockTransport(handler),
        rng=FixedRng(),
        clock=lambda: now[0],
    )
    try:
        manager.fetch_href("/desktop/one.png", "desktop")
        now[0] = 20.0
        manager.fetch_href("/desktop/two.png", "desktop")
        result = manager.maintain()
        assert result["removed"] == 0
        assert result["files"] == 2
        assert result["marked_for_refresh"] == 1
        with db.get_conn(settings.database_path) as conn:
            marked = list(
                conn.execute(
                    "SELECT href, fetched_at FROM webdav_cache "
                    "WHERE maintenance_mark=1"
                )
            )
        assert len(marked) == 1
        assert marked[0]["fetched_at"] == 0
        assert len([p for p in settings.cache_dir.iterdir() if p.is_file()]) == 2

        manager.fetch_href(marked[0]["href"], "desktop")
        assert requests[-1].headers["if-none-match"]
    finally:
        manager.close()


def test_remote_403_as_only_source_returns_503(tmp_path: Path) -> None:
    def forbidden(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(403)

    settings = webdav_settings(tmp_path)
    app = create_app(
        settings,
        webdav_transport=httpx.MockTransport(forbidden),
        rng=FixedRng(),
    )
    with TestClient(app) as client:
        response = client.get("/random", params={"type": "desktop"})
        assert response.status_code == 503
        assert response.json()["detail"] == "remote source unavailable"


def test_healthy_remote_with_no_objects_returns_404(tmp_path: Path) -> None:
    def empty_propfind(request: httpx.Request) -> httpx.Response:
        assert request.method == "PROPFIND"
        return httpx.Response(207, content=multistatus(False, False))

    settings = webdav_settings(tmp_path)
    app = create_app(
        settings,
        webdav_transport=httpx.MockTransport(empty_propfind),
        rng=FixedRng(),
    )
    with TestClient(app) as client:
        response = client.get("/random", params={"type": "desktop"})
        assert response.status_code == 404
        cache_health = client.get("/health").json()["cache"]
        assert cache_health["files"] == 0
        assert cache_health["bytes"] == 0
        assert cache_health["refresh_after_seconds"] == 3600
        assert cache_health["rotate_percent"] == 10
