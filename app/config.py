from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
CONTENT_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_host: str = "0.0.0.0"
    app_bind_port: int = 8080
    app_port: int = 8080
    data_dir: Path = Path("./data")
    images_dir: Path = Path("./data/images")
    database_path: Path = Path("./data/database/images.db")
    log_dir: Path = Path("./data/logs")
    log_level: str = "INFO"
    admin_token: str = ""
    fallback_enabled: bool = True
    square_policy: str = "both"
    scan_on_startup: bool = True
    scan_interval_seconds: int = 300
    max_pick_retries: int = 8
    trusted_proxy_headers: bool = True

    @field_validator("square_policy")
    @classmethod
    def validate_square_policy(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in {"both", "desktop", "mobile"}:
            raise ValueError("SQUARE_POLICY must be one of: both, desktop, mobile")
        return normalized

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, value: str) -> str:
        return value.strip().upper()

    def ensure_directories(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.images_dir.mkdir(parents=True, exist_ok=True)
        (self.images_dir / "desktop").mkdir(parents=True, exist_ok=True)
        (self.images_dir / "mobile").mkdir(parents=True, exist_ok=True)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_directories()
    return settings
