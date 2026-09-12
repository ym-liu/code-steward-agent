import io
import logging
from pathlib import Path
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import patch

from Services.ConfigService import ConfigService
from Services.LogsService import LogsService, configure_logging
from Services.ScanService import ScanService


class LogsServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "service.log"
        self.logs = LogsService(self.path)

    def test_legacy_and_current_entries_are_returned_newest_first(self):
        self.path.write_text(
            "2026-09-01 10:00:00,123 [INFO] Earlier scan complete.\n"
            "2026-09-11T18:00:00.456+00:00 [WARNING] [code-steward.scanner] Scan blocked.\n",
            encoding="utf-8",
        )
        result = self.logs.list_logs()
        self.assertEqual(result["count"], 2)
        self.assertEqual(result["items"][0]["source"], "code-steward.scanner")
        self.assertEqual(result["items"][0]["timestamp"], "2026-09-11T18:00:00.456+00:00")
        self.assertIsNone(result["items"][1]["source"])
        self.assertEqual(result["items"][1]["timestamp"], "2026-09-01T10:00:00.123")

    def test_filters_apply_before_limit_and_preserve_matching_order(self):
        self.logs.append_log("WARNING", "Scan blocked because paused")
        self.logs.append_log("WARNING", "Missing configuration")
        self.logs.append_log("WARNING", "Scan blocked because busy")
        self.logs.append_log("INFO", "Scan complete")
        result = self.logs.list_logs(limit=1, level="WARNING", search="BLOCKED")
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["items"][0]["message"], "Scan blocked because busy")

    def test_appended_context_survives_reopening_the_service(self):
        self.logs.append_log("INFO", "File discovered", {"name": "测试.py"})
        reopened = LogsService(self.path)
        entry = reopened.list_logs()["items"][0]
        self.assertIn("测试.py", entry["message"])
        self.assertIn("+00:00", entry["timestamp"])

    def test_multiline_exceptions_stay_with_their_entry(self):
        self.path.write_text(
            "2026-09-01 10:00:00,000 [ERROR] Scan failed.\n"
            "Traceback (most recent call last):\n"
            '  File "scanner.py", line 5, in scan\n'
            "PermissionError: access denied\n"
            "2026-09-01 10:00:01,000 [INFO] Recovery complete.\n",
            encoding="utf-8",
        )
        result = self.logs.list_logs(level="ERROR", search="access denied")
        self.assertEqual(result["count"], 1)
        self.assertIn("Traceback", result["items"][0]["message"])
        self.assertTrue(result["items"][0]["message"].endswith("PermissionError: access denied"))
        self.assertNotIn("Recovery", result["items"][0]["message"])

    def test_reverse_reader_handles_unicode_crlf_and_chunk_boundaries(self):
        message = "汉字" * 5000
        self.path.write_bytes(
            ("2026-09-01 10:00:00,000 [INFO] " + message + "\r\n"
             "2026-09-01 10:00:01,000 [INFO] Last entry without newline").encode("utf-8")
        )
        entries = self.logs.list_logs()["items"]
        self.assertEqual(entries[0]["message"], "Last entry without newline")
        self.assertEqual(entries[1]["message"], message)

    def test_limit_does_not_read_the_entire_large_history(self):
        data = ("2026-09-01 10:00:00,000 [INFO] Older entry\n" * 10000).encode()
        class CountedStream(io.BytesIO):
            bytes_read = 0
            def read(self, size=-1):
                chunk = super().read(size)
                self.bytes_read += len(chunk)
                return chunk
        stream = CountedStream(data)
        with patch.object(Path, "open", return_value=stream):
            result = self.logs.list_logs(limit=1)
        self.assertEqual(result["count"], 1)
        self.assertLess(stream.bytes_read, len(data) // 10)

    def test_missing_file_or_no_matches_is_an_empty_list(self):
        self.assertEqual(self.logs.list_logs()["items"], [])
        self.logs.append_log("INFO", "Ready")
        self.assertEqual(self.logs.list_logs(level="ERROR")["count"], 0)

    def test_invalid_service_arguments_are_rejected(self):
        for limit in (0, -1, 1001, True, "20"):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                self.logs.list_logs(limit=limit)
        with self.assertRaises(ValueError):
            self.logs.list_logs(level="unknown")

    def test_concurrent_writes_produce_complete_readable_records(self):
        def append(index):
            self.logs.append_log("INFO", f"File {index} discovered: 测试.py")
        with ThreadPoolExecutor(max_workers=4) as executor:
            list(executor.map(append, range(50)))
        entries = self.logs.list_logs()["items"]
        self.assertEqual(len(entries), 50)
        self.assertEqual(len({entry["message"] for entry in entries}), 50)


class ProjectLogCaptureTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "service.log"
        self.project_logger = logging.getLogger("code-steward")
        previous = (self.project_logger.handlers[:], self.project_logger.level, self.project_logger.propagate)
        self.project_logger.handlers = []
        def restore():
            for handler in self.project_logger.handlers:
                handler.close()
            self.project_logger.handlers, self.project_logger.level, self.project_logger.propagate = previous
        self.addCleanup(restore)
        with patch("sys.stderr", io.StringIO()):
            configure_logging(self.path)
        self.logs = LogsService(self.path)

    def test_reconfiguration_does_not_duplicate_project_events(self):
        configure_logging(self.path)
        logging.getLogger("code-steward.scanner").info("Discovery captured")
        entries = self.logs.list_logs()
        self.assertEqual(entries["count"], 1)
        self.assertEqual(entries["items"][0]["source"], "code-steward.scanner")

    def test_paused_and_busy_scan_rejections_are_visible_in_logs(self):
        state = SimpleNamespace(value="paused")
        scanner = ScanService(None, None, code_steward_service=SimpleNamespace(state=state))
        scanner.enqueue_manual_scan()
        state.value = "running"
        scanner._is_scanning = True
        scanner.enqueue_manual_scan()
        result = self.logs.list_logs(level="WARNING", search="Scan blocked")
        self.assertEqual(result["count"], 2)
        self.assertIn("already in progress", result["items"][0]["message"])
        self.assertIn("paused", result["items"][1]["message"])

    def test_invalid_configuration_is_logged_and_preserves_previous_values(self):
        config = Path(self.temporary.name) / "config.yaml"
        config.write_text("scanning:\n  root_paths: []\n", encoding="utf-8")
        with patch("Services.ConfigService.CONFIG_PATH", str(config)):
            service = ConfigService()
            previous = service.get_config()
            config.write_text("scanning: [broken", encoding="utf-8")
            with self.assertRaises(Exception):
                service.reload()
            self.assertEqual(service.get_config(), previous)
        self.assertEqual(self.logs.list_logs(level="ERROR", search="Configuration load failed")["count"], 1)

    def test_non_mapping_configuration_is_rejected_and_logged(self):
        config = Path(self.temporary.name) / "config.yaml"
        for contents in ("[]", "false", "0", "hello"):
            config.write_text(contents, encoding="utf-8")
            with self.subTest(contents=contents), patch("Services.ConfigService.CONFIG_PATH", str(config)):
                with self.assertRaises(ValueError):
                    ConfigService()
        self.assertEqual(self.logs.list_logs(level="ERROR")["count"], 4)


if __name__ == "__main__":
    unittest.main()
