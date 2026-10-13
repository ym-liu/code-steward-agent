"""Static facts, program-side claim checks and the single repair request; no real model."""
import json
from pathlib import Path
import tempfile
import unittest

from ai_lab.common import SAMPLES, SETTINGS, LabError, save_json
from ai_lab.explain.checks import check_claims, clean_answer, fill_from_facts
from ai_lab.explain.config import load_config
from ai_lab.explain.files import snapshot
from ai_lab.explain.request import messages_for, source_view
from tests import test_ai_lab as fixtures

LIBRARY = "class Solution:\n    def twoSum(self, nums: List[int], target: int) -> List[int]:\n        return [0, 1]\n"


def answer(**changes):
    value = {"purpose": "Finds two numbers that add up to a target.", "inputs": ["nums", "target"],
             "outputs": ["indices of the two numbers"], "usage": ["Import it and call Solution().twoSum(nums, target)"],
             "dependencies": [], "relationships": [], "unknowns": ["List is used but never imported."],
             "evidence": [{"line": 3, "claim": "Returns the two indices."}]}
    value.update(changes)
    return value


class TokenSavingTests(unittest.TestCase):
    def test_message_leaves_out_blank_lines_and_facts_the_model_already_sees(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "tool.py"
            path.write_text("import os\n\n\ndef run(path):\n    return os.listdir(path)\n", encoding="utf-8")
            case = snapshot(path, "tool.py", 60000)
        content = messages_for(case)[-1]["content"]
        self.assertIn("----- BEGIN SOURCE -----\n1: import os\n4: def run(path):\n5:     return", content)
        self.assertNotIn("\n2: ", content)
        facts_line = content.split("\n")[3]
        self.assertNotIn(", ", facts_line.replace('", "', ""))  # compact separators
        facts = json.loads(facts_line)
        self.assertNotIn("extension", facts)
        self.assertNotIn("line_count", facts)
        self.assertEqual(case["facts"]["line_count"], 5)  # still used by the program checks

    def test_packed_cut_off_or_library_usage_items_are_flagged(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "tool.py"
            path.write_text("import argparse\nimport csv\nparser = argparse.ArgumentParser()\nparser.parse_args()\n"
                            "print(csv.__name__)\n", encoding="utf-8")
            case = snapshot(path, "tool.py", 60000)
        problems = lambda **changes: [i["problem"] for i in check_claims(answer(**{
            "usage": ["Run python tool.py"], "unknowns": [], "evidence": [{"line": 4, "claim": "Parses arguments."}],
            **changes}), case) if not i.get("minor")]
        self.assertEqual(problems(), [])
        self.assertTrue(any("line breaks" in p for p in problems(inputs=["a: first\nb: second"])))
        self.assertFalse(any("line breaks" in p for p in problems(inputs=["a: first\n"])))
        value = answer(inputs=["nums: numbers\n", " target "])
        clean_answer(value)
        self.assertEqual(value["inputs"], ["nums: numbers", "target"])
        self.assertTrue(any("cut off" in p for p in problems(outputs=["x" * 180])))
        self.assertTrue(any("libraries in usage" in p for p in problems(usage=["Run python tool.py; needs argparse and csv"])))
        unknown = snapshot(SAMPLES / "unknown.txt", "unknown.txt", 60000)
        self.assertTrue(any("do not guess" in i["problem"] for i in check_claims(answer(
            purpose="Contains commands for an unknown system.", inputs=[], outputs=[], usage=[]), unknown)))


    def test_filled_usage_names_the_method_the_file_never_calls_itself(self):
        source = ("class TrieNode:\n    def __init__(self):\n        self.children = {}\n\n"
                  "    def addWord(self, word):\n        self.children[word] = True\n\n\n"
                  "class Solution:\n    def findWords(self, board, words):\n        root = TrieNode()\n"
                  "        for w in words:\n            root.addWord(w)\n        return list(root.children)\n")
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "trie.py"
            path.write_text(source, encoding="utf-8")
            case = snapshot(path, "trie.py", 60000)
        value = answer(inputs=[], usage=[])
        fill_from_facts(value, case)
        self.assertEqual(value["usage"], ["From other code, create a Solution object and call findWords(board, words)."])
        self.assertEqual(value["inputs"][0], "board, words (passed to findWords)")


class LongFileTests(unittest.TestCase):
    def long_case(self):
        body = "".join(f"    total = total + {i}  # step {i}\n" for i in range(400))
        source = ("\"\"\"Add numbers from a file.\"\"\"\nimport sys\nimport json\n\n\ndef add_all(path):\n"
                  "    total = 0\n" + body + "    return total\n\n\n"
                  "if __name__ == \"__main__\":\n    data = json.load(open(sys.argv[1]))\n    print(add_all(data))\n")
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "adder.py"
            path.write_text(source, encoding="utf-8")
            return snapshot(path, "adder.py", 60000), source.splitlines()

    def test_short_files_and_disabled_setting_send_every_nonblank_line(self):
        case, lines = self.long_case()
        text, shown, left_out = source_view(case, None)
        self.assertEqual(left_out, 0)
        self.assertEqual(shown, [n for n, line in enumerate(lines, 1) if line.strip()])
        self.assertEqual(source_view(case, {"long_file_tokens": 50000, "long_file_keep_tokens": 2000})[2], 0)

    def test_long_file_keeps_key_lines_within_budget_and_marks_gaps(self):
        case, lines = self.long_case()
        config = {"long_file_tokens": 1500, "long_file_keep_tokens": 600}
        text, shown, left_out = source_view(case, config)
        self.assertGreater(left_out, 300)
        self.assertLessEqual(len(text), 600 * 3 * 1.5)
        kept = {lines[n - 1].strip() for n in shown}
        for key in ("import sys", "import json", "def add_all(path):", "return total", 'if __name__ == "__main__":',
                    "data = json.load(open(sys.argv[1]))"):
            self.assertIn(key, kept)
        self.assertRegex(text, r"\.\.\. \(\d+ lines left out\)")
        self.assertEqual(sorted(shown), shown)
        message = messages_for(case, config)[1]["content"]
        self.assertIn("long file: only key lines are shown", message)
        self.assertNotIn("long file", messages_for(case)[1]["content"])
        bad = dict(load_config(SETTINGS / "default.json"), long_file_tokens=1000, long_file_keep_tokens=1000)
        with tempfile.TemporaryDirectory() as folder, self.assertRaises(LabError):
            path = Path(folder) / "c.json"
            path.write_text(json.dumps(bad), encoding="utf-8")
            load_config(path)


class FactTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def facts(self, name, text):
        path = self.root / name
        path.write_text(text, encoding="utf-8")
        return snapshot(path, name, 60000)["facts"]

    def test_python_entry_points_distinguish_libraries_scripts_and_main_guards(self):
        library = self.facts("library.py", LIBRARY)
        self.assertIsNone(library["entry_point"])
        self.assertEqual(library["names_without_visible_definition"], [{"name": "List", "line": 2}])
        settings = self.facts("settings.py", '"""Settings."""\nimport os\nLIMIT = 3\nNAMES = ["a", "b"]\n')
        self.assertIsNone(settings["entry_point"])
        script = self.facts("script.py", "import sys\nprint(len(sys.argv))\n")
        self.assertEqual(script["entry_point"], {"kind": "top-level code", "line": 2})
        self.assertEqual(script["command_line"], [{"line": 2, "code": "print(len(sys.argv))"}])
        guarded = self.facts("tool.py", "def main():\n    pass\n\nif __name__ == '__main__':\n    main()\n")
        self.assertEqual(guarded["entry_point"], {"kind": "main guard", "line": 4})

    def test_python_reads_writes_and_paths_are_listed_with_lines(self):
        facts = self.facts("train.py", "from pathlib import Path\nimport json\n"
                           "cfg = json.loads(Path('settings.json').read_text())\n"
                           "model.save_weights('best.h5')\nmodel.load_weights('best.h5')\n"
                           "with open(cfg['out'], 'w') as f:\n    f.write('x')\nname = text.replace('a', 'b')\n")
        self.assertEqual([item["line"] for item in facts["reads_files"]], [3, 5])
        self.assertEqual([item["line"] for item in facts["writes_files"]], [4, 6])
        self.assertEqual(facts["file_references"], [{"line": 3, "text": "settings.json"}, {"line": 4, "text": "best.h5"}])
        self.assertIn("model", [item["name"] for item in facts["names_without_visible_definition"]])

    def test_other_languages_get_approximate_facts_and_comments_are_ignored(self):
        cpp = self.facts("merge.cpp", "/**\n * struct ListNode {\n */\n#include <vector>\nclass Solution {\n};\n")
        self.assertEqual(cpp["imports"], ["vector"])
        self.assertIsNone(cpp["entry_point"])
        self.assertEqual([d["name"] for d in cpp["definitions"]], ["Solution"])
        java = self.facts("App.java", "import java.util.Map;\npublic class App {\n  public static void main(String[] a) {}\n}\n")
        self.assertEqual(java["entry_point"], {"kind": "main method", "line": 3})
        self.assertEqual(java["imports"], ["java.util.Map"])
        shell = self.facts("launch.sh", '#!/bin/sh\n# Launch\nexec ./bin/tool --config ./settings.json "$@"\n')
        self.assertEqual(shell["entry_point"]["line"], 3)
        self.assertEqual([r["text"] for r in shell["file_references"]], ["./bin/tool", "./settings.json"])
        self.assertEqual(shell["command_line"][0]["line"], 3)
        text = self.facts("notes.txt", "LOAD $ITEMS\n")
        self.assertNotIn("entry_point", text)
        self.assertEqual(text["language"], "unknown (plain text file)")

    def test_model_message_keeps_source_as_plain_lines(self):
        path = self.root / "library.py"
        path.write_text(LIBRARY, encoding="utf-8")
        content = messages_for(snapshot(path, "library.py", 60000))[-1]["content"]
        self.assertTrue(content.startswith("FILE: library.py\n"))
        self.assertIn('\n1: class Solution:\n2:     def twoSum(self, nums: List[int]', content)
        self.assertTrue(content.endswith("----- END SOURCE -----"))

    def test_plain_run_summary_and_public_methods_help_small_models(self):
        library = self.facts("cache.py", "class Cache:\n    def __init__(self, capacity):\n        self.capacity = capacity\n"
                                         "    def get(self, key) -> int:\n        return -1\n    def _evict(self):\n        pass\n")
        self.assertTrue(library["runs_when_executed"].startswith("No. Nothing runs"))
        self.assertEqual(library["definitions"], [{"name": "Cache", "line": 1, "kind": "class",
                                                   "methods": ["Cache(capacity)", "get(key) -> int"]}])
        self.assertNotIn("argument_declarations", library)
        script = self.facts("tool.py", "def main():\n    pass\n\nif __name__ == '__main__':\n    main()\n")
        self.assertIn("block at line 4", script["runs_when_executed"])
        broken = self.facts("broken.py", "class A:\n    def f(self):\n        return 1\n      x = 2\n")
        self.assertIsNone(broken["entry_point"])
        self.assertIn("estimated", broken["runs_when_executed"])

    def test_import_use_is_ranked_and_unused_imports_are_listed(self):
        imports = "".join(f"import {name}\n" for name in ("os", "sys", "json", "math", "re", "csv", "time", "uuid"))
        facts = self.facts("big.py", imports + "import numpy as np\nprint(np.zeros(1), np.ones(1), os.sep, sys.argv)\n")
        self.assertEqual(facts["main_imports"][:3], ["numpy", "os", "sys"])
        self.assertEqual(facts["unused_imports"], ["csv", "json", "math", "re", "time", "uuid"])

    def test_cpp_and_java_list_missing_includes_imports_and_comment_only_types(self):
        cpp = self.facts("merge.cpp", "/*\n  Uses a vector and stringstream.\n*/\n/**\n * struct ListNode {\n */\n"
                                      "class Solution {\npublic:\n    ListNode* merge(ListNode* a, vector<int>& b) {\n"
                                      "        return a;\n    }\nprivate:\n    void helper(int x) {\n    }\n};\n")
        self.assertEqual(cpp["definitions"], [{"name": "Solution", "line": 7, "kind": "class",
                                               "methods": ["merge(ListNode* a, vector<int>& b) -> ListNode*"]}])
        self.assertEqual(sorted((m["name"], m["note"]) for m in cpp["names_without_visible_definition"]),
                         [("ListNode", "defined only in a comment"), ("vector", "no #include <vector>")])
        java = self.facts("Cache.java", "class Cache {\n    private Map<Integer, Integer> m = new HashMap<>();\n"
                                        "    public Cache(int capacity) {\n    }\n    public int get(int key) {\n        return -1;\n    }\n}\n")
        self.assertEqual(java["definitions"][0]["methods"], ["Cache(int capacity)", "get(int key) -> int"])
        self.assertEqual([m["name"] for m in java["names_without_visible_definition"]], ["HashMap", "Map"])

    def test_scripts_say_which_arguments_they_forward(self):
        shell = self.facts("launch.sh", '#!/bin/sh\nexec ./bin/tool --config ./settings.json "$@"\n')
        self.assertEqual(shell["command_line"][0]["uses"], "all arguments given to this script")
        self.assertEqual(shell["runs_when_executed"], "Yes. The script runs from top to bottom when started.")


class ClaimCheckTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def case(self, name, text):
        path = self.root / name
        path.write_text(text, encoding="utf-8")
        return snapshot(path, name, 60000)

    def problems(self, case, **changes):
        return [(issue["field"], issue["item"]) for issue in check_claims(answer(**changes), case)]

    def test_correct_library_answer_has_no_flags(self):
        self.assertEqual(check_claims(answer(), self.case("two_sum.py", LIBRARY)), [])

    def test_invented_run_commands_are_flagged_only_when_nothing_runs(self):
        library = self.case("two_sum.py", LIBRARY)
        for usage in ("python two_sum.py", "Run the script with a list as input", "Compile and run the code"):
            self.assertEqual(self.problems(library, usage=[usage]), [("usage", usage)], usage)
        for usage in ("Not a standalone program; import it from other code.", "Requires a Python environment."):
            self.assertEqual(self.problems(library, usage=[usage]), [], usage)
        java = self.case("Solution.java", "public class Solution {\n  public int f() { return 1; }\n}\n")
        self.assertEqual(self.problems(java, usage=["java Solution"], evidence=[{"line": 2, "claim": "x"}]),
                         [("usage", "java Solution")])
        self.assertEqual(self.problems(java, usage=["Requires a Java runtime environment."], evidence=[{"line": 2, "claim": "x"}]), [])
        script = self.case("count.py", "import sys\nprint(len(sys.argv))\n")
        self.assertEqual(self.problems(script, usage=["python count.py a b"], evidence=[{"line": 2, "claim": "x"}]), [])

    def test_placeholders_languages_local_classes_and_self_references_are_flagged(self):
        library = self.case("two_sum.py", LIBRARY)
        flagged = self.problems(library, dependencies=["Python", "Solution class", "List[int]"],
                                relationships=["None", "two_sum.py"])
        self.assertEqual(sorted(flagged), [("dependencies", "List[int]"), ("dependencies", "Python"),
                                           ("dependencies", "Solution class"), ("relationships", "None"),
                                           ("relationships", "two_sum.py")])
        script = self.case("count.py", "import sys\nfrom pathlib import Path\nprint(Path(sys.argv[1]).read_text())\n")
        fine = ["Python standard library (sys, pathlib)", "pathlib"]
        self.assertEqual(self.problems(script, dependencies=fine, usage=[], evidence=[{"line": 3, "claim": "x"}]), [])
        self.assertEqual(self.problems(library, dependencies=["List (from typing)"]), [])

    def test_invented_files_flags_and_libraries_are_flagged(self):
        case = self.case("filter.py", "import argparse\np = argparse.ArgumentParser()\np.add_argument('--date')\n"
                                      "args = p.parse_args()\nprint(open('rows.csv').read())\n")
        flagged = self.problems(case, usage=["python filter.py --date 2024-01-01 --verbose"],
                                inputs=["rows.csv", "ListNode.h"], dependencies=["argparse", "iostream"],
                                evidence=[{"line": 5, "claim": "x"}])
        self.assertEqual(sorted(flagged), [("dependencies", "iostream"), ("inputs", "ListNode.h"),
                                           ("usage", "python filter.py --date 2024-01-01 --verbose")])

    def test_null_purpose_comment_evidence_and_non_english_are_flagged(self):
        library = self.case("two_sum.py", "# Two sum\n" + LIBRARY)
        flagged = self.problems(library, purpose=None, evidence=[{"line": 1, "claim": "x"}], outputs=["结果"])
        self.assertEqual(sorted(flagged), [("evidence", "line 1"), ("outputs", "结果"), ("purpose", None)])
        unknown = self.case("notes.txt", "FRAME Z17\nAPPLY GLORP /Q\n")
        self.assertEqual(self.problems(unknown, purpose=None, usage=[], unknowns=["Unknown syntax."],
                                       evidence=[{"line": 2, "claim": "x"}]), [])
        shell = self.case("run.sh", "#!/bin/sh\necho hi\n")
        self.assertEqual(self.problems(shell, usage=["./run.sh"], evidence=[{"line": 1, "claim": "x"}]), [])

    def test_softer_run_claims_and_internal_terms_are_flagged(self):
        library = self.case("two_sum.py", LIBRARY)
        for usage in ("No command-line arguments; run directly in an IDE.", "Run directly with no arguments",
                      "It runs directly when executed as a script.", "Run as a module or import it."):
            self.assertEqual(self.problems(library, usage=[usage]), [("usage", usage)], usage)
        fine = "No command-line arguments or entry point exist to run this file directly."
        self.assertEqual(self.problems(library, usage=[fine]), [])
        leaked = "No run command exists because entry_point is null."
        self.assertEqual(self.problems(library, usage=[leaked]), [("usage", leaked)])

    def test_relationships_must_be_files_programs_or_configs_named_in_code(self):
        case = self.case("app.py", "from .core import run\nimport json\nfrom collections import deque\n"
                                   "class Node:\n    pass\ncfg = json.loads(open('settings.json').read())\nrun(cfg, deque())\n")
        bad = ["Node: a cache entry", "deque: a queue type", "json: parses text", "Ollama server: runs models", "HashMap: lookups"]
        good = ["settings.json: read for settings", ".core: provides run()"]
        flagged = self.problems(case, relationships=bad + good, usage=[], unknowns=[], evidence=[{"line": 7, "claim": "x"}])
        self.assertEqual(sorted(set(flagged)), sorted(("relationships", item) for item in bad))

    def test_unknowns_cannot_call_defined_imported_or_built_in_names_missing(self):
        case = self.case("lru.py", "from collections import deque\nclass Node:\n    pass\nfor i in range(3):\n    deque()\n")
        flagged = self.problems(case, unknowns=["Node is not imported.", "The code imports nothing for range, which is not defined."],
                                usage=[], evidence=[{"line": 5, "claim": "x"}])
        self.assertEqual(len(flagged), 2)
        self.assertEqual(self.problems(case, unknowns=["TreeNode is used but never defined."], usage=[],
                                       evidence=[{"line": 5, "claim": "x"}]), [])
        copied = self.case("demo.py", 'def f():\n    return "Demo only: no real model was used."\n')
        self.assertEqual(self.problems(copied, unknowns=["Demo only: no real model was used."], usage=[],
                                       evidence=[{"line": 2, "claim": "x"}]), [("unknowns", "Demo only: no real model was used.")])

    def test_filler_forwarded_options_and_guessed_purposes_are_flagged(self):
        library = self.case("two_sum.py", LIBRARY)
        flagged = self.problems(library, inputs=["nums", "i, j: row and column indices"],
                                outputs=["the indices", "printed output, if any"])
        self.assertEqual(sorted(flagged), [("inputs", "i, j: row and column indices"), ("outputs", "printed output, if any")])
        shell = self.case("launch.sh", '#!/bin/sh\nexec ./bin/tool --config ./settings.json "$@"\n')
        self.assertEqual(self.problems(shell, usage=["./launch.sh --config ./settings.json extra"], unknowns=[],
                                       evidence=[{"line": 2, "claim": "x"}]),
                         [("usage", "./launch.sh --config ./settings.json extra")])
        self.assertEqual(self.problems(shell, usage=["./launch.sh extra-arguments"], unknowns=[],
                                       evidence=[{"line": 2, "claim": "x"}]), [])
        unknown = self.case("notes.txt", "FRAME Z17\nAPPLY GLORP /Q\n")
        guess = "This appears to be a config file for a custom tool."
        self.assertEqual(self.problems(unknown, purpose=guess, usage=[], unknowns=[], evidence=[{"line": 2, "claim": "x"}]),
                         [("purpose", guess)])

    def test_cleanup_removes_placeholders_and_duplicates_without_the_model(self):
        value = answer(purpose="None", inputs=["nums", "None", "nums"], relationships=["null"])
        removed = clean_answer(value)
        self.assertEqual((value["purpose"], value["inputs"], value["relationships"]), (None, ["nums"], []))
        self.assertEqual(len(removed), 4)


class RoundThreeCheckTests(unittest.TestCase):
    setUp = ClaimCheckTests.setUp
    case = ClaimCheckTests.case
    problems = ClaimCheckTests.problems

    def test_known_language_never_gets_a_null_purpose(self):
        broken = self.case("broken.py", "class A:\n    def f(self):\n        return 1\n      x = 2\n")
        self.assertIn(("purpose", None), self.problems(broken, purpose=None, usage=[], evidence=[{"line": 3, "claim": "x"}]))

    def test_notes_about_absence_and_usage_notes_are_not_outputs_or_inputs(self):
        library = self.case("two_sum.py", LIBRARY)
        flagged = self.problems(library, inputs=["nums", "The method is called by other code, not executed directly"],
                                outputs=["No direct output; state changes inside the object", "None when nothing is found",
                                         "indices of the pair, or no output if none exist"])
        self.assertEqual(sorted(flagged), [("inputs", "The method is called by other code, not executed directly"),
                                           ("outputs", "No direct output; state changes inside the object")])

    def test_internal_variables_are_not_inputs_or_outputs_but_returned_ones_are(self):
        script = self.case("filter.py", "import csv\nrows = list(csv.reader(open('a.csv')))\nselected = rows[:1]\n"
                                        "def first(items):\n    best = items[0]\n    return best\nprint(selected)\n")
        flagged = self.problems(script, inputs=["items: values given to first()"], outputs=["selected: the kept rows", "best: the first item"],
                                usage=[], unknowns=[], evidence=[{"line": 7, "claim": "x"}])
        self.assertEqual(flagged, [("outputs", "selected: the kept rows")])

    def test_install_names_are_accepted_and_unused_libraries_are_flagged(self):
        case = self.case("vision.py", "import cv2\nfrom sklearn.cluster import KMeans\nprint(cv2.imread('a.png'), KMeans())\n")
        fine = ["opencv-python", "scikit-learn", "cv2"]
        self.assertEqual(self.problems(case, dependencies=fine, usage=[], unknowns=[], evidence=[{"line": 3, "claim": "x"}]), [])
        usage = "Needs TensorFlow/PyTorch installed."
        problems = [issue["problem"] for issue in check_claims(answer(usage=[usage], unknowns=[], evidence=[{"line": 3, "claim": "x"}]), case)]
        self.assertEqual([p.split()[0] for p in problems], ["TensorFlow", "PyTorch"])

    def test_files_a_wrapper_passes_itself_are_not_user_inputs(self):
        shell = self.case("launch.sh", '#!/bin/sh\nexec ./bin/tool --config ./settings.json "$@"\n')
        bad_usage = "Run ./launch.sh followed by the settings file path and any arguments."
        flagged = self.problems(shell, inputs=["./settings.json: the settings file", "arguments forwarded to tool"],
                                usage=[bad_usage, "Make sure ./settings.json exists first."], unknowns=[],
                                evidence=[{"line": 2, "claim": "x"}])
        self.assertEqual(sorted(flagged), [("inputs", "./settings.json: the settings file"), ("usage", bad_usage)])
        note = snapshot(self.root / "launch.sh", "launch.sh", 60000)["facts"]["command_line"][0]["note"]
        self.assertEqual(note, "The script itself passes --config ./settings.json to ./bin/tool; "
                               "the user's own arguments are added after that.")


class RoundFourCheckTests(unittest.TestCase):
    setUp = ClaimCheckTests.setUp
    case = ClaimCheckTests.case
    problems = ClaimCheckTests.problems

    def test_copied_example_names_in_usage_are_flagged(self):
        case = self.case("prices.py", "def total(prices):\n    return sum(prices)\n\n")
        copied = "From other code, create a Solution object and call twoSum(nums, target)"
        flagged = check_claims(answer(usage=["From other code, call total(prices)", copied], unknowns=[],
                                      evidence=[{"line": 2, "claim": "x"}]), case)
        self.assertEqual(sorted(issue["problem"].split()[0] for issue in flagged), ["Solution", "twoSum"])

    def test_defined_helpers_and_built_in_claims_in_unknowns_are_flagged(self):
        cpp = self.case("course.cpp", "class Solution {\npublic:\n    bool canFinish(int n) {\n        return dfs(n);\n    }\n"
                                      "private:\n    bool dfs(int n) {\n        return true;\n    }\n};\n")
        claim = "The implementation relies on a missing dfs helper function."
        self.assertEqual(self.problems(cpp, unknowns=[claim], usage=[], evidence=[{"line": 4, "claim": "x"}]), [("unknowns", claim)])
        library = self.case("two_sum.py", LIBRARY)
        claims = ["The code uses the built-in List type but does not import it.", "No external libraries are required."]
        self.assertEqual(sorted(self.problems(library, unknowns=claims)), sorted(("unknowns", claim) for claim in claims))
        self.assertEqual(self.problems(library, unknowns=["The total size is not defined for empty input."]), [])

    def test_internal_variable_names_anywhere_in_inputs_are_flagged(self):
        case = self.case("brackets.py", "class Solution:\n    def isValid(self, s):\n        bracketMap = {')': '('}\n"
                                        "        stack = [c for c in s if c in bracketMap]\n        return not stack\n")
        item = "The bracketMap dictionary defining matching pairs"
        self.assertEqual(self.problems(case, inputs=["s: the text to check", item], usage=[], unknowns=[],
                                       evidence=[{"line": 4, "claim": "x"}]), [("inputs", item)])

    def test_unparsable_python_still_gets_definitions_for_filling(self):
        broken = self.case("broken.py", "class Grid:\n    def count(self, grid) -> int:\n        return 1\n      x = 2\n")
        self.assertEqual(broken["facts"]["definitions"], [{"name": "Grid", "line": 1, "kind": "class", "methods": ["count(grid) -> int"]}])
        value = answer(purpose="Counts cells.", inputs=[], outputs=[], usage=[])
        filled = fill_from_facts(value, broken)
        self.assertEqual([(f["field"], f["item"]) for f in filled],
                         [("inputs", "grid (passed to count)"), ("outputs", "int returned by count"),
                          ("usage", "From other code, create a Grid object and call count(grid), "
                                    "after the syntax error at line 4 is fixed.")])


class RepairFlowTests(unittest.TestCase):
    def setUp(self):
        fixtures.HttpWorkflowTests.setUp(self)
        path = self.root / "two_sum.py"
        path.write_text(LIBRARY, encoding="utf-8")
        self.cases = [snapshot(path, "two_sum.py", 60000)]

    def run_lab(self, *outputs):
        self.server.state["outputs"] = [json.dumps(value) if isinstance(value, dict) else value for value in outputs]
        folder, code = fixtures.HttpWorkflowTests.run_lab(self)
        return folder, code, json.loads((folder / "results.jsonl").read_text())

    def test_flagged_answer_gets_one_repair_request_and_the_better_answer_is_kept(self):
        bad = answer(usage=["python two_sum.py"], dependencies=["Python"])
        folder, code, row = self.run_lab(bad, answer())
        self.assertEqual(code, 0)
        self.assertEqual(self.server.state["chat_count"], 2)
        first, second = [body for path, body in self.server.state["requests"] if path == "/api/chat"]
        self.assertEqual(second["messages"][:len(first["messages"])], first["messages"])
        self.assertEqual(second["messages"][-2], {"role": "assistant", "content": json.dumps(bad)})
        self.assertIn('usage "python two_sum.py": Nothing runs', second["messages"][-1]["content"])
        self.assertEqual(second["format"], first["format"])
        self.assertEqual(row["status"], "valid")
        self.assertEqual(row["analysis"], answer())
        self.assertEqual(json.loads(row["first_raw_output"]), bad)
        self.assertTrue(row["repair"]["used"] and row["claim_checks_passed"])
        self.assertEqual(len(row["repair"]["requested_for"]), 2)
        self.assertIn("this is the corrected answer", (folder / "report.html").read_text())
        run = json.loads((folder / "run.json").read_text())
        self.assertEqual((run["input_version"], run["claim_checks_version"], run["config"]["repair_attempts"]), ("5", "6", 1))

    def test_worse_or_broken_repair_keeps_the_first_answer_and_shows_flags(self):
        bad = answer(usage=["python two_sum.py"])
        folder, code, row = self.run_lab(bad, "not JSON")
        self.assertEqual(code, 0)
        self.assertEqual(row["analysis"], bad)
        self.assertFalse(row["repair"]["used"])
        self.assertIn("Invalid JSON", row["repair"]["error"])
        self.assertEqual([issue["field"] for issue in row["check_issues"]], ["usage"])
        report = (folder / "report.html").read_text()
        self.assertIn("Needs checking (found by the program)", report)
        self.assertIn("Flagged by program checks", (folder / "summary.md").read_text())

    def test_repair_can_be_disabled_and_clean_answers_send_one_request(self):
        self.config["repair_attempts"] = 0
        _, _, row = self.run_lab(answer(usage=["python two_sum.py"]))
        self.assertEqual(self.server.state["chat_count"], 1)
        self.assertNotIn("repair", row)
        self.assertFalse(row["claim_checks_passed"])
        self.config["repair_attempts"] = 1
        self.server.state.update(chat_count=0, requests=[])
        _, _, row = self.run_lab(answer())
        self.assertEqual(self.server.state["chat_count"], 1)
        self.assertTrue(row["claim_checks_passed"])

    def test_placeholders_and_minor_flags_do_not_cost_a_repair_request(self):
        _, _, row = self.run_lab(answer(inputs=["nums", "target", "None"]))
        self.assertEqual(self.server.state["chat_count"], 1)
        self.assertEqual(row["analysis"]["inputs"], ["nums", "target"])
        self.assertEqual(row["cleaned"], [{"field": "inputs", "item": "None"}])
        self.assertTrue(row["claim_checks_passed"])
        path = self.root / "commented.py"
        path.write_text("# Two sum\n" + LIBRARY, encoding="utf-8")
        self.cases = [snapshot(path, "commented.py", 60000)]
        self.server.state.update(chat_count=0, requests=[])
        _, _, row = self.run_lab(answer(evidence=[{"line": 1, "claim": "Two sum"}]))
        self.assertEqual(self.server.state["chat_count"], 1)
        self.assertEqual([issue.get("minor") for issue in row["check_issues"]], [True])

    def test_repair_that_drops_correct_items_is_not_used(self):
        bad = answer(usage=["python two_sum.py"])
        folder, code, row = self.run_lab(bad, answer(usage=[], inputs=[]))
        self.assertEqual(code, 0)
        self.assertFalse(row["repair"]["used"])
        self.assertIn("dropped parts that had no problems: inputs", row["repair"]["error"])
        self.assertEqual(row["analysis"], bad)

    def test_null_purpose_without_unknowns_is_repaired_instead_of_failing(self):
        broken = answer(purpose=None, unknowns=[])
        folder, code, row = self.run_lab(broken, answer())
        self.assertEqual(code, 0)
        self.assertEqual(row["status"], "valid")
        self.assertTrue(row["repair"]["used"] and row["evidence_checks_passed"])
        self.assertEqual(row["first_errors"], ["Unknown purpose must explain what is missing in unknowns."])
        self.assertEqual(json.loads(row["first_raw_output"]), broken)
        asked = [issue["field"] for issue in row["repair"]["requested_for"]]
        self.assertEqual(asked, ["purpose"])  # the duplicate format rule is not repeated
        self.server.state.update(chat_count=0, requests=[])
        _, code, row = self.run_lab(broken, broken)
        self.assertEqual((code, row["status"]), (1, "invalid_output"))
        self.assertFalse(row["repair"]["used"])

    def test_repair_that_leaves_outputs_empty_is_not_used(self):
        path = self.root / "save.py"
        path.write_text("from pathlib import Path\ntext = 'x'\nPath('out.txt').write_text(text)\n", encoding="utf-8")
        self.cases = [snapshot(path, "save.py", 60000)]
        script = dict(answer(usage=["Run it directly."], unknowns=[]), inputs=[])
        bad = dict(script, outputs=["out.txt, if any"])
        _, _, row = self.run_lab(bad, dict(script, outputs=[]))
        self.assertFalse(row["repair"]["used"])
        self.assertIn("left these parts empty: outputs", row["repair"]["error"])
        self.assertEqual(row["analysis"]["outputs"], ["out.txt, if any"])

    def test_empty_library_parts_are_filled_from_facts_without_a_request(self):
        folder, _, row = self.run_lab(answer(inputs=[], usage=[]))
        self.assertEqual(self.server.state["chat_count"], 1)
        self.assertEqual(row["analysis"]["inputs"], ["nums, target (passed to twoSum)"])
        self.assertEqual(row["analysis"]["usage"], ["From other code, create a Solution object and call twoSum(nums, target)."])
        self.assertEqual([item["field"] for item in row["filled"]], ["inputs", "usage"])
        self.assertIn("the program filled them from the code: inputs, how to use", (folder / "report.html").read_text())

    def test_older_configs_get_the_repair_default_and_bad_values_fail(self):
        config = load_config(SETTINGS / "default.json")
        self.assertEqual(config["repair_attempts"], 1)
        config["repair_attempts"] = 2
        path = self.root / "bad.json"
        save_json(path, config)
        with self.assertRaisesRegex(LabError, "repair_attempts"):
            load_config(path)


if __name__ == "__main__":
    unittest.main()
