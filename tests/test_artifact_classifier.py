import os
import tempfile
import unittest
from unittest.mock import patch

from Services.ArtifactsService import ArtifactsService
from Services.ScanService import ScanService


class FakeConfigService:
    def __init__(self, root_path):
        self.config = {
            "scanning": {
                "root_paths": [root_path],
                "include_extensions": [".py"],
                "exclude_folders": [],
                "max_file_size_bytes": 1024 * 1024,
            }
        }

    def get(self, *keys, default=None):
        value = self.config
        for key in keys:
            value = value.get(key, default)
        return value


class ArtifactClassifierTests(unittest.TestCase):
    def test_create_modify_and_move_keep_one_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            checkpoint = os.path.join(directory, "checkpoint.json")
            artifacts = ArtifactsService(os.path.join(directory, "artifacts.sqlite3"))
            scanner = ScanService(
                config_service=FakeConfigService(directory),
                logs_service=None,
                artifacts_service=artifacts,
            )
            original_path = os.path.join(directory, "hello.py")
            moved_path = os.path.join(directory, "renamed.py")

            with patch("Services.ScanService.CHECKPOINT_FILE", checkpoint):
                with open(original_path, "w") as file:
                    file.write("print('hello')\n")
                created = scanner.enqueue_manual_scan(events=[{
                    "event_type": "created",
                    "source_path": original_path,
                    "destination_path": None,
                }])["results"][0]

                with open(original_path, "w") as file:
                    file.write("print('changed')\n")
                modified = scanner.enqueue_manual_scan(events=[{
                    "event_type": "modified",
                    "source_path": original_path,
                    "destination_path": None,
                }])["results"][0]

                os.rename(original_path, moved_path)
                moved = scanner.enqueue_manual_scan(events=[{
                    "event_type": "moved",
                    "source_path": original_path,
                    "destination_path": moved_path,
                }])["results"][0]

            self.assertEqual(created["change_type"], "created")
            self.assertEqual(created["type"], "Python source file")
            self.assertEqual(modified["change_type"], "modified")
            self.assertNotEqual(created["hash"], modified["hash"])
            self.assertEqual(moved["change_type"], "moved")
            self.assertEqual(moved["previous_path"], original_path)
            self.assertEqual(created["id"], modified["id"])
            self.assertEqual(created["id"], moved["id"])
            self.assertEqual(artifacts.list_artifacts()["count"], 1)
            self.assertEqual(artifacts.list_artifacts()["items"][0]["path"], moved_path)


if __name__ == "__main__":
    unittest.main()
