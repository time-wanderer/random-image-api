"""Secure image archive importer.

The archive is completely validated before any member data is read.  Images are
stored as ``<sha256>.<detected-format>`` below an orientation directory.
For the ``both`` square policy, one canonical copy is stored under ``desktop``;
the application catalog can expose square images to both client pools.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import stat
import sys
import tarfile
import tempfile
import warnings
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Iterator, Literal

from PIL import Image, ImageOps, UnidentifiedImageError

SquarePolicy = Literal["both", "desktop", "mobile"]
_FORMAT_EXTENSIONS = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp"}
_DRIVE_RE = re.compile(r"^[A-Za-z]:")


class ImportErrorBase(Exception):
    """Expected import failure suitable for a concise CLI diagnostic."""


class ArchiveSecurityError(ImportErrorBase):
    """The archive violates a security or resource limit."""


@dataclass(frozen=True, slots=True)
class ImportLimits:
    max_archive_bytes: int = 512 * 1024 * 1024
    max_members: int = 10_000
    max_member_bytes: int = 100 * 1024 * 1024
    max_total_bytes: int = 1024 * 1024 * 1024
    max_compression_ratio: float = 200.0
    max_image_pixels: int = 100_000_000


@dataclass(slots=True)
class ImportSummary:
    archive: str
    output_dir: str
    dry_run: bool
    square_policy: str
    square_both_storage: str = "desktop"
    members: int = 0
    files_examined: int = 0
    imported: int = 0
    duplicates: int = 0
    skipped: int = 0
    desktop: int = 0
    mobile: int = 0
    square: int = 0
    bytes_read: int = 0

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class _Member:
    name: str
    size: int
    compressed_size: int | None
    source: object


def _validate_member_name(name: str) -> None:
    if not name or "\x00" in name:
        raise ArchiveSecurityError("archive member has an empty name or NUL byte")
    if "\\" in name:
        raise ArchiveSecurityError(f"backslashes are forbidden in member path: {name!r}")
    if name.startswith("/") or _DRIVE_RE.match(name):
        raise ArchiveSecurityError(f"absolute or drive-qualified member path: {name!r}")
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts:
        raise ArchiveSecurityError(f"path traversal in archive member: {name!r}")


def _check_positive_limits(limits: ImportLimits) -> None:
    integer_limits = (
        limits.max_archive_bytes,
        limits.max_members,
        limits.max_member_bytes,
        limits.max_total_bytes,
        limits.max_image_pixels,
    )
    if any(value <= 0 for value in integer_limits) or limits.max_compression_ratio <= 0:
        raise ValueError("all import limits must be positive")


def _ratio(uncompressed: int, compressed: int) -> float:
    if uncompressed == 0:
        return 0.0
    if compressed <= 0:
        return float("inf")
    return uncompressed / compressed


def _validate_common(members: list[_Member], archive_size: int, limits: ImportLimits) -> None:
    if len(members) > limits.max_members:
        raise ArchiveSecurityError("archive member count limit exceeded")
    total = 0
    for member in members:
        if member.size < 0 or member.size > limits.max_member_bytes:
            raise ArchiveSecurityError(f"member size limit exceeded: {member.name!r}")
        total += member.size
        if total > limits.max_total_bytes:
            raise ArchiveSecurityError("archive expanded size limit exceeded")
        if member.compressed_size is not None and _ratio(member.size, member.compressed_size) > limits.max_compression_ratio:
            raise ArchiveSecurityError(f"compression ratio limit exceeded: {member.name!r}")
    # tar.gz has no reliable per-member compressed size; enforce an aggregate bound.
    if members and members[0].compressed_size is None and _ratio(total, archive_size) > limits.max_compression_ratio:
        raise ArchiveSecurityError("archive compression ratio limit exceeded")


def _zip_members(archive: zipfile.ZipFile, limits: ImportLimits, archive_size: int) -> list[_Member]:
    members: list[_Member] = []
    infos = archive.infolist()
    if len(infos) > limits.max_members:
        raise ArchiveSecurityError("archive member count limit exceeded")
    for info in infos:
        _validate_member_name(info.filename)
        mode = (info.external_attr >> 16) & 0xFFFF
        file_type = stat.S_IFMT(mode)
        if stat.S_ISLNK(mode) or (file_type and not (stat.S_ISREG(mode) or stat.S_ISDIR(mode))):
            raise ArchiveSecurityError(f"non-regular ZIP member is forbidden: {info.filename!r}")
        if info.is_dir():
            continue
        members.append(_Member(info.filename, info.file_size, info.compress_size, info))
    _validate_common(members, archive_size, limits)
    return members


def _tar_members(archive: tarfile.TarFile, limits: ImportLimits, archive_size: int) -> list[_Member]:
    members: list[_Member] = []
    infos = archive.getmembers()
    if len(infos) > limits.max_members:
        raise ArchiveSecurityError("archive member count limit exceeded")
    for info in infos:
        _validate_member_name(info.name)
        if info.isdir():
            continue
        if not info.isfile():
            raise ArchiveSecurityError(f"link or special TAR member is forbidden: {info.name!r}")
        members.append(_Member(info.name, info.size, None, info))
    _validate_common(members, archive_size, limits)
    return members


def _read_limited(stream: BinaryIO, member: _Member, total_so_far: int, limits: ImportLimits) -> bytes:
    chunks: list[bytes] = []
    count = 0
    while True:
        chunk = stream.read(min(1024 * 1024, limits.max_member_bytes - count + 1))
        if not chunk:
            break
        count += len(chunk)
        if count > limits.max_member_bytes:
            raise ArchiveSecurityError(f"streamed member size limit exceeded: {member.name!r}")
        if total_so_far + count > limits.max_total_bytes:
            raise ArchiveSecurityError("streamed total size limit exceeded")
        chunks.append(chunk)
    if count != member.size:
        raise ArchiveSecurityError(f"member size changed while reading: {member.name!r}")
    return b"".join(chunks)


def _inspect_image(payload: bytes, max_pixels: int) -> tuple[str, int, int]:
    old_limit = Image.MAX_IMAGE_PIXELS
    try:
        Image.MAX_IMAGE_PIXELS = max_pixels
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(payload)) as image:
                detected = (image.format or "").upper()
                if detected not in _FORMAT_EXTENSIONS:
                    raise UnidentifiedImageError(f"unsupported image format: {detected or 'unknown'}")
                image.load()
                visual = ImageOps.exif_transpose(image)
                width, height = visual.size
        return detected, width, height
    except Image.DecompressionBombError as exc:
        raise ArchiveSecurityError(f"Pillow decompression-bomb limit exceeded: {exc}") from exc
    except Image.DecompressionBombWarning as exc:
        raise ArchiveSecurityError(f"Pillow decompression-bomb limit exceeded: {exc}") from exc
    finally:
        Image.MAX_IMAGE_PIXELS = old_limit


def _orientation(width: int, height: int) -> str:
    if width > height:
        return "desktop"
    if height > width:
        return "mobile"
    return "square"


def _destination_orientation(orientation: str, square_policy: SquarePolicy) -> str:
    if orientation != "square":
        return orientation
    # A single canonical path avoids duplicate content for policy=both.
    return "desktop" if square_policy in {"both", "desktop"} else "mobile"


def _open_archive(path: Path):
    lower = path.name.lower()
    if lower.endswith(".zip"):
        return zipfile.ZipFile(path, "r"), "zip"
    if lower.endswith((".tar.gz", ".tgz")):
        return tarfile.open(path, "r:gz"), "tar"
    raise ImportErrorBase("supported archive types are .zip, .tar.gz, and .tgz")


def import_archive(
    archive_path: str | os.PathLike[str],
    output_dir: str | os.PathLike[str],
    *,
    dry_run: bool = False,
    square_policy: SquarePolicy = "both",
    limits: ImportLimits | None = None,
) -> ImportSummary:
    """Validate and import supported images from a ZIP or gzip-compressed TAR."""
    if square_policy not in {"both", "desktop", "mobile"}:
        raise ValueError("square_policy must be one of: both, desktop, mobile")
    limits = limits or ImportLimits()
    _check_positive_limits(limits)
    source = Path(archive_path)
    destination = Path(output_dir)
    try:
        archive_size = source.stat().st_size
    except OSError as exc:
        raise ImportErrorBase(f"cannot read archive: {exc}") from exc
    if archive_size > limits.max_archive_bytes:
        raise ArchiveSecurityError("archive file size limit exceeded")

    summary = ImportSummary(str(source), str(destination), dry_run, square_policy)
    staged: list[tuple[Path, Path]] = []
    staging: Path | None = None
    seen: set[tuple[str, str]] = set()

    try:
        try:
            archive, kind = _open_archive(source)
        except (zipfile.BadZipFile, tarfile.TarError, OSError) as exc:
            raise ImportErrorBase(f"invalid archive: {exc}") from exc
        with archive:
            try:
                members = _zip_members(archive, limits, archive_size) if kind == "zip" else _tar_members(archive, limits, archive_size)
            except (zipfile.BadZipFile, tarfile.TarError, UnicodeError, OSError) as exc:
                raise ImportErrorBase(f"cannot validate archive: {exc}") from exc
            summary.members = len(members)
            if not dry_run:
                destination.mkdir(parents=True, exist_ok=True)
                staging = Path(tempfile.mkdtemp(prefix=".import-staging-", dir=destination))

            total_read = 0
            for member in members:
                summary.files_examined += 1
                try:
                    if kind == "zip":
                        stream = archive.open(member.source, "r")
                    else:
                        stream = archive.extractfile(member.source)
                        if stream is None:
                            raise ArchiveSecurityError(f"cannot read TAR member: {member.name!r}")
                    with stream:
                        payload = _read_limited(stream, member, total_read, limits)
                except (zipfile.BadZipFile, RuntimeError, EOFError, OSError) as exc:
                    raise ImportErrorBase(f"cannot read member {member.name!r}: {exc}") from exc
                total_read += len(payload)
                summary.bytes_read = total_read
                try:
                    detected, width, height = _inspect_image(payload, limits.max_image_pixels)
                except (UnidentifiedImageError, OSError, ValueError, SyntaxError):
                    summary.skipped += 1
                    continue

                orientation = _orientation(width, height)
                setattr(summary, orientation, getattr(summary, orientation) + 1)
                target_group = _destination_orientation(orientation, square_policy)
                digest = hashlib.sha256(payload).hexdigest()
                target = destination / target_group / f"{digest}{_FORMAT_EXTENSIONS[detected]}"
                identity = (target_group, digest)
                if identity in seen or target.is_file():
                    summary.duplicates += 1
                    continue
                seen.add(identity)
                summary.imported += 1
                if not dry_run:
                    assert staging is not None
                    staged_file = staging / f"{len(staged)}-{digest}{_FORMAT_EXTENSIONS[detected]}"
                    with staged_file.open("xb") as handle:
                        handle.write(payload)
                        handle.flush()
                        os.fsync(handle.fileno())
                    staged.append((staged_file, target))

        if not dry_run:
            for staged_file, target in staged:
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(staged_file, target)
        return summary
    finally:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)


def _parser() -> argparse.ArgumentParser:
    defaults = ImportLimits()
    parser = argparse.ArgumentParser(description="Securely import image archives")
    parser.add_argument("archive", type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--square-policy", choices=("both", "desktop", "mobile"), default="both")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true", dest="json_output")
    parser.add_argument("--max-archive-bytes", type=int, default=defaults.max_archive_bytes)
    parser.add_argument("--max-members", type=int, default=defaults.max_members)
    parser.add_argument("--max-member-bytes", type=int, default=defaults.max_member_bytes)
    parser.add_argument("--max-total-bytes", type=int, default=defaults.max_total_bytes)
    parser.add_argument("--max-compression-ratio", type=float, default=defaults.max_compression_ratio)
    parser.add_argument("--max-image-pixels", type=int, default=defaults.max_image_pixels)
    return parser


def _human_summary(summary: ImportSummary) -> str:
    mode = "DRY-RUN" if summary.dry_run else "IMPORT"
    return (
        f"{mode} complete: imported={summary.imported} duplicates={summary.duplicates} "
        f"skipped={summary.skipped} desktop={summary.desktop} mobile={summary.mobile} "
        f"square={summary.square}; square policy={summary.square_policy} "
        f"(both uses canonical desktop storage)"
    )


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    limits = ImportLimits(
        max_archive_bytes=args.max_archive_bytes,
        max_members=args.max_members,
        max_member_bytes=args.max_member_bytes,
        max_total_bytes=args.max_total_bytes,
        max_compression_ratio=args.max_compression_ratio,
        max_image_pixels=args.max_image_pixels,
    )
    try:
        summary = import_archive(
            args.archive,
            args.output_dir,
            dry_run=args.dry_run,
            square_policy=args.square_policy,
            limits=limits,
        )
    except (ImportErrorBase, ValueError) as exc:
        if args.json_output:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        else:
            print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.json_output:
        print(json.dumps({"ok": True, **summary.to_dict()}, ensure_ascii=False, sort_keys=True))
    else:
        print(_human_summary(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
