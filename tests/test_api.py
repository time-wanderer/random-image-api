from __future__ import annotations

from io import BytesIO
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image

from app.config import Settings
from app.main import create_app
from tests.conftest import make_image

WINDOWS_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)
MACOS_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.0 Safari/605.1.15"
)
ANDROID_UA = (
    "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/143.0.0.0 Mobile Safari/537.36"
)
IPHONE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)


def image_size(content: bytes) -> tuple[int, int]:
    with Image.open(BytesIO(content)) as image:
        return image.size


def test_health(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["database"] == "ok"
    assert payload["images"]["total"] == 5
    assert payload["images"]["desktop"] == 2
    assert payload["images"]["mobile"] == 2
    assert payload["images"]["square"] == 1


def test_windows_user_agent_prefers_landscape(client: TestClient) -> None:
    response = client.get("/random", headers={"User-Agent": WINDOWS_UA})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/")
    assert response.headers["x-client-type"] == "desktop"
    width, height = image_size(response.content)
    assert width >= height


def test_macos_user_agent_prefers_landscape(client: TestClient) -> None:
    response = client.get("/random", headers={"User-Agent": MACOS_UA})
    assert response.status_code == 200
    assert response.headers["x-client-type"] == "desktop"
    width, height = image_size(response.content)
    assert width >= height


def test_android_user_agent_prefers_portrait(client: TestClient) -> None:
    response = client.get("/random", headers={"User-Agent": ANDROID_UA})
    assert response.status_code == 200
    assert response.headers["x-client-type"] == "mobile"
    width, height = image_size(response.content)
    assert height >= width


def test_iphone_user_agent_prefers_portrait(client: TestClient) -> None:
    response = client.get("/random", headers={"User-Agent": IPHONE_UA})
    assert response.status_code == 200
    assert response.headers["x-client-type"] == "mobile"
    width, height = image_size(response.content)
    assert height >= width


def test_explicit_type_overrides_user_agent(client: TestClient) -> None:
    response = client.get(
        "/random",
        params={"type": "desktop"},
        headers={"User-Agent": ANDROID_UA},
    )
    assert response.status_code == 200
    assert response.headers["x-client-type"] == "desktop"
    width, height = image_size(response.content)
    assert width >= height


def test_explicit_mobile_type(client: TestClient) -> None:
    response = client.get("/random", params={"type": "mobile"})
    assert response.status_code == 200
    assert response.headers["x-client-type"] == "mobile"
    width, height = image_size(response.content)
    assert height >= width


def test_invalid_type_returns_400(client: TestClient) -> None:
    response = client.get("/random", params={"type": "test"})
    assert response.status_code == 400
    assert "desktop or mobile" in response.json()["detail"]


def test_missing_user_agent_defaults_to_desktop(client: TestClient) -> None:
    response = client.get("/random", headers={"User-Agent": ""})
    assert response.status_code == 200
    assert response.headers["x-client-type"] == "desktop"


def test_unusual_user_agent_defaults_to_desktop(client: TestClient) -> None:
    response = client.get("/random", headers={"User-Agent": "curl/8.5.0"})
    assert response.status_code == 200
    assert response.headers["x-client-type"] == "desktop"


def test_empty_catalog_returns_404(settings: Settings) -> None:
    app = create_app(settings)
    with TestClient(app) as client:
        response = client.get("/random")
        assert response.status_code == 404
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["images"]["total"] == 0


def test_missing_images_dir_is_created(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        images_dir=tmp_path / "missing-images",
        database_path=tmp_path / "db" / "images.db",
        log_dir=tmp_path / "logs",
        scan_interval_seconds=3600,
    )
    app = create_app(settings)
    with TestClient(app) as client:
        response = client.get("/health")
        assert response.status_code == 200
        assert settings.images_dir.exists()
        assert client.get("/random").status_code == 404


def test_desktop_missing_falls_back_to_mobile(settings: Settings) -> None:
    make_image(settings.images_dir / "mobile" / "only-mobile.png", (100, 200), "#111111")
    app = create_app(settings)
    with TestClient(app) as client:
        response = client.get("/random", params={"type": "desktop"})
        assert response.status_code == 200
        assert response.headers["x-fallback-used"] == "true"
        assert response.headers["x-image-orientation"] == "mobile"


def test_mobile_missing_falls_back_to_desktop(settings: Settings) -> None:
    make_image(settings.images_dir / "desktop" / "only-desktop.png", (200, 100), "#222222")
    app = create_app(settings)
    with TestClient(app) as client:
        response = client.get("/random", params={"type": "mobile"})
        assert response.status_code == 200
        assert response.headers["x-fallback-used"] == "true"
        assert response.headers["x-image-orientation"] == "desktop"


def test_corrupted_image_is_skipped(settings: Settings) -> None:
    make_image(settings.images_dir / "desktop" / "good.png", (220, 110), "#00aa00")
    bad = settings.images_dir / "desktop" / "bad.jpg"
    bad.write_bytes(b"not-an-image")
    app = create_app(settings)
    with TestClient(app) as client:
        health = client.get("/health").json()
        assert health["images"]["total"] == 1
        response = client.get("/random", params={"type": "desktop"})
        assert response.status_code == 200
        assert response.headers["x-image-file"].endswith("good.png")


def test_deleted_file_is_dropped_and_fallback_used(settings: Settings) -> None:
    good = settings.images_dir / "desktop" / "good.png"
    ghost = settings.images_dir / "desktop" / "ghost.png"
    make_image(good, (200, 80), "#009999")
    make_image(ghost, (210, 90), "#990000")
    app = create_app(settings)
    with TestClient(app) as client:
        ghost.unlink()
        response = client.get("/random", params={"type": "desktop"})
        assert response.status_code == 200
        assert response.headers["x-image-file"].endswith("good.png")


def test_unsupported_extension_is_ignored(settings: Settings) -> None:
    make_image(settings.images_dir / "desktop" / "ok.png", (180, 90), "#444444")
    (settings.images_dir / "desktop" / "note.gif").write_bytes(b"GIF89a")
    app = create_app(settings)
    with TestClient(app) as client:
        payload = client.get("/health").json()
        assert payload["images"]["total"] == 1


def test_database_path_failure_returns_503(populated_settings: Settings, tmp_path: Path) -> None:
    populated_settings.database_path = tmp_path / "missing-dir" / "nope.db"
    app = create_app(populated_settings)
    with TestClient(app) as client:
        client.app.state.catalog.settings.database_path = Path("/proc/this-should-not-be-a-sqlite-file")
        response = client.get("/random")
        assert response.status_code in {404, 503}


def test_admin_rescan_requires_token(populated_settings: Settings) -> None:
    populated_settings.admin_token = "secret-token"
    app = create_app(populated_settings)
    with TestClient(app) as client:
        denied = client.post("/admin/rescan")
        assert denied.status_code == 401
        allowed = client.post("/admin/rescan", headers={"X-Admin-Token": "secret-token"})
        assert allowed.status_code == 200
        assert allowed.json()["status"] == "ok"
