"""The organization plan in the product: groups, topics, proposed folders and saved topic answers.

The AI lab's fake Ollama server answers; no real model is used. The plan is a dry run: every test
also checks that the scanned folders are left exactly as they were.
"""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest

from Services.AnalysisService import AnalysisService
from Services.ArtifactsService import ArtifactsService
from Services.OrganizationService import OrganizationService
from Services.ScanService import ScanService
from tests.test_analysis import FakeOllama, Settings

LAB_FILES = {
    "lab/train.py": "from utils.metrics import accuracy\nimport torch\n\nif __name__ == '__main__':\n    print(accuracy([1], [1]))\n",
    "lab/utils/metrics.py": "def accuracy(truth, preds):\n    return sum(t == p for t, p in zip(truth, preds)) / len(truth)\n",
    "lab/run.sh": "#!/bin/sh\npython train.py\n",
    "lab/.env": "API_KEY=replace-me\n",
    "misc/tool.py": "import csv, sys\nfor row in csv.reader(open(sys.argv[1])):\n    print(row)\n",
    "misc/tool copy.py": "import csv, sys\nfor row in csv.reader(open(sys.argv[1])):\n    print(row)\n",
    "misc/notes.md": "# Notes\n",
}


def topic(name, topic_id, reason):
    return json.dumps({"reason": reason, "topic": topic_id, "folder_name": name})


class OrganizationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.roots = [self.base / "scripts"]
        self.ollama = FakeOllama(self.addCleanup)
        lab_settings = json.loads((Path(__file__).resolve().parents[1] / "ai_lab/settings/default.json").read_text())
        lab_settings.update(base_url=self.ollama.url, models={"fixture": "fixture:latest"}, default_model="fixture")
        self.lab_file = self.base / "lab-settings.json"
        self.lab_file.write_text(json.dumps(lab_settings), encoding="utf-8")
        self.write_files(self.roots[0], LAB_FILES)

    def write_files(self, root, files):
        for name, text in files.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")

    def services(self, explain=True):
        settings = Settings(scanning={"root_paths": [str(root) for root in self.roots]},
                            ai={"model": "fixture", "settings_file": str(self.lab_file)})
        artifacts = ArtifactsService(str(self.base / "artifacts.sqlite3"))
        scanner = ScanService(settings, None, artifacts)
        analysis = AnalysisService(settings, artifacts)
        scanner.listeners.append(analysis)
        scanner.process(full_scan="startup-scan")
        while explain and analysis.step():
            pass
        return analysis, OrganizationService(settings, artifacts, analysis, scanner)

    def snapshot(self):
        return sorted((str(path), path.stat().st_mtime_ns) for root in self.roots for path in root.rglob("*"))

    def test_files_that_belong_together_get_one_topic_and_folder_and_nothing_moves(self):
        _, organization = self.services()
        before = self.snapshot()
        self.ollama.state["outputs"] = [topic("model-trainer", "machine-learning", "Trains a model."),
                                        topic("csv-printer", "data-processing", "Prints CSV rows.")]
        plan = organization.build()
        self.assertEqual(self.snapshot(), before, "a plan never touches the files")
        self.assertEqual(plan["topics_from"], "AI, checked by keyword hints")
        groups = {unit["folder"]: sorted(unit["members"]) for unit in plan["units"] if not unit["duplicate_of"]}
        self.assertEqual(groups, {
            "machine-learning/lab": ["lab/.env", "lab/run.sh", "lab/train.py", "lab/utils/metrics.py"],
            "data-processing/csv-printer": ["misc/tool.py"]})
        actions = {(action["action"], action.get("from") or action.get("path") or "", action.get("to", ""))
                   for action in plan["actions"]}
        self.assertIn(("keep-out-of-git", "machine-learning/lab/.env", ""), actions)
        self.assertIn(("skip", "misc/tool copy.py", ""), actions)
        self.assertIn(("copy", "misc/notes.md", "docs/notes.md"), actions)  # "misc" is too generic to keep
        self.assertEqual(sum(action["action"] == "write" for action in plan["actions"]), 2)
        self.assertEqual(plan["summary"]["code_files_not_explained_yet"], 0)
        self.assertEqual(organization.latest()["created_at"], plan["created_at"])
        self.assertIn("Nothing was copied, moved or deleted", organization.page())

    def test_a_second_plan_reuses_saved_topics_for_unchanged_groups(self):
        _, organization = self.services()
        self.ollama.state["outputs"] = [topic("model-trainer", "machine-learning", "Trains a model."),
                                        topic("csv-printer", "data-processing", "Prints CSV rows.")]
        organization.build()
        calls = self.ollama.state["chat_count"]
        organization.build()
        self.assertEqual(self.ollama.state["chat_count"], calls)

    def test_without_ollama_the_keyword_hints_choose_the_topics(self):
        analysis, organization = self.services()
        settings = json.loads(self.lab_file.read_text())
        settings["base_url"] = "http://127.0.0.1:9"  # nothing listens there
        self.lab_file.write_text(json.dumps(settings), encoding="utf-8")
        analysis.reload_settings()
        with self.assertLogs("code-steward.organization", level="WARNING"):
            plan = organization.build()
        self.assertEqual(plan["topics_from"], "keyword hints only (Ollama is not running; open Ollama)")
        for unit in plan["units"]:
            if not unit["duplicate_of"]:
                self.assertIsNone(unit["ai_topic"])
                self.assertEqual(unit["topic"], unit["rule_topic"] or "other")

    def test_several_roots_share_one_plan_without_name_clashes(self):
        self.roots.append(self.base / "more-scripts")
        self.write_files(self.roots[1], {"misc/notes.md": "# Other notes\n", "misc/tool.py": "print('another tool')\n"})
        _, organization = self.services(explain=False)
        plan = organization.build(use_ai=False)
        targets = [action["to"] for action in plan["actions"] if "to" in action]
        self.assertEqual(len(targets), len(set(targets)))
        self.assertEqual({record["root_folder"] for record in plan["files"]}, {str(root) for root in self.roots})
        copies = {os.path.relpath(action["from_path"], self.base): action["to"] for action in plan["actions"]
                  if action["action"] == "copy" and "notes.md" in action["from"]}
        self.assertEqual(sorted(copies.values()), ["docs/notes-2.md", "docs/notes.md"])

    def test_a_cloud_backed_model_is_never_asked_for_topics(self):
        _, organization = self.services()
        self.ollama.state["remote_host"] = "https://ollama.com"
        chats = self.ollama.state["chat_count"]
        with self.assertLogs("code-steward.organization", level="WARNING"):
            plan = organization.build()
        self.assertEqual(plan["topics_from"], "keyword hints only (the model runs in the cloud; choose a local model)")
        self.assertEqual(self.ollama.state["chat_count"], chats)

    def test_a_model_that_stops_answering_does_not_stop_the_plan(self):
        _, organization = self.services()
        self.ollama.state["chat_error"] = True
        chats = self.ollama.state["chat_count"]
        plan = organization.build()
        groups = [unit for unit in plan["units"] if not unit["duplicate_of"]]
        self.assertEqual(self.ollama.state["chat_count"], chats + 1, "no more requests after the first failure")
        self.assertEqual([unit["ai_topic"] for unit in groups], [None] * len(groups))
        self.assertEqual(plan["topics_from"],
                         f"AI, checked by keyword hints; keyword hints for {len(groups)} group(s) without an AI answer")

    def test_saved_topics_belong_to_one_model_version(self):
        _, organization = self.services()
        answers = [topic("model-trainer", "machine-learning", "Trains a model."),
                   topic("csv-printer", "data-processing", "Prints CSV rows.")]
        self.ollama.state["outputs"] = list(answers)
        organization.build()
        chats = self.ollama.state["chat_count"]
        self.ollama.state.update(outputs=list(answers), digests={"fixture:latest": "sha256:newer"})
        organization.build()
        self.assertEqual(self.ollama.state["chat_count"], chats + 2)

    def test_a_file_edited_after_the_scan_is_planned_as_it_is_now(self):
        _, organization = self.services(explain=False)
        edited = self.roots[0] / "misc" / "tool copy.py"  # an exact copy of misc/tool.py when it was scanned
        edited.write_text("print('a different tool now')\n", encoding="utf-8")
        plan = organization.build(use_ai=False)
        self.assertNotIn("skip", {action["action"] for action in plan["actions"]})
        now = hashlib.sha256(edited.read_bytes()).hexdigest()
        self.assertEqual(next(r["sha256"] for r in plan["files"] if r["path"] == "misc/tool copy.py"), now)
        stored = next(a for a in organization.artifacts_service.list_artifacts()["items"] if a["path"] == str(edited))
        self.assertEqual(stored["hash"], now, "the changed file is scanned again before planning")

    def test_every_action_has_its_own_target_even_next_to_an_existing_notes_file(self):
        self.write_files(self.roots[0], {"project/README.md": "# Project\n",
                                         "project/CODE_STEWARD_NOTES.md": "# Notes written by hand\n",
                                         "project/main.py": "print('hello world')\n"})
        _, organization = self.services(explain=False)
        plan = organization.build(use_ai=False)
        targets = [action["to"].lower() for action in plan["actions"] if "to" in action]
        self.assertEqual(len(targets), len(set(targets)))
        written = [action["to"] for action in plan["actions"] if action["action"] == "write"]
        self.assertTrue(any(target.endswith("/project/CODE_STEWARD_NOTES-2.md") for target in written), written)

    def test_code_files_not_explained_yet_are_counted_in_the_summary(self):
        _, organization = self.services(explain=False)
        plan = organization.build(use_ai=False)
        self.assertEqual(plan["summary"]["code_files_not_explained_yet"], 5)
        self.assertEqual(plan["topics_from"], "keyword hints only (AI not requested)")


if __name__ == "__main__":
    unittest.main()
