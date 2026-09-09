import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from unittest.mock import patch

from Services.ArtifactsService import ArtifactsService
from Services.ScanService import ScanService


class FakeConfigService:
    def __init__(self, root_path, include_extensions=None):
        self.config = {
            "scanning": {
                "root_paths": [str(root_path)],
                "include_extensions": [".py"] if include_extensions is None else include_extensions,
                "exclude_folders": [],
                "max_file_size_bytes": 1024 * 1024,
            }
        }

    def get(self, *keys, default=None):
        value = self.config
        for key in keys:
            value = value.get(key, default)
        return value


class ArtifactPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.root = self.directory / "samples"
        self.root.mkdir()
        self.database = self.directory / "artifacts.sqlite3"
        self.checkpoint = self.directory / "scan_checkpoint.json"
        checkpoint_patch = patch("Services.ScanService.CHECKPOINT_FILE", str(self.checkpoint))
        checkpoint_patch.start()
        self.addCleanup(checkpoint_patch.stop)

    def create_services(self, database=None, include_extensions=None):
        artifacts = ArtifactsService(str(database or self.database))
        scanner = ScanService(
            config_service=FakeConfigService(self.root, include_extensions),
            logs_service=None,
            artifacts_service=artifacts,
        )
        return artifacts, scanner

    def write_script(self, name="hello.py", contents="print('hello')\n"):
        path = self.root / name
        path.write_text(contents, encoding="utf-8")
        return path

    def test_service_restart_restores_all_metadata_and_skips_unchanged_files(self):
        self.write_script()
        self.write_script("other.py", "print('other')\n")
        artifacts, scanner = self.create_services()
        scanner.enqueue_manual_scan()
        saved = artifacts.list_artifacts()
        self.assertEqual(saved["count"], 2)

        restarted_artifacts, restarted_scanner = self.create_services()
        self.assertEqual(restarted_artifacts.list_artifacts(), saved)
        for artifact in saved["items"]:
            self.assertEqual(restarted_artifacts.get_artifact(artifact["id"]), artifact)
        self.assertEqual(restarted_scanner.enqueue_manual_scan()["results"], [])
        self.assertEqual(restarted_artifacts.list_artifacts(), saved)

    def test_separate_processes_restore_ids_and_metadata(self):
        self.write_script()
        script = """
import json
import sys
from Services.ArtifactsService import ArtifactsService
from Services.ScanService import ScanService

class Config:
    def get(self, *keys, default=None):
        settings = {"root_paths": [sys.argv[2]], "include_extensions": [".py"]}
        return settings.get(keys[-1], default)

artifacts = ArtifactsService(sys.argv[1])
scanner = ScanService(Config(), None, artifacts)
before = artifacts.list_artifacts()
result = scanner.enqueue_manual_scan()
print(json.dumps({"before": before, "scan": result, "after": artifacts.list_artifacts()}))
"""
        def run_process():
            process = subprocess.run(
                [sys.executable, "-c", script, str(self.database), str(self.root)],
                cwd=Path(__file__).resolve().parents[1],
                capture_output=True,
                text=True,
                timeout=30,
                check=True,
            )
            return json.loads(process.stdout)

        first = run_process()
        second = run_process()
        self.assertEqual(first["before"]["count"], 0)
        self.assertEqual(first["scan"]["files_found"], 1)
        self.assertEqual(second["before"], first["after"])
        self.assertEqual(second["scan"]["results"], [])
        self.assertEqual(second["after"], first["after"])

    def test_legacy_checkpoint_does_not_hide_files_from_an_empty_database(self):
        path = self.write_script()
        file_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        self.checkpoint.write_text(json.dumps({str(path): file_hash}), encoding="utf-8")
        artifacts, scanner = self.create_services()

        result = scanner.enqueue_manual_scan()

        self.assertEqual(result["files_found"], 1)
        self.assertEqual(result["results"][0]["path"], str(path))
        self.assertEqual(result["results"][0]["hash"], file_hash)
        self.assertEqual(artifacts.list_artifacts()["count"], 1)
        restarted_artifacts, _ = self.create_services()
        self.assertEqual(restarted_artifacts.list_artifacts(), artifacts.list_artifacts())

    def test_modify_and_move_after_restarts_keep_the_original_id(self):
        original = self.write_script()
        _, scanner = self.create_services()
        created = scanner.enqueue_manual_scan()["results"][0]

        self.write_script(contents="print('changed after restart')\n")
        _, restarted_scanner = self.create_services()
        modified = restarted_scanner.enqueue_manual_scan()["results"][0]
        self.assertEqual(modified["id"], created["id"])
        self.assertEqual(modified["change_type"], "modified")
        self.assertNotEqual(modified["hash"], created["hash"])

        destination = self.root / "renamed.py"
        original.rename(destination)
        _, restarted_scanner = self.create_services()
        moved = restarted_scanner.enqueue_manual_scan(events=[{
            "event_type": "moved",
            "source_path": str(original),
            "destination_path": str(destination),
        }])["results"][0]
        self.assertEqual(moved["id"], created["id"])
        self.assertEqual(moved["change_type"], "moved")
        self.assertEqual(moved["previous_path"], str(original))
        self.assertEqual(moved["path"], str(destination))
        self.assertEqual(moved["hash"], modified["hash"])

        restarted_artifacts, restarted_scanner = self.create_services()
        self.assertEqual(restarted_artifacts.list_artifacts(), {"items": [moved], "count": 1})
        self.assertEqual(restarted_scanner.enqueue_manual_scan()["results"], [])

    def test_delete_event_remains_deleted_after_restart(self):
        path = self.write_script()
        _, scanner = self.create_services()
        created = scanner.enqueue_manual_scan()["results"][0]
        path.unlink()
        _, restarted_scanner = self.create_services()
        restarted_scanner.enqueue_manual_scan(events=[{
            "event_type": "deleted",
            "source_path": str(path),
            "destination_path": None,
        }])

        restarted_artifacts, restarted_scanner = self.create_services()
        self.assertEqual(restarted_artifacts.list_artifacts(), {"items": [], "count": 0})
        self.assertEqual(restarted_artifacts.get_artifact(created["id"])["message"], "Artifact not found.")
        self.assertEqual(restarted_scanner.enqueue_manual_scan()["results"], [])

    def test_database_and_sidecars_inside_scan_root_are_not_registered(self):
        path = self.write_script()
        database = self.root / "artifacts.sqlite3"
        artifacts, scanner = self.create_services(database=database, include_extensions=[])
        connection = sqlite3.connect(str(database))
        try:
            # Keep real WAL and shared-memory files present throughout the scan.
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA user_version=1")
            connection.commit()
            journal = Path(str(database) + "-journal")
            journal.write_bytes(b"")
            for suffix in ("", "-journal", "-wal", "-shm"):
                self.assertTrue(Path(str(database) + suffix).exists())

            result = scanner.enqueue_manual_scan()

            self.assertEqual([item["path"] for item in result["results"]], [str(path)])
            self.assertEqual(artifacts.list_artifacts()["count"], 1)
            self.assertEqual(scanner.enqueue_manual_scan()["results"], [])
        finally:
            connection.close()

    def test_failed_insert_is_reported_and_can_be_retried(self):
        self.write_script()
        artifacts, scanner = self.create_services()
        with closing(sqlite3.connect(str(self.database))) as connection:
            connection.execute("""
                CREATE TRIGGER reject_artifact BEFORE INSERT ON artifacts
                BEGIN SELECT RAISE(ABORT, 'test storage failure'); END
            """)
            connection.commit()

        with self.assertLogs("code-steward.scanner", level="ERROR"):
            with self.assertRaises(sqlite3.IntegrityError):
                scanner.enqueue_manual_scan()

        self.assertFalse(scanner.get_status()["is_scanning"])
        self.assertEqual(artifacts.list_artifacts()["count"], 0)
        with closing(sqlite3.connect(str(self.database))) as connection:
            connection.execute("DROP TRIGGER reject_artifact")
            connection.commit()
        retried = scanner.enqueue_manual_scan()
        self.assertEqual(retried["files_found"], 1)
        self.assertEqual(artifacts.list_artifacts()["count"], 1)

    def test_failed_update_preserves_saved_metadata_until_retry(self):
        self.write_script()
        artifacts, scanner = self.create_services()
        created = scanner.enqueue_manual_scan()["results"][0]
        self.write_script(contents="print('new contents')\n")
        with closing(sqlite3.connect(str(self.database))) as connection:
            connection.execute("""
                CREATE TRIGGER reject_update BEFORE UPDATE ON artifacts
                BEGIN SELECT RAISE(ABORT, 'test storage failure'); END
            """)
            connection.commit()

        with self.assertLogs("code-steward.scanner", level="ERROR"):
            with self.assertRaises(sqlite3.IntegrityError):
                scanner.enqueue_manual_scan()

        restarted_artifacts, restarted_scanner = self.create_services()
        self.assertEqual(restarted_artifacts.get_artifact(created["id"]), created)
        self.assertFalse(scanner.get_status()["is_scanning"])
        with closing(sqlite3.connect(str(self.database))) as connection:
            connection.execute("DROP TRIGGER reject_update")
            connection.commit()
        updated = restarted_scanner.enqueue_manual_scan()["results"][0]
        self.assertEqual(updated["id"], created["id"])
        self.assertNotEqual(updated["hash"], created["hash"])
        self.assertEqual(updated["change_type"], "modified")

    def test_api_reads_and_scan_can_use_the_service_from_different_threads(self):
        self.write_script()
        artifacts, scanner = self.create_services()
        created = scanner.enqueue_manual_scan()["results"][0]
        self.write_script(contents="print('updated by scanner thread')\n")
        start = threading.Barrier(2)

        def scan_from_worker():
            start.wait(timeout=10)
            return scanner.enqueue_manual_scan()

        def read_from_api_worker():
            start.wait(timeout=10)
            for _ in range(10):
                self.assertEqual(artifacts.list_artifacts()["count"], 1)
                self.assertEqual(artifacts.get_artifact(created["id"])["id"], created["id"])

        with ThreadPoolExecutor(max_workers=2) as executor:
            scan_future = executor.submit(scan_from_worker)
            read_future = executor.submit(read_from_api_worker)
            result = scan_future.result(timeout=20)
            read_future.result(timeout=20)

        self.assertEqual(result["results"][0]["id"], created["id"])
        self.assertEqual(result["results"][0]["change_type"], "modified")
        self.assertEqual(artifacts.get_artifact(created["id"]), result["results"][0])

    def test_file_disappearing_before_size_does_not_stop_other_files(self):
        missing = self.write_script("disappears.py")
        remaining = self.write_script("remaining.py")
        artifacts, scanner = self.create_services()
        real_getsize = os.path.getsize

        def remove_before_size(path):
            if path == str(missing):
                missing.unlink()
            return real_getsize(path)

        with patch("Services.ScanService.os.path.getsize", side_effect=remove_before_size):
            with self.assertLogs("code-steward.scanner", level="WARNING"):
                result = scanner.enqueue_manual_scan()

        self.assertEqual([item["path"] for item in result["results"]], [str(remaining)])
        self.assertEqual(artifacts.list_artifacts()["count"], 1)
        self.assertFalse(scanner.get_status()["is_scanning"])

    def test_file_disappearing_before_mtime_does_not_stop_other_files(self):
        missing = self.write_script("disappears.py")
        remaining = self.write_script("remaining.py")
        artifacts, scanner = self.create_services()
        real_getmtime = os.path.getmtime

        def remove_before_mtime(path):
            if path == str(missing):
                missing.unlink()
            return real_getmtime(path)

        with patch("Services.ScanService.os.path.getmtime", side_effect=remove_before_mtime):
            with self.assertLogs("code-steward.scanner", level="WARNING"):
                result = scanner.enqueue_manual_scan()

        self.assertEqual([item["path"] for item in result["results"]], [str(remaining)])
        self.assertEqual(artifacts.list_artifacts()["count"], 1)
        self.assertFalse(scanner.get_status()["is_scanning"])


if __name__ == "__main__":
    unittest.main()
