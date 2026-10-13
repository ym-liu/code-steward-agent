"""Offline streaming and display regressions. No real Ollama models are contacted."""
from contextlib import redirect_stdout
import io
import json
import threading
import unittest
from unittest.mock import patch

from ai_lab.common import LabError
from ai_lab.explain.ollama_client import OllamaClient
from ai_lab.testing.live_status import LiveStatus, SystemSampler
from tests import test_ai_lab as fixtures


def ndjson(chunks):
    return io.BytesIO(b"\n".join(json.dumps(chunk).encode() for chunk in chunks) + b"\n")


class StreamingTests(unittest.TestCase):
    def test_split_json_is_reassembled_and_final_actual_token_metrics_are_preserved(self):
        raw = json.dumps(fixtures.answer())
        chunks = [{"message": {"role": "assistant", "content": part}, "done": False}
                  for part in (raw[:12], raw[12:70], raw[70:])]
        chunks.append({"message": {"role": "assistant", "content": ""}, "done": True,
                       "done_reason": "stop", "eval_count": 73, "eval_duration": 2000000000})
        seen = []
        response = OllamaClient.read_chat_stream(ndjson(chunks), seen.append)
        self.assertEqual(response["message"]["content"], raw)
        self.assertEqual(response["eval_count"], 73)  # Not the four stream chunks.
        self.assertEqual(response["eval_duration"], 2000000000)
        self.assertEqual(len(seen), 4)

    def test_final_chunk_text_and_earlier_tool_calls_cannot_be_lost(self):
        chunks = [{"message": {"content": "a", "tool_calls": [{"function": {"name": "never-run"}}]}, "done": False},
                  {"message": {"content": "b"}, "done": True}]
        response = OllamaClient.read_chat_stream(ndjson(chunks), lambda chunk: None)
        self.assertEqual(response["message"]["content"], "ab")
        self.assertEqual(response["message"]["tool_calls"][0]["function"]["name"], "never-run")

    def test_transport_end_without_done_is_a_failure_even_with_complete_answer_json(self):
        chunks = [{"message": {"content": json.dumps(fixtures.answer())}, "done": False}]
        with self.assertRaises(LabError) as error:
            OllamaClient.read_chat_stream(ndjson(chunks), lambda chunk: None)
        self.assertEqual(error.exception.code, "response_error")

    def test_stream_errors_and_nontext_messages_are_rejected(self):
        for chunk in ({"error": "model failed"}, {"done": True},
                      {"message": {"content": 12}, "done": True}, ["invalid"]):
            with self.subTest(chunk=chunk), self.assertRaises(LabError):
                OllamaClient.read_chat_stream(ndjson([chunk]), lambda item: None)

    def test_stream_byte_limit_is_enforced_before_decoding(self):
        with self.assertRaisesRegex(LabError, "8 MiB"):
            OllamaClient.read_chat_stream(io.BytesIO(b"x" * (8 * 1024 * 1024 + 1)), lambda chunk: None)


class TerminalBuffer(io.StringIO):
    def isatty(self):
        return True


class FakeSampler:
    def sample(self):
        return {"cpu_percent": 34, "ram_used_gib": 6.2, "ram_total_gib": 16.0}

    def close(self):
        pass


class DisplayTests(unittest.TestCase):
    def test_status_is_one_line_and_live_speed_excludes_preparation_then_goes_idle(self):
        now = [0.0]
        output = TerminalBuffer()
        status = LiveStatus(FakeSampler(), output, clock=lambda: now[0])
        status.refresh()
        now[0] = 100.0
        self.assertIn("Preparing", status.text())
        for instant in (100.0, 100.5, 101.0):
            now[0] = instant
            status.on_chunk({"message": {"content": "PRIVATE MODEL TEXT"}})
        self.assertIn("~2.0 tok/s", status.text())
        self.assertIn("CPU 34%", status.text())
        self.assertIn("RAM 6.2/16.0 GiB", status.text())
        status.refresh()
        now[0] = 110.0
        self.assertIn("~0.0 tok/s", status.text())
        self.assertNotIn("PRIVATE", output.getvalue())
        self.assertNotIn("\n", output.getvalue())
        status.stop()

    def test_unavailable_resources_remain_unknown_and_never_block_the_model(self):
        sampler = FakeSampler()
        sampler.sample = lambda: (_ for _ in ()).throw(OSError("not available"))
        output = io.StringIO()
        status = LiveStatus(sampler, output)
        status.refresh()
        self.assertIn("CPU -- | RAM --", status.text())
        self.assertIsNone(status.summary()["system_cpu_peak_percent"])
        self.assertIsNone(status.summary()["system_ram_peak_gib"])
        self.assertEqual(output.getvalue(), "")  # No repetitive rows in redirected logs.
        status.stop()

    def test_stop_wakes_the_monitor_and_retains_sampled_peaks(self):
        status = LiveStatus(FakeSampler(), TerminalBuffer())
        status.start()
        status.stop()
        self.assertFalse(status.thread.is_alive())
        self.assertEqual(status.summary()["system_ram_peak_gib"], 6.2)

    def test_cpu_percent_uses_interval_deltas_and_unknown_baseline_is_not_zero(self):
        sampler = SystemSampler()
        with patch.object(sampler, "_cpu_ticks", side_effect=[(200, 1000), (230, 1100), (1, 2)]), \
             patch.object(sampler, "_memory", return_value=(6 * 1024**3, 16 * 1024**3)):
            self.assertIsNone(sampler.sample()["cpu_percent"])
            current = sampler.sample()
            self.assertAlmostEqual(current["cpu_percent"], 70)
            self.assertEqual(current["ram_used_gib"], 6)
            self.assertIsNone(sampler.sample()["cpu_percent"])
        sampler.close()


class LiveWorkflowTests(unittest.TestCase):
    setUp = fixtures.HttpWorkflowTests.setUp
    run_lab = fixtures.HttpWorkflowTests.run_lab

    def test_stream_mode_saves_final_speed_total_time_and_compact_report(self):
        with redirect_stdout(io.StringIO()) as output:
            folder, code = fixtures.run_experiment(self.client, self.config, ["fixture:latest"],
                                                  self.cases, self.root / "results", simple=True)
        self.assertEqual(code, 0)
        request = next(body for path, body in self.server.state["requests"] if path == "/api/chat")
        self.assertTrue(request["stream"])
        self.assertFalse(request["think"] or request["truncate"] or request["shift"])
        row = json.loads((folder / "results.jsonl").read_text())
        self.assertEqual(row["output_tokens"], 50)
        self.assertEqual(row["tokens_per_second"], 50)
        metadata = json.loads((folder / "run.json").read_text())
        self.assertGreaterEqual(metadata["wall_ms"], row["wall_ms"])
        self.assertEqual(metadata["live_metrics"]["resource_scope"], "whole machine, including other applications")
        self.assertIn("Total time:", output.getvalue())
        report = (folder / "report.html").read_text()
        self.assertIn("Total time:", report)
        self.assertIn("50.0 tok/s", report)
        self.assertFalse(any(thread.name == "ai-lab-status" for thread in threading.enumerate()))

    def test_live_monitor_is_stopped_after_cancellation_without_extra_model_request(self):
        with patch.object(self.client, "analyze", side_effect=KeyboardInterrupt):
            folder, code = self.run_lab(simple=True)
        self.assertEqual(code, 130)
        self.assertFalse(any(thread.name == "ai-lab-status" for thread in threading.enumerate()))
        self.assertEqual(sum(path == "/api/generate" for path, _ in self.server.state["requests"]), 1)
        self.assertEqual(json.loads((folder / "run.json").read_text())["status"], "interrupted")

    def test_truncated_stream_stops_the_batch_and_keeps_an_unfinished_report(self):
        self.server.state["stream_truncated"] = True
        folder, code = self.run_lab(simple=True, repeat=2)
        self.assertEqual(code, 1)
        self.assertEqual(self.server.state["chat_count"], 1)
        metadata = json.loads((folder / "run.json").read_text())
        self.assertEqual(metadata["status"], "aborted")
        self.assertEqual(metadata["expected_records"], 2)
        row = json.loads((folder / "results.jsonl").read_text())
        self.assertEqual(row["status"], "response_error")
        self.assertFalse(any(thread.name == "ai-lab-status" for thread in threading.enumerate()))


if __name__ == "__main__":
    unittest.main()
