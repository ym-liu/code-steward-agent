"""Offline checks for personal model choices. No downloads or inference."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ai_lab.cli import guided_args, model_name
from ai_lab.common import SETTINGS, LabError, save_json
from ai_lab.explain.config import load_config, resolve_model


class CustomModelMenuTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = load_config(SETTINGS / "default.json")
        save_json(self.root / "default.json", self.config)
        self.original = (self.root / "default.json").read_bytes()
        self.saved_path = self.root / "models.local.json"

    def choose(self, answers):
        with patch("ai_lab.cli.SETTINGS", self.root), patch("builtins.input", side_effect=answers) as question, \
             patch("ai_lab.cli.OllamaClient") as client, redirect_stdout(io.StringIO()) as output:
            arguments = guided_args()
        client.assert_not_called()
        self.assertEqual((self.root / "default.json").read_bytes(), self.original)
        return arguments, output.getvalue(), question.call_count

    def test_copied_names_commands_and_model_page_links_resolve_to_the_same_name(self):
        expected = "example-model:3b"
        for value in (expected, f"ollama run {expected}", f'ollama pull "{expected}"',
                      "https://ollama.com/library/example-model%3A3b?tab=readme"):
            with self.subTest(value=value):
                self.assertEqual(model_name(value), expected)
        self.assertEqual(model_name("https://ollama.com/author/example-model:3b"), "author/example-model:3b")

    def test_invalid_commands_urls_and_cloud_names_are_rejected_as_text(self):
        for value in ("", "--config", "ollama run example-model:3b --verbose", "ollama run example-model:3b && touch marker",
                      "https://other.example/model", "https://user:secret@ollama.com/library/model",
                      "https://ollama.com/library/", "https://ollama.com/library/example-model/tags",
                      "https://[invalid", 'ollama run "unfinished', "remote-model:cloud"):
            with self.subTest(value=value), self.assertRaises(LabError):
                model_name(value)

    def test_saved_model_reappears_and_uses_the_same_fixed_folder_workflow(self):
        arguments, output, count = self.choose(["c", "ollama run example-model:3b", ""])
        self.assertEqual(count, 3)
        self.assertEqual(arguments[arguments.index("--model") + 1], "example-model:3b")
        for option in ("--pull", "--simple-output", "--open-report"):
            self.assertIn(option, arguments)
        self.assertNotIn("--input", arguments)
        self.assertNotIn("--suite", arguments)
        self.assertIn("Saved to the menu", output)
        self.assertEqual(json.loads(self.saved_path.read_text()), {"models": ["example-model:3b"]})
        next_choice = len(self.config["models"]) + 1
        arguments, output, count = self.choose([str(next_choice)])
        self.assertEqual(count, 1)
        self.assertIn(f"{next_choice}. example-model:3b", output)
        self.assertEqual(arguments[arguments.index("--model") + 1], "example-model:3b")

    def test_one_time_choice_and_back_do_not_write_preferences(self):
        arguments, _, _ = self.choose(["c", "example-model:3b", "n"])
        self.assertEqual(arguments[arguments.index("--model") + 1], "example-model:3b")
        self.assertFalse(self.saved_path.exists())
        arguments, _, count = self.choose(["c", "0", "2"])
        self.assertEqual(count, 3)
        self.assertEqual(arguments[arguments.index("--model") + 1], "qwen-9b")
        self.assertFalse(self.saved_path.exists())

    def test_preset_or_saved_model_does_not_create_duplicate_menu_entries(self):
        first = next(iter(self.config["models"]))
        arguments, _, count = self.choose(["c", first])
        self.assertEqual(count, 2)
        self.assertEqual(arguments[arguments.index("--model") + 1], resolve_model(self.config, first))
        self.assertFalse(self.saved_path.exists())
        save_json(self.saved_path, {"models": ["example-model"]})
        arguments, _, count = self.choose(["c", "example-model:latest"])
        self.assertEqual(count, 2)
        self.assertEqual(arguments[arguments.index("--model") + 1], "example-model:latest")
        self.assertEqual(json.loads(self.saved_path.read_text()), {"models": ["example-model"]})

    def test_invalid_input_can_be_corrected_and_invalid_save_answer_is_reprompted(self):
        arguments, output, count = self.choose(["c", "not a model", "example-model:3b", "maybe", "yes"])
        self.assertEqual(count, 5)
        self.assertIn("Enter Y or N", output)
        self.assertEqual(arguments[arguments.index("--model") + 1], "example-model:3b")

    def test_corrupt_preferences_leave_presets_available_and_are_not_overwritten(self):
        self.saved_path.write_text("broken JSON")
        arguments, output, _ = self.choose(["1"])
        self.assertEqual(arguments[arguments.index("--model") + 1], "qwen-4b")
        self.assertIn("preset models are still available", output)
        arguments, _, _ = self.choose(["c", "example-model:3b"])
        self.assertEqual(arguments[arguments.index("--model") + 1], "example-model:3b")
        self.assertEqual(self.saved_path.read_text(), "broken JSON")

    def test_write_failure_keeps_the_one_time_model_choice(self):
        with patch("ai_lab.cli.save_json", side_effect=PermissionError("read only")):
            arguments, output, _ = self.choose(["c", "example-model:3b", "y"])
        self.assertEqual(arguments[arguments.index("--model") + 1], "example-model:3b")
        self.assertIn("It will still run this time", output)


if __name__ == "__main__":
    unittest.main()
