"""Offline tests: scripted model responses, real temporary SQLite/files/Git; no Ollama calls."""
import copy
import asyncio
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import tempfile
import sys
import types
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from ai.common import AIError
from ai.organize import page_html, plan_for, topic_key
from ai.reuse import (assess_pair, automatic_reuse, checked_answer, copy_actions, fast_client, finish_suggestion,
                      profiles, project_candidates, shortlist, topic_suggestions)
from Services.ArtifactsService import ArtifactsService
from Services.AnalysisService import AnalysisService
from Services.ConfigService import validate
from Services.ExecutionService import ExecutionError, ExecutionService
from Services.OrganizationService import OrganizationService, plan_digest
from Services.ScanService import ScanService
from Services.VersionService import VersionService


TOPICS = {"version": 1, "topics": [
    {"id": "machine-learning", "description": "Train models", "hints": ["training", "model"]},
    {"id": "data-processing", "description": "Process datasets", "hints": ["csv", "dataset"]},
    {"id": "other", "description": "Other tools", "hints": []}]}


class Settings:
    def __init__(self, root):
        self.values = validate({"scanning": {"root_paths": [str(root / "watched")]},
                                "organization": {"library_path": str(root / "library")}})

    def get(self, section, key=None, default=None):
        values = self.values.get(section, {})
        return values if key is None else values.get(key, default)


def evidence(profile):
    return {"group": profile["id"], "file": profile["files"][0]["path"],
            "quote": "reads csv rows for training"}


def answer(source, targets):
    item = {"target": "data-processing", "verdict": "compatible", "reason": "Loads CSV datasets for reuse.",
            "requirements": ["Configure the input path."], "unknowns": ["Not execution-tested."],
            "evidence": [evidence(source)]}
    project = {**copy.deepcopy(item), "target": targets[0]["id"], "verdict": "adaptation-needed",
               "requirements": ["Import the copied loader and check column names."],
               "evidence": [evidence(source), evidence(targets[0])]} if targets else None
    return {"topics": [] if source["topic"] == "data-processing" else [item],
            "projects": [project] if project else []}


class ScriptedModel:
    def __init__(self):
        self.calls = 0

    def ask(self, tag, messages, schema, num_predict=256):
        self.calls += 1
        if "decisions" in schema["properties"]:
            payload = json.loads(messages[-1]["content"])
            value = {"decisions": [{"file": item["file"], "verdict": "incompatible", "reason": "Topic placement only.",
                     "source_evidence": "S1", "target_evidence": "none" if item["target"] is None else "T1",
                     "topics": [{"topic": "data-processing", "reason": "CSV processing"}]}
                     for item in payload["items"]]}
        elif "verdict" in schema["properties"]:
            payload = json.loads(messages[-1]["content"])
            value = {"target": payload["target"]["id"], "verdict": "adaptation-needed",
                     "reason": "The loader handles the target's CSV input.",
                     "requirements": ["Import the loader and verify the column names."], "unknowns": ["Not run-tested."],
                     "source_evidence": payload["source"]["files"][0]["facts"][0]["id"],
                     "target_evidence": payload["target"]["files"][0]["facts"][0]["id"]}
        else:
            value = {"topic": "machine-learning", "folder_name": "csv-loader", "reason": "Processes training data.",
                     "additional_topics": [{"topic": "data-processing", "reason": "Reusable CSV processing."}]}
        return {"done": True, "done_reason": "stop", "message": {"content": json.dumps(value)}}


class ReuseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        watched = self.root / "watched"
        watched.mkdir()
        self.settings = Settings(self.root)
        self.artifacts = ArtifactsService(str(self.root / "records.sqlite3"))
        self.scan = ScanService(self.settings, None, self.artifacts)
        self.org = OrganizationService(self.settings, self.artifacts, SimpleNamespace(), self.scan)
        self.plan = {"created_at": "2026-10-23T00:00:00Z", "summary": {"files": 2, "types": {"script": 2},
                     "sensitive": 0, "duplicates": 0, "folders": 2}, "files": [], "units": [], "loose": [], "actions": []}
        for index, name in enumerate(("loader.py", "train.py"), 1):
            code = f"def load_{index}(path):\n    return open(path).read().splitlines()\n"
            (watched / name).write_bytes(code.encode())
            record = {"path": name, "root_folder": str(watched), "sha256": hashlib.sha256(code.encode()).hexdigest(),
                      "size": len(code), "type": "script", "role": "module", "language": "Python", "sensitive": False,
                      "sensitive_reason": None, "duplicate_of": None, "explained": True, "confidence": .85,
                      "summary": "reads csv rows for training", "dependencies": ["csv"], "reasons": []}
            folder = f"machine-learning/project-{index}"
            unit = {"id": f"u{index}", "root_folder": str(watched), "root": ".", "members": [name], "kind": "single",
                    "duplicate_of": None, "folder": folder, "topic": "machine-learning", "topic_confidence": .9,
                    "name": f"project-{index}", "topic_note": "AI and keyword hints agree."}
            self.plan["files"].append(record)
            self.plan["units"].append(unit)
            self.plan["actions"].append({"action": "copy", "unit": unit["id"], "from": name,
                                         "from_path": str(watched / name), "to": folder + "/" + name})
        self.model = ScriptedModel()

    def save(self):
        self.plan["plan_id"] = plan_digest(self.plan)
        with self.org._connect() as db:
            db.execute("INSERT OR REPLACE INTO organization_plans VALUES (1, ?, ?)",
                       (self.plan["created_at"], json.dumps(self.plan)))

    def test_partial_plan_warns_on_page_and_survives_service_restart(self):
        self.plan["topics_from"] = "AI, checked by keyword hints"
        self.plan["reuse"] = {"state": "partial", "suggestions": [], "errors": [],
                              "assessed_pairs": 1, "candidate_pairs": 3}
        self.save()
        self.assertIn("Reuse analysis is incomplete", page_html(self.plan, TOPICS))
        reopened = OrganizationService(self.settings, self.artifacts, SimpleNamespace(), self.scan)
        self.assertEqual(reopened.status()["state"], "partial")

    def test_page_distinguishes_in_progress_build_from_previous_saved_plan(self):
        page = page_html(self.plan, TOPICS, {"state": "building", "progress": "Checking <file>"})
        self.assertIn("Building a new plan", page)
        self.assertIn("previous saved plan", page)
        self.assertIn("Checking &lt;file&gt;", page)
        self.assertIn('http-equiv="refresh"', page)
        self.assertNotIn('http-equiv="refresh"', page_html(self.plan, TOPICS))

    def make_suggestions(self):
        suggestions = []
        for source in profiles(self.plan).values():
            targets = [p for p in profiles(self.plan).values() if p["id"] != source["id"]]
            for item in checked_answer(answer(source, targets), source, targets, TOPICS):
                suggestions.append(finish_suggestion(self.plan, item))
        self.plan["reuse"] = {"state": "on-demand", "suggestions": suggestions}
        self.save()
        return self.plan["reuse"]["suggestions"]

    def test_model_assesses_topics_and_projects_and_quotes_are_verified(self):
        found = self.make_suggestions()
        self.assertEqual(len(found), 4)
        self.assertEqual({s["kind"] for s in found}, {"topic", "project"})
        self.assertTrue(all(s["state"] == "pending" for s in found))
        self.assertTrue(all(s["destinations"] for s in found))
        self.assertEqual(len(self.plan["actions"]), 2)  # pending suggestions never copy anything
        source, target = profiles(self.plan).values()
        bad = answer(source, [target])
        bad["projects"][0]["evidence"][1]["quote"] = "invented compatibility proof"
        with self.assertRaisesRegex(ValueError, "does not match"):
            checked_answer(bad, source, [target], TOPICS)
        bad = answer(source, [target])
        bad["projects"][0]["evidence"] = [evidence(source)]
        with self.assertRaisesRegex(ValueError, "source and target"):
            checked_answer(bad, source, [target], TOPICS)
        bad = answer(source, [target])
        bad["projects"][0]["target"] = "../../outside"
        with self.assertRaisesRegex(ValueError, "unknown target"):
            checked_answer(bad, source, [target], TOPICS)

    def test_cache_reuse_and_content_invalidation(self):
        storage = {}
        cache = SimpleNamespace(get=storage.get, put=lambda k, v: storage.__setitem__(k, v))
        first = assess_pair(self.plan, "u1", "u2", self.model, "test", cache)
        self.assertEqual(self.model.calls, 1)
        self.assertEqual(first, assess_pair(self.plan, "u1", "u2", self.model, "test", cache))
        self.assertEqual(self.model.calls, 1)
        self.plan["files"][0]["sha256"] = "a" * 64
        assess_pair(self.plan, "u1", "u2", self.model, "test", cache)
        self.assertEqual(self.model.calls, 2)

    def test_numbered_evidence_preserves_punctuation_unicode_and_short_facts(self):
        text = 'CSV "rows"\nwith café and \\paths'
        self.plan["files"][0]["summary"] = text
        self.plan["files"][1]["summary"] = "I/O"
        result = assess_pair(self.plan, "u1", "u2", self.model, "test")
        citations = result["suggestion"]["evidence"]
        self.assertEqual(citations[0], {"evidence_id": "S1", "group": "u1", "file": "loader.py", "quote": "Purpose: " + text})
        self.assertEqual(citations[1]["quote"], "Purpose: I/O")
        self.assertEqual(citations[1]["evidence_id"], "T1")

    def test_unknown_wrong_side_and_paraphrased_evidence_ids_are_rejected(self):
        original = self.model.ask
        for replacement, expected in (("S999", "Unknown source"), ("T1", "other group"),
                                      ("This script reads CSV rows", "Unknown source")):
            def bad(*args, **kwargs):
                response = original(*args, **kwargs)
                value = json.loads(response["message"]["content"])
                value["source_evidence"] = replacement
                response["message"]["content"] = json.dumps(value)
                return response
            with self.subTest(replacement=replacement), patch.object(self.model, "ask", side_effect=bad):
                with self.assertRaisesRegex(ValueError, expected):
                    assess_pair(self.plan, "u1", "u2", self.model, "test")

    def test_new_pair_cache_does_not_reuse_legacy_quote_answers(self):
        storage = {"reuse-pair-v2:old": {"evidence": [{"quote": "mismatched quote"}]}}
        cache = SimpleNamespace(get=storage.get, put=lambda k, v: storage.__setitem__(k, v))
        result = assess_pair(self.plan, "u1", "u2", self.model, "test", cache)
        self.assertEqual(self.model.calls, 1)
        key = next(key for key in storage if key.startswith("reuse-pair-v3:"))
        self.assertEqual(storage[key]["source_evidence"], "S1")
        self.assertNotIn("evidence", storage[key])
        self.assertEqual(assess_pair(self.plan, "u1", "u2", self.model, "test", cache), result)
        self.assertEqual(self.model.calls, 1)

    def test_ai_disabled_and_model_failure_are_explicit(self):
        with self.assertRaises(AIError):
            assess_pair(self.plan, "u1", "u2", None, None)
        def fail(*args, **kwargs):
            raise AIError("No local model", "connection_error")
        with self.assertRaises(AIError):
            assess_pair(self.plan, "u1", "u2", SimpleNamespace(ask=fail), "test")

    def test_sensitive_unexplained_and_large_groups_are_not_sent_to_model(self):
        self.plan["files"][0]["sensitive"] = True
        self.plan["files"][1]["explained"] = False
        self.assertEqual(profiles(self.plan), {})
        report = topic_suggestions(self.plan)
        self.assertEqual(report["eligible_groups"], 0)
        self.assertEqual(self.model.calls, 0)

    def test_incomplete_answers_and_unsafe_suggestions_never_enter_the_plan(self):
        source, target = profiles(self.plan).values()
        value = answer(source, [target])
        value["projects"][0]["verdict"] = "incompatible"
        self.assertEqual(len(checked_answer(value, source, [target], TOPICS)), 1)
        def incomplete(*args, **kwargs):
            return {"done": True, "done_reason": "length", "message": {"content": json.dumps(value)}}
        with self.assertRaisesRegex(ValueError, "incomplete"):
            assess_pair(self.plan, "u1", "u2", SimpleNamespace(ask=incomplete), "test")

    def test_existing_code_in_target_not_recommended_and_secrets_redacted(self):
        self.plan["files"][0]["summary"] += " password = 'actual-secret-value'"
        source, target = profiles(self.plan).values()
        self.assertNotIn("actual-secret-value", source["files"][0]["description"])
        target["code_hashes"] = source["code_hashes"]
        self.assertEqual(shortlist(source, [source, target]), [])

    def test_group_layout_including_helpers_and_license_is_preserved(self):
        source = self.plan["units"][0]
        source["kind"], source["root"] = "linked", "project"
        source["members"] = ["project/lib/loader.py", "project/LICENSE"]
        self.plan["actions"][0].update(to=source["folder"] + "/lib/loader.py", **{"from": "project/lib/loader.py"})
        self.plan["actions"].append({"action": "copy", "unit": source["id"], "from": "project/LICENSE",
                                     "to": source["folder"] + "/LICENSE"})
        proposal = {"id": "a" * 20, "source": source["id"], "target": "u2", "kind": "project",
                    "reason": "Shared loader", "verdict": "adaptation-needed", "requirements": [], "unknowns": []}
        actions = copy_actions(self.plan, proposal)
        self.assertTrue(any(a["to"].endswith("/lib/loader.py") for a in actions))
        self.assertTrue(any(a["to"].endswith("/LICENSE") for a in actions))
        self.assertEqual(self.plan["actions"][0]["to"], source["folder"] + "/lib/loader.py")

    def test_review_persistence_reject_stale_plan_and_operation_lock(self):
        proposal = self.make_suggestions()[0]
        old_id = self.plan["plan_id"]
        reviewed = self.org.review_reuse(old_id, proposal["id"], True)
        self.assertNotEqual(reviewed["plan_id"], old_id)
        with self.assertRaisesRegex(ValueError, "plan changed"):
            self.org.review_reuse(old_id, proposal["id"], False)
        reopened = OrganizationService(self.settings, self.artifacts, SimpleNamespace(), self.scan)
        self.assertEqual(reopened.latest()["reuse"]["suggestions"][0]["state"], "accepted")
        self.org.operation_lock.acquire()
        try:
            with self.assertRaisesRegex(ValueError, "in progress"):
                self.org.review_reuse(reviewed["plan_id"], proposal["id"], False)
        finally:
            self.org.operation_lock.release()
        rejected = self.org.review_reuse(reviewed["plan_id"], proposal["id"], False)
        self.assertEqual(rejected["suggestion"]["state"], "rejected")
        self.assertEqual(len(self.org.latest()["actions"]), 2)

    def test_topic_and_project_copies_preflight_execute_git_and_undo(self):
        if not shutil.which("git"):
            self.skipTest("Git is required for this integration test.")
        suggestions = self.make_suggestions()
        current = self.plan["plan_id"]
        # Historical project plans still support execution/undo even though new approvals are retired.
        for proposal in suggestions[:2]:
            self.plan["actions"].extend(copy_actions(self.plan, proposal))
        self.save()
        current = self.plan["plan_id"]
        version = VersionService(self.settings)
        execution = ExecutionService(self.settings, self.artifacts, self.org, self.scan, version)
        preview = execution.preflight(current)
        self.assertEqual(preview["summary"].get("would-copy"), 6, preview["results"])  # copies and notes
        self.assertFalse((self.root / "library").exists())
        report = execution.execute(current)
        self.assertEqual(report["state"], "complete")
        self.assertNotIn("failed", report["summary"])
        self.assertNotIn("conflict", report["summary"])
        self.assertTrue(report["commits"][0]["commit"])
        self.assertEqual(len(execution.placements()), 2)  # reuse copies never displace primary locations
        reuse_copies = [r for r in report["results"] if r.get("reuse_id") and r["action"] == "copy"]
        self.assertEqual(len(reuse_copies), 2)
        for item in reuse_copies:
            self.assertEqual((self.root / "library" / item["to"]).read_bytes(), Path(item["from_path"]).read_bytes())
        self.assertTrue(any("/_reuse/" in r["to"] for r in reuse_copies))
        edited = self.root / "library" / reuse_copies[0]["to"]
        edited.write_text("user edit", encoding="utf-8")
        undone = execution.undo(report["run_id"])
        self.assertEqual(edited.read_text(), "user edit")
        self.assertTrue(any(item["status"] == "retained" for item in undone["undo_results"]))
        self.assertTrue(all(Path(r["from_path"]).exists() for r in reuse_copies))

    def test_changed_source_skips_reuse_copies(self):
        proposal = self.make_suggestions()[0]
        current = self.org.review_reuse(self.plan["plan_id"], proposal["id"], True)["plan_id"]
        Path(self.plan["actions"][0]["from_path"]).write_text("changed after review", encoding="utf-8")
        execution = ExecutionService(self.settings, self.artifacts, self.org, self.scan)
        preview = execution.preflight(current)
        copies = [r for r in preview["results"] if r["action"] == "copy" and r.get("reuse_id")]
        self.assertEqual(copies[0]["status"], "skipped")
        self.assertIn("Source changed", copies[0]["reason"])

    def test_html_escapes_model_text_and_supports_older_plans(self):
        proposal = self.make_suggestions()[0]
        proposal["reason"] = '<script>alert("injected")</script>'
        page = page_html(self.plan, TOPICS)
        self.assertNotIn('<script>', page)
        self.assertIn('&lt;script&gt;', page)
        self.assertIn('/organization/reuse/review', page)
        self.plan.pop("reuse")
        self.assertIn("Proposed folders", page_html(self.plan, TOPICS))

    def test_build_runs_reuse_assessment_with_real_scanner_records(self):
        self.scan.process(full_scan="test")
        analysis = SimpleNamespace(tag="test", topic_model=lambda: (self.model, "test", "digest"),
            analysis_for=lambda _: {"status": "done", "analysis": {"purpose": "reads csv rows for training",
                "inputs": ["CSV path"], "outputs": ["Rows"], "dependencies": ["csv"]}})
        org = OrganizationService(self.settings, self.artifacts, analysis, self.scan)
        org.topics = lambda: TOPICS
        result = org.build()
        self.assertEqual(result["reuse"]["state"], "complete")
        self.assertEqual(self.model.calls, 3)  # two topic requests plus one batched file assessment
        self.assertEqual(len(result["reuse"]["suggestions"]), 2)
        self.assertEqual(result["summary"]["accepted_reuse"], 2)
        self.assertEqual(result["summary"]["reuse_copy_files"], 2)
        self.assertEqual(result["plan_id"], plan_digest(result))
        self.assertEqual(result["files"][0]["inputs"], ["CSV path"])
        self.assertTrue(result["files"][0]["interfaces"])

    def test_old_topic_cache_is_reused_without_any_ai_request(self):
        record = copy.deepcopy(self.plan["files"][0])
        unit = {"kind": "single", "root": ".", "members": [record["path"]]}
        key = topic_key(unit, {record["path"]: record}, TOPICS, "test")
        cached = {key: {"topic": "machine-learning", "reason": "Original cached answer.",
                        "folder_name": "old-loader", "seconds": 20}}
        cache = SimpleNamespace(get=cached.get, put=lambda k, v: cached.__setitem__(k, v))
        plan = plan_for([record], {}, {}, self.model, "test", TOPICS, topic_cache=cache, progress=lambda _: None)
        self.assertEqual(self.model.calls, 0)
        self.assertEqual(plan["units"][0]["name"], "old-loader")
        self.assertEqual(plan["units"][0]["additional_topics"], [])

    def test_project_checks_are_retired_without_changing_saved_plan(self):
        self.save()
        original = self.org.latest()
        self.assertEqual(self.org.reuse_candidates()["candidates"], [])
        with self.assertRaisesRegex(ValueError, "retired"):
            self.org.start_reuse_check(self.plan["plan_id"], "u1", "u2")
        self.assertEqual(self.org.latest(), original)
        self.assertEqual(self.model.calls, 0)

    def test_foreground_priority_waits_for_inflight_work_and_restores_queue(self):
        analysis = AnalysisService.__new__(AnalysisService)
        analysis._lock = threading.Lock()
        analysis._inference_lock = threading.Lock()
        analysis._foreground_waiters = 0
        analysis._wake = threading.Event()
        inflight, finish, interactive = threading.Event(), threading.Event(), threading.Event()
        def work():
            inflight.set()
            finish.wait(3)
            return True
        analysis._background_step = work
        background = threading.Thread(target=analysis.step)
        background.start()
        self.assertTrue(inflight.wait(1))
        def request():
            with analysis.foreground():
                interactive.set()
                self.assertFalse(analysis.step())
        foreground = threading.Thread(target=request)
        foreground.start()
        self.assertFalse(interactive.wait(.05))  # in-flight work is not interrupted
        finish.set()
        background.join(2)
        foreground.join(2)
        self.assertTrue(interactive.is_set())
        self.assertEqual(analysis._foreground_waiters, 0)
        with self.assertRaises(RuntimeError):
            with analysis.foreground():
                raise RuntimeError("interactive failure")
        self.assertEqual(analysis._foreground_waiters, 0)
        analysis._background_step = lambda: True
        self.assertTrue(analysis.step())

    def test_interactive_client_budgets_do_not_change_background_settings(self):
        client = SimpleNamespace(config={"num_ctx": 32768, "num_predict": 2048, "timeout_seconds": 1800})
        fast = fast_client(client)
        self.assertEqual(fast.config, {"num_ctx": 4096, "num_predict": 512, "timeout_seconds": 120})
        self.assertEqual(client.config["num_ctx"], 32768)

    def test_review_http_contract_success_validation_and_stale_id(self):
        from fastapi import FastAPI
        proposal = self.make_suggestions()[0]
        # Import the actual router with a temporary container to avoid touching the user's database.
        container = types.ModuleType("Services.ServiceContainer")
        container.services = SimpleNamespace(organization_service=self.org)
        spec = importlib.util.spec_from_file_location("_reuse_test_controller",
            Path(__file__).resolve().parents[1] / "Controllers" / "OrganizationController.py")
        module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {"Services.ServiceContainer": container, "_reuse_test_controller": module}):
            spec.loader.exec_module(module)
            app = FastAPI()
            app.include_router(module.router)

            async def post(body, path="/organization/reuse/review"):
                output = []
                received = False
                async def receive():
                    nonlocal received
                    if not received:
                        received = True
                        return {"type": "http.request", "body": json.dumps(body).encode(), "more_body": False}
                    return {"type": "http.disconnect"}
                async def send(message):
                    output.append(message)
                await app({"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
                           "scheme": "http", "method": "POST", "path": path,
                           "raw_path": path.encode(), "query_string": b"",
                           "headers": [(b"content-type", b"application/json")], "server": ("test", 80),
                           "client": ("test", 1)}, receive, send)
                return output[0]["status"], json.loads(b"".join(m.get("body", b"") for m in output[1:]))

            request = {"plan_id": self.plan["plan_id"], "suggestion_id": proposal["id"], "accept": True}
            status, response = asyncio.run(post(request))
            self.assertEqual(status, 200)
            self.assertNotEqual(response["plan_id"], request["plan_id"])
            self.assertEqual(asyncio.run(post(request))[0], 409)
            self.assertEqual(asyncio.run(post({"accept": True}))[0], 422)
            self.org.analysis_service = SimpleNamespace(topic_model=lambda: (self.model, "test", "digest"))
            current = self.org.latest()["plan_id"]
            status, response = asyncio.run(post({"plan_id": current, "source": "u1", "target": "u2"},
                                               "/organization/reuse/check"))
            self.assertEqual(status, 409)
            self.assertIn("retired", response["detail"])


if __name__ == "__main__":
    unittest.main()
