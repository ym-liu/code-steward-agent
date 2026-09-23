"""Meaningful offline regression tests; never starts the production scanner."""
from contextlib import redirect_stdout, redirect_stderr
import csv
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from ai_lab.cli import main
from ai_lab.core import (ROOT, LabError, collect_cases, load_config, messages_for,
                         snapshot, validate_analysis)
from ai_lab.ollama_client import OllamaClient
from ai_lab.reporting import regenerate, save_json
from ai_lab.runner import DemoClient, result_for, run_experiment


def answer():
    return {"purpose": "Print a value", "inputs": [], "outputs": ["standard output"],
            "dependencies": [], "unknowns": [], "evidence": [{"line": 1, "claim": "Print call"}]}


class FixtureHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def respond(self, value, code=200):
        payload = json.dumps(value).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        state = self.server.state
        if self.path == "/api/version":
            return self.respond({"version": "fixture-1"})
        if self.path == "/api/tags":
            return self.respond({"models": [{"name": m, "digest": "sha256:" + m, "size": 400000000} for m in state["installed"]]})
        if self.path == "/api/ps":
            return self.respond({"models": [{"name": m, "size": 512 * 1024**2, "size_vram": state["vram"]} for m in state["running"]]})
        self.respond({"error": "not found"}, 404)

    def do_POST(self):
        state = self.server.state
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        state["requests"].append((self.path, body))
        tag = body["model"]
        canonical = tag if ":" in tag else tag + ":latest"
        if self.path == "/api/show":
            return self.respond({"details": {"parameter_size": "test"}, "remote_host": state.get("remote_host")})
        if self.path == "/api/pull":
            state["installed"].append(canonical)
            if state.get("pull_failure"):
                return self.respond({"status": "downloading"})
            return self.respond({"status": "success"})
        if self.path == "/api/generate":
            state["running"] = [m for m in state["running"] if m != canonical]
            return self.respond({"done": True, "done_reason": "unload"})
        if self.path == "/api/chat":
            state["running"] = [canonical]
            state["chat_count"] += 1
            content = state["outputs"].pop(0) if state["outputs"] else json.dumps(answer())
            if state.get("chat_error"):
                return self.respond({"error": "fixture model unavailable"}, 500)
            return self.respond({"done": True, "done_reason": state.get("done_reason", "stop"),
                                 "message": {"content": content}, "load_duration": 100000000,
                                 "total_duration": 1100000000, "prompt_eval_count": 100,
                                 "eval_count": 50, "eval_duration": 1000000000})
        self.respond({"error": "not found"}, 404)


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = load_config()

    def write(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def test_snapshots_read_text_extract_facts_and_never_execute(self):
        marker = self.root / "should-not-exist"
        path = self.write("test.py", f"from pathlib import Path\nPath({str(marker)!r}).touch()\n")
        case = snapshot(path, path.name, 2500)
        self.assertEqual(case["facts"]["imports"], ["pathlib"])
        self.assertFalse(marker.exists())
        old_hash = case["source_hash"]
        path.write_text("print(2)", encoding="utf-8")
        self.assertIn("touch()", case["source"])
        self.assertNotEqual(snapshot(path, path.name, 2500)["source_hash"], old_hash)

    def test_oversize_binary_and_empty_inputs_are_not_truncated_or_analyzed(self):
        for name, data in [("long.py", b"x" * 10001), ("empty.py", b""), ("binary.py", b"a\0b"), ("bad.py", b"\xff\xfe\xff")]:
            with self.subTest(name=name):
                path = self.root / name
                path.write_bytes(data)
                self.assertIn("input_error", snapshot(path, name, 2500))

    def test_folder_selection_excludes_generated_and_dependency_folders(self):
        self.write("a.py", "print(1)")
        self.write("nested/b.sh", "echo hello")
        self.write(".git/private.py", "secret")
        self.write("ai-results/private.py", "secret")
        self.write("node_modules/private.js", "secret")
        self.write("data.bin", "ignored")
        cases = collect_cases(self.root, self.config)
        self.assertEqual([c["name"] for c in cases], ["a.py", "nested/b.sh"])

    def test_folder_limit_fails_instead_of_silently_selecting_a_subset(self):
        self.write("a.py", "print(1)")
        self.write("b.py", "print(2)")
        self.config["max_files"] = 1
        with self.assertRaisesRegex(LabError, "Too many"):
            collect_cases(self.root, self.config)

    def test_reference_answers_never_enter_model_messages(self):
        cases = collect_cases(None, self.config)
        self.assertEqual(len(cases), 6)
        cases[0]["rubric"] = {"secret_answer": "MUST_NOT_BE_IN_PROMPT"}
        rendered = json.dumps(messages_for(cases[0]))
        self.assertNotIn("MUST_NOT_BE_IN_PROMPT", rendered)
        self.assertIn("csv.DictReader", rendered)

    def test_schema_and_evidence_bounds_are_separate(self):
        value = answer()
        value["evidence"][0]["line"] = 99
        _, json_ok, schema_ok, errors = validate_analysis(json.dumps(value), 1)
        self.assertTrue(json_ok and schema_ok)
        self.assertIn("outside", errors[0])

    def test_schema_rejects_boolean_lines_extra_fields_and_wrong_types(self):
        for mutation in (lambda v: v["evidence"][0].update(line=True),
                         lambda v: v.update(extra="invented"), lambda v: v.update(inputs="file")):
            value = answer()
            mutation(value)
            self.assertFalse(validate_analysis(json.dumps(value), 2)[2])

    def test_duplicate_keys_and_markdown_wrapped_json_are_invalid(self):
        self.assertFalse(validate_analysis('{"purpose":null,"purpose":"x"}', 1)[1])
        self.assertFalse(validate_analysis("```json\n{}\n```", 1)[1])

    def test_no_evidence_or_unexplained_unknown_is_not_valid(self):
        value = answer()
        value["evidence"] = []
        self.assertTrue(validate_analysis(json.dumps(value), 1)[3])
        value["purpose"] = None
        self.assertTrue(validate_analysis(json.dumps(value), 1)[3])
        value["unknowns"] = ["Missing implementation"]
        self.assertFalse(validate_analysis(json.dumps(value), 1)[3])

    def test_invalid_numeric_config_fails_before_inference(self):
        self.config["num_thread"] = True
        config_path = self.root / "config.json"
        save_json(config_path, self.config)
        with self.assertRaises(LabError):
            load_config(config_path)

    def test_missing_config_key_returns_an_actionable_error(self):
        del self.config["base_url"]
        path = self.root / "config.json"
        save_json(path, self.config)
        with self.assertRaisesRegex(LabError, "Config keys"):
            load_config(path)

    def test_external_endpoints_and_credentials_are_rejected(self):
        for address in ("https://example.com", "http://127.0.0.1.evil:11434", "http://user:pass@localhost", "http://localhost/path", "http://localhost?target=x"):
            with self.subTest(address=address), self.assertRaises(LabError):
                OllamaClient(dict(self.config, base_url=address))


class HttpWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
        self.server.state = {"installed": ["fixture:latest"], "running": [], "vram": 0,
                             "requests": [], "outputs": [], "chat_count": 0}
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        def stop():
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(timeout=3)
        self.addCleanup(stop)
        self.config = load_config()
        self.config.update(base_url=f"http://127.0.0.1:{self.server.server_port}",
                           models={"one": "fixture:latest", "two": "fixture2:latest"}, default_model="one")
        self.client = OllamaClient(self.config)
        path = self.root / "a.py"
        path.write_text("print(1)\n", encoding="utf-8")
        self.cases = [snapshot(path, "a.py", 2500)]

    def run_lab(self, **kwargs):
        with redirect_stdout(io.StringIO()):
            return run_experiment(self.client, self.config, kwargs.pop("models", ["fixture:latest"]),
                                  self.cases, self.root / "results", **kwargs)

    def test_real_http_adapter_sends_schema_cpu_limits_and_no_reference_answers(self):
        self.cases[0]["rubric"] = {"secret": "REFERENCE_ONLY"}
        folder, code = self.run_lab()
        self.assertEqual(code, 0)
        body = next(body for path, body in self.server.state["requests"] if path == "/api/chat")
        self.assertEqual(body["options"]["num_gpu"], 0)
        self.assertEqual(body["options"]["num_thread"], 2)
        self.assertFalse(body["stream"] or body["truncate"] or body["shift"])
        self.assertIn("required", body["format"])
        self.assertNotIn("REFERENCE_ONLY", json.dumps(body))
        result = json.loads((folder / "results.jsonl").read_text())
        self.assertTrue(result["cpu_verified"])
        self.assertEqual(result["ollama_allocated_mib"], 512)
        self.assertEqual(result["tokens_per_second"], 50)
        self.assertEqual(self.server.state["running"], [])
        self.assertEqual(json.loads((folder / "run.json").read_text())["ollama_version"], "fixture-1")

    def test_comparison_keeps_same_inputs_parameters_and_saves_distinct_model_digests(self):
        folder, code = self.run_lab(models=["fixture:latest", "fixture2:latest"], allow_pull=True, repeat=2)
        self.assertEqual(code, 0)
        rows = [json.loads(line) for line in (folder / "results.jsonl").read_text().splitlines()]
        self.assertEqual(len(rows), 4)
        requests = [body for path, body in self.server.state["requests"] if path == "/api/chat"]
        self.assertTrue(all(r["messages"] == requests[0]["messages"] and r["options"] == requests[0]["options"] for r in requests))
        self.assertNotEqual(rows[0]["model_digest"], rows[-1]["model_digest"])
        self.assertEqual(rows[0]["source_hash"], rows[-1]["source_hash"])

    def test_missing_model_does_not_download_without_pull(self):
        folder, code = self.run_lab(models=["missing:latest"])
        self.assertEqual(code, 1)
        self.assertFalse(any(path == "/api/pull" for path, _ in self.server.state["requests"]))
        self.assertIn("not installed", (folder / "summary.md").read_text())

    def test_incomplete_download_is_not_reported_successful(self):
        self.server.state["pull_failure"] = True
        with redirect_stdout(io.StringIO()), self.assertRaisesRegex(LabError, "before success"):
            self.client.pull("new:latest")

    def test_invalid_output_is_preserved_and_not_retried(self):
        self.server.state["outputs"] = ["not JSON"]
        folder, code = self.run_lab()
        self.assertEqual(code, 1)
        row = json.loads((folder / "results.jsonl").read_text())
        self.assertEqual(row["raw_output"], "not JSON")
        self.assertFalse(row["json_valid"])
        self.assertEqual(self.server.state["chat_count"], 1)

    def test_gpu_allocation_fails_cpu_run_but_is_allowed_in_auto_mode(self):
        self.server.state["vram"] = 1234
        _, code = self.run_lab()
        self.assertEqual(code, 1)
        self.config["device"] = "auto"
        _, code = self.run_lab()
        self.assertEqual(code, 0)

    def test_generation_limit_is_not_a_valid_analysis_even_when_json_is_complete(self):
        self.server.state["done_reason"] = "length"
        folder, code = self.run_lab()
        self.assertEqual(code, 1)
        self.assertIn("limit", (folder / "summary.md").read_text())

    def test_cloud_backed_model_is_rejected_before_source_is_sent(self):
        self.server.state["remote_host"] = "https://ollama.com"
        folder, code = self.run_lab()
        self.assertEqual(code, 1)
        self.assertEqual(self.server.state["chat_count"], 0)
        self.assertIn("Cloud-backed", (folder / "summary.md").read_text())

    def test_other_running_models_are_not_unloaded(self):
        self.server.state["running"] = ["someone-elses-model:latest"]
        _, code = self.run_lab()
        self.assertEqual(code, 1)
        self.assertEqual(self.server.state["running"], ["someone-elses-model:latest"])

    def test_timeout_stops_further_work_and_preserves_partial_results(self):
        with patch.object(self.client, "analyze", side_effect=LabError("fixture timeout", "timeout")):
            folder, code = self.run_lab(repeat=3)
        self.assertEqual(code, 1)
        metadata = json.loads((folder / "run.json").read_text())
        self.assertEqual(metadata["status"], "aborted")
        self.assertEqual(len((folder / "results.jsonl").read_text().splitlines()), 1)
        # Only the pre-batch unload ran. No new request is made after timeout.
        self.assertEqual(sum(p == "/api/generate" for p, _ in self.server.state["requests"]), 1)

    def test_keyboard_interrupt_keeps_reports_without_sending_another_model_request(self):
        with patch.object(self.client, "analyze", side_effect=KeyboardInterrupt):
            folder, code = self.run_lab(repeat=3)
        self.assertEqual(code, 130)
        self.assertEqual(json.loads((folder / "run.json").read_text())["status"], "interrupted")
        self.assertTrue((folder / "summary.md").exists())
        self.assertEqual(sum(p == "/api/generate" for p, _ in self.server.state["requests"]), 1)

    def test_manual_scores_survive_report_regeneration_and_aggregate(self):
        folder, _ = self.run_lab()
        review = folder / "review.csv"
        with review.open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            fields, rows = reader.fieldnames, list(reader)
        for key in rows[0]:
            if key.endswith("_score"):
                rows[0][key] = "2"
        rows[0]["notes"] = "Manual review with comma, preserved"
        with review.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
        saved = review.read_bytes()
        regenerate(folder)
        self.assertEqual(saved, review.read_bytes())
        self.assertIn("10.00 (1)", (folder / "summary.md").read_text())
        self.assertEqual(self.server.state["chat_count"], 1)

    def test_demo_has_no_real_timings_and_never_contacts_ollama(self):
        with redirect_stdout(io.StringIO()):
            folder, code = run_experiment(DemoClient(), self.config, ["DEMO-NOT-A-REAL-MODEL"],
                                          self.cases, self.root, demo=True)
        self.assertEqual(code, 0)
        self.assertEqual(self.server.state["requests"], [])
        row = json.loads((folder / "results.jsonl").read_text())
        self.assertIsNone(row["wall_ms"])
        self.assertIsNone(row["cpu_verified"])
        self.assertIn("DEMO ONLY", (folder / "summary.md").read_text())

    def test_command_line_works_from_another_directory_without_pip_dependencies(self):
        config_path = self.root / "config.json"
        save_json(config_path, self.config)
        command = [sys.executable, str(ROOT / "ai_test.py"), "run", "--config", str(config_path),
                   "--model", "one", "--input", str(self.root / "a.py"), "--output", str(self.root / "cli-output")]
        completed = subprocess.run(command, cwd=self.root, capture_output=True, text=True, timeout=20)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        folder = next((self.root / "cli-output").iterdir())
        self.assertTrue((folder / "review.csv").exists())

    def test_bad_config_returns_friendly_cli_error(self):
        path = self.root / "broken.json"
        path.write_text("{broken", encoding="utf-8")
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as errors:
            code = main(["run", "--config", str(path)])
        self.assertEqual(code, 1)
        self.assertIn("Cannot read JSON", errors.getvalue())
        self.assertNotIn("Traceback", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
