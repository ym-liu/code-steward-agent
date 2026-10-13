"""File-change handling: event aggregator, watcher handler, scanner, background loop, settings and types.

Each test guards one requirement or one problem found in the review of the earlier file triggering.
The last class uses the real operating-system watcher and takes a few seconds.
"""
import copy
import hashlib
import logging
import os
from pathlib import Path
import shutil
import sqlite3
import json
import tempfile
import time
import unittest
from unittest.mock import patch

from Services.ArtifactsService import ArtifactsService
from Services.ClassificationService import ClassificationService
from Services.ConfigService import ConfigService, masked, validate
from Services.EventAggregator import EventAggregator
from Services.FileEventService import FileEventService
from Services.LogsService import configure_logging
from Services.ScanService import ScanService
from service import CodeStewardService


class Settings:
    """Validated settings like ConfigService, without a file."""

    def __init__(self, **sections):
        self.config = validate(sections)

    def get(self, *keys, default=None):
        value = self.config
        for key in keys:
            if not isinstance(value, dict):
                return default
            value = value.get(key, default)
        return value


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def wait_until(condition, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.05)
    return condition()


class Workspace(unittest.TestCase):
    """A temporary root folder with a scanner, database and event aggregator."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = self.base / "scripts"
        self.root.mkdir()
        self.settings = Settings(scanning={"root_paths": [str(self.root)], "exclude_folders": [".git"]},
                                 events={"quiet_seconds": 0, "max_wait_seconds": 0})
        self.artifacts = ArtifactsService(str(self.base / "artifacts.sqlite3"))
        self.scanner = ScanService(self.settings, None, self.artifacts)
        self.aggregator = EventAggregator(quiet_seconds=0, max_wait_seconds=0)
        self.watcher = FileEventService(self.aggregator, self.scanner)
        self.handler = self.watcher.event_handler

    def write(self, name, text="print(1)\n"):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def stored(self):
        return {item["path"]: item for item in self.artifacts.list_artifacts()["items"]}

    def flush(self):
        files, folders, reason = self.aggregator.take_due(everything=True)
        return self.scanner.process(files=files, folders=folders, full_scan=reason)

    def count_hashes(self):
        calls = []
        real = self.scanner._hash_file
        self.scanner._hash_file = lambda path: calls.append(path) or real(path)
        self.addCleanup(setattr, self.scanner, "_hash_file", real)
        return calls


class EventAggregatorTests(unittest.TestCase):
    def test_a_burst_of_events_for_one_file_becomes_one_item_after_a_quiet_period(self):
        clock = Clock()
        aggregator = EventAggregator(quiet_seconds=2, max_wait_seconds=30, clock=clock)
        for _ in range(5):
            aggregator.add("/data/tool.py")
            clock.now += 0.5
        self.assertEqual(aggregator.take_due(), ([], [], None))
        clock.now += 2
        files, _, _ = aggregator.take_due()
        self.assertEqual([(item["path"], item["events"]) for item in files], [(os.path.abspath("/data/tool.py"), 5)])

    def test_a_file_that_never_stops_changing_is_still_checked_after_the_maximum_wait(self):
        clock = Clock()
        aggregator = EventAggregator(quiet_seconds=2, max_wait_seconds=30, clock=clock)
        for _ in range(31):
            aggregator.add("/data/app.log")
            clock.now += 1
        self.assertEqual(len(aggregator.take_due()[0]), 1)

    def test_a_chain_of_renames_is_one_move_from_the_first_name(self):
        aggregator = EventAggregator(quiet_seconds=0)
        aggregator.add("/data/b.py", moved_from="/data/a.py")
        aggregator.add("/data/c.py", moved_from="/data/b.py")
        files, _, _ = aggregator.take_due()
        self.assertEqual([(item["path"], item["moved_from"]) for item in files],
                         [(os.path.abspath("/data/c.py"), os.path.abspath("/data/a.py"))])

    def test_too_many_waiting_paths_become_one_full_scan_instead_of_growing_without_limit(self):
        aggregator = EventAggregator(quiet_seconds=0, max_pending_files=10)
        with self.assertLogs("code-steward.events", level="WARNING"):
            for number in range(25):
                aggregator.add(f"/data/file{number}.py")
        files, folders, reason = aggregator.take_due()
        self.assertEqual(reason, "overflow-scan")
        self.assertLessEqual(len(files), 10)


class ScannerTests(Workspace):
    def test_one_changed_file_is_hashed_alone_not_the_whole_folder(self):
        for number in range(50):
            self.write(f"tool{number}.py", f"print({number})\n")
        self.scanner.process(full_scan="startup-scan")
        hashed = self.count_hashes()
        self.write("tool7.py", "print('edited')\n")
        self.handler.handle("modified", str(self.root / "tool7.py"))
        run = self.flush()
        self.assertEqual(hashed, [str(self.root / "tool7.py")])
        self.assertEqual([item["change_type"] for item in run["results"]], ["modified"])

    def test_full_scan_skips_unchanged_files_and_removes_deleted_ones(self):
        for number in range(20):
            self.write(f"tool{number}.py", f"print({number})\n")
        self.scanner.process(full_scan="startup-scan")
        (self.root / "tool3.py").unlink()
        self.write("tool4.py", "print('a longer new version')\n")
        hashed = self.count_hashes()
        run = self.scanner.process(full_scan="periodic-scan")
        self.assertEqual(hashed, [str(self.root / "tool4.py")])
        self.assertEqual(run["removed"], [str(self.root / "tool3.py")])
        self.assertNotIn(str(self.root / "tool3.py"), self.stored())

    def test_files_moved_out_of_the_roots_lose_their_records_and_folders_too(self):
        outside = self.base / "elsewhere"
        outside.mkdir()
        self.write("single.py")
        self.write("pkg/a.py")
        self.write("pkg/b.py")
        self.scanner.process(full_scan="startup-scan")
        os.replace(self.root / "single.py", outside / "single.py")
        self.handler.handle("moved", str(self.root / "single.py"), str(outside / "single.py"))
        shutil.move(str(self.root / "pkg"), str(outside / "pkg"))
        self.handler.handle("moved", str(self.root / "pkg"), str(outside / "pkg"), is_directory=True)
        self.flush()
        self.assertEqual(self.stored(), {})

    def test_editor_save_through_a_temporary_file_keeps_the_same_identity(self):
        path = self.write("tool.py", "print(1)\n")
        first = self.scanner.process(full_scan="startup-scan")["results"][0]
        for version in range(2, 5):
            swap = self.root / ".tool.py.swp"
            swap.write_text(f"print({version})\n", encoding="utf-8")
            # The order macOS reported for such a save, including a delete for a file that still exists.
            for event in (("created", swap), ("modified", swap)):
                self.handler.handle(event[0], str(event[1]))
            os.replace(swap, path)
            self.handler.handle("moved", str(swap), str(path))
            for event_type in ("created", "modified", "deleted"):
                self.handler.handle(event_type, str(path))
            run = self.flush()
            self.assertEqual([(item["id"], item["change_type"]) for item in run["results"]], [(first["id"], "modified")])

    def test_renames_keep_the_identity_even_in_a_chain(self):
        self.write("a.py")
        first = self.scanner.process(full_scan="startup-scan")["results"][0]
        os.replace(self.root / "a.py", self.root / "b.py")
        self.handler.handle("moved", str(self.root / "a.py"), str(self.root / "b.py"))
        os.replace(self.root / "b.py", self.root / "c.py")
        self.handler.handle("moved", str(self.root / "b.py"), str(self.root / "c.py"))
        moved = self.flush()["results"]
        self.assertEqual([(item["id"], item["path"], item["change_type"]) for item in moved],
                         [(first["id"], str(self.root / "c.py"), "moved")])

    def test_a_file_renamed_over_another_leaves_one_record_for_that_path(self):
        self.write("a.py", "print('a')\n")
        self.write("b.py", "print('b')\n")
        ids = {Path(item["path"]).name: item["id"] for item in self.scanner.process(full_scan="startup-scan")["results"]}
        os.replace(self.root / "a.py", self.root / "b.py")
        self.handler.handle("moved", str(self.root / "a.py"), str(self.root / "b.py"))
        run = self.flush()
        self.assertEqual(run["removed"], [str(self.root / "a.py")])
        kept = self.stored()
        self.assertEqual(list(kept), [str(self.root / "b.py")])
        self.assertEqual((kept[str(self.root / "b.py")]["id"], kept[str(self.root / "b.py")]["hash"]),
                         (ids["b.py"], hashlib.sha256(b"print('a')\n").hexdigest()))

    def test_an_unplugged_drive_keeps_its_records_until_it_is_back(self):
        drive = self.base / "usb-drive"
        self.settings.config["scanning"]["root_paths"] = [str(self.root), str(drive)]
        (drive / "tools").mkdir(parents=True)
        (drive / "tools" / "backup.py").write_text("print('backup')\n", encoding="utf-8")
        self.scanner.process(full_scan="startup-scan")
        shutil.move(str(drive), str(self.base / "unplugged"))
        # Watchers report a drive that goes away as deleted folders and files.
        self.handler.handle("deleted", str(drive), is_directory=True)
        self.handler.handle("deleted", str(drive / "tools" / "backup.py"))
        self.flush()
        with self.assertLogs("code-steward.scanner", level="WARNING"):
            self.scanner.process(full_scan="periodic-scan")
        self.assertIn(str(drive / "tools" / "backup.py"), self.stored())
        shutil.move(str(self.base / "unplugged"), str(drive))
        self.assertEqual(self.scanner.process(full_scan="periodic-scan")["removed"], [])

    @unittest.skipIf(os.name == "nt", "creating links needs developer mode on Windows")
    def test_links_never_lead_into_denied_or_outside_folders(self):
        private, elsewhere, alias = self.base / "private", self.base / "elsewhere", self.base / "alias-root"
        for folder in (private, elsewhere):
            folder.mkdir()
            (folder / "keys.py").write_text("API_KEY = 'abc123456'\n", encoding="utf-8")
        os.symlink(private, alias, target_is_directory=True)  # a root that is a link to a denied folder
        os.symlink(elsewhere, self.root / "linked", target_is_directory=True)  # a linked folder inside a root
        self.settings.config["scanning"]["root_paths"] = [str(self.root), str(alias)]
        self.settings.config["scanning"]["deny_paths"] = [str(private)]
        self.write("real.py")
        with self.assertLogs("code-steward.scanner", level="WARNING"):
            self.scanner.process(full_scan="startup-scan")
        self.assertEqual(list(self.stored()), [str(self.root / "real.py")])
        self.assertFalse(self.handler.handle("created", str(self.root / "linked" / "keys.py")))
        self.assertFalse(self.handler.handle("created", str(self.root / "linked"), is_directory=True))
        self.assertFalse(self.handler.handle("created", str(alias / "keys.py")))
        self.assertNotIn(str(alias), self.watcher._allowed_roots())

    def test_records_of_a_root_added_to_deny_paths_are_removed(self):
        self.write("tool.py")
        self.scanner.process(full_scan="startup-scan")
        self.settings.config["scanning"]["deny_paths"] = [str(self.root)]
        with self.assertLogs("code-steward.scanner", level="WARNING"):
            self.scanner.process(full_scan="periodic-scan")
        self.assertEqual(self.stored(), {})

    def test_own_files_temporary_files_git_folders_links_and_system_folders_are_skipped(self):
        self.write("real.py")
        self.write(".git/config", "[core]\n")
        self.write("draft.txt~", "x\n")
        self.write("~$report.docx", "x\n")
        log = self.write("service.log", "line\n")
        self.scanner.own_paths = {os.path.normcase(str(log))}
        outside = self.base / "private.py"
        outside.write_text("API_KEY = 'abc123456'\n", encoding="utf-8")
        try:
            os.symlink(outside, self.root / "link.py")
        except OSError:
            pass  # Windows without developer mode cannot create links; the other checks still run
        self.scanner.process(full_scan="startup-scan")
        self.assertEqual(list(self.stored()), [str(self.root / "real.py")])
        self.assertFalse(self.scanner.in_scope("/etc/hosts"))
        for event in ("service.log", ".git/config", "draft.txt~"):
            self.assertFalse(self.handler.handle("modified", str(self.root / event)))

    def test_root_paths_written_as_one_line_is_one_folder_not_letters(self):
        settings = Settings(scanning={"root_paths": str(self.root)})
        self.assertEqual(settings.get("scanning", "root_paths"), [str(self.root)])
        walked = []
        with patch("Services.ScanService.os.walk", side_effect=lambda top, **kwargs: walked.append(top) or iter(())):
            ScanService(settings, None, None).process(full_scan="manual-scan")
        self.assertEqual(walked, [str(self.root)])

    @unittest.skipIf(os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0), "needs POSIX permissions")
    def test_an_unreadable_folder_is_reported_and_its_records_are_kept(self):
        self.write("locked/inner.py")
        self.scanner.process(full_scan="startup-scan")
        os.chmod(self.root / "locked", 0)
        self.addCleanup(os.chmod, self.root / "locked", 0o755)
        with self.assertLogs("code-steward.scanner", level="WARNING") as logs:
            self.scanner.process(full_scan="periodic-scan")
        self.assertTrue(any("Cannot open folder" in line for line in logs.output))
        self.assertIn(str(self.root / "locked" / "inner.py"), self.stored())

    def test_a_large_file_is_listed_without_reading_all_of_it(self):
        settings = Settings(scanning={"root_paths": [str(self.root)], "max_file_size_bytes": 10})
        scanner = ScanService(settings, None, self.artifacts)
        self.write("big.csv", "a,b\n" * 100)
        result = scanner.process(full_scan="startup-scan")["results"][0]
        self.assertIsNone(result["hash"])
        self.assertIn("not hashed", " ".join(result["classification_reasons"]))

    def test_an_old_database_is_upgraded_and_keeps_its_ids(self):
        path = self.write("kept.py", "print('kept')\n")
        database = self.base / "old.sqlite3"
        with sqlite3.connect(database) as connection:
            connection.execute("CREATE TABLE artifacts (id TEXT PRIMARY KEY, path TEXT NOT NULL UNIQUE, metadata TEXT NOT NULL)")
            import hashlib
            record = {"id": "old-id", "path": str(path), "hash": hashlib.sha256(path.read_bytes()).hexdigest(),
                      "size_bytes": path.stat().st_size, "name": "kept.py"}
            connection.execute("INSERT INTO artifacts VALUES (?, ?, ?)",
                               ("old-id", os.path.normcase(str(path)), json.dumps(record)))
        artifacts = ArtifactsService(str(database))
        scanner = ScanService(self.settings, None, artifacts)
        run = scanner.process(full_scan="startup-scan")
        self.assertEqual(run["results"], [])
        self.assertEqual(artifacts.known_state(str(path))["id"], "old-id")
        self.assertEqual(artifacts.list_artifacts()["items"][0]["artifact_type"], "script")
        with sqlite3.connect(database) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 2)


class BackgroundServiceTests(Workspace):
    def make_service(self, watcher=None):
        self.settings.config["service"]["tick_seconds"] = 0.05
        service = CodeStewardService(file_event_service=watcher, scan_service=self.scanner, aggregator=self.aggregator)
        self.scanner.code_steward_service = service
        self.addCleanup(lambda: (service.stop(), service.wait_stopped(5)))
        return service

    def test_start_scans_at_once_and_finds_changes_made_while_stopped(self):
        self.write("one.py")
        self.write("two.py")
        service = self.make_service()
        service.start()
        self.assertTrue(wait_until(lambda: len(self.stored()) == 2))
        service.stop()
        self.assertTrue(service.wait_stopped(5))
        (self.root / "two.py").unlink()
        self.write("three.py")
        service.start()
        self.assertTrue(wait_until(lambda: sorted(Path(p).name for p in self.stored()) == ["one.py", "three.py"]))

    def test_changes_wait_during_pause_and_are_handled_after_resume(self):
        self.write("tool.py", "print(1)\n")
        service = self.make_service()
        service.start()
        self.assertTrue(wait_until(lambda: len(self.stored()) == 1))
        before = self.stored()[str(self.root / "tool.py")]["hash"]
        service.pause()
        self.write("tool.py", "print('changed while paused')\n")
        self.handler.handle("modified", str(self.root / "tool.py"))
        time.sleep(0.3)
        self.assertEqual(self.aggregator.status()["waiting_files"], 1)
        self.assertEqual(self.stored()[str(self.root / "tool.py")]["hash"], before)
        service.resume()
        self.assertTrue(wait_until(lambda: self.stored()[str(self.root / "tool.py")]["hash"] != before))

    def test_changes_during_a_running_scan_are_handled_by_the_next_step(self):
        for number in range(3):
            self.write(f"tool{number}.py", f"print({number})\n")
        real = self.scanner._hash_file
        self.scanner._hash_file = lambda path: time.sleep(0.2) or real(path)
        service = self.make_service()
        service.start()
        self.assertTrue(wait_until(lambda: self.scanner.get_status()["is_scanning"], timeout=5))
        self.write("tool0.py", "print('changed during the scan')\n")
        self.handler.handle("modified", str(self.root / "tool0.py"))
        changed = len("print('changed during the scan')\n")
        self.assertTrue(wait_until(lambda: self.stored().get(str(self.root / "tool0.py"), {}).get("size_bytes") == changed))

    def test_a_failed_step_is_logged_and_retried_with_a_full_scan(self):
        self.write("tool.py")
        real = self.artifacts.classify
        failures = []

        def fails_once(*args, **kwargs):
            if not failures:
                failures.append(1)
                raise sqlite3.OperationalError("database is locked")
            return real(*args, **kwargs)
        self.artifacts.classify = fails_once
        service = self.make_service()
        service.MAX_BACKOFF_SECONDS = 0.2
        with self.assertLogs("code-steward.service", level="ERROR"):
            service.start()
            self.assertTrue(wait_until(lambda: len(self.stored()) == 1))
        self.assertTrue(wait_until(lambda: service.last_error is None))

    def test_a_watcher_that_cannot_start_does_not_stop_the_service(self):
        self.settings.config["scanning"]["watch_mode"] = "events"
        watcher = FileEventService(self.aggregator, self.scanner)
        broken = unittest.mock.MagicMock()
        broken.return_value.start.side_effect = OSError("cannot watch this drive")
        with patch("Services.FileEventService.Observer", broken):
            service = self.make_service(watcher)
            with self.assertLogs("code-steward.file-events", level="WARNING"):
                self.assertTrue(service.start())
        self.assertEqual(service.state.value, "running")
        self.assertFalse(watcher.status()["running"])
        self.assertTrue(service.stop())
        self.assertTrue(service.wait_stopped(5))
        self.assertTrue(service.start())

    def test_a_missing_folder_is_watched_once_it_appears(self):
        missing = self.base / "usb-drive"
        self.settings.config["scanning"]["root_paths"] = [str(missing)]
        self.settings.config["scanning"]["watch_mode"] = "polling"
        with self.assertLogs("code-steward.file-events", level="WARNING"):
            self.watcher.start()
        self.addCleanup(self.watcher.stop)
        self.assertEqual(self.watcher.status()["missing"], [str(missing)])
        missing.mkdir()
        self.assertTrue(self.watcher.refresh())
        self.assertEqual(self.watcher.status()["watched"], [str(missing)])


class SettingsAndLogTests(unittest.TestCase):
    def test_invalid_settings_list_every_problem_and_secrets_are_hidden(self):
        with self.assertRaises(ValueError) as error:
            validate({"service": {"tick_seconds": "fast"}, "scanning": {"include_extensions": ["py"],
                                                                         "watch_mode": "sometimes"}})
        for problem in ("service.tick_seconds", "include_extensions must start with a dot", "watch_mode"):
            self.assertIn(problem, str(error.exception))
        shown = masked({"github": {"token": "ghp_secret", "user": "kim"}, "database_password": ""})
        self.assertEqual(shown, {"github": {"token": "***", "user": "kim"}, "database_password": ""})

    def test_reload_keeps_the_previous_settings_when_the_file_is_invalid(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.yaml"
            path.write_text("scanning:\n  scan_interval_seconds: 120\n", encoding="utf-8")
            with patch("Services.ConfigService.CONFIG_PATH", str(path)):
                config = ConfigService()
                changed = []
                config.on_reload(changed.append)
                path.write_text("scanning:\n  scan_interval_seconds: 1\n", encoding="utf-8")
                with self.assertRaises(ValueError), self.assertLogs("code-steward.config", level="ERROR"):
                    config.reload()
                self.assertEqual(config.get("scanning", "scan_interval_seconds"), 120)
                path.write_text("scanning:\n  scan_interval_seconds: 600\n", encoding="utf-8")
                config.reload()
                self.assertEqual(len(changed), 1)

    def test_the_log_file_starts_over_at_its_size_limit(self):
        project_logger = logging.getLogger("code-steward")
        previous = (project_logger.handlers[:], project_logger.level)
        with tempfile.TemporaryDirectory() as folder:
            log = Path(folder) / "service.log"
            try:
                project_logger.handlers = []
                with patch("sys.stderr"):
                    configure_logging(log, "INFO", max_bytes=2000, backup_count=2)
                    for number in range(100):
                        logging.getLogger("code-steward.scanner").info("Artifact created: file%d.py", number)
                self.assertTrue((Path(folder) / "service.log.1").exists())
                self.assertFalse((Path(folder) / "service.log.3").exists())
                self.assertLessEqual(log.stat().st_size, 2000)
            finally:
                for handler in project_logger.handlers:
                    handler.close()
                project_logger.handlers, project_logger.level = previous


class ClassificationTests(unittest.TestCase):
    def describe(self, name, content, root=None):
        data = content if isinstance(content, bytes) else content.encode("utf-8")
        return ClassificationService().describe(os.path.join(root or "/data/samples", name), data, root or "/data/samples")

    def test_each_requirement_type_is_recognized(self):
        cases = {
            ("tool.py", "import sys\nprint(sys.argv)\n"): "script",
            ("run_all.sh", "#!/bin/sh\npython train.py\npython evaluate.py\n"): "wrapper",
            ("backup", "#!/bin/bash\nrsync -a src/ dst/\nrsync -a a/ b/\n"): "script",
            ("app.exe", b"MZ" + b"\x00" * 64): "binary",
            ("hello", b"\x7fELF\x02" + b"\x00" * 64): "binary",
            ("README.md", "# Notes\n"): "documentation",
            ("LICENSE", "MIT License\n"): "documentation",
            ("prices.csv", "a,b\n1,2\n"): "data",
            ("examples/input.csv", "a,b\n1,2\n"): "sample",
            ("urls.txt", "https://example.com/a\nhttps://example.com/b\n"): "data",
            ("notes.txt", "things i always forget\nrestart the server after a deploy\n"): "documentation",
            ("mystery.txt", "FRAME Z17\nLOAD $ITEMS\n"): "unknown",
        }
        for (name, content), expected in cases.items():
            with self.subTest(name=name):
                result = self.describe(name, content)
                self.assertEqual(result["artifact_type"], expected)
                self.assertTrue(0 < result["confidence"] < 1 and result["reasons"])

    def test_a_root_named_samples_does_not_make_every_file_a_sample(self):
        self.assertEqual(self.describe("prices.csv", "a,b\n1,2\n", root="/home/kim/samples")["artifact_type"], "data")

    def test_settings_files_and_written_secrets_are_sensitive_but_placeholders_are_not(self):
        self.assertTrue(self.describe(".env", "API_KEY=replace-me\n")["sensitive"])
        self.assertTrue(self.describe("configs/base.yaml", "lr: 0.1\n")["sensitive"])
        self.assertTrue(self.describe("send.py", 'password = "hunter2hunter"\n')["sensitive"])
        self.assertFalse(self.describe("send.py", 'password = "change-me"\n')["sensitive"])
        self.assertFalse(self.describe("missing_config.py", "import json\n")["sensitive"])


class RealWatcherTests(Workspace):
    """The operating system's own change events, end to end (a few seconds)."""

    def test_new_files_editor_saves_and_folders_moved_away(self):
        self.settings.config["service"]["tick_seconds"] = 0.1
        self.settings.config["scanning"]["watch_mode"] = "events"
        watcher = FileEventService(self.aggregator, self.scanner)
        service = CodeStewardService(file_event_service=watcher, scan_service=self.scanner, aggregator=self.aggregator)
        self.scanner.code_steward_service = service
        self.addCleanup(lambda: (service.stop(), service.wait_stopped(5)))
        service.start()
        self.assertTrue(wait_until(lambda: self.scanner.get_status()["last_full_scan_at"] is not None))
        path = self.write("tool.py", "print(1)\n")
        self.assertTrue(wait_until(lambda: str(path) in self.stored()))
        first_id = self.stored()[str(path)]["id"]
        swap = self.root / ".tool.py.swp"
        saved = "print('saved by an editor')\n"
        swap.write_text(saved, encoding="utf-8")
        os.replace(swap, path)
        self.assertTrue(wait_until(lambda: self.stored().get(str(path), {}).get("size_bytes") == len(saved)))
        self.assertEqual(self.stored()[str(path)]["id"], first_id)
        self.write("pkg/lib.py")
        self.assertTrue(wait_until(lambda: str(self.root / "pkg" / "lib.py") in self.stored()))
        shutil.move(str(self.root / "pkg"), str(self.base / "pkg"))
        self.assertTrue(wait_until(lambda: str(self.root / "pkg" / "lib.py") not in self.stored()))

    def test_a_root_that_disappears_and_comes_back_is_watched_again(self):
        self.settings.config["scanning"].update(watch_mode="polling", polling_interval_seconds=1)
        self.assertTrue(self.watcher.start())
        self.addCleanup(self.watcher.stop)
        shutil.move(str(self.root), str(self.base / "away"))
        with self.assertLogs("code-steward.file-events", level="WARNING"):
            self.assertTrue(self.watcher.refresh())
        self.assertEqual(self.watcher.status()["missing"], [str(self.root)])
        self.assertFalse(self.watcher.refresh())  # nothing changed since
        shutil.move(str(self.base / "away"), str(self.root))
        self.assertTrue(self.watcher.refresh())
        self.assertEqual(self.watcher.status()["watched"], [str(self.root)])
        self.aggregator.clear()
        self.write("new.py")
        self.assertTrue(wait_until(lambda: self.aggregator.status()["waiting_files"] == 1, timeout=10))


if __name__ == "__main__":
    unittest.main()
