from __future__ import annotations

import logging
import random
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from PIL import Image, UnidentifiedImageError

from app.config import CONTENT_TYPES, SUPPORTED_EXTENSIONS, Settings
from app import db

logger = logging.getLogger("random_image_api.catalog")

Orientation = Literal["desktop", "mobile", "square"]
ClientType = Literal["desktop", "mobile"]


@dataclass(frozen=True, slots=True)
class ImageRecord:
    id: int
    rel_path: str
    abs_path: Path
    width: int
    height: int
    orientation: Orientation
    format: str
    content_type: str
    file_size: int

    @property
    def filename(self) -> str:
        return Path(self.rel_path).name


class NoImageAvailable(Exception):
    """Raised when no usable image can be returned."""


def classify_orientation(width: int, height: int) -> Orientation:
    if width > height:
        return "desktop"
    if height > width:
        return "mobile"
    return "square"


def read_image_meta(path: Path) -> tuple[int, int, str]:
    with Image.open(path) as image:
        image.verify()
    with Image.open(path) as image:
        width, height = image.size
        fmt = (image.format or path.suffix.lstrip(".")).lower()
    return width, height, fmt


class Catalog:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._lock = threading.RLock()
        self._by_id: dict[int, ImageRecord] = {}
        self._desktop_ids: list[int] = []
        self._mobile_ids: list[int] = []
        self._square_ids: list[int] = []
        self.last_scan_error: str | None = None
        self.last_scan_at: str | None = None

    def scan(self) -> dict[str, int]:
        settings = self.settings
        settings.ensure_directories()
        images_dir = settings.images_dir.resolve()
        seen: set[str] = set()
        scanned = 0
        skipped = 0

        with db.get_conn(settings.database_path) as conn:
            existing = {
                row["rel_path"]: row for row in db.list_images(conn)
            }
            for path in self._iter_image_files(images_dir):
                rel_path = path.relative_to(images_dir).as_posix()
                seen.add(rel_path)
                try:
                    stat = path.stat()
                except OSError as exc:
                    logger.warning("stat failed for %s: %s", path, exc)
                    skipped += 1
                    continue

                prev = existing.get(rel_path)
                if (
                    prev is not None
                    and int(prev["mtime_ns"]) == stat.st_mtime_ns
                    and int(prev["file_size"]) == stat.st_size
                ):
                    continue

                try:
                    width, height, fmt = read_image_meta(path)
                except (UnidentifiedImageError, OSError, ValueError) as exc:
                    logger.warning("skip unreadable image %s: %s", path, exc)
                    if prev is not None:
                        db.delete_by_rel_path(conn, rel_path)
                    skipped += 1
                    continue

                suffix = path.suffix.lower()
                record = {
                    "rel_path": rel_path,
                    "width": width,
                    "height": height,
                    "orientation": classify_orientation(width, height),
                    "format": fmt,
                    "content_type": CONTENT_TYPES.get(suffix, "application/octet-stream"),
                    "file_size": stat.st_size,
                    "mtime_ns": stat.st_mtime_ns,
                }
                db.upsert_image(conn, record)
                scanned += 1

            removed = db.delete_missing(conn, seen)
            rows = db.list_images(conn)

        self._rebuild_cache(images_dir, rows)
        self.last_scan_error = None
        self.last_scan_at = db.utc_now()
        logger.info(
            "scan complete updated=%s removed=%s skipped=%s total=%s",
            scanned,
            removed,
            skipped,
            len(self._by_id),
        )
        return {
            "updated": scanned,
            "removed": removed,
            "skipped": skipped,
            "total": len(self._by_id),
        }

    def _iter_image_files(self, images_dir: Path):
        if not images_dir.exists():
            return
        for path in images_dir.rglob("*"):
            if not path.is_file():
                continue
            if path.name.startswith(".") or path.name == ".gitkeep":
                continue
            if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
                continue
            yield path

    def _rebuild_cache(self, images_dir: Path, rows) -> None:
        by_id: dict[int, ImageRecord] = {}
        desktop_ids: list[int] = []
        mobile_ids: list[int] = []
        square_ids: list[int] = []

        for row in rows:
            abs_path = images_dir / row["rel_path"]
            if not abs_path.is_file():
                continue
            record = ImageRecord(
                id=int(row["id"]),
                rel_path=row["rel_path"],
                abs_path=abs_path,
                width=int(row["width"]),
                height=int(row["height"]),
                orientation=row["orientation"],
                format=row["format"],
                content_type=row["content_type"],
                file_size=int(row["file_size"]),
            )
            by_id[record.id] = record
            if record.orientation == "desktop":
                desktop_ids.append(record.id)
            elif record.orientation == "mobile":
                mobile_ids.append(record.id)
            else:
                square_ids.append(record.id)
                if self.settings.square_policy in {"both", "desktop"}:
                    desktop_ids.append(record.id)
                if self.settings.square_policy in {"both", "mobile"}:
                    mobile_ids.append(record.id)

        with self._lock:
            self._by_id = by_id
            self._desktop_ids = desktop_ids
            self._mobile_ids = mobile_ids
            self._square_ids = square_ids

    def counts(self) -> dict[str, int]:
        with self._lock:
            desktop = sum(1 for item in self._by_id.values() if item.orientation == "desktop")
            mobile = sum(1 for item in self._by_id.values() if item.orientation == "mobile")
            square = len(self._square_ids)
            return {
                "total": len(self._by_id),
                "desktop": desktop,
                "mobile": mobile,
                "square": square,
            }

    def pick(
        self,
        preferred: ClientType,
        allow_fallback: bool | None = None,
    ) -> tuple[ImageRecord, bool]:
        if allow_fallback is None:
            allow_fallback = self.settings.fallback_enabled

        with self._lock:
            primary = list(self._desktop_ids if preferred == "desktop" else self._mobile_ids)
            secondary = list(self._mobile_ids if preferred == "desktop" else self._desktop_ids)

        fallback_used = False
        pool = primary
        if not pool and allow_fallback:
            pool = secondary
            fallback_used = True
        if not pool:
            raise NoImageAvailable("no images available")

        last_error: Exception | None = None
        tried: set[int] = set()
        for _ in range(max(1, self.settings.max_pick_retries)):
            remaining = [item_id for item_id in pool if item_id not in tried]
            if not remaining and allow_fallback and not fallback_used and secondary:
                pool = secondary
                fallback_used = True
                remaining = [item_id for item_id in pool if item_id not in tried]
            if not remaining:
                break
            item_id = random.choice(remaining)
            tried.add(item_id)
            with self._lock:
                record = self._by_id.get(item_id)
            if record is None:
                continue
            try:
                if not record.abs_path.is_file():
                    raise FileNotFoundError(record.abs_path)
                read_image_meta(record.abs_path)
                return record, fallback_used
            except (FileNotFoundError, UnidentifiedImageError, OSError, ValueError) as exc:
                last_error = exc
                logger.warning("dropping unusable image %s: %s", record.rel_path, exc)
                self._drop(record)

        if last_error:
            raise NoImageAvailable(f"images exist but none are readable: {last_error}") from last_error
        raise NoImageAvailable("no images available")

    def _drop(self, record: ImageRecord) -> None:
        with self._lock:
            self._by_id.pop(record.id, None)
            self._desktop_ids = [i for i in self._desktop_ids if i != record.id]
            self._mobile_ids = [i for i in self._mobile_ids if i != record.id]
            self._square_ids = [i for i in self._square_ids if i != record.id]
        try:
            with db.get_conn(self.settings.database_path) as conn:
                db.delete_by_rel_path(conn, record.rel_path)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("failed to delete stale db row %s: %s", record.rel_path, exc)

    def database_ok(self) -> bool:
        return db.ping(self.settings.database_path)
