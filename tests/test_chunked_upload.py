from __future__ import annotations

import asyncio
import os
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import db
from app.chunked_upload import UploadError, UploadStore


class StreamRequest:
    def __init__(self, chunks: list[bytes]):
        self.headers = {"content-type": "application/offset+octet-stream"}
        self.chunks = chunks
        self.read_started = False

    async def stream(self):
        self.read_started = True
        for chunk in self.chunks:
            yield chunk


class BlockingStreamRequest(StreamRequest):
    def __init__(self, entered: asyncio.Event, release: asyncio.Event, payload: bytes):
        super().__init__([payload])
        self.entered = entered
        self.release = release

    async def stream(self):
        self.read_started = True
        self.entered.set()
        await self.release.wait()
        yield self.chunks[0]


def store_for(path: Path, **overrides) -> UploadStore:
    values = {
        "database_path": path / "images.db",
        "upload_tmp_dir": path / "tmp",
        "images_dir": path / "images",
        "admin_chunked_upload_enabled": True,
        "admin_chunk_recommended_bytes": 8,
        "admin_chunk_min_bytes": 1,
        "admin_chunk_max_bytes": 16,
        "admin_chunked_max_upload_bytes": 128,
        "admin_chunked_upload_ttl_seconds": 60,
        "admin_chunked_max_active_tasks": 2,
        "admin_chunked_max_inflight_patches": 2,
        "admin_chunked_min_free_bytes": 1,
    }
    values.update(overrides)
    return UploadStore(SimpleNamespace(**values))


@pytest.mark.asyncio
async def test_patch_admission_rejects_before_reading_body(tmp_path: Path) -> None:
    store = store_for(
        tmp_path,
        admin_chunked_max_active_tasks=3,
        admin_chunked_max_inflight_patches=1,
    )
    first = store.create("owner", "first.zip", ".zip", 4)
    second = store.create("owner", "second.zip", ".zip", 4)
    entered = asyncio.Event()
    release = asyncio.Event()
    active_request = BlockingStreamRequest(entered, release, b"1234")
    active = asyncio.create_task(
        store.append(first["id"], "owner", active_request, 0, 4)
    )
    await asyncio.wait_for(entered.wait(), timeout=1)

    duplicate = StreamRequest([b"1234"])
    with pytest.raises(UploadError) as duplicate_error:
        await store.append(first["id"], "owner", duplicate, 0, 4)
    assert (duplicate_error.value.status, duplicate_error.value.code) == (
        409,
        "upload_in_progress",
    )
    assert duplicate.read_started is False

    excess = StreamRequest([b"1234"])
    with pytest.raises(UploadError) as excess_error:
        await store.append(second["id"], "owner", excess, 0, 4)
    assert (excess_error.value.status, excess_error.value.code) == (
        429,
        "too_many_inflight_chunks",
    )
    assert excess.read_started is False
    assert store.status(second["id"], "owner")["committed_offset"] == 0

    release.set()
    completed = await asyncio.wait_for(active, timeout=1)
    assert completed["committed_offset"] == 4


def test_capacity_checks_use_correct_filesystems_and_receiving_reservations(
    tmp_path: Path, monkeypatch
) -> None:
    store = store_for(
        tmp_path,
        admin_chunked_min_free_bytes=10,
        admin_chunked_max_active_tasks=3,
    )
    observed: list[Path] = []

    def usage(path):
        observed.append(Path(path))
        return type("Usage", (), {"free": 100})()

    monkeypatch.setattr("app.chunked_upload.shutil.disk_usage", usage)
    upload = store.create("owner", "upload.zip", ".zip", 20)
    assert observed[-1] == store.root
    store.delete(upload["id"], "owner")

    receiving = store.create("other", "reserved.zip", ".zip", 50)
    observed.clear()
    with pytest.raises(UploadError) as caught:
        store.ensure_import_capacity("target", 41)
    assert caught.value.code == "insufficient_storage"
    assert observed == [store.images_root]
    assert store.status(receiving["id"], "other")["committed_offset"] == 0

    store.ensure_import_capacity("target", 40)
    assert observed[-1] == store.images_root


@pytest.mark.asyncio
async def test_partial_chunk_is_truncated_and_offset_is_not_advanced(tmp_path: Path) -> None:
    store = store_for(tmp_path)
    row = store.create("owner", "archive.zip", ".zip", 12)
    with pytest.raises(UploadError) as caught:
        await store.append(row["id"], "owner", StreamRequest([b"abc"]), 0, 4)
    assert caught.value.code == "chunk_length_mismatch"
    current = store.status(row["id"], "owner")
    assert current["committed_offset"] == 0
    assert (store.root / row["id"] / "upload.bin").stat().st_size == 0


@pytest.mark.asyncio
async def test_sequential_append_file_ahead_recovery_and_missing_file_cleanup(tmp_path: Path) -> None:
    store = store_for(tmp_path)
    row = store.create("owner", "archive.zip", ".zip", 8)
    current = await store.append(row["id"], "owner", StreamRequest([b"abcd"]), 0, 4)
    assert current["committed_offset"] == 4
    path = store.root / row["id"] / "upload.bin"
    with path.open("ab") as handle:
        handle.write(b"uncommitted")
    recovered = store.status(row["id"], "owner")
    assert recovered["committed_offset"] == 4 and path.read_bytes() == b"abcd"
    path.unlink()
    with pytest.raises(UploadError) as missing:
        store.status(row["id"], "owner")
    assert missing.value.code == "upload_data_lost"
    with db.get_conn(store.database_path) as conn:
        assert conn.execute("SELECT 1 FROM upload_tasks WHERE id=?", (row["id"],)).fetchone() is None


def test_ttl_orphan_and_path_boundary_cleanup(tmp_path: Path) -> None:
    store = store_for(tmp_path)
    row = store.create("owner", "archive.zip", ".zip", 8)
    orphan = store.root / ("a" * 48)
    orphan.mkdir(); (orphan / "upload.bin").write_bytes(b"x")
    old = time.time() - 120
    os.utime(orphan, (old, old))
    unrelated = store.root / "keep-me"
    unrelated.mkdir()
    with db.get_conn(store.database_path) as conn:
        conn.execute("UPDATE upload_tasks SET expires_at=? WHERE id=?", (time.time() - 1, row["id"]))
    store.clean()
    assert not (store.root / row["id"]).exists()
    assert not orphan.exists()
    assert unrelated.exists()
    with pytest.raises(UploadError):
        store._paths("../outside")


def test_concurrent_create_respects_global_quota(tmp_path: Path) -> None:
    first = store_for(tmp_path, admin_chunked_max_active_tasks=1)
    second = store_for(tmp_path, admin_chunked_max_active_tasks=1)

    def create(store: UploadStore):
        try:
            return store.create("owner", "archive.zip", ".zip", 8)
        except UploadError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(create, (first, second)))
    assert sum(not isinstance(item, UploadError) for item in results) == 1
    errors = [item for item in results if isinstance(item, UploadError)]
    assert len(errors) == 1 and errors[0].code == "too_many_active_uploads"


def test_nonblocking_lock_closes_handle_and_clean_skips_active_task(tmp_path: Path) -> None:
    store = store_for(tmp_path)
    row = store.create("owner", "archive.zip", ".zip", 8)
    fd_before = len(list(Path("/proc/self/fd").iterdir()))
    with store.locked(row["id"], "owner"):
        with pytest.raises(BlockingIOError):
            with store.locked(row["id"], "owner", nonblocking=True):
                pass
        store.clean(now=time.time() + 120)
        assert (store.root / row["id"] / "upload.bin").exists()
        with db.get_conn(store.database_path) as conn:
            assert conn.execute("SELECT 1 FROM upload_tasks WHERE id=?", (row["id"],)).fetchone()
    assert len(list(Path("/proc/self/fd").iterdir())) == fd_before
    store.clean(now=time.time() + 120)
    assert not (store.root / row["id"]).exists()


def test_symlink_task_is_invalidated_without_touching_target(tmp_path: Path) -> None:
    store = store_for(tmp_path)
    row = store.create("owner", "archive.zip", ".zip", 8)
    task_dir = store.root / row["id"]
    task_dir.joinpath("upload.bin").unlink()
    task_dir.rmdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    marker = outside / "keep"
    marker.write_text("safe", encoding="utf-8")
    task_dir.symlink_to(outside, target_is_directory=True)
    with pytest.raises(UploadError) as caught:
        store.status(row["id"], "owner")
    assert caught.value.code == "upload_not_found"
    store.clean()
    assert marker.read_text(encoding="utf-8") == "safe"
    assert task_dir.is_symlink()
    with db.get_conn(store.database_path) as conn:
        assert conn.execute("SELECT 1 FROM upload_tasks WHERE id=?", (row["id"],)).fetchone() is None


def test_disk_reservation_counts_remaining_bytes_once_and_preview_zero(tmp_path: Path, monkeypatch) -> None:
    store = store_for(tmp_path, admin_chunked_max_active_tasks=4, admin_chunked_min_free_bytes=20)
    usage = type("Usage", (), {"free": 100})()
    monkeypatch.setattr("app.chunked_upload.shutil.disk_usage", lambda _path: usage)
    first = store.create("owner", "first.zip", ".zip", 40)
    with db.get_conn(store.database_path) as conn:
        conn.execute(
            "UPDATE upload_tasks SET committed_offset=expected_size,state='preview_ready' WHERE id=?",
            (first["id"],),
        )
    # preview_ready owns its already-written file but reserves no future bytes;
    # current free space already accounts for bytes physically present.
    second = store.create("owner", "second.zip", ".zip", 80)
    assert second["expected_size"] == 80
    with pytest.raises(UploadError) as caught:
        store.create("owner", "third.zip", ".zip", 1)
    assert caught.value.code == "insufficient_storage"


@pytest.mark.asyncio
async def test_nonfinal_chunk_below_negotiated_minimum_is_rejected(tmp_path: Path) -> None:
    store = store_for(tmp_path, admin_chunk_min_bytes=4, admin_chunk_recommended_bytes=8)
    row = store.create("owner", "archive.zip", ".zip", 10)
    with pytest.raises(UploadError) as caught:
        await store.append(row["id"], "owner", StreamRequest([b"abc"]), 0, 3)
    assert caught.value.code == "chunk_too_small"
    assert store.status(row["id"], "owner")["committed_offset"] == 0
    first = await store.append(row["id"], "owner", StreamRequest([b"abcdefgh"]), 0, 8)
    assert first["committed_offset"] == 8
    final = await store.append(row["id"], "owner", StreamRequest([b"ij"]), 8, 2)
    assert final["committed_offset"] == 10



def test_same_task_concurrent_append_commits_once(tmp_path: Path) -> None:
    first = store_for(tmp_path)
    second = store_for(tmp_path)
    row = first.create("owner", "archive.zip", ".zip", 4)
    barrier = Barrier(3)

    def append(store: UploadStore):
        barrier.wait(timeout=2)
        try:
            return store.append_bytes(str(row["id"]), "owner", b"abcd", 0, 4)
        except UploadError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(append, store) for store in (first, second)]
        barrier.wait(timeout=2)
        results = [future.result(timeout=5) for future in futures]
    assert sum(not isinstance(item, UploadError) for item in results) == 1
    assert [item.code for item in results if isinstance(item, UploadError)] == ["offset_mismatch"]
    assert first.status(str(row["id"]), "owner")["committed_offset"] == 4
    assert (first.root / row["id"] / "upload.bin").read_bytes() == b"abcd"


def test_complete_style_lock_excludes_concurrent_patch_and_cleanup(tmp_path: Path) -> None:
    store = store_for(tmp_path)
    row = store.create("owner", "archive.zip", ".zip", 4)
    complete_entered = Barrier(2)
    release_complete = Barrier(2)
    patch_started = Barrier(2)

    def hold_complete_lock() -> None:
        with store.locked(str(row["id"]), "owner"):
            complete_entered.wait(timeout=2)
            release_complete.wait(timeout=5)

    def patch() -> object:
        patch_started.wait(timeout=2)
        return store.append_bytes(str(row["id"]), "owner", b"abcd", 0, 4)

    with ThreadPoolExecutor(max_workers=2) as pool:
        holder = pool.submit(hold_complete_lock)
        complete_entered.wait(timeout=2)
        patch_future = pool.submit(patch)
        patch_started.wait(timeout=2)
        assert not patch_future.done()
        store.clean(now=time.time() + 120)
        assert (store.root / row["id"] / "upload.bin").exists()
        assert not patch_future.done()
        release_complete.wait(timeout=2)
        holder.result(timeout=5)
        result = patch_future.result(timeout=5)
    assert result["committed_offset"] == 4
    assert (store.root / row["id"] / "upload.bin").read_bytes() == b"abcd"


def test_preview_import_reservation_excludes_current_archive_bytes(tmp_path: Path, monkeypatch) -> None:
    store = store_for(tmp_path, admin_chunked_max_active_tasks=4, admin_chunked_min_free_bytes=20)
    monkeypatch.setattr("app.chunked_upload.shutil.disk_usage", lambda _path: type("Usage", (), {"free": 100})())
    first = store.create("owner", "first.zip", ".zip", 40)
    second = store.create("owner", "second.zip", ".zip", 40)
    with db.get_conn(store.database_path) as conn:
        conn.execute("UPDATE upload_tasks SET committed_offset=expected_size,state='preview_ready',import_required_bytes=30 WHERE id=?", (first["id"],))
        conn.execute("UPDATE upload_tasks SET committed_offset=expected_size,state='preview_ready',import_required_bytes=40 WHERE id=?", (second["id"],))
    store.ensure_import_capacity(str(first["id"]), 30)
    with pytest.raises(UploadError) as caught:
        store.ensure_import_capacity(str(first["id"]), 41)
    assert caught.value.code == "insufficient_storage"


def test_mark_preview_persists_summary_and_import_required_bytes(tmp_path: Path) -> None:
    store = store_for(tmp_path)
    row = store.create("owner", "archive.zip", ".zip", 4)
    store.append_bytes(str(row["id"]), "owner", b"data", 0, 4)
    marked = store.mark_preview(
        str(row["id"]),
        "owner",
        "a" * 64,
        {"members": 1, "imported": 1},
        [("b" * 64, "topic")],
        4,
    )
    assert marked["state"] == "preview_ready"
    assert marked["import_required_bytes"] == 4
    assert marked["error_code"] is None
    assert marked["summary_json"] == '{"members": 1, "imported": 1}'
    assert marked["entries_json"] == '[["' + ("b" * 64) + '", "topic"]]'
