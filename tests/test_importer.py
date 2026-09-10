from __future__ import annotations

import errno
import io
import os
import stat
import tarfile
import zipfile
from concurrent.futures import ThreadPoolExecutor
import threading
from pathlib import Path

import pytest
from PIL import Image

from app import db
from app import importer as importer_module
from app.importer import (
    ArchiveSecurityError,
    ImportLimits,
    _validate_member_name,
    import_archive,
    main,
)


def image_bytes(fmt: str, size: tuple[int, int], color: str = "red") -> bytes:
    output = io.BytesIO()
    Image.new("RGB", size, color).save(output, format=fmt)
    return output.getvalue()


def test_explicit_archive_suffix_opens_extensionless_chunk_storage(tmp_path: Path) -> None:
    stored = tmp_path / "upload.bin"
    with zipfile.ZipFile(stored, "w") as archive:
        archive.writestr("safe.png", image_bytes("PNG", (12, 8)))

    with pytest.raises(importer_module.ImportErrorBase):
        import_archive(stored, tmp_path / "without-metadata", dry_run=True)

    summary = import_archive(
        stored,
        tmp_path / "with-metadata",
        dry_run=True,
        archive_suffix=".zip",
    )
    assert summary.imported == 1
    assert summary.import_required_bytes > 0


def make_zip(path: Path, files: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, payload in files.items():
            archive.writestr(name, payload)
    return path


def test_rejects_actual_nul_in_member_name() -> None:
    with pytest.raises(ArchiveSecurityError, match="NUL byte"):
        _validate_member_name("bad\0name.jpg")


def test_rejects_path_traversal_absolute_drive_and_backslash(tmp_path: Path) -> None:
    names = ("../escape.jpg", "/absolute.jpg", "C:/drive.jpg", "safe/../../escape.jpg", "..\\escape.jpg")
    for index, name in enumerate(names):
        archive = make_zip(tmp_path / f"bad-{index}.zip", {name: b"x"})
        with pytest.raises(ArchiveSecurityError):
            import_archive(archive, tmp_path / f"out-{index}")
        assert not (tmp_path / f"out-{index}").exists()


def test_rejects_zip_symlink_and_tar_links_and_special_files(tmp_path: Path) -> None:
    zip_path = tmp_path / "link.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        info = zipfile.ZipInfo("link.jpg")
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(info, "target")
    with pytest.raises(ArchiveSecurityError):
        import_archive(zip_path, tmp_path / "zip-out")

    for kind, linkname in ((tarfile.SYMTYPE, "target"), (tarfile.LNKTYPE, "target"), (tarfile.FIFOTYPE, "")):
        tar_path = tmp_path / f"special-{kind!r}.tar.gz"
        with tarfile.open(tar_path, "w:gz") as archive:
            info = tarfile.TarInfo("special")
            info.type = kind
            info.linkname = linkname
            archive.addfile(info)
        with pytest.raises(ArchiveSecurityError):
            import_archive(tar_path, tmp_path / f"tar-out-{kind!r}")


def test_validates_all_members_before_writing(tmp_path: Path) -> None:
    archive = make_zip(
        tmp_path / "late-invalid.zip",
        {"good.png": image_bytes("PNG", (20, 10)), "../bad": b"bad"},
    )
    output = tmp_path / "output"
    with pytest.raises(ArchiveSecurityError):
        import_archive(archive, output)
    assert not output.exists()


def test_rejects_member_total_ratio_and_pillow_bomb_limits(tmp_path: Path) -> None:
    payload = b"0" * 10_000
    archive = make_zip(tmp_path / "bomb.zip", {"huge.bin": payload})
    with pytest.raises(ArchiveSecurityError, match="member size"):
        import_archive(archive, tmp_path / "one", limits=ImportLimits(max_member_bytes=100))
    with pytest.raises(ArchiveSecurityError, match="expanded size"):
        import_archive(
            archive,
            tmp_path / "two",
            limits=ImportLimits(max_member_bytes=20_000, max_total_bytes=100),
        )
    with pytest.raises(ArchiveSecurityError, match="compression ratio"):
        import_archive(
            archive,
            tmp_path / "three",
            limits=ImportLimits(max_member_bytes=20_000, max_compression_ratio=2),
        )

    image_archive = make_zip(tmp_path / "pixels.zip", {"large.png": image_bytes("PNG", (30, 30))})
    with pytest.raises(ArchiveSecurityError, match="Pillow decompression-bomb"):
        import_archive(image_archive, tmp_path / "pixels", limits=ImportLimits(max_image_pixels=100))


def test_mixed_orientations_wrong_extensions_and_square_both(tmp_path: Path) -> None:
    wide = image_bytes("PNG", (30, 10), "red")
    tall = image_bytes("JPEG", (10, 30), "green")
    square = image_bytes("WEBP", (20, 20), "blue")
    archive = make_zip(
        tmp_path / "images.zip",
        {"wide.jpeg": wide, "tall.png": tall, "square.jpg": square, "notes.txt": b"not image"},
    )
    output = tmp_path / "images"
    summary = import_archive(archive, output, square_policy="both")

    assert (summary.imported, summary.skipped) == (3, 1)
    assert (summary.desktop, summary.mobile, summary.square) == (1, 1, 1)
    assert summary.square_both_storage == "square"
    assert {path.suffix for path in (output / "desktop").iterdir()} == {".png"}
    assert {path.suffix for path in (output / "mobile").iterdir()} == {".jpg"}
    assert {path.suffix for path in (output / "square").iterdir()} == {".webp"}
    assert all(len(path.stem) == 64 for path in output.rglob("*.*"))


def test_dry_run_writes_nothing_and_json_cli_succeeds(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    archive = make_zip(tmp_path / "dry.zip", {"photo.png": image_bytes("PNG", (20, 10))})
    output = tmp_path / "missing"
    summary = import_archive(archive, output, dry_run=True)
    assert summary.imported == 1
    assert not output.exists()

    assert main([str(archive), "--output-dir", str(output), "--dry-run", "--json"]) == 0
    assert '"ok": true' in capsys.readouterr().out
    assert not output.exists()


def test_content_hash_deduplicates_within_and_across_runs(tmp_path: Path) -> None:
    payload = image_bytes("PNG", (20, 10))
    archive = make_zip(tmp_path / "dupes.zip", {"a.png": payload, "folder/b.jpeg": payload})
    output = tmp_path / "output"

    first = import_archive(archive, output)
    second = import_archive(archive, output)
    assert (first.imported, first.duplicates) == (1, 1)
    assert (second.imported, second.duplicates) == (0, 2)
    files = [path for path in output.rglob("*") if path.is_file()]
    assert len(files) == 1
    assert files[0].read_bytes() == payload


def test_tar_gz_import_and_failure_exit_code(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    payload = image_bytes("JPEG", (10, 20))
    archive_path = tmp_path / "images.tar.gz"
    with tarfile.open(archive_path, "w:gz") as archive:
        info = tarfile.TarInfo("portrait.wrong")
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))
    summary = import_archive(archive_path, tmp_path / "output")
    assert summary.imported == 1
    assert len(list((tmp_path / "output" / "mobile").glob("*.jpg"))) == 1

    bad = make_zip(tmp_path / "bad.zip", {"../escape": b"x"})
    assert main([str(bad), "--output-dir", str(tmp_path / "bad-output")]) == 2
    assert "error:" in capsys.readouterr().err


def test_import_rolls_back_files_when_commit_fails_midway(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = make_zip(
        tmp_path / "two-images.zip",
        {
            "wide.png": image_bytes("PNG", (30, 10), "red"),
            "tall.png": image_bytes("PNG", (10, 30), "blue"),
        },
    )
    output = tmp_path / "images"
    real_replace = importer_module.os.replace
    calls = 0

    def failing_replace(source: Path, target: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated commit failure")
        real_replace(source, target)

    monkeypatch.setattr(importer_module.os, "replace", failing_replace)
    with pytest.raises(OSError, match="simulated commit failure"):
        import_archive(archive, output)

    assert calls == 2
    assert not [path for path in output.rglob("*") if path.is_file()]


def test_cli_import_assigns_multiple_tags_in_sqlite(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    archive = make_zip(
        tmp_path / "tagged.zip",
        {
            "wide.png": image_bytes("PNG", (30, 10)),
            "tall.jpg": image_bytes("JPEG", (10, 30)),
        },
    )
    output = tmp_path / "images"
    database = tmp_path / "database" / "images.db"

    result = main([
        str(archive),
        "--images-dir", str(output),
        "--database-path", str(database),
        "--tag", "nature",
        "--tag", "featured",
        "--tag", "nature",
        "--json",
    ])

    assert result == 0
    cli_output = capsys.readouterr().out
    assert '"ok": true' in cli_output
    assert "tag_targets" not in cli_output
    assert "created_paths" not in cli_output
    with db.get_conn(database) as conn:
        assert int(conn.execute("SELECT COUNT(*) FROM images").fetchone()[0]) == 2
        assert {row[0] for row in conn.execute("SELECT slug FROM tags")} == {"nature", "featured"}
        assert int(conn.execute("SELECT COUNT(*) FROM image_tags").fetchone()[0]) == 4


def test_cli_tags_require_database_and_dry_run_writes_nothing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    archive = make_zip(tmp_path / "dry-tags.zip", {"photo.png": image_bytes("PNG", (20, 10))})
    output = tmp_path / "images"
    database = tmp_path / "database" / "images.db"

    assert main([str(archive), "--output-dir", str(output), "--tag", "nature"]) == 2
    assert "--database-path is required" in capsys.readouterr().err
    assert not output.exists()

    assert main([
        str(archive),
        "--output-dir", str(output),
        "--database-path", str(database),
        "--tag", "nature",
        "--dry-run",
    ]) == 0
    assert not output.exists()
    assert not database.exists()



def test_dry_run_reports_only_new_image_bytes(tmp_path: Path) -> None:
    existing = image_bytes("PNG", (20, 10), "red")
    added = image_bytes("PNG", (10, 20), "blue")
    output = tmp_path / "images"
    import_archive(make_zip(tmp_path / "first.zip", {"existing.png": existing}), output)
    archive = make_zip(tmp_path / "preview.zip", {"a.png": existing, "b.png": added})

    summary = import_archive(archive, output, dry_run=True)

    assert (summary.imported, summary.duplicates) == (1, 1)
    assert summary.import_required_bytes == len(added)
    assert summary.to_dict()["import_required_bytes"] == len(added)


def test_global_import_lock_permissions_and_nofollow(tmp_path: Path) -> None:
    root = tmp_path / "uploads"
    with importer_module.global_import_lock(root):
        assert stat.S_IMODE(root.stat().st_mode) == 0o700
        assert stat.S_IMODE((root / ".import.lock").stat().st_mode) == 0o600
    (root / ".import.lock").unlink()
    outside = tmp_path / "outside"
    outside.write_text("keep", encoding="utf-8")
    (root / ".import.lock").symlink_to(outside)
    with pytest.raises(importer_module.ImportErrorBase):
        with importer_module.global_import_lock(root):
            pass
    assert outside.read_text(encoding="utf-8") == "keep"


def test_import_enospc_is_retryable_and_rolls_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    archive = make_zip(tmp_path / "image.zip", {"x.png": image_bytes("PNG", (20, 10))})
    real_fsync = os.fsync
    raised = False

    def fail_once(fd: int) -> None:
        nonlocal raised
        if not raised:
            raised = True
            raise OSError(errno.ENOSPC, "full")
        real_fsync(fd)

    monkeypatch.setattr(importer_module.os, "fsync", fail_once)
    with pytest.raises(importer_module.ImportStorageError):
        import_archive(archive, tmp_path / "images")
    assert not [item for item in (tmp_path / "images").rglob("*") if item.is_file()]



def test_pillow_limit_guard_serializes_threads(monkeypatch: pytest.MonkeyPatch) -> None:
    entered = threading.Event()
    release = threading.Event()
    second_entered = threading.Event()
    original = importer_module.Image.MAX_IMAGE_PIXELS

    def first() -> None:
        with importer_module.pillow_guard(101):
            entered.set()
            assert release.wait(5)
            assert importer_module.Image.MAX_IMAGE_PIXELS == 101

    def second() -> None:
        assert entered.wait(5)
        with importer_module.pillow_guard(202):
            second_entered.set()
            assert importer_module.Image.MAX_IMAGE_PIXELS == 202

    with ThreadPoolExecutor(max_workers=2) as pool:
        one = pool.submit(first)
        two = pool.submit(second)
        assert entered.wait(5)
        assert not second_entered.wait(0.05)
        release.set()
        one.result(timeout=5)
        two.result(timeout=5)
    assert importer_module.Image.MAX_IMAGE_PIXELS == original
