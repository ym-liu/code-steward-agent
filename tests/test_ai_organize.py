"""File types, groups that stay together, topics and the dry-run plan; no real model."""
import json
from pathlib import Path
import tempfile
import unittest

from ai_lab.common import LabError
from ai_lab.explain.files import snapshot
from ai_lab.organize import (INTERPRETER_RUN, load_topics, organize, resolve, score, write_page)


def explained(purpose, usage=(), dependencies=()):
    return {"status": "valid", "check_issues": [], "analysis": {
        "purpose": purpose, "inputs": [], "outputs": [], "usage": list(usage), "dependencies": list(dependencies),
        "relationships": [], "unknowns": [], "evidence": [{"line": 1, "claim": "x"}]}}


class FakeClient:
    """Answers like the model would, so the plan logic can be tested offline."""
    def __init__(self, folder_name="model-trainer"):
        self.calls, self.folder_name = [], folder_name

    def ask(self, tag, messages, schema, num_predict=256):
        self.calls.append(messages)
        text = messages[-1]["content"]
        topic = "machine-learning" if "train" in text else "data-processing" if "CSV" in text else "other"
        answer = {"reason": "<script>alert(1)</script> Trains a model.", "topic": topic, "folder_name": self.folder_name}
        return {"message": {"content": json.dumps(answer)}}


class OrganizeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        files = {
            "proj/README.md": "# Project\n",
            "proj/app.py": "import helper\n\nif __name__ == '__main__':\n    helper.run()\n",
            "proj/helper.py": "def run():\n    print('hi')\n",
            "tools/run.sh": "#!/bin/sh\nset -e\npython train.py --config ${1:-cfg/base.yaml}\npython eval.py out/result.json\n",
            "tools/train.py": "import argparse\nimport torch\n\nif __name__ == '__main__':\n    p = argparse.ArgumentParser()\n"
                              "    p.add_argument('--config', default='cfg/base.yaml')\n    p.parse_args()\n",
            "tools/eval.py": '"""Shared by train.py and eval.py."""\nimport json\nprint(json.load(open("out/result.json")))\n',
            "tools/cfg/base.yaml": "lr: 0.1\n",
            "tools/.env": "API_KEY=replace-me\n",
            "tools/out/result.json": "{\"acc\": 0.9}\n",
            "loose/a.py": "import csv, sys\nfor row in csv.reader(open(sys.argv[1])):\n    print(row)\n",
            "loose/a copy.py": "import csv, sys\nfor row in csv.reader(open(sys.argv[1])):\n    print(row)\n",
            "loose/key.py": 'password = "hunter2hunter"\nprint(len(password))\n',
            "loose/notes.txt": "things i always forget about the server\nrestart the web app after each deploy\nssh box\n",
            "loose/weird.txt": "FRAME Z17\nLOAD $ITEMS\nEMIT SLOT_B\n",
            "loose/urls.txt": "https://example.com/a\nhttps://example.com/b\n",
            "loose/examples/in.csv": "a,b\n1,2\n",
        }
        for name, text in files.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        (self.root / "loose/prog").write_bytes(b"\x7fELF\x02\x01\x01" + b"\x00" * 64)
        self.cases = [snapshot(self.root / name, name, 60000) for name in sorted(files)
                      if Path(name).suffix in {".py", ".sh", ".txt"}]
        self.rows = {
            "tools/run.sh": explained("Runs train.py and then eval.py.", ["Run sh run.sh"]),
            "tools/train.py": explained("Trains a small model with torch.", ["Run python train.py"], ["torch"]),
            "proj/helper.py": explained("Prints a greeting.", ["From other code, call run()"]),
            "loose/a.py": explained("Prints the rows of a CSV file.", ["Run python a.py data.csv"], ["csv"]),
            "loose/notes.txt": explained("Notes about restarting a web server."),
            "loose/weird.txt": explained(None),
        }
        for case in self.cases:
            if case["name"] in self.rows:
                self.rows[case["name"]]["source_hash"] = case["source_hash"]

    def plan(self, client=None):
        before = sorted(p.as_posix() for p in self.root.rglob("*"))
        plan = organize(self.root, self.cases, self.rows, client, "fake" if client else None, load_topics(), progress=lambda *_: None)
        self.assertEqual(sorted(p.as_posix() for p in self.root.rglob("*")), before, "a dry run must not touch the folder")
        return plan

    def test_every_file_gets_a_type_role_and_sensitive_flag(self):
        plan = self.plan()
        files = {r["path"]: r for r in plan["files"]}
        expected = {"tools/run.sh": "wrapper", "tools/train.py": "script", "proj/helper.py": "script",
                    "tools/cfg/base.yaml": "data", "tools/.env": "data", "loose/notes.txt": "documentation",
                    "loose/weird.txt": "unknown", "loose/urls.txt": "data", "loose/examples/in.csv": "sample",
                    "loose/prog": "binary", "proj/README.md": "documentation"}
        self.assertEqual({path: files[path]["type"] for path in expected}, expected)
        self.assertEqual(files["tools/train.py"]["role"], "tool")
        self.assertEqual(files["proj/helper.py"]["role"], "module")
        sensitive = sorted(path for path, record in files.items() if record["sensitive"])
        self.assertEqual(sensitive, ["loose/key.py", "tools/.env", "tools/cfg/base.yaml"])
        self.assertEqual(files["loose/a copy.py"]["duplicate_of"], "loose/a.py")
        self.assertTrue(all(0.05 <= record["confidence"] <= 0.99 for record in files.values()))
        self.assertGreater(files["tools/run.sh"]["confidence"], files["tools/eval.py"]["confidence"])

    def test_files_that_use_each_other_stay_together_and_loose_files_go_by_type(self):
        plan = self.plan()
        units = {u["root"]: u for u in plan["units"] if u["kind"] != "single"}
        self.assertEqual(units["proj"]["kind"], "project")
        self.assertEqual(sorted(units["tools"]["members"]), ["tools/.env", "tools/cfg/base.yaml", "tools/eval.py",
                                                            "tools/out/result.json", "tools/run.sh", "tools/train.py"])
        moves = {a["from"]: a["to"] for a in plan["actions"] if a["action"] == "copy"}
        self.assertTrue(moves["tools/cfg/base.yaml"].endswith("/tools/cfg/base.yaml"))
        self.assertEqual(moves["loose/notes.txt"], "docs/loose/notes.txt")
        self.assertEqual(moves["loose/urls.txt"], "data/loose/urls.txt")
        self.assertEqual(moves["loose/examples/in.csv"], "samples/loose/in.csv")
        self.assertEqual(moves["loose/prog"], "bin/prog")
        self.assertEqual(moves["loose/weird.txt"], "needs-review/weird.txt")
        skipped = [a for a in plan["actions"] if a["action"] == "skip"]
        self.assertEqual([(a["from"], a["same_as"]) for a in skipped], [("loose/a copy.py", "loose/a.py")])
        hidden = {a["path"] for a in plan["actions"] if a["action"] == "keep-out-of-git"}
        self.assertEqual(hidden, {moves["tools/.env"], moves["tools/cfg/base.yaml"], moves["loose/key.py"]})
        handled = sorted(a["from"] for a in plan["actions"] if a["action"] in ("copy", "skip"))
        self.assertEqual(handled, sorted(r["path"] for r in plan["files"]), "every file gets exactly one action")
        targets = [a["to"] for a in plan["actions"] if a["action"] in ("copy", "write")]
        self.assertEqual(len(targets), len(set(targets)))
        self.assertFalse([t for t in targets if t.startswith("/") or ".." in t.split("/")])
        notes = [a["to"] for a in plan["actions"] if a["action"] == "write"]
        self.assertTrue(any(path.endswith("proj/CODE_STEWARD_NOTES.md") for path in notes), "an existing README is never replaced")

    def test_ai_topics_are_checked_against_keywords_and_names_are_validated(self):
        client = FakeClient()
        plan = self.plan(client)
        units = {u["root"]: u for u in plan["units"] if not u.get("duplicate_of")}
        tools = units["tools"]
        self.assertEqual((tools["topic"], tools["ai_topic"], tools["rule_topic"]), ("machine-learning",) * 3)
        self.assertEqual(tools["topic_confidence"], 0.9)
        self.assertTrue(tools["folder"].startswith("machine-learning/tools"))
        single = next(u for u in plan["units"] if u["members"] == ["loose/a.py"])
        self.assertEqual(single["name"], "model-trainer")
        self.assertEqual(len(client.calls), len([u for u in plan["units"] if not u.get("duplicate_of")]))
        self.assertIn("UNIT: tools", client.calls[-1][-1]["content"] + "".join(m[-1]["content"] for m in client.calls))
        for bad in ("final-version-2", "machine-learning", "Not A Name"):
            plan = organize(self.root, self.cases, self.rows, FakeClient(bad), "fake", load_topics(), progress=lambda *_: None)
            single = next(u for u in plan["units"] if u["members"] == ["loose/a.py"])
            self.assertEqual(single["name"], "a")

    def test_without_ai_the_keyword_check_decides_and_unclear_groups_need_review(self):
        plan = self.plan()
        units = {u["root"]: u for u in plan["units"] if u["kind"] != "single"}
        self.assertEqual(units["tools"]["topic"], "machine-learning")
        self.assertEqual(units["tools"]["topic_confidence"], 0.6)
        self.assertTrue(units["proj"]["folder"].startswith("needs-review/"))

    def test_page_escapes_model_text_and_says_nothing_was_moved(self):
        plan = self.plan(FakeClient())
        path = self.root.parent / (self.root.name + "-plan.html")
        self.addCleanup(lambda: path.unlink(missing_ok=True))
        write_page(path, plan, load_topics())
        page = path.read_text(encoding="utf-8")
        self.assertIn("Nothing was copied, moved or deleted", page)
        self.assertNotIn("<script>", page)
        self.assertIn("&lt;script&gt;", page)

    def test_score_counts_only_the_requested_split(self):
        plan = self.plan(FakeClient())
        reference = {"classification": {"files": {
            "tools/run.sh": {"type": "wrapper", "role": ["tool"], "topic": ["machine-learning"]},
            "tools/train.py": {"type": "script", "role": ["tool"], "topic": ["machine-learning"]},
            "loose/a.py": {"type": "script", "role": ["tool"], "topic": ["data-processing"]},
            "tools/.env": {"type": "data", "sensitive": True}}},
            "plan": {"keep_together": [["tools/"]], "keep_apart": ["loose/a.py", "loose/key.py"],
                     "not_copied_twice": [["loose/a.py", "loose/a copy.py"]], "needs_review": ["loose/weird.txt"]}}
        result = score(plan, reference, {"sealed": ["loose/"]})
        self.assertEqual(result["types"], {"correct": 3, "total": 3})
        self.assertEqual(result["kept_together"], {"correct": 1, "total": 1})
        self.assertEqual(result["sensitive"]["found"], 1)
        self.assertEqual(result["topics"]["total"], 2)
        sealed = score(plan, reference, {"sealed": ["loose/"]}, part="sealed")
        self.assertEqual(sealed["types"]["total"], 1)

    def test_paths_written_in_scripts_resolve_to_scanned_files(self):
        files = {"lab/cfg/base.yaml", "lab/scripts/boot.ps1", "lab/scripts/boot.sh"}
        self.assertEqual(resolve("${1:-cfg/base.yaml}", "lab", files), "lab/cfg/base.yaml")
        self.assertEqual(resolve("%~dp0scripts\\boot.ps1", "lab", files), "lab/scripts/boot.ps1")
        self.assertEqual(resolve("$ROOT/scripts/boot.sh", "lab", files), "lab/scripts/boot.sh")
        self.assertEqual(resolve("base.yaml", "lab", files), "lab/cfg/base.yaml")
        self.assertIsNone(resolve("/etc/passwd", "lab", files))
        self.assertIsNone(INTERPRETER_RUN.search('"""Helpers shared by train.py and evaluate.py."""'))
        self.assertIsNotNone(INTERPRETER_RUN.search("python3 prices_to_csv.py < raw.jsonl"))

    def test_topic_list_must_be_valid(self):
        path = self.root / "topics.json"
        path.write_text(json.dumps({"topics": [{"id": "Bad Name", "description": "x"}, {"id": "other", "description": "y"}]}))
        with self.assertRaises(LabError):
            load_topics(path)
        path.write_text(json.dumps({"topics": [{"id": "games", "description": "x"}, {"id": "other", "description": "y"}]}))
        self.assertEqual([t["id"] for t in load_topics(path)["topics"]], ["games", "other"])


if __name__ == "__main__":
    unittest.main()
