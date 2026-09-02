from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import field_validator, model_validator
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
    app_bind_port: int = 10086
    app_port: int = 10086
    data_dir: Path = Path("./data")
    images_dir: Path = Path("./data/images")
    database_path: Path = Path("./data/database/images.db")
    log_dir: Path = Path("./data/logs")
    log_level: str = "INFO"
    admin_token: str = ""
    admin_session_secret: str = ""
    admin_path: str = "/manage-images"
    admin_cookie_name: str = "ria_admin_session"
    admin_session_ttl_seconds: int = 1800
    admin_preview_ttl_seconds: int = 600
    admin_login_window_seconds: int = 60
    admin_login_max_attempts: int = 5
    admin_max_upload_bytes: int = 25 * 1024 * 1024
    admin_page_size: int = 20
    admin_max_archive_bytes: int = 512 * 1024 * 1024
    admin_max_archive_members: int = 10_000
    admin_max_archive_member_bytes: int = 100 * 1024 * 1024
    admin_max_archive_total_bytes: int = 1024 * 1024 * 1024
    admin_max_archive_compression_ratio: float = 200.0
    admin_max_image_pixels: int = 100_000_000
    upload_tmp_dir: Path | None = None
    fallback_enabled: bool = True
    square_policy: str = "both"
    scan_on_startup: bool = True
    scan_interval_seconds: int = 300
    max_pick_retries: int = 8
    trusted_proxy_headers: bool = True

    storage_mode: str = "local"
    hybrid_remote_probability: float = 0.9
    webdav_base_url: str = ""
    webdav_desktop_root: str = "/desktop/"
    webdav_mobile_root: str = "/mobile/"
    webdav_allowed_hosts: str = ""
    webdav_username: str = ""
    webdav_password: str = ""
    webdav_timeout_seconds: float = 10.0
    webdav_sync_interval_seconds: int = 300
    webdav_max_xml_bytes: int = 2 * 1024 * 1024
    webdav_max_objects: int = 10_000
    webdav_max_download_bytes: int = 25 * 1024 * 1024
    cache_dir: Path | None = None
    cache_max_bytes: int = 1024 * 1024 * 1024
    cache_max_files: int = 2_000
    cache_refresh_after_seconds: int = 3600
    cache_rotate_percent: int = 10

    @field_validator("admin_path")
    @classmethod
    def validate_admin_path(cls, value: str) -> str:
        path = "/" + value.strip().strip("/")
        if path == "/" or "//" in path or any(part in {".", ".."} for part in path.split("/")):
            raise ValueError("ADMIN_PATH must be a normalized non-root path")
        reserved = ("/random", "/health", "/admin", "/docs", "/redoc", "/openapi.json")
        if any(path == item or path.startswith(item + "/") for item in reserved):
            raise ValueError("ADMIN_PATH conflicts with an API route")
        return path

    @field_validator("admin_cookie_name")
    @classmethod
    def validate_cookie_name(cls, value: str) -> str:
        if not value or not value.replace("_", "").replace("-", "").isalnum():
            raise ValueError("ADMIN_COOKIE_NAME is invalid")
        return value

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

    @field_validator("storage_mode")
    @classmethod
    def validate_storage_mode(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in {"local", "hybrid"}:
            raise ValueError("STORAGE_MODE must be one of: local, hybrid")
        return normalized

    @field_validator("hybrid_remote_probability")
    @classmethod
    def validate_remote_probability(cls, value: float) -> float:
        if not 0.0 <= value <= 1.0:
            raise ValueError("HYBRID_REMOTE_PROBABILITY must be between 0 and 1")
        return value

    @field_validator(
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
        "admin_session_ttl_seconds",
        "admin_preview_ttl_seconds",
        "admin_login_window_seconds",
        "admin_login_max_attempts",
        "admin_max_upload_bytes",
        "admin_page_size",
        "admin_max_archive_bytes",
        "admin_max_archive_members",
        "admin_max_archive_member_bytes",
        "admin_max_archive_total_bytes",
        "admin_max_archive_compression_ratio",
        "admin_max_image_pixels",
    )
    @classmethod
    def validate_positive_limits(cls, value: int | float) -> int | float:
        if value <= 0:
            raise ValueError(
                "timeout, interval, size, count, and cache limits must be positive"
            )
        return value

    @field_validator("cache_rotate_percent")
    @classmethod
    def validate_cache_rotate_percent(cls, value: int) -> int:
        if not 0 <= value <= 100:
            raise ValueError("CACHE_ROTATE_PERCENT must be between 0 and 100")
        return value

    @model_validator(mode="after")
    def set_cache_dir(self) -> "Settings":
        if self.cache_dir is None:
            self.cache_dir = self.data_dir / "cache" / "webdav"
        if self.upload_tmp_dir is None:
            self.upload_tmp_dir = self.data_dir / "tmp" / "admin"
        return self

    @property
    def webdav_hosts(self) -> set[str]:
        return {
            item.strip().lower()
            for item in self.webdav_allowed_hosts.split(",")
            if item.strip()
        }

    def ensure_directories(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.images_dir.mkdir(parents=True, exist_ok=True)
        (self.images_dir / "desktop").mkdir(parents=True, exist_ok=True)
        (self.images_dir / "mobile").mkdir(parents=True, exist_ok=True)
        (self.images_dir / "square").mkdir(parents=True, exist_ok=True)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        assert self.cache_dir is not None
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        (self.cache_dir / "tmp").mkdir(parents=True, exist_ok=True)
        assert self.upload_tmp_dir is not None
        self.upload_tmp_dir.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_directories()
    return settings
