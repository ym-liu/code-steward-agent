import unittest

from ai.imports import direct_imports
from ai.organize import build_units


class ImportGroupingTests(unittest.TestCase):
    def group(self, sources, tools):
        records = [{"path": p, "type": "script", "role": "tool" if p in tools else "module",
                    "duplicate_of": None, "sensitive": False} for p in sources]
        links = {p: [] for p in sources}
        for (_, target), evidence in direct_imports({("", r["path"]): r for r in records}, sources).items():
            for item in evidence:
                links[item["importer"][1]].append(target)
        units, loose = build_units(records, links, set())
        self.assertEqual(loose, [])
        self.assertEqual(sorted(p for u in units for p in u["members"]), sorted(sources))
        return {frozenset(u["members"]) for u in units}

    def test_two_programs_and_shared_utilities_despite_flattened_paths(self):
        common = "from shared.dataloader import DataLoader\nfrom shared.metrics import score\nfrom shared.utils import seed\n"
        sources = {
            "files/train.py": common + "from sentiment.model import Classifier\nfrom sentiment.tokenizer import tokenize\n",
            "files/model.py": "class Classifier: pass\n",
            "files/tokenizer.py": "def tokenize(): pass\n",
            "files/mnt/sales/train.py": common + "from sales.model import Regressor\nfrom sales.windowing import windows\n",
            "files/mnt/sales/model.py": "class Regressor: pass\n",
            "files/windowing.py": "def windows(): pass\n",
            "files/dataloader.py": "class DataLoader: pass\n",
            "files/metrics.py": "def score(): pass\n",
            "files/utils.py": "def seed(): pass\n"}
        self.assertEqual(self.group(sources, {"files/train.py", "files/mnt/sales/train.py"}), {
            frozenset({"files/train.py", "files/model.py", "files/tokenizer.py"}),
            frozenset({"files/mnt/sales/train.py", "files/mnt/sales/model.py", "files/windowing.py"}),
            frozenset({"files/dataloader.py", "files/metrics.py", "files/utils.py"})})

    def test_transitive_helpers_follow_program_and_unrelated_file_stays_separate(self):
        sources = {"run.py": "import helper\n", "helper.py": "import nested\n", "nested.py": "import helper\n",
                   "unrelated.py": "print('hello')\n"}
        self.assertEqual(self.group(sources, {"run.py", "unrelated.py"}), {
            frozenset({"run.py", "helper.py", "nested.py"}), frozenset({"unrelated.py"})})

    def test_existing_marked_projects_remain_intact(self):
        records = [{"path": p, "type": "documentation" if p.endswith('.md') else "script",
                    "role": "tool", "duplicate_of": None} for p in ["repo/README.md", "repo/a.py", "repo/b.py"]]
        units, loose = build_units(records, {}, set())
        self.assertEqual(len(units), 1)
        self.assertEqual(len(units[0]["members"]), 3)
        self.assertEqual(loose, [])


if __name__ == "__main__":
    unittest.main()
