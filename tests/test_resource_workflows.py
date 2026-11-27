"""CPU admission, checkpoint and cancellation checks without real load or AI."""
from copy import deepcopy
import hashlib
import io
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from watchdog.observers.api import EventQueue, ObservedWatch

from ai.organize import plan_for
from Services.AnalysisService import AnalysisService
from Services.ArtifactsService import ArtifactsService
from Services.ConfigService import DEFAULTS
from Services.EventAggregator import EventAggregator
from Services.ExecutionService import ExecutionService
from Services.GovernedPolling import GovernedPollingEmitter
from Services.OrganizationService import OrganizationService, SOURCE_LIMIT
from Services.ResourceGovernor import ResourceCancelled
from Services.ScanService import ScanService
from service import CodeStewardService


class Gate:
    """Deterministically simulate busy -> available, or shutdown while busy."""
    def __init__(self, available=True):
        self.available = threading.Event()
        self.entered = threading.Event()
        self.closing = False
        self.calls = 0
        if available:
            self.available.set()

    def can_work(self):
        return self.available.is_set() and not self.closing

    def checkpoint(self, cancel=None):
        self.calls += 1
        self.entered.set()
        while True:
            if self.closing or (cancel is not None and cancel()):
                raise ResourceCancelled("Cancelled safely for shutdown")
            if self.available.wait(0.01):
                if self.closing or (cancel is not None and cancel()):
                    raise ResourceCancelled("Cancelled safely for shutdown")
                return

    def close(self):
        self.closing = True
        self.available.set()


class Config:
    def __init__(self, root):
        self.values = deepcopy(DEFAULTS)
        self.values["scanning"]["root_paths"] = [str(root)]

    def get_config(self):
        return deepcopy(self.values)

    def get(self, *keys, default=None):
        value = self.values
        for key in keys:
            if not isinstance(value, dict) or key not in value:
                return default
            value = value[key]
        return value


class ResourceWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.config = Config(self.root)

    def thread(self, work, gate):
        outcome = {}

        def run():
            try:
                outcome["result"] = work()
            except BaseException as error:
                outcome["error"] = error

        thread = threading.Thread(target=run, daemon=True)
        thread.start()

        def finish():
            gate.close()
            thread.join(1)
        self.addCleanup(finish)
        return thread, outcome

    def test_busy_service_keeps_events_then_processes_them_after_recovery(self):
        aggregator = EventAggregator(quiet_seconds=0)
        aggregator.add(str(self.root / "new.py"))
        scan = SimpleNamespace(process=Mock(), config_service=self.config)
        service = CodeStewardService(scan_service=scan, aggregator=aggregator)
        service.resource_governor = Gate(available=False)
        service.do_work()
        scan.process.assert_not_called()
        self.assertEqual(aggregator.status()["waiting_files"], 1)
        service.resource_governor.available.set()
        service.do_work()
        self.assertEqual(aggregator.status()["waiting_files"], 0)
        self.assertEqual(scan.process.call_args.kwargs["files"][0]["path"], str(self.root / "new.py"))

    def test_scan_waits_for_resources_without_touching_source_then_resumes(self):
        source = self.root / "small.py"
        raw = b"print('keep the original')\n"
        source.write_bytes(raw)
        scan = ScanService(self.config, None)
        gate = scan.resource_governor = Gate(available=False)
        with patch.object(scan, "_load_checkpoint", return_value={}), patch.object(scan, "_save_checkpoint"):
            thread, outcome = self.thread(lambda: scan.process(full_scan="test"), gate)
            self.assertTrue(gate.entered.wait(1))
            self.assertTrue(thread.is_alive())
            self.assertEqual(source.read_bytes(), raw)
            self.assertIsNone(scan.get_status()["last_full_scan_at"])
            gate.available.set()
            thread.join(1)
            self.assertFalse(thread.is_alive())
            self.assertNotIn("error", outcome)
            self.assertIsNotNone(scan.get_status()["last_full_scan_at"])
            self.assertEqual(source.read_bytes(), raw)

    def test_cancelled_partial_scan_cannot_delete_records_or_claim_completion(self):
        scan = ScanService(self.config, None)
        scan.resource_governor = Gate()
        with patch.object(scan, "_load_checkpoint", return_value={str(self.root / "missing.py"): "old"}), \
                patch.object(scan, "_save_checkpoint") as save, \
                patch.object(scan, "_walk", side_effect=ResourceCancelled("shutdown")), \
                patch.object(scan, "_remove") as remove:
            with self.assertRaises(ResourceCancelled):
                scan.process(full_scan="test")
            remove.assert_not_called()
            save.assert_not_called()
            self.assertFalse(scan.get_status()["is_scanning"])
            self.assertIsNone(scan.get_status()["last_full_scan_at"])

    def test_execution_hash_and_copy_wait_before_reading_and_preserve_contents(self):
        raw = b"original contents\n" * 100000
        source, output = io.BytesIO(raw), io.BytesIO()
        execution = object.__new__(ExecutionService)
        gate = execution.resource_governor = Gate(available=False)
        thread, outcome = self.thread(lambda: execution._hash(source, output), gate)
        self.assertTrue(gate.entered.wait(1))
        self.assertEqual(source.tell(), 0)
        self.assertEqual(output.getvalue(), b"")
        gate.available.set()
        thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertEqual(outcome["result"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(output.getvalue(), raw)
        self.assertGreater(gate.calls, 1)

    def test_atomic_update_checksum_finishes_without_waiting_on_renamed_target(self):
        target = self.root / "original.txt"
        raw = b"previous committed version"
        target.write_bytes(raw)
        execution = object.__new__(ExecutionService)
        execution._save = Mock()
        gate = execution.resource_governor = Gate(available=False)
        result = {"previous_hash": hashlib.sha256(raw).hexdigest()}
        aside = execution._set_aside(None, self.root, target.name, result, {})
        self.assertEqual(gate.calls, 0)
        self.assertFalse(target.exists())
        self.assertEqual((self.root / aside).read_bytes(), raw)
        execution._put_back(None, self.root, aside, target.name, result)
        self.assertEqual(target.read_bytes(), raw)
        self.assertEqual(list(self.root.iterdir()), [target])
        self.assertNotIn("update_aside", result)

    def test_chunked_organization_read_keeps_full_hash_and_bounded_source(self):
        path = self.root / "large.py"
        raw = b"print('same content')\n" * 20000
        path.write_bytes(raw)
        organization = object.__new__(OrganizationService)
        organization.resource_governor = Gate()
        file_hash, source = organization._read({"path": str(path), "artifact_type": "script", "hash": "existing"})
        self.assertEqual(file_hash, hashlib.sha256(raw).hexdigest())
        self.assertEqual(source, raw[:SOURCE_LIMIT * 4].decode()[:SOURCE_LIMIT])
        self.assertGreater(organization.resource_governor.calls, 2)

    def test_cancelled_organization_wait_releases_operation_lock(self):
        artifacts = ArtifactsService(str(self.root / "index.sqlite3"))
        organization = OrganizationService(self.config, artifacts, SimpleNamespace(), SimpleNamespace())
        gate = organization.resource_governor = Gate(available=False)
        self.assertTrue(organization.start(use_ai=False))
        self.assertTrue(gate.entered.wait(1))
        gate.close()
        self.assertTrue(organization.wait(1))
        self.assertEqual(organization.state, "interrupted")
        self.assertIn("shutdown", organization.last_error)
        self.assertTrue(organization.operation_lock.acquire(blocking=False))
        organization.operation_lock.release()

    def test_plan_builder_honors_cancellation_without_sending_model_requests(self):
        client = Mock()
        checkpoint = Mock(side_effect=ResourceCancelled("shutdown"))
        with self.assertRaises(ResourceCancelled):
            plan_for([], {}, {}, client=client, checkpoint=checkpoint)
        client.ask.assert_not_called()

    def analysis(self):
        source = self.root / "source.py"
        source.write_text("print('source')\n", encoding="utf-8")
        artifact = {"id": "test-file", "path": str(source), "hash": hashlib.sha256(source.read_bytes()).hexdigest(),
                    "artifact_type": "script"}
        artifacts = SimpleNamespace(database_path=str(self.root / "analysis.sqlite3"), find_artifact=lambda _: artifact)
        analysis = AnalysisService(self.config, artifacts)
        analysis.queue(artifact)
        return analysis

    def test_busy_analysis_does_not_check_model_or_consume_job_attempt(self):
        analysis = self.analysis()
        analysis.resource_governor = Gate(available=False)
        with patch.object(analysis, "_connection") as connection:
            self.assertFalse(analysis.step())
            connection.assert_not_called()
        with analysis._connect() as db:
            job = db.execute("SELECT status, attempts FROM analysis_jobs").fetchone()
        self.assertEqual(tuple(job), ("queued", 0))

    def test_cpu_cancelled_ai_response_is_requeued_without_failed_attempt(self):
        analysis = self.analysis()
        analysis.resource_governor = Gate()
        request = SimpleNamespace(profile=analysis.profile, ai_settings=analysis.ai_settings, tag=analysis.tag,
                                  model_digest=None, client=object())
        with patch.object(analysis, "_connection", return_value=request), \
                patch("Services.AnalysisService.result_for", return_value={"status": "resource_busy", "error": "CPU busy"}):
            self.assertTrue(analysis.step())
        with analysis._connect() as db:
            job = db.execute("SELECT status, attempts FROM analysis_jobs").fetchone()
        self.assertEqual(tuple(job), ("queued", 0))
        self.assertEqual(analysis.state, "waiting: CPU resources")
        self.assertLessEqual(analysis._wait_until - time.time(), analysis.IDLE_SECONDS)

    def test_concurrent_force_requeue_cannot_make_cpu_cancelled_attempts_negative(self):
        for reason in ("resource_busy", "shutdown"):
            with self.subTest(reason=reason):
                analysis = self.analysis()
                analysis.resource_governor = Gate()
                request = SimpleNamespace(profile=analysis.profile, ai_settings=analysis.ai_settings, tag=analysis.tag,
                                          model_digest=None, client=object())

                def cancelled(*_):
                    # A user queues this same unchanged file again while its prior request is active.
                    analysis.queue(analysis.artifacts_service.find_artifact("test-file"), force=True)
                    if reason == "shutdown":
                        raise ResourceCancelled("shutdown")
                    return {"status": "resource_busy", "error": "CPU busy"}

                with patch.object(analysis, "_connection", return_value=request), \
                        patch("Services.AnalysisService.result_for", side_effect=cancelled):
                    if reason == "shutdown":
                        with self.assertRaises(ResourceCancelled):
                            analysis.step()
                    else:
                        analysis.step()
                with analysis._connect() as db:
                    job = db.execute("SELECT status, attempts, force FROM analysis_jobs").fetchone()
                self.assertEqual(tuple(job), ("queued", 0, 1))

    def test_polling_start_and_stop_do_not_block_when_cpu_is_busy(self):
        gate = Gate(available=False)
        events = EventQueue()
        emitter = GovernedPollingEmitter(events, ObservedWatch(str(self.root), recursive=True), governor=gate, timeout=0.01)
        self.addCleanup(lambda: (emitter.stop(), emitter.join(1)))
        started = time.monotonic()
        emitter.start()
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertTrue(gate.entered.wait(1))
        self.assertFalse(emitter._snapshot_ready)
        emitter.stop()
        emitter.join(0.5)
        self.assertFalse(emitter.is_alive())
        self.assertTrue(events.empty())

    def test_polling_keeps_snapshot_and_observes_changes_after_cpu_recovers(self):
        gate = Gate()
        events = EventQueue()
        emitter = GovernedPollingEmitter(events, ObservedWatch(str(self.root), recursive=True), governor=gate, timeout=0.01)
        self.addCleanup(lambda: (emitter.stop(), emitter.join(1)))
        emitter.start()
        deadline = time.monotonic() + 1
        while not emitter._snapshot_ready and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertTrue(emitter._snapshot_ready)
        gate.entered.clear()
        gate.available.clear()
        self.assertTrue(gate.entered.wait(1))
        new_file = self.root / "created-during-pause.txt"
        new_file.write_text("must be found", encoding="utf-8")
        self.assertTrue(events.empty())
        gate.available.set()
        deadline = time.monotonic() + 1
        found = False
        while time.monotonic() < deadline and not found:
            while not events.empty():
                event, _ = events.get_nowait()
                found |= event.event_type == "created" and event.src_path == str(new_file)
            time.sleep(0.005)
        self.assertTrue(found)

    def test_first_polling_baseline_is_immediate_and_requests_reconciliation(self):
        gate = Gate()
        aggregator = EventAggregator()
        events = EventQueue()
        emitter = GovernedPollingEmitter(events, ObservedWatch(str(self.root), recursive=True), governor=gate, timeout=30,
            on_initial_snapshot=lambda: aggregator.request_full_scan("polling-baseline-scan"))
        self.addCleanup(lambda: (emitter.stop(), emitter.join(1)))
        emitter.start()
        deadline = time.monotonic() + 0.5
        while not aggregator.status()["full_scan_requested"] and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertTrue(emitter._snapshot_ready)
        self.assertEqual(aggregator.status()["full_scan_requested"], "polling-baseline-scan")


if __name__ == "__main__":
    unittest.main()
