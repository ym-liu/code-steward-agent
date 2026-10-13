"""Exercise the one-choice launcher, readable reports and bounded descriptions."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ai_lab.cli import guided_args, guided_device, main
from ai_lab.common import SETTINGS, save_json
from ai_lab.explain.answer import validate_analysis
from ai_lab.explain.config import load_config
from ai_lab.explain.files import collect_cases, snapshot
from ai_lab.testing.report_records import regenerate
from tests import test_ai_lab as fixtures


class DescriptionTests(unittest.TestCase):
    def test_visible_parameters_and_required_flags_are_extracted_as_facts(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tool.py"
            path.write_text('import argparse\np = argparse.ArgumentParser()\np.add_argument("--date", required=True)\ndef total(prices):\n    return sum(prices)\n')
            case = snapshot(path, path.name, 2500)
            self.assertEqual(case["facts"]["definitions"][0]["parameters"], ["prices"])
            self.assertEqual(case["facts"]["argument_declarations"], [{"names": ["--date"], "line": 3, "required_value": True}])

    def test_missing_usage_or_relationships_and_overlong_lists_are_rejected(self):
        for change in (lambda value: value.pop("usage"), lambda value: value.pop("relationships"),
                       lambda value: value.update(inputs=["a", "b", "c", "d"]),
                       lambda value: value.update(purpose="x" * 241)):
            value = fixtures.answer()
            change(value)
            self.assertFalse(validate_analysis(json.dumps(value), 1)[2])

    def test_simple_config_reads_complete_long_source_without_execution(self):
        config = load_config(SETTINGS / "default.json")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker = root / "must-not-be-created"
            source = "# comment\n" * 4000 + f"open({str(marker)!r}, 'w').write('bad')\n"
            path = root / "long.py"
            path.write_text(source)
            case = snapshot(path, path.name, config["max_source_chars"])
            self.assertEqual(case["source"], source)
            self.assertFalse(marker.exists())
            path.write_text(source + "#" * 60000)
            self.assertEqual(snapshot(path, path.name, config["max_source_chars"])["input_error_code"], "input_too_large")

    def test_common_code_formats_are_found_recursively_and_notebooks_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "nested").mkdir()
            for name in ("a.py", "b.cpp", "nested/c.java", "nested/d.go", "ignored.ipynb"):
                (root / name).write_text("example source")
            cases = collect_cases(root, load_config(SETTINGS / "default.json"))
            self.assertEqual([case["name"] for case in cases], ["a.py", "b.cpp", "nested/c.java", "nested/d.go"])


class SimpleWorkflowTests(unittest.TestCase):
    def setUp(self):
        fixtures.HttpWorkflowTests.setUp(self)
        self.samples = self.root / "samples"
        self.samples.mkdir()
        (self.samples / "a.py").write_text("print(1)\n")
        (self.samples / "nested").mkdir()
        (self.samples / "nested/new.js").write_text("console.log(2);\n")
        (self.root / "settings").mkdir()
        save_json(self.root / "settings" / "default.json", self.config)

    def launch(self, answers=("1", "")):
        args = ["--interactive"]
        old = Path.cwd()
        # Output location is fixed by the parser; isolate it without changing cwd.
        with fixtures.lab_folders(self.root), patch("builtins.input", side_effect=list(answers)) as question, \
             patch("ai_lab.cli.webbrowser.open", return_value=True) as browser, redirect_stdout(io.StringIO()) as output:
            code = main(args)
        self.assertEqual(Path.cwd(), old)
        self.assertEqual(question.call_count, len(answers))
        folder = next((self.root / "ai-results").iterdir())
        return code, folder, browser, output.getvalue()

    def test_one_model_choice_tests_every_file_and_opens_a_readable_report(self):
        code, folder, browser, output = self.launch()
        self.assertEqual(code, 0)
        self.assertEqual(self.server.state["chat_count"], 2)
        requests = [body for path, body in self.server.state["requests"] if path == "/api/chat"]
        self.assertEqual([body["messages"][-1]["content"].split("\n")[0] for body in requests], ["FILE: a.py", "FILE: nested/new.js"])
        browser.assert_called_once_with((folder / "report.html").resolve().as_uri())
        report = (folder / "report.html").read_text()
        self.assertIn("2 explanations ready; 0 files not completed", report)
        self.assertIn("Inputs", report)
        self.assertIn("How to use", report)
        self.assertIn("Show code evidence", report)
        self.assertIn("nested/new.js", report)
        self.assertNotIn("schema_valid", report)
        self.assertNotIn("Fill", output)

    def test_invalid_output_does_not_hide_other_files_and_report_text_cannot_execute_html(self):
        value = fixtures.answer()
        value["purpose"] = '<script>alert("bad")</script>'
        self.server.state["outputs"] = ["broken JSON", json.dumps(value)]
        code, folder, browser, _ = self.launch()
        self.assertEqual(code, 1)
        self.assertEqual(self.server.state["chat_count"], 2)
        report = (folder / "report.html").read_text()
        self.assertIn("1 file not completed", report)
        self.assertNotIn('<script>alert', report)
        self.assertIn("&lt;script&gt;", report)
        self.assertIn("Content-Security-Policy", report)
        browser.assert_called_once()

    def test_failure_before_first_file_keeps_an_honest_report_for_all_planned_files(self):
        with patch("ai_lab.explain.ollama_client.OllamaClient.version", side_effect=fixtures.LabError("offline", "connection_error")):
            code, folder, browser, _ = self.launch()
        self.assertEqual(code, 1)
        self.assertEqual(self.server.state["chat_count"], 0)
        report = (folder / "report.html").read_text()
        self.assertIn("0 explanations ready; 2 files not completed", report)
        self.assertIn("a.py", report)
        self.assertIn("nested/new.js", report)
        browser.assert_called_once()

    def test_old_saved_six_field_answers_remain_readable_without_rewriting_scores(self):
        _, folder, _, _ = self.launch()
        rows = [json.loads(line) for line in (folder / "results.jsonl").read_text().splitlines()]
        for row in rows:
            row["analysis"].pop("usage")
            row["analysis"].pop("relationships")
        (folder / "results.jsonl").write_text('\n'.join(json.dumps(row) for row in rows) + '\n')
        review = (folder / "review.csv").read_bytes()
        regenerate(folder)
        self.assertEqual((folder / "review.csv").read_bytes(), review)
        self.assertIn("How to use", (folder / "report.html").read_text())

    def test_invalid_model_choice_can_be_corrected_without_a_test_set_question(self):
        with patch("builtins.input", side_effect=["wrong", "2"]) as question, redirect_stdout(io.StringIO()):
            args = guided_args()
        self.assertEqual(question.call_count, 2)
        self.assertEqual(args[args.index("--model") + 1], "qwen-9b")
        self.assertNotIn("--suite", args)

    def test_cpu_is_the_default_device_and_is_verified(self):
        code, folder, _, output = self.launch()
        self.assertEqual(code, 0)
        options = [body["options"] for path, body in self.server.state["requests"] if path == "/api/chat"]
        self.assertTrue(options and all(item["num_gpu"] == 0 for item in options))
        self.assertEqual(json.loads((folder / "run.json").read_text())["config"]["device"], "cpu")
        self.assertIn("(CPU only, threads: ", output)
        self.assertNotIn("GPU", (folder / "report.html").read_text())

    def test_gpu_choice_allows_gpu_and_marks_the_report_as_a_preview(self):
        code, folder, _, output = self.launch(("1", "2"))
        self.assertEqual(code, 0)
        options = [body["options"] for path, body in self.server.state["requests"] if path == "/api/chat"]
        self.assertTrue(options and all(item["num_gpu"] == -1 for item in options))
        self.assertEqual(json.loads((folder / "run.json").read_text())["config"]["device"], "auto")
        self.assertIn("(GPU allowed)", output)
        self.assertIn("GPU was allowed for this preview run", (folder / "report.html").read_text())
        self.assertIn("GPU was allowed for this preview run", (folder / "report.md").read_text())
        # The shared launcher profile stays CPU-only.
        self.assertEqual(load_config(SETTINGS / "default.json")["device"], "cpu")

    def test_device_question_can_be_corrected_or_quit_without_running(self):
        with patch("builtins.input", side_effect=["fast", "gpu"]) as question, redirect_stdout(io.StringIO()) as output:
            self.assertEqual(guided_device(), "auto")
        self.assertEqual(question.call_count, 2)
        self.assertIn("Enter 1 for CPU, 2 for GPU", output.getvalue())
        with patch("builtins.input", side_effect=["cpu"]), redirect_stdout(io.StringIO()):
            self.assertEqual(guided_device(), "cpu")
        with fixtures.lab_folders(self.root), patch("builtins.input", side_effect=["1", "0"]), \
             redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--interactive"]), 0)
        self.assertEqual(self.server.state["chat_count"], 0)
        self.assertFalse((self.root / "ai-results").exists())


if __name__ == "__main__":
    unittest.main()
