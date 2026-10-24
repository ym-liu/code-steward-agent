"""CPU policy contracts use controlled samples, never CPU stress or real inference."""
import json
from pathlib import Path
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
import unittest

from Services.ResourceGovernor import ResourceCancelled, ResourceGovernor, checkpoint
from Services.ResourcePolicy import DEFAULTS, validate_policy


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class Sampler:
    def __init__(self, value=20.0):
        self.value = value

    def __call__(self):
        if isinstance(self.value, Exception):
            raise self.value
        return self.value


def verified_priority():
    return {"applied": True, "name": "test-idle", "value": 64}


class ResourceGovernorTests(unittest.TestCase):
    def make(self, *, policy=None, clock=None, priority_setter=None, cpu_count=8):
        sampler = Sampler()
        governor = ResourceGovernor(policy, sampler=sampler, clock=clock or time.monotonic,
                                    priority_setter=priority_setter or verified_priority,
                                    cpu_count=cpu_count)
        self.addCleanup(governor.stop)
        return governor, sampler

    def wait_for(self, predicate, timeout=2):
        deadline = time.monotonic() + timeout
        while not predicate():
            if time.monotonic() >= deadline:
                self.fail("Timed out waiting for a controlled worker")
            time.sleep(0.005)

    def test_first_sampler_baseline_is_ignored_in_the_monitor_thread(self):
        primed = threading.Event()
        release = threading.Event()
        calls = []

        def sample():
            calls.append((threading.current_thread().name, threading.get_ident()))
            if len(calls) == 1:
                primed.set()
                release.wait(2)
                return 0.0  # psutil's meaningless first nonblocking result
            return 95.0

        governor = ResourceGovernor({"sample_seconds": 0.2}, sampler=sample,
                                    priority_setter=verified_priority)
        self.addCleanup(governor.stop)
        self.addCleanup(release.set)
        governor.start()
        self.assertTrue(primed.wait(1))
        self.assertFalse(governor.can_work())
        self.assertEqual(governor.snapshot()["state"], "warming")
        self.assertIsNone(governor.snapshot()["cpu_percent"])
        release.set()
        self.wait_for(lambda: governor.snapshot()["state"] == "paused")
        self.assertFalse(governor.can_work())
        self.assertGreaterEqual(len(calls), 2)
        self.assertEqual({item[1] for item in calls}, {calls[0][1]})
        self.assertEqual(calls[0][0], "code-steward-resources")

    def test_pressure_changes_pacing_and_requires_consecutive_cool_samples(self):
        clock = Clock()
        governor, sampler = self.make(clock=clock)
        self.assertFalse(governor.can_work())
        sampler.value = 20
        governor.sample_once()
        self.assertEqual(governor.snapshot()["state"], "active")
        self.assertEqual(governor.snapshot()["delay_seconds"], 0)
        cap = governor.ai_thread_limit()
        sampler.value = 55
        governor.sample_once()
        moderate_delay = governor.snapshot()["delay_seconds"]
        self.assertEqual(governor.snapshot()["state"], "throttled")
        self.assertTrue(governor.can_work())
        self.assertGreater(moderate_delay, 0)
        sampler.value = 75
        governor.sample_once()
        self.assertGreater(governor.snapshot()["delay_seconds"], moderate_delay)
        sampler.value = 80
        governor.sample_once()
        self.assertFalse(governor.can_work())
        self.assertTrue(governor.should_cancel_ai())
        for cpu, expected_streak in ((70, 0), (40, 1), (41, 0), (40, 1), (39, 2)):
            sampler.value = cpu
            governor.sample_once()
            self.assertEqual(governor.snapshot()["state"], "paused")
            self.assertEqual(governor.snapshot()["cool_samples"], expected_streak)
            self.assertFalse(governor.can_work())
        sampler.value = 40
        governor.sample_once()
        self.assertEqual(governor.snapshot()["state"], "active")
        self.assertTrue(governor.can_work())
        self.assertFalse(governor.should_cancel_ai())
        self.assertEqual(governor.ai_thread_limit(), cap)

    def test_stale_samples_block_work_until_fresh_cool_recovery(self):
        clock = Clock()
        governor, sampler = self.make(clock=clock)
        governor.sample_once()
        self.assertTrue(governor.can_work())
        clock.now += 3.001
        self.assertEqual(governor.snapshot()["state"], "stale")
        self.assertFalse(governor.can_work())
        self.assertTrue(governor.should_cancel_ai())
        for number in range(1, 4):
            governor.sample_once()
            self.assertEqual(governor.can_work(), number == 3)
        self.assertEqual(governor.snapshot()["state"], "active")

    def test_sensor_errors_and_invalid_samples_fail_closed_then_recover(self):
        governor, sampler = self.make(clock=Clock())
        governor.sample_once()
        self.assertTrue(governor.can_work())
        for invalid in (RuntimeError("CPU sensor failed"), float("nan"), float("inf"), -1, 101, True):
            sampler.value = invalid
            governor.sample_once()
            status = governor.snapshot()
            self.assertEqual(status["state"], "unavailable")
            self.assertFalse(status["work_allowed"])
            self.assertIsNone(status["cpu_percent"])
            self.assertIsNone(status["sample_age_seconds"])
            self.assertTrue(status["error"])
        sampler.value = 20
        for number in range(1, 4):
            governor.sample_once()
            self.assertEqual(governor.can_work(), number == 3)
        self.assertIsNone(governor.snapshot()["error"])

    def test_unverified_or_denied_priority_never_admits_work(self):
        for result in (None, {}, {"applied": False}, {"applied": 1}, PermissionError("denied")):
            def apply(result=result):
                if isinstance(result, Exception):
                    raise result
                return result

            with self.subTest(priority=result):
                governor, _ = self.make(priority_setter=apply)
                governor.sample_once()
                self.assertFalse(governor.can_work())
                self.assertEqual(governor.snapshot()["state"], "unavailable")
                self.assertFalse(governor.snapshot()["priority"]["applied"])

    def test_priority_loss_after_work_was_admitted_blocks_it(self):
        allowed = [True]

        def apply():
            if not allowed[0]:
                raise PermissionError("priority changed externally")
            return verified_priority()

        governor, _ = self.make(priority_setter=apply)
        governor.sample_once()
        self.assertTrue(governor.can_work())
        allowed[0] = False
        governor.sample_once()
        self.assertFalse(governor.can_work())
        self.assertTrue(governor.should_cancel_ai())

    def test_shutdown_releases_a_blocked_checkpoint(self):
        governor, sampler = self.make()
        sampler.value = 95
        governor.sample_once()
        outcome = []

        def work():
            try:
                governor.checkpoint()
                outcome.append("ran")
            except ResourceCancelled:
                outcome.append("cancelled")

        worker = threading.Thread(target=work)
        worker.start()
        self.wait_for(lambda: governor.snapshot()["waiting_checkpoints"] == 1)
        governor.begin_shutdown()
        worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(outcome, ["cancelled"])
        self.assertEqual(governor.snapshot()["waiting_checkpoints"], 0)
        self.assertEqual(governor.snapshot()["state"], "closing")

    def test_caller_cancellation_releases_checkpoint_without_global_shutdown(self):
        governor, sampler = self.make()
        sampler.value = 95
        governor.sample_once()
        cancel = threading.Event()
        outcome = []

        def work():
            try:
                governor.checkpoint(cancel=cancel.is_set)
            except ResourceCancelled:
                outcome.append("cancelled")

        worker = threading.Thread(target=work)
        worker.start()
        self.wait_for(lambda: governor.snapshot()["waiting_checkpoints"] == 1)
        cancel.set()
        worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(outcome, ["cancelled"])
        self.assertEqual(governor.snapshot()["state"], "paused")

    def test_checkpoint_does_not_run_if_pressure_arrives_during_pacing_wait(self):
        governor, sampler = self.make(policy={"max_delay_seconds": 0.5})
        sampler.value = 75
        governor.sample_once()
        completed = threading.Event()
        cancelled = threading.Event()

        def work():
            try:
                governor.checkpoint()
                completed.set()
            except ResourceCancelled:
                cancelled.set()

        worker = threading.Thread(target=work)
        worker.start()
        self.wait_for(lambda: governor.snapshot()["waiting_checkpoints"] == 1)
        sampler.value = 99
        governor.sample_once()
        self.assertFalse(completed.wait(0.55))
        governor.begin_shutdown()
        worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertTrue(cancelled.is_set())

    def test_ai_slots_are_serial_and_shutdown_releases_waiting_inference(self):
        governor, _ = self.make()
        governor.sample_once()
        first_entered = threading.Event()
        release_first = threading.Event()
        second_entered = threading.Event()
        second_cancelled = threading.Event()

        def first():
            with governor.ai_slot():
                first_entered.set()
                release_first.wait(2)

        def second():
            try:
                with governor.ai_slot():
                    second_entered.set()
            except ResourceCancelled:
                second_cancelled.set()

        one = threading.Thread(target=first)
        two = threading.Thread(target=second)
        one.start()
        self.assertTrue(first_entered.wait(1))
        two.start()
        self.assertFalse(second_entered.wait(0.15))
        self.assertEqual(governor.snapshot()["active_ai_requests"], 1)
        governor.begin_shutdown()
        two.join(1)
        release_first.set()
        one.join(1)
        self.assertFalse(one.is_alive())
        self.assertFalse(two.is_alive())
        self.assertTrue(second_cancelled.is_set())
        self.assertFalse(second_entered.is_set())
        self.assertEqual(governor.snapshot()["active_ai_requests"], 0)

    def test_ai_slot_is_released_after_generation_exception(self):
        governor, _ = self.make()
        governor.sample_once()
        with self.assertRaisesRegex(ValueError, "generation failed"):
            with governor.ai_slot():
                raise ValueError("generation failed")
        with governor.ai_slot():
            self.assertEqual(governor.snapshot()["active_ai_requests"], 1)
        self.assertEqual(governor.snapshot()["active_ai_requests"], 0)

    def test_stable_ai_thread_cap_respects_user_and_available_cores(self):
        governor, sampler = self.make(policy={"ai_max_threads": 4}, cpu_count=3, clock=Clock())
        self.assertEqual(governor.ai_thread_limit(), 2)
        self.assertEqual(governor.ai_thread_limit(1), 1)
        self.assertEqual(governor.ai_thread_limit(100), 2)
        for cpu in (20, 65, 99):
            sampler.value = cpu
            governor.sample_once()
            self.assertEqual(governor.ai_thread_limit(), 2)
        dual, _ = self.make(policy={"ai_max_threads": 4}, cpu_count=2)
        self.assertEqual(dual.ai_thread_limit(), 1)
        single, _ = self.make(policy={"ai_max_threads": 4}, cpu_count=1)
        self.assertEqual(single.ai_thread_limit(), 1)

    def test_configuration_reload_waits_for_real_sample_and_does_not_refresh_age(self):
        clock = Clock()
        governor, _ = self.make(clock=clock)
        governor.sample_once()
        clock.now += 1
        governor.configure({"pause_cpu_percent": 70})
        status = governor.snapshot()
        self.assertFalse(status["work_allowed"])
        self.assertEqual(status["sample_age_seconds"], 1)
        self.assertEqual(status["state"], "warming")
        clock.now += 3
        governor.configure({"pause_cpu_percent": 75})
        status = governor.snapshot()
        self.assertFalse(status["work_allowed"])
        self.assertEqual(status["sample_age_seconds"], 4)
        self.assertEqual(status["state"], "stale")

    def test_single_core_host_defers_inference_without_blocking_other_light_work(self):
        governor, _ = self.make(cpu_count=1)
        governor.sample_once()
        status = governor.snapshot()
        self.assertTrue(status["work_allowed"])
        self.assertFalse(status["ai_work_allowed"])
        self.assertIn("single-core", status["ai_defer_reason"])
        with self.assertRaisesRegex(ResourceCancelled, "single-core"):
            with governor.ai_slot():
                self.fail("Single-core inference was admitted")
        self.assertEqual(governor.snapshot()["active_ai_requests"], 0)

    def test_invalid_reload_keeps_the_previous_policy(self):
        governor, _ = self.make()
        governor.sample_once()
        original = governor.snapshot()["policy"]
        with self.assertRaises(ValueError):
            governor.configure({"pause_cpu_percent": float("nan")})
        self.assertEqual(governor.snapshot()["policy"], original)
        self.assertTrue(governor.can_work())

    def test_optional_owner_checkpoint_preserves_ungoverned_unit_fixtures(self):
        checkpoint(SimpleNamespace())
        called = []
        checkpoint(SimpleNamespace(resource_governor=SimpleNamespace(checkpoint=lambda: called.append(True))))
        self.assertEqual(called, [True])

    def test_report_is_detached_from_mutable_internal_policy(self):
        governor, _ = self.make()
        status = governor.snapshot()
        status["policy"]["pause_cpu_percent"] = 1
        status["priority"]["applied"] = True
        self.assertEqual(governor.snapshot()["policy"], DEFAULTS)
        self.assertFalse(governor.snapshot()["priority"]["applied"])

    def test_native_priority_and_inheritance_are_verified_only_in_subprocess(self):
        import psutil
        original_priority = psutil.Process().nice()
        code = """
import json, subprocess, sys
import psutil
from Services.ResourceGovernor import apply_low_priority
reported = apply_low_priority()
child = subprocess.run([sys.executable, '-B', '-c', 'import psutil; print(int(psutil.Process().nice()))'],
                       check=True, capture_output=True, text=True, timeout=10)
print(json.dumps({'reported': reported, 'actual': int(psutil.Process().nice()), 'child': int(child.stdout)}))
"""
        result = subprocess.run([sys.executable, "-B", "-c", code], check=True, capture_output=True,
                                text=True, timeout=20, cwd=Path(__file__).resolve().parents[1])
        value = json.loads(result.stdout)
        self.assertTrue(value["reported"]["applied"])
        expected = int(psutil.IDLE_PRIORITY_CLASS) if sys.platform == "win32" else 19
        self.assertEqual(value["actual"], expected)
        self.assertEqual(value["child"], expected)
        self.assertEqual(psutil.Process().nice(), original_priority)


class ResourcePolicyTests(unittest.TestCase):
    def test_defaults_are_copied_and_policy_is_mandatory(self):
        policy = validate_policy()
        self.assertEqual(policy, DEFAULTS)
        policy["pause_cpu_percent"] = 1
        self.assertEqual(validate_policy()["pause_cpu_percent"], 80)
        with self.assertRaises(ValueError):
            validate_policy({"enabled": False})

    def test_invalid_types_numbers_and_unknown_settings_are_rejected(self):
        bad = [[], "off", 1, {"unknown": 1}, {"sample_seconds": True},
               {"sample_seconds": float("nan")}, {"sample_seconds": float("inf")},
               {"sample_seconds": 0.1}, {"max_delay_seconds": 0}, {"pause_cpu_percent": 101},
               {"resume_cpu_percent": -1}, {"resume_samples": 2.0}, {"resume_samples": 1},
               {"ai_max_threads": 4.0}, {"ai_max_threads": 5}, {"ai_max_threads": True},
               {"slow_cpu_percent": "50"}, {"pause_cpu_percent": 50},
               {"resume_cpu_percent": 50}, {"sample_seconds": 5, "max_sample_age_seconds": 3}]
        for policy in bad:
            with self.subTest(policy=policy):
                with self.assertRaises(ValueError):
                    validate_policy(policy)


if __name__ == "__main__":
    unittest.main()
