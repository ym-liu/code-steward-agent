import hashlib
import tempfile
import unittest
from pathlib import Path

from ai.collection_reuse import collection_reuse, direct_imports


class DirectImportReuseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.plan = {"files": [], "units": [], "actions": []}

    def add(self, path, code):
        file = self.root / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(code.encode())
        record = {"path": path, "root_folder": str(self.root), "sha256": hashlib.sha256(code.encode()).hexdigest(),
                  "type": "script", "language": "Python", "role": "module", "summary": path,
                  "explained": True, "sensitive": False, "local_dependencies": [], "dependencies": []}
        self.plan["files"].append(record)
        self.plan["units"].append({"id": path, "kind": "single", "root": str(Path(path).parent),
            "root_folder": str(self.root), "folder": "ml/" + path.replace("/", "-"),
            "topic": "ml", "members": [path]})
        return record

    def imports(self):
        return direct_imports({(r["root_folder"], r["path"]): r for r in self.plan["files"]})

    def test_flattened_loader_copied_to_both_importers_without_ai(self):
        self.add("files/dataloader.py", "class DataLoader: pass\ndef train_val_split(): pass\n")
        self.add("files/train.py", "from shared.dataloader import DataLoader, train_val_split\n")
        self.add("files/mnt/sales/train.py", "from shared.dataloader import DataLoader, train_val_split\n")
        report = collection_reuse(self.plan, {"topics": [{"id": "ml", "description": "ML"}]}, None, "none")
        self.assertEqual(report["state"], "complete", report)
        self.assertEqual(report["model_calls"], 0)
        self.assertEqual(report["assessed_pairs"], 2)
        self.assertEqual({s["target"] for s in report["suggestions"]}, {"files/train.py", "files/mnt/sales/train.py"})
        self.assertTrue(all(s["evidence_type"] == "direct-import" for s in report["suggestions"]))
        self.assertEqual(len([a for a in self.plan["actions"] if a["action"] == "copy"]), 2)

    def test_ambiguous_flattened_modules_and_non_import_text_are_not_evidence(self):
        self.add("one/dataloader.py", "class DataLoader: pass\n")
        self.add("two/dataloader.py", "class DataLoader: pass\n")
        self.add("train.py", "from shared.dataloader import DataLoader\n")
        self.add("notes.py", '\"\"\"from shared.dataloader import DataLoader\"\"\"\n')
        self.assertEqual(dict(self.imports()), {})

    def test_symbols_disambiguate_but_changed_files_are_not_evidence(self):
        loader = self.add("one/model.py", "class Classifier: pass\n")
        self.add("two/model.py", "class Regressor: pass\n")
        self.add("train.py", "from sentiment.model import Classifier\n")
        self.assertEqual(set(self.imports()), {(str(self.root), loader["path"])})
        (self.root / "train.py").write_text("from sentiment.model import Regressor\n")
        self.assertEqual(dict(self.imports()), {})


if __name__ == "__main__":
    unittest.main()
