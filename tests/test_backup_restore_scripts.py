"""Isolated integration tests for backup/restore shell safety contracts."""

from __future__ import annotations

import io
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class BackupRestoreScriptsIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name) / "project"
        (self.root / "scripts").mkdir(parents=True)
        (self.root / "backups").mkdir()
        for name in ("backup.sh", "restore.sh"):
            shutil.copy2(PROJECT_ROOT / "scripts" / name, self.root / "scripts" / name)
        self.fake_bin = Path(self.tempdir.name) / "bin"
        self.fake_bin.mkdir()
        docker = self.fake_bin / "docker"
        docker.write_text(
            """#!/bin/sh
case "${FAKE_DOCKER_MODE:-stopped}:$*" in
  error:*) exit 125 ;;
  *:'compose version') echo 'Docker Compose version fake' ;;
  running:'compose ps --status running -q api') echo fake-container-id ;;
  stopped:'compose ps --status running -q api') exit 0 ;;
  *) exit 64 ;;
esac
""",
            encoding="utf-8",
        )
        docker.chmod(0o755)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def env(self, mode: str = "stopped", **values: str) -> dict[str, str]:
        env = os.environ.copy()
        env.update(
            PATH=f"{self.fake_bin}:{env['PATH']}",
            FAKE_DOCKER_MODE=mode,
            RESTORE_CONFIRM="YES",
            RESTORE_FREE_SPACE_MARGIN_BYTES="0",
        )
        env.update(values)
        return env

    def run_restore(
        self, archive: Path, mode: str = "stopped", **values: str
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(self.root / "scripts" / "restore.sh"), str(archive)],
            cwd=self.root,
            env=self.env(mode, **values),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
            check=False,
        )

    @staticmethod
    def database_bytes(task_count: int = 1) -> bytes:
        with tempfile.NamedTemporaryFile() as handle:
            with sqlite3.connect(handle.name) as conn:
                conn.execute("CREATE TABLE upload_tasks (id TEXT PRIMARY KEY)")
                conn.executemany(
                    "INSERT INTO upload_tasks VALUES (?)",
                    [(f"task-{index}",) for index in range(task_count)],
                )
            return Path(handle.name).read_bytes()

    def archive(
        self, *, images: bool = True, database: bool = True, payload_size: int = 4
    ) -> Path:
        count = len(list((self.root / "backups").glob("case-*")))
        path = self.root / "backups" / f"case-{count}.tar.gz"
        with tarfile.open(path, "w:gz") as archive:
            if images:
                directory = tarfile.TarInfo("data/images")
                directory.type = tarfile.DIRTYPE
                archive.addfile(directory)
                payload = b"x" * payload_size
                image = tarfile.TarInfo("data/images/desktop/example.jpg")
                image.size = len(payload)
                archive.addfile(image, io.BytesIO(payload))
            if database:
                payload = self.database_bytes()
                db = tarfile.TarInfo("data/database/images.db")
                db.size = len(payload)
                archive.addfile(db, io.BytesIO(payload))
        return path

    def seed_live_data(self) -> None:
        (self.root / "data/images").mkdir(parents=True)
        (self.root / "data/images/old.jpg").write_bytes(b"old")
        (self.root / "data/database").mkdir(parents=True)
        (self.root / "data/database/images.db").write_bytes(self.database_bytes(2))
        chunked = self.root / "data/tmp/admin/chunked/task-old"
        chunked.mkdir(parents=True)
        (chunked / "upload.bin").write_bytes(b"partial")

    def test_restore_refuses_running_api_before_live_changes(self) -> None:
        self.seed_live_data()
        result = self.run_restore(self.archive(), mode="running")
        self.assertEqual(result.returncode, 4, result.stderr)
        self.assertIn("Refusing online restore", result.stderr)
        self.assertEqual((self.root / "data/images/old.jpg").read_bytes(), b"old")
        self.assertTrue((self.root / "data/tmp/admin/chunked/task-old/upload.bin").is_file())

    def test_restore_requires_images(self) -> None:
        self.seed_live_data()
        result = self.run_restore(self.archive(images=False))
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertIn("requires data/images", result.stderr)
        self.assertTrue((self.root / "data/images/old.jpg").is_file())

    def test_restore_requires_database(self) -> None:
        self.seed_live_data()
        result = self.run_restore(self.archive(database=False))
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertIn("requires data/database/images.db", result.stderr)
        self.assertTrue((self.root / "data/database/images.db").is_file())

    def test_restore_rejects_expanded_size_over_limit(self) -> None:
        self.seed_live_data()
        result = self.run_restore(
            self.archive(payload_size=32), RESTORE_MAX_TOTAL_BYTES="16"
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("RESTORE_MAX_TOTAL_BYTES", result.stderr)
        self.assertTrue((self.root / "data/images/old.jpg").is_file())

        unsafe_factor = self.run_restore(
            self.archive(), RESTORE_EXPANDED_SPACE_FACTOR="1"
        )
        self.assertNotEqual(unsafe_factor.returncode, 0)
        self.assertIn("RESTORE_EXPANDED_SPACE_FACTOR must be at least 2", unsafe_factor.stderr)
        self.assertTrue((self.root / "data/images/old.jpg").is_file())

    def test_restore_succeeds_and_clears_upload_tasks_and_chunked(self) -> None:
        self.seed_live_data()
        result = self.run_restore(self.archive())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.root / "data/images/desktop/example.jpg").is_file())
        for orientation in ("desktop", "mobile", "square"):
            self.assertTrue((self.root / "data/images" / orientation).is_dir())
        self.assertFalse((self.root / "data/images/old.jpg").exists())
        self.assertFalse((self.root / "data/tmp/admin/chunked").exists())
        with sqlite3.connect(self.root / "data/database/images.db") as conn:
            count = conn.execute("SELECT COUNT(*) FROM upload_tasks").fetchone()[0]
        self.assertEqual(count, 0)

    def test_restore_switch_failure_rolls_back_database_images_and_chunked(self) -> None:
        self.seed_live_data()
        real_rm = shutil.which("rm")
        self.assertIsNotNone(real_rm)
        fail_marker = Path(self.tempdir.name) / "rm-failed-once"
        wrapper = self.fake_bin / "rm"
        wrapper.write_text(
            f"""#!/bin/sh
case "$*" in
  *data/tmp/admin/chunked*)
    if [ ! -e "$FAIL_RM_MARKER" ]; then
      : > "$FAIL_RM_MARKER"
      exit 71
    fi
    ;;
esac
exec {real_rm} "$@"
""",
            encoding="utf-8",
        )
        wrapper.chmod(0o755)

        result = self.run_restore(
            self.archive(), FAIL_RM_MARKER=str(fail_marker)
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "rolling back previous images, database, and chunked uploads",
            result.stderr,
        )
        self.assertEqual((self.root / "data/images/old.jpg").read_bytes(), b"old")
        self.assertFalse((self.root / "data/images/desktop/example.jpg").exists())
        self.assertEqual(
            (self.root / "data/tmp/admin/chunked/task-old/upload.bin").read_bytes(),
            b"partial",
        )
        with sqlite3.connect(self.root / "data/database/images.db") as conn:
            count = conn.execute("SELECT COUNT(*) FROM upload_tasks").fetchone()[0]
        self.assertEqual(count, 2)

    def test_restore_rejects_unsafe_members(self) -> None:
        for kind in ("duplicate", "link", "escape"):
            with self.subTest(kind=kind):
                path = self.root / "backups" / f"unsafe-{kind}.tar.gz"
                with tarfile.open(path, "w:gz") as archive:
                    first = tarfile.TarInfo("data/images")
                    first.type = tarfile.DIRTYPE
                    archive.addfile(first)
                    names = {
                        "duplicate": "./data/images",
                        "link": "data/link",
                        "escape": "../escape",
                    }
                    unsafe = tarfile.TarInfo(names[kind])
                    unsafe.type = tarfile.SYMTYPE if kind == "link" else tarfile.DIRTYPE
                    unsafe.linkname = "/tmp/escape" if kind == "link" else ""
                    archive.addfile(unsafe)
                result = self.run_restore(path)
                self.assertNotEqual(result.returncode, 0)
                self.assertRegex(result.stderr, "Duplicate|Links and special|Unsafe backup")

    def test_backup_refuses_running_and_requires_override_when_unknown(self) -> None:
        self.seed_live_data()
        command = [str(self.root / "scripts" / "backup.sh")]
        running = subprocess.run(
            command,
            cwd=self.root,
            env=self.env("running", BACKUP_OFFLINE_CONFIRMED="1"),
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(running.returncode, 4, running.stderr)
        self.assertIn("Refusing online backup", running.stderr)

        denied = subprocess.run(
            command,
            cwd=self.root,
            env=self.env("error"),
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(denied.returncode, 4, denied.stderr)
        allowed = subprocess.run(
            command,
            cwd=self.root,
            env=self.env("error", BACKUP_OFFLINE_CONFIRMED="1"),
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(allowed.returncode, 0, allowed.stderr)
        archive_path = Path(allowed.stdout.strip().split(": ", 1)[1])
        with tarfile.open(archive_path, "r:gz") as archive:
            manifest_file = archive.extractfile("./MANIFEST.txt")
            self.assertIsNotNone(manifest_file)
            manifest = manifest_file.read().decode("utf-8")
        self.assertNotIn(str(self.root), manifest)

        restored = self.run_restore(archive_path)
        self.assertEqual(restored.returncode, 0, restored.stderr)
        with sqlite3.connect(self.root / "data/database/images.db") as conn:
            count = conn.execute("SELECT COUNT(*) FROM upload_tasks").fetchone()[0]
        self.assertEqual(count, 0)


if __name__ == "__main__":
    unittest.main()
