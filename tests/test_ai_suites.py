"""Corpus integrity, answer isolation, input boundaries and end-to-end selection."""
import ast
from contextlib import redirect_stdout, redirect_stderr
import copy
import csv
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ai_lab.cli import guided_args, main
from ai_lab.core import LabError, load_config, messages_for, read_json
from ai_lab.reporting import regenerate, save_json
from ai_lab.suites import DATASETS, check_inputs, load_suite
from tests import test_ai_lab as fixtures


class DatasetTests(unittest.TestCase):
    def setUp(self):
        self.config = load_config()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_semantic_cases_fit_default_budget_and_cover_eight_languages(self):
        cases = load_suite("dev", self.config) + load_suite("holdout", self.config)
        self.assertEqual(len(cases), 36)
        self.assertTrue(all("input_error" not in case for case in cases))
        self.assertEqual({Path(c["name"]).suffix for c in cases},
                         {".py", ".sh", ".bat", ".ps1", ".js", ".ts", ".rb", ".pl", ".txt"})
        self.assertTrue(all(c["rubric"]["must_not_claim"] and c["evidence_lines"] for c in cases))

    def test_case_index_and_manifests_account_for_every_fixture(self):
        index = (DATASETS / "CASE_INDEX.md").read_text(encoding="utf-8")
        identifiers = []
        for suite, count in (("dev", 24), ("holdout", 12), ("robustness", 6)):
            manifest = read_json(DATASETS / suite / "manifest.json")
            self.assertEqual(len(manifest["cases"]), count)
            listed = {str(Path(c["file"])) for c in manifest["cases"]}
            actual = {str(p.relative_to(DATASETS / suite)) for p in (DATASETS / suite / "cases").iterdir() if p.is_file()}
            self.assertEqual(listed, actual)
            for case in manifest["cases"]:
                identifiers.append(case["id"])
                self.assertIn(f"| {case['id']} |", index)
        self.assertEqual(len(identifiers), len(set(identifiers)))

    def test_holdout_has_no_duplicate_sources_or_task_families_from_dev(self):
        dev = load_suite("dev", self.config)
        holdout = load_suite("holdout", self.config)
        self.assertFalse({c["family"] for c in dev} & {c["family"] for c in holdout})
        all_sources = [c["source"] for c in dev + holdout]
        self.assertEqual(len(all_sources), len(set(all_sources)))

    def test_only_the_deliberate_syntax_error_python_case_fails_parsing(self):
        invalid = []
        for suite in ("dev", "holdout"):
            for case in load_suite(suite, self.config):
                if case["name"].endswith(".py"):
                    try:
                        ast.parse(case["source"])
                    except SyntaxError:
                        invalid.append(case["case_id"])
        self.assertEqual(invalid, ["D19"])

    def test_dataset_answers_labels_and_difficulty_are_not_sent_to_model(self):
        for case in load_suite("dev", self.config) + load_suite("holdout", self.config):
            case["rubric"] = {"answer": "PRIVATE_REFERENCE_SENTINEL"}
            case["category"] = "PRIVATE_CATEGORY_SENTINEL"
            messages = messages_for(case)
            self.assertNotIn("PRIVATE_", json.dumps(messages))
            payload = json.loads(messages[1]["content"])
            self.assertEqual(set(payload), {"file", "facts", "numbered_source"})

    def test_robustness_checks_all_match_expected_reasons_without_any_ollama(self):
        with patch("ai_lab.cli.OllamaClient", side_effect=AssertionError("No model client allowed")), redirect_stdout(io.StringIO()):
            code = main(["check-inputs", "--output", str(self.root)])
        self.assertEqual(code, 0)
        folder = next(self.root.iterdir())
        result = read_json(folder / "input_checks.json")
        self.assertEqual(result["mode"], "input_validation_only")
        self.assertEqual(len(result["cases"]), 6)
        self.assertTrue(all(row["passed"] for row in result["cases"]))
        self.assertFalse((folder / "review.csv").exists())

    def test_larger_budget_exposes_unexpected_acceptance_instead_of_false_pass(self):
        self.config["max_source_chars"] = 10000
        with redirect_stdout(io.StringIO()):
            code = check_inputs(self.config, self.root)
        self.assertEqual(code, 1)
        rows = read_json(next(self.root.iterdir()) / "input_checks.json")["cases"]
        self.assertEqual([r["case_id"] for r in rows if not r["passed"]], ["R03"])
        self.assertEqual(next(r for r in rows if r["case_id"] == "R03")["actual"], "accepted")

    def test_manifest_cannot_escape_suite_or_repeat_an_identifier(self):
        manifest = read_json(DATASETS / "dev" / "manifest.json")
        for change, message in ((lambda m: m["cases"][0].update(file="../outside.py"), "inside"),
                                (lambda m: m["cases"][1].update(id=m["cases"][0]["id"]), "Duplicate"),
                                (lambda m: m["cases"][0].update(evidence_lines=[10000]), "outside")):
            broken = copy.deepcopy(manifest)
            change(broken)
            with patch("ai_lab.suites.read_json", return_value=broken), self.assertRaisesRegex(LabError, message):
                load_suite("dev", self.config)

    def test_custom_input_and_named_suite_cannot_be_accidentally_mixed(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            main(["run", "--input", "somewhere.py", "--suite", "dev"])
        self.assertEqual(error.exception.code, 2)

    def test_guided_menu_can_select_development_set(self):
        with patch("builtins.input", side_effect=["a", "2"]), redirect_stdout(io.StringIO()):
            args = guided_args()
        self.assertEqual(args, ["compare", "--pull", "--suite", "dev"])

    def test_named_suite_demo_keeps_reference_checklists_and_category_report(self):
        with redirect_stdout(io.StringIO()):
            code = main(["demo", "--suite", "dev", "--output", str(self.root)])
        self.assertEqual(code, 0)
        folder = next(self.root.iterdir())
        rows = [json.loads(line) for line in (folder / "results.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(rows), 24)
        self.assertTrue(all(row["suite"] == "dev" and row["case_id"] for row in rows))
        self.assertTrue(all(row["wall_ms"] is None for row in rows))
        summary = (folder / "summary.md").read_text(encoding="utf-8")
        self.assertIn("DEMO ONLY", summary)
        self.assertIn("By category", summary)
        self.assertIn("must_not_claim", summary)


class SuiteHttpTests(unittest.TestCase):
    def setUp(self):
        # Reuse the isolated HTTP fixture, without inheriting or rediscovering its test methods.
        fixtures.HttpWorkflowTests.setUp(self)

    def test_cli_compares_same_dev_cases_and_retains_human_scores_by_category(self):
        config_path = self.root / "config.json"
        save_json(config_path, self.config)
        output = self.root / "comparison"
        with redirect_stdout(io.StringIO()):
            code = main(["compare", "--config", str(config_path), "--suite", "dev", "--pull", "--output", str(output)])
        self.assertEqual(code, 0)
        self.assertEqual(self.server.state["chat_count"], 48)
        requests = [b for path, b in self.server.state["requests"] if path == "/api/chat"]
        self.assertEqual([r["messages"] for r in requests[:24]], [r["messages"] for r in requests[24:]])
        folder = next(output.iterdir())
        with (folder / "review.csv").open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            fields, rows = reader.fieldnames, list(reader)
        self.assertEqual(len(rows), 48)
        self.assertEqual(rows[0]["case_id"], "D01")
        for field in fields:
            if field.endswith("_score"):
                rows[0][field] = "2"
        review = folder / "review.csv"
        with review.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
        before = review.read_bytes()
        regenerate(folder)
        self.assertEqual(review.read_bytes(), before)
        report = (folder / "summary.md").read_text(encoding="utf-8")
        self.assertIn("| fixture:latest | data_processing | 3 | 3 | 10.00 (1) |", report)
        self.assertEqual(read_json(folder / "run.json")["suites"], ["dev"])


if __name__ == "__main__":
    unittest.main()
