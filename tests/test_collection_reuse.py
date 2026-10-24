"""Business-level reuse tests: one file, several projects, normal preflight/execute/undo."""
import copy
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from types import SimpleNamespace

from ai.collection_reuse import collection_reuse, discover, context, batch_request, DECISION, TOPIC_CACHE_PROMPT, PROMPT
from ai.reuse import fingerprint
from ai.common import AIError
from ai.organize import load_topics, plan_for, links_for
from ai.explain.facts import static_facts
from Services.ArtifactsService import ArtifactsService
from Services.ExecutionService import ExecutionService
from Services.OrganizationService import OrganizationService, plan_digest
from Services.ScanService import ScanService
from test_reuse import Settings, TOPICS, ScriptedModel


class CollectionModel:
    """Uses visible fact text to recognize the deliberately shared loader; no source-code execution."""
    def __init__(self):
        self.calls = 0
        self.omit_one = False
        self.bad_evidence = False

    def ask(self, tag, messages, schema, num_predict=1024):
        self.calls += 1
        payload = json.loads(messages[-1]["content"])
        if "useful" in schema["properties"]:
            item = payload["items"][0]
            text = " ".join(f["text"] for file in item["source"]["files"] for f in file["facts"])
            value = {"source_contribution": "Load CSV dataset rows", "target_operation": "Read labelled training rows",
                     "useful": "Reusable CSV dataset loader" in text, "source_changes_required": False,
                     "source_evidence": "S999" if self.bad_evidence else "S1", "target_evidence": "T1"}
            return {"done": True, "done_reason": "stop", "message": {"content": json.dumps(value)}}
        decisions = []
        for item in payload["items"]:
            text = " ".join(f["text"] for file in item["source"]["files"] for f in file["facts"])
            shared_loader = "Reusable CSV dataset loader" in text
            decisions.append({"file": item["file"], "verdict": "compatible" if shared_loader and item["target"] else "incompatible",
                "reason": "The CSV dataset loader supplies this project's training rows." if shared_loader else "No additional use demonstrated.",
                "source_evidence": "S999" if self.bad_evidence else "S1",
                "target_evidence": "T1" if item["target"] else "none",
                "topics": [{"topic": "data-processing", "reason": "Loads reusable CSV datasets."}]
                    if shared_loader and schema["properties"]["decisions"]["items"]["properties"]["topics"].get("maxItems") != 0 else []})
        if self.omit_one:
            decisions = decisions[1:]
        return {"done": True, "done_reason": "stop", "message": {"content": json.dumps({"decisions": decisions})}}


class CollectionReuseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.watched = self.root / "watched"
        self.watched.mkdir()
        self.settings = Settings(self.root)
        self.plan = {"created_at": "2026-10-24T00:00:00Z", "files": [], "units": [], "actions": [], "loose": [], "summary": {}}
        self.add_project("tools", [
            ("loader.py", "Reusable CSV dataset loader returns labelled training rows.", ["helper.py"], "from .helper import parse\ndef load(path): return parse(path)\n"),
            ("helper.py", "Parse CSV image dataset records into labelled training rows.", [], "def parse(path): return path\n"),
            ("unrelated.py", "Download weather bulletins over HTTP.", [], "print('weather')\n")])
        self.add_project("project-a", [("train.py", "Train image classifiers from CSV dataset training rows.", [], "print('train a')\n")])
        self.add_project("project-b", [("train.py", "Train image classifiers from CSV dataset training rows.", [], "print('train b')\n")])
        self.model = CollectionModel()

    def add_record(self, path, summary, dependencies, code, kind="script", explained=True, sensitive=False):
        file = self.watched / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(code.encode())
        record = {"root_folder": str(self.watched), "path": path, "sha256": hashlib.sha256(code.encode()).hexdigest(),
                  "type": kind, "role": "module" if kind == "script" else None, "size": len(code),
                  "language": "Python" if kind == "script" else "text", "summary": summary,
                  "inputs": ["CSV dataset rows"] if summary else [], "outputs": ["Labelled training rows"] if summary else [],
                  "dependencies": ["csv"] if summary else [], "interfaces": [], "unknowns": [],
                  "sensitive": sensitive, "explained": explained, "local_dependencies": dependencies,
                  "duplicate_of": None, "confidence": .85, "reasons": []}
        self.plan["files"].append(record)
        return record

    def add_project(self, name, files):
        unit = {"id": "p-" + name, "kind": "project", "root": name, "root_folder": str(self.watched),
                "folder": "machine-learning/" + name, "topic": "machine-learning", "members": [], "duplicate_of": None}
        self.plan["units"].append(unit)
        for file, summary, deps, code in files:
            path = name + "/" + file
            record = self.add_record(path, summary, [name + "/" + dep for dep in deps], code)
            unit["members"].append(path)
        for name_in_project in ("README.md", "LICENSE"):
            path = name + "/" + name_in_project
            self.add_record(path, None, [], "Project documentation and license.\n", kind="documentation")
            unit["members"].append(path)
        for path in unit["members"]:
            self.plan["actions"].append({"action": "copy", "unit": unit["id"], "from": path,
                "from_path": str(self.watched / path), "to": unit["folder"] + "/" + path.split("/", 1)[1]})

    def build(self, cache=None):
        self.plan["reuse"] = collection_reuse(self.plan, TOPICS, self.model, "scripted", cache, batch_size=3)
        return self.plan["reuse"]

    def test_one_file_reused_by_two_projects_not_the_whole_source_project(self):
        report = self.build()
        self.assertEqual(report["state"], "complete", report["errors"])
        self.assertEqual(report["considered_files"], 5)
        self.assertEqual(report["assessed_files"], 5)
        self.assertEqual(report["projects"], 3)
        self.assertEqual(report["deferred_pairs"], 0)
        matches = [s for s in report["suggestions"] if s["kind"] == "project" and s["source_file"] == "tools/loader.py"]
        self.assertEqual({s["target"] for s in matches}, {"p-project-a", "p-project-b"})
        for match in matches:
            self.assertEqual(set(match["supporting_files"]), {"tools/loader.py", "tools/helper.py", "tools/LICENSE"})
            self.assertTrue(all("/_reuse/" in path for path in match["destinations"]))
            self.assertFalse(any("unrelated" in path or "README" in path for path in match["destinations"]))
        self.assertEqual(len([s for s in report["suggestions"] if s["kind"] == "topic"]), 1)
        self.assertLess(report["model_calls"], report["candidate_pairs"])

    def test_topic_only_mode_never_copies_into_projects(self):
        report = collection_reuse(self.plan, TOPICS, self.model, "scripted", topics_only=True)
        self.assertEqual(report["state"], "complete", report["errors"])
        self.assertEqual(report["mode"], "additional-topics")
        self.assertEqual(report["candidate_pairs"], 0)
        self.assertEqual(report["verification_calls"], 0)
        self.assertTrue(report["suggestions"])
        self.assertTrue(all(s["kind"] == "topic" for s in report["suggestions"]))
        copies = [a for a in self.plan["actions"] if a.get("reuse_id") and a["action"] == "copy"]
        self.assertTrue(copies)
        self.assertTrue(all(a["to"].startswith("data-processing/") and "/_reuse/" not in a["to"] for a in copies))

    def test_usual_preflight_execute_and_undo_needs_no_individual_approvals(self):
        self.build()
        artifacts = ArtifactsService(str(self.root / "db.sqlite3"))
        scan = ScanService(self.settings, None, artifacts)
        org = OrganizationService(self.settings, artifacts, SimpleNamespace(), scan)
        self.plan["plan_id"] = plan_digest(self.plan)
        with org._connect() as db:
            db.execute("INSERT INTO organization_plans VALUES (1, ?, ?)", (self.plan["created_at"], json.dumps(self.plan)))
        execution = ExecutionService(self.settings, artifacts, org, scan)
        preview = execution.preflight(self.plan["plan_id"])
        self.assertNotIn("conflict", preview["summary"])
        self.assertNotIn("skipped", preview["summary"])
        run = execution.execute(self.plan["plan_id"])
        self.assertEqual(run["state"], "complete")
        self.assertNotIn("failed", run["summary"])
        extra = [r for r in run["results"] if r.get("reuse_id") and r["action"] == "copy"]
        self.assertEqual(len(extra), 9)  # loader + helper + license in two projects and one extra topic
        for result in extra:
            self.assertEqual((self.root / "library" / result["to"]).read_bytes(), Path(result["from_path"]).read_bytes())
        originals = {str(p): p.read_bytes() for p in self.watched.rglob("*") if p.is_file()}
        execution.undo(run["run_id"])
        self.assertTrue(all(not (self.root / "library" / r["to"]).exists() for r in extra))
        self.assertEqual(originals, {str(p): p.read_bytes() for p in self.watched.rglob("*") if p.is_file()})

    def test_complete_second_plan_uses_cache_without_new_model_calls(self):
        original = copy.deepcopy(self.plan)
        stored = {}
        cache = SimpleNamespace(get=stored.get, put=lambda key, value: stored.__setitem__(key, value))
        first = self.build(cache)
        calls = self.model.calls
        self.plan = copy.deepcopy(original)
        second = self.build(cache)
        self.assertEqual(self.model.calls, calls)
        self.assertEqual(second["model_calls"], 0)
        self.assertEqual(second["assessed_pairs"], first["assessed_pairs"])
        self.assertEqual(second["suggestions"], first["suggestions"])
        self.assertGreater(second["cache_hits"], 1)

    def test_runtime_topic_sets_are_json_safe_and_cache_order_independent(self):
        path = self.root / "topics.json"
        path.write_text(json.dumps(TOPICS), encoding="utf-8")
        topics = load_topics(path)
        self.assertIsInstance(topics["topics"][0]["hints"], set)
        original = copy.deepcopy(self.plan)
        stored = {}
        cache = SimpleNamespace(get=stored.get, put=lambda key, value: stored.__setitem__(key, value))
        report = collection_reuse(self.plan, topics, self.model, "test", cache)
        self.assertEqual(report["state"], "complete", report["errors"])
        json.dumps(self.plan)
        calls = self.model.calls
        self.plan = original
        topics["topics"][0]["hints"] = {"extra-keyword", "training", "model"}
        report = collection_reuse(self.plan, topics, self.model, "test", cache)
        self.assertEqual(self.model.calls, calls)
        self.assertEqual(report["model_calls"], 0)
        self.assertIsInstance(topics["topics"][0]["hints"], set)

    def test_full_plan_build_uses_real_topic_loader_and_persists_successfully(self):
        path = self.root / "topics.json"
        path.write_text(json.dumps(TOPICS), encoding="utf-8")
        self.settings.values["organization"]["topics_file"] = str(path)
        artifacts = ArtifactsService(str(self.root / "db.sqlite3"))
        scan = ScanService(self.settings, None, artifacts)
        scan.process(full_scan="test")
        model = ScriptedModel()
        analysis = SimpleNamespace(topic_model=lambda: (model, "test", "digest"),
            analysis_for=lambda _: {"status": "done", "analysis": {"purpose": "Reads CSV dataset training rows.",
                "inputs": ["CSV dataset"], "outputs": ["Training rows"], "dependencies": ["csv"]}})
        org = OrganizationService(self.settings, artifacts, analysis, scan)
        self.assertIsInstance(org.topics()["topics"][0]["hints"], set)
        plan = org.build()
        self.assertEqual(org.state, "ready")
        self.assertEqual(plan["reuse"]["state"], "complete", plan["reuse"]["errors"])
        self.assertEqual(org.latest(), plan)
        self.assertEqual(plan["plan_id"], plan_digest(plan))

    def test_no_global_pair_cap_and_standalone_file_is_not_a_destination_project(self):
        for n in range(5):
            self.add_project("more-" + str(n), [("train.py", "Train classifiers from CSV dataset rows.", [], f"print({n})\n")])
        report = self.build()
        loader_projects = {s["target"] for s in report["suggestions"] if s.get("source_file") == "tools/loader.py" and s["kind"] == "project"}
        self.assertEqual(len(loader_projects), 7)
        self.assertEqual(report["unassessed_files"], 0)
        self.assertEqual(report["deferred_pairs"], 0)

    def test_large_projects_do_not_hide_their_files(self):
        self.add_project("large", [(f"step-{n}.py", "Train classifiers from CSV dataset training rows.", [], f"print('large {n}')\n") for n in range(25)])
        sources, projects, tasks, skipped, _ = discover(self.plan)
        self.assertEqual(len(sources), 30)
        self.assertIn("p-large", projects)
        self.assertFalse(skipped)
        self.assertTrue(any(t["project"] and t["project"]["unit"]["id"] == "p-large" and t["source"]["record"]["path"] == "tools/loader.py" for t in tasks))

    def test_standalone_file_is_a_source_but_never_a_destination_project(self):
        self.add_record("standalone.py", "Parse CSV dataset training rows.", [], "print('standalone')\n")
        self.plan["units"].append({"id": "standalone", "kind": "single", "root": ".", "root_folder": str(self.watched),
            "folder": "data-processing/standalone", "topic": "data-processing", "members": ["standalone.py"], "duplicate_of": None})
        sources, projects, tasks, _, _ = discover(self.plan)
        self.assertTrue(any(s["record"]["path"] == "standalone.py" for s in sources.values()))
        self.assertNotIn("standalone", projects)
        self.assertFalse(any(t["project"] and t["project"]["unit"]["id"] == "standalone" for t in tasks))

    def test_scanning_facts_retain_the_actual_import_dependency_for_copying(self):
        records = copy.deepcopy(self.plan["files"])
        sources = {r["path"]: (self.watched / r["path"]).read_text() for r in records if r["type"] == "script"}
        facts = {path: static_facts(source, ".py") for path, source in sources.items()}
        part = plan_for(records, sources, facts, topics=TOPICS, progress=lambda _: None)
        loader = next(r for r in part["files"] if r["path"] == "tools/loader.py")
        self.assertIn("tools/helper.py", loader["local_dependencies"])

    def test_changed_helper_invalidates_dependent_file_assessment(self):
        stored = {}
        cache = SimpleNamespace(get=stored.get, put=lambda key, value: stored.__setitem__(key, value))
        original = copy.deepcopy(self.plan)
        self.build(cache)
        first_calls = self.model.calls
        self.plan = original
        next(r for r in self.plan["files"] if r["path"] == "tools/helper.py")["sha256"] = "a" * 64
        report = self.build(cache)
        self.assertGreater(self.model.calls, first_calls)
        self.assertGreater(report["cache_hits"], 0)

    def test_group_renumbering_does_not_trigger_new_ai_or_different_copy_paths(self):
        stored = {}
        cache = SimpleNamespace(get=stored.get, put=lambda key, value: stored.__setitem__(key, value))
        original = copy.deepcopy(self.plan)
        first = self.build(cache)
        calls = self.model.calls
        expected = {path for s in first["suggestions"] for path in s["destinations"]}
        self.plan = original
        for n, unit in enumerate(self.plan["units"]):
            previous = unit["id"]
            unit["id"] = "new-" + str(n)
            for action in self.plan["actions"]:
                if action["unit"] == previous:
                    action["unit"] = unit["id"]
        second = self.build(cache)
        self.assertEqual(self.model.calls, calls)
        self.assertEqual({path for s in second["suggestions"] for path in s["destinations"]}, expected)

    def test_missing_explanations_sensitive_dependencies_and_bad_model_output_are_visible(self):
        next(r for r in self.plan["files"] if r["path"] == "tools/helper.py")["sensitive"] = True
        next(r for r in self.plan["files"] if r["path"] == "project-b/train.py")["explained"] = False
        self.model.bad_evidence = True
        report = self.build()
        self.assertEqual(report["state"], "partial")
        self.assertTrue(any(r["file"] == "tools/loader.py" and "dependency" in r["reason"] for r in report["skipped_files"]))
        self.assertTrue(any(r["file"] == "project-b/train.py" for r in report["skipped_files"]))
        self.assertTrue(report["errors"])
        self.assertFalse(report["suggestions"])

    def test_omitted_decisions_do_not_silently_claim_full_coverage(self):
        self.model.omit_one = True
        report = self.build()
        self.assertEqual(report["state"], "partial")
        self.assertGreater(report["unassessed_files"], 0)
        self.assertTrue(any("omitted" in error["reason"] for error in report["errors"]))

    def test_timeout_retries_failed_items_without_abandoning_other_projects(self):
        normal = self.model.ask
        seen = []
        def ask(tag, messages, schema, num_predict=1024):
            items = json.loads(messages[-1]["content"])["items"]
            seen.append([item["target"]["id"] if item["target"] else None for item in items])
            if len(seen) == 1:
                raise AIError("Timed out", "timeout")
            return normal(tag, messages, schema, num_predict)
        self.model.ask = ask
        report = self.build()
        self.assertEqual(report["state"], "complete", report["errors"])
        self.assertEqual(report["deferred_pairs"], 0)
        self.assertEqual(report["unassessed_files"], 0)
        self.assertEqual(len(seen[-1]), 1)
        self.assertTrue(all(target is not None for target in seen[0]))

    def test_topic_only_schema_cannot_claim_project_compatibility(self):
        sources, _, tasks, _, records = discover(self.plan)
        source = next(iter(sources.values()))
        task = {"source": source, "project": None}
        item, catalog, _ = context(task, records)
        _, schema = batch_request([(task, item, catalog, "key")], TOPICS)
        properties = schema["properties"]["decisions"]["items"]["properties"]
        self.assertEqual(properties["verdict"]["enum"], ["incompatible"])
        self.assertEqual(properties["target_evidence"]["enum"], ["none"])
        self.assertNotIn("other", properties["topics"]["items"]["properties"]["topic"]["enum"])

    def test_cached_other_and_unsupported_topic_copies_are_filtered(self):
        normal = self.model.ask
        def ask(*args, **kwargs):
            response = normal(*args, **kwargs)
            decisions = json.loads(response["message"]["content"])
            if "decisions" not in decisions:
                return response
            for item in decisions["decisions"]:
                item["topics"] = [{"topic": "other", "reason": "No target project specified."}]
            response["message"]["content"] = json.dumps(decisions)
            return response
        self.model.ask = ask
        stored = {}
        cache = SimpleNamespace(get=stored.get, put=lambda k, v: stored.__setitem__(k, v))
        original = copy.deepcopy(self.plan)
        first = self.build(cache)
        self.assertFalse(any(s["kind"] == "topic" for s in first["suggestions"]))
        self.plan = original
        second = self.build(cache)
        self.assertGreater(second["cache_hits"], 0)
        self.assertFalse(any(s["kind"] == "topic" for s in second["suggestions"]))

    def test_docstring_filename_is_not_a_dependency_but_actual_import_is(self):
        source = '\"\"\"Helpers shared by train.py and evaluate.py.\"\"\"\nfrom .helper import parse\n\ndef score():\n    \"\"\"See train.py.\"\"\"\n    return 1\n'
        links = links_for("utils/metrics.py", source, ".py", {},
                          {"utils/metrics.py", "utils/helper.py", "train.py", "evaluate.py"})
        self.assertIn("utils/helper.py", links[0])
        self.assertNotIn("train.py", links[0])
        self.assertNotIn("evaluate.py", links[0])

    def test_unrelated_files_need_no_inference_after_candidate_screening(self):
        for record in self.plan["files"]:
            record.update(summary="Independent greeting", inputs=[], outputs=[], dependencies=[], interfaces=[])
        # Identical generic descriptions would still match each other. Distinct
        # vocabulary demonstrates the cheap no-candidate path without model calls.
        for n, record in enumerate(self.plan["files"]):
            record["summary"] = "greeting" + str(n)
        report = self.build()
        self.assertEqual(report["state"], "complete")
        self.assertEqual(report["model_calls"], 0)
        self.assertEqual(report["files_without_candidates"], 5)
        self.assertEqual(report["assessed_files"], 5)

    def test_small_metric_module_is_found_for_long_classification_project_description(self):
        metric = next(r for r in self.plan["files"] if r["path"] == "tools/helper.py")
        metric.update(summary="Calculates classification accuracy from labels and predictions.",
                      inputs=["Truth labels", "Predictions"], outputs=["Accuracy"], dependencies=["collections"],
                      interfaces=[], role="module")
        target = next(r for r in self.plan["files"] if r["path"] == "project-a/train.py")
        target.update(summary="Trains a convolutional neural network to classify handwritten digits from MNIST datasets.",
                      inputs=["Training images"], outputs=["Saved model weights"], dependencies=["torch", "numpy"])
        _, _, tasks, _, _ = discover(self.plan)
        self.assertTrue(any(t["source"]["record"]["path"] == "tools/helper.py" and t["project"]
                            and t["project"]["unit"]["id"] == "p-project-a" for t in tasks))

    def test_initial_positive_does_not_copy_when_independent_check_rejects_it(self):
        normal = self.model.ask
        def ask(tag, messages, schema, num_predict=1024):
            response = normal(tag, messages, schema, num_predict)
            if "useful" in schema["properties"]:
                value = json.loads(response["message"]["content"])
                value.update(useful=False)
                response["message"]["content"] = json.dumps(value)
            return response
        self.model.ask = ask
        report = self.build()
        self.assertEqual(report["state"], "complete", report["errors"])
        self.assertEqual(report["rejected_matches"], 2)
        self.assertFalse(any(s["kind"] == "project" for s in report["suggestions"]))
        self.assertFalse(any('/_reuse/' in a['to'] for a in self.plan['actions']))

    def test_caller_wiring_flag_is_confirmed_automatically_without_manual_approval(self):
        normal = self.model.ask
        def ask(tag, messages, schema, num_predict=1024):
            response = normal(tag, messages, schema, num_predict)
            if "decisions" in schema["properties"]:
                value = json.loads(response["message"]["content"])
                for decision in value["decisions"]:
                    if decision["verdict"] == "compatible":
                        decision.update(verdict="adaptation-needed", reason="The caller needs to import and call the loader.")
                response["message"]["content"] = json.dumps(value)
            return response
        self.model.ask = ask
        report = self.build()
        matches = [s for s in report["suggestions"] if s["kind"] == "project" and s["source_file"] == "tools/loader.py"]
        self.assertEqual(len(matches), 2)
        self.assertTrue(all(s["state"] == "accepted" for s in matches))
        self.assertTrue(all(any(a.get("reuse_id") == s["id"] for a in self.plan["actions"]) for s in matches))

    def test_vague_operation_is_rejected_without_copying_or_blocking_the_plan(self):
        normal = self.model.ask
        def ask(tag, messages, schema, num_predict=1024):
            response = normal(tag, messages, schema, num_predict)
            if "useful" in schema["properties"] and not any("Correction required" in m["content"] for m in messages):
                value = json.loads(response["message"]["content"])
                value.update(source_contribution="evaluate", target_operation="evaluate")
                response["message"]["content"] = json.dumps(value)
            return response
        self.model.ask = ask
        report = self.build()
        self.assertEqual(report["state"], "complete", report["errors"])
        self.assertEqual(report["verification_calls"], 2)
        self.assertEqual(report["unsupported_matches"], 2)
        matches = [s for s in report["suggestions"] if s["kind"] == "project"]
        self.assertFalse(matches)
        self.assertFalse(any('/_reuse/' in a['to'] for a in self.plan['actions']))

    def test_vague_cached_confirmation_is_rejected_without_repeating_ai(self):
        original = copy.deepcopy(self.plan)
        stored = {}
        cache = SimpleNamespace(get=stored.get, put=lambda k, v: stored.__setitem__(k, v))
        self.build(cache)
        for key, value in stored.items():
            if key.startswith("file-reuse-confirm-v2:"):
                value["reason"] = "checkpoint evaluation → evaluate"
        self.plan = original
        report = self.build(cache)
        self.assertEqual(report["state"], "complete", report["errors"])
        self.assertEqual(report["verification_calls"], 0)
        self.assertEqual(report["model_calls"], 0)
        self.assertEqual(report["unsupported_matches"], 2)
        self.assertFalse(any(s['kind'] == 'project' for s in report['suggestions']))
        self.assertTrue(all("checkpoint evaluation" not in s["reason"] for s in report["suggestions"]))

    def test_malformed_confirmation_is_partial_not_an_assessed_negative(self):
        normal = self.model.ask
        def ask(tag, messages, schema, num_predict=1024):
            response = normal(tag, messages, schema, num_predict)
            if "useful" in schema["properties"]:
                value = json.loads(response["message"]["content"])
                value["source_changes_required"] = "false"
                response["message"]["content"] = json.dumps(value)
            return response
        self.model.ask = ask
        report = self.build()
        self.assertEqual(report["state"], "partial")
        self.assertEqual(report["verification_pending"], 2)
        self.assertTrue(report["errors"])
        self.assertFalse(any(a.get('reuse_id') and '/_reuse/' in a['to'] for a in self.plan['actions']))

    def test_language_port_is_not_automatically_copied_even_if_model_claims_compatibility(self):
        next(r for r in self.plan["files"] if r["path"] == "project-a/train.py")["language"] = "Java"
        report = self.build()
        self.assertTrue(any(s["file"] == "tools/loader.py" and s["target"].endswith("/project-a")
                            for s in report["runtime_exclusions"]))
        self.assertFalse(any(s["kind"] == "project" and s["source_file"] == "tools/loader.py" and s["target"] == "p-project-a"
                             for s in report["suggestions"]))
        self.assertFalse(any(a.get("reuse_id") and '/project-a/_reuse/' in a['to'] for a in self.plan['actions']))

    def test_confirmation_timeout_excludes_only_unconfirmed_copy_and_is_retryable(self):
        normal = self.model.ask
        def ask(tag, messages, schema, num_predict=1024):
            items = json.loads(messages[-1]["content"])["items"]
            confirmation = "useful" in schema["properties"]
            if confirmation and items[0]["target"]["id"] == "p-project-a":
                raise AIError("Timed out", "timeout")
            return normal(tag, messages, schema, num_predict)
        self.model.ask = ask
        original = copy.deepcopy(self.plan)
        stored = {}
        cache = SimpleNamespace(get=stored.get, put=lambda k, v: stored.__setitem__(k, v))
        report = self.build(cache)
        self.assertEqual(report["state"], "partial")
        self.assertEqual(report["verification_pending"], 1)
        self.assertEqual(report["deferred_pairs"], 1)
        matches = [s for s in report["suggestions"] if s["kind"] == "project"]
        pending = next(s for s in matches if s["target"] == "p-project-a")
        included = next(s for s in matches if s["target"] == "p-project-b")
        self.assertEqual(pending["state"], "unverified")
        self.assertEqual(included["state"], "accepted")
        self.assertFalse(any(a.get("reuse_id") == pending["id"] for a in self.plan["actions"]))
        self.plan = original
        self.model.ask = normal
        retried = self.build(cache)
        self.assertEqual(retried["state"], "complete", retried["errors"])
        self.assertEqual(retried["verification_calls"], 1)
        self.assertEqual(retried["model_calls"], 1)

    def test_framework_port_is_not_automatically_copied(self):
        next(r for r in self.plan["files"] if r["path"] == "tools/loader.py")["dependencies"] = ["torch", "csv"]
        next(r for r in self.plan["files"] if r["path"] == "project-a/train.py")["dependencies"] = ["tensorflow", "csv"]
        report = self.build()
        excluded = next(s for s in report["runtime_exclusions"] if s["file"] == "tools/loader.py" and s["target"].endswith("/project-a"))
        self.assertIn("framework", excluded["reason"])
        self.assertFalse(any(a.get("reuse_id") and '/project-a/_reuse/' in a['to'] for a in self.plan['actions']))

    def test_cached_source_labels_survive_runtime_exclusion_without_more_ai(self):
        original = copy.deepcopy(self.plan)
        stored = {}
        cache = SimpleNamespace(get=stored.get, put=lambda k, v: stored.__setitem__(k, v))
        self.build(cache)
        self.plan = original
        for record in self.plan["files"]:
            if record["path"].startswith(("project-a/", "project-b/")):
                record["language"] = "Java"
        report = self.build(cache)
        self.assertEqual(report["model_calls"], 0)
        self.assertTrue(report["runtime_exclusions"])
        self.assertTrue(any(s["kind"] == "topic" and s["source_file"] == "tools/loader.py" for s in report["suggestions"]))
        self.assertFalse(any(s["kind"] == "project" and s["source_file"] == "tools/loader.py" for s in report["suggestions"]))

    def test_legacy_topic_cache_is_reused_with_current_placement_guards(self):
        for unit in self.plan["units"]:
            unit["kind"] = "single"
        _, _, tasks, _, records = discover(self.plan)
        taxonomy = {"version": "1", "topics": [{"id": t["id"], "description": t["description"]} for t in TOPICS["topics"]]}
        stored = {}
        for task in tasks:
            item, _, snapshot = context(task, records)
            key = "file-reuse-v1:" + fingerprint({"prompt": TOPIC_CACHE_PROMPT, "model": "scripted",
                       "topics": taxonomy, "format": DECISION, "snapshot": snapshot})
            stored[key] = {"file": item["file"], "verdict": "incompatible", "reason": "No project match.",
                           "source_evidence": "S1", "target_evidence": "none",
                           "topics": [{"topic": "other", "reason": "No specific topic fits."}]}
        cache = SimpleNamespace(get=stored.get, put=lambda k, v: stored.__setitem__(k, v))
        report = self.build(cache)
        self.assertEqual(report["state"], "complete")
        self.assertEqual(report["model_calls"], 0)
        self.assertEqual(report["cache_hits"], 5)
        self.assertFalse(report["suggestions"])

    def test_project_evidence_preserves_later_outputs_despite_long_input_lists(self):
        target = next(r for r in self.plan["files"] if r["path"] == "project-a/train.py")
        target["inputs"] = ["Long input description " * 30, "Another long parameter description " * 20]
        target["outputs"] = ["Printed dataset shapes", "Model summary", "Test loss and test accuracy"]
        _, _, tasks, _, records = discover(self.plan)
        task = next(t for t in tasks if t["project"] and t["project"]["unit"]["id"] == "p-project-a"
                    and t["source"]["record"]["path"] == "tools/loader.py")
        item, _, _ = context(task, records)
        facts = item["target"]["files"][0]["facts"]
        self.assertEqual(len(item["target"]["files"]), 1)
        self.assertTrue(any(f["text"].startswith("Outputs:") and "test accuracy" in f["text"] for f in facts))

    def test_tool_purpose_evidence_cache_survives_contract_display_improvement(self):
        for record in self.plan["files"]:
            if record["type"] == "script":
                record["role"] = "tool"
        _, _, tasks, _, records = discover(self.plan)
        taxonomy = {"version": "1", "topics": [{"id": t["id"], "description": t["description"]} for t in TOPICS["topics"]]}
        stored = {}
        for task in tasks:
            item, _, snapshot = context(task, records, legacy=True)
            key = "file-reuse-v1:" + fingerprint({"prompt": PROMPT, "model": "scripted", "topics": taxonomy,
                                                 "format": DECISION, "snapshot": snapshot})
            stored[key] = {"file": item["file"], "verdict": "incompatible", "reason": "No concrete additional task.",
                           "source_evidence": "S1", "target_evidence": "T1", "topics": []}
        cache = SimpleNamespace(get=stored.get, put=lambda k, v: stored.__setitem__(k, v))
        report = self.build(cache)
        self.assertEqual(report["state"], "complete", report["errors"])
        self.assertEqual(report["model_calls"], 0)
        self.assertEqual(report["assessed_pairs"], len(tasks))


if __name__ == "__main__":
    unittest.main()
