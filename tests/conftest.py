from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.config import Settings
from app.main import create_app


def make_image(path: Path, size: tuple[int, int], color: str = "#336699") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    data_dir = tmp_path / "data"
    images_dir = data_dir / "images"
    (images_dir / "desktop").mkdir(parents=True)
    (images_dir / "mobile").mkdir(parents=True)
    return Settings(
        data_dir=data_dir,
        images_dir=images_dir,
        database_path=data_dir / "database" / "images.db",
        log_dir=data_dir / "logs",
        fallback_enabled=True,
        square_policy="both",
        scan_on_startup=True,
        scan_interval_seconds=3600,
        admin_token="",
    )


@pytest.fixture
def populated_settings(settings: Settings) -> Settings:
    images = settings.images_dir
    make_image(images / "desktop" / "wide.jpg", (320, 180), "#1d4ed8")
    make_image(images / "desktop" / "wide.png", (240, 120), "#0f766e")
    make_image(images / "mobile" / "tall.webp", (180, 320), "#b45309")
    make_image(images / "mobile" / "tall.jpeg", (120, 240), "#7c3aed")
    make_image(images / "square.png", (160, 160), "#be123c")
    return settings


@pytest.fixture
def client(populated_settings: Settings) -> TestClient:
    app = create_app(populated_settings)
    with TestClient(app) as test_client:
        yield test_client
