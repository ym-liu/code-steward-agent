"""Cooperative CPU admission and pacing; this module never controls other processes."""
from contextlib import contextmanager
import logging
import math
import os
import sys
import threading
import time

from Services.ResourcePolicy import validate_policy


logger = logging.getLogger("code-steward.resources")


class ResourceCancelled(RuntimeError):
    """Leave a safe work boundary so shutdown can drain locks and journals."""


def checkpoint(owner):
    governor = getattr(owner, "resource_governor", None)
    if governor is not None:
        service = getattr(owner, "code_steward_service", None)
        if service is None:
            service = getattr(getattr(owner, "scan_service", None), "code_steward_service", None)
        if service is None:
            governor.checkpoint()
        else:
            # /system/stop must also release a scan waiting for CPU, without
            # permanently closing the governor needed by a later /system/start.
            governor.checkpoint(cancel=lambda: getattr(service.state, "value", None) == "stopping")


def apply_low_priority():
    """Lower only this process; child processes/threads inherit its CPU priority."""
    import psutil
    process = psutil.Process()
    if sys.platform == "win32":
        target = psutil.IDLE_PRIORITY_CLASS
        if process.nice() != target:
            process.nice(target)
        if process.nice() != target:
            raise RuntimeError("Windows did not apply IDLE_PRIORITY_CLASS")
        return {"applied": True, "name": "idle", "value": int(target)}
    if process.nice() < 19:
        process.nice(19)
    actual = process.nice()
    if actual < 19:
        raise RuntimeError("The operating system did not apply nice 19")
    return {"applied": True, "name": "nice", "value": actual}


def system_cpu_percent():
    import psutil
    return psutil.cpu_percent(interval=None)


class ResourceGovernor:
    def __init__(self, policy=None, *, sampler=None, priority_setter=None,
                 clock=time.monotonic, cpu_count=None):
        self._policy = validate_policy(policy)
        self._sample = sampler or system_cpu_percent
        self._set_priority = priority_setter or apply_low_priority
        self._clock = clock
        self._cpu_count = max(1, cpu_count or os.cpu_count() or 1)
        self._condition = threading.Condition(threading.RLock())
        self._stop = threading.Event()
        self._thread = None
        self._closing = False
        self._priority = {"applied": False, "name": "unknown", "value": None}
        self._state = "warming"
        self._cpu = None
        self._sample_at = None
        self._error = None
        self._cool_samples = 0
        self._waiters = 0
        self._ai_lock = threading.Lock()
        self._active_ai = 0
        self._cancelled_ai = 0
        self._deferred_checks = 0
        self._transitions = 0

    def start(self):
        with self._condition:
            if self._thread is not None and self._thread.is_alive():
                return
            if self._closing:
                raise ResourceCancelled("The resource governor is closing")
            self._stop.clear()
            self._apply_priority()
            self._thread = threading.Thread(target=self._run, name="code-steward-resources", daemon=True)
            self._thread.start()

    def _run(self):
        # psutil keeps a baseline per calling thread. Prime on THIS thread, and
        # never accept its first meaningless zero as a CPU measurement.
        try:
            self._sample()
        except Exception as error:
            with self._condition:
                self._fault(error)
        while not self._stop.wait(self._policy["sample_seconds"]):
            self.sample_once()

    def _apply_priority(self):
        try:
            priority = self._set_priority()
            if not isinstance(priority, dict) or priority.get("applied") is not True:
                raise RuntimeError("Low process priority could not be verified")
            self._priority = priority
            return True
        except Exception as error:
            self._priority = {"applied": False, "name": "unavailable", "value": None}
            self._fault(error)
            return False

    def sample_once(self):
        with self._condition:
            if self._closing:
                return
            if not self._apply_priority():
                return
            try:
                self._record_sample(self._sample())
            except Exception as error:
                self._fault(error)

    def _fault(self, error):
        self._error = f"{type(error).__name__}: {error}"
        self._cpu, self._sample_at, self._cool_samples = None, None, 0
        self._change_state("unavailable")
        self._condition.notify_all()

    def _change_state(self, state):
        if state != self._state:
            self._transitions += 1
            logger.info("Resource state %s -> %s (CPU %s%%).", self._state, state, self._cpu)
            self._state = state

    def _record_sample(self, cpu):
        if isinstance(cpu, bool) or not isinstance(cpu, (int, float)) or not math.isfinite(cpu) or not 0 <= cpu <= 100:
            raise ValueError("System CPU sample must be a finite percentage from 0 to 100")
        previous = self._state
        self._cpu, self._sample_at, self._error = float(cpu), self._clock(), None
        policy = self._policy
        if cpu >= policy["pause_cpu_percent"]:
            self._cool_samples = 0
            self._change_state("paused")
        elif previous in {"paused", "unavailable", "stale"}:
            self._cool_samples = self._cool_samples + 1 if cpu <= policy["resume_cpu_percent"] else 0
            if self._cool_samples >= policy["resume_samples"]:
                self._cool_samples = 0
                self._change_state("active")
        else:
            self._cool_samples = 0
            self._change_state("throttled" if cpu >= policy["slow_cpu_percent"] else "active")
        self._condition.notify_all()

    def _state_now(self):
        if self._closing:
            return "closing"
        if not self._priority["applied"]:
            return "unavailable"
        if self._sample_at is not None and self._clock() - self._sample_at > self._policy["max_sample_age_seconds"]:
            self._cool_samples = 0
            self._change_state("stale")
        return self._state

    def can_work(self):
        with self._condition:
            return self._state_now() in {"active", "throttled"}

    def should_cancel_ai(self):
        return not self.can_work()

    def _delay(self):
        if self._state_now() != "throttled":
            return 0.0
        span = self._policy["pause_cpu_percent"] - self._policy["slow_cpu_percent"]
        fraction = (self._cpu - self._policy["slow_cpu_percent"]) / span
        return self._policy["max_delay_seconds"] * (0.1 + 0.9 * fraction)

    def checkpoint(self, cancel=None):
        """Wait outside atomic publication steps; cancellation always wakes shutdown."""
        with self._condition:
            self._waiters += 1
            try:
                while True:
                    if self._closing or (cancel is not None and cancel()):
                        raise ResourceCancelled("Background work yielded for shutdown or cancellation")
                    if self._state_now() in {"active", "throttled"}:
                        delay = self._delay()
                        if delay:
                            deadline = self._clock() + delay
                            while self._clock() < deadline:
                                if self._closing or (cancel is not None and cancel()):
                                    raise ResourceCancelled("Background work yielded for shutdown or cancellation")
                                self._condition.wait(min(0.1, deadline - self._clock()))
                            # A pressure sample can arrive during the pacing wait.
                            if self._state_now() not in {"active", "throttled"}:
                                continue
                        return
                    self._deferred_checks += 1
                    self._condition.wait(0.1)
            finally:
                self._waiters -= 1

    def ai_thread_limit(self, requested="auto"):
        # Reserve a core on supported multi-core hosts. Stable across samples: changing the
        # runner's thread option while loaded can reload a shared Ollama model.
        cap = min(self._policy["ai_max_threads"], max(1, self._cpu_count - 1))
        if isinstance(requested, int) and not isinstance(requested, bool) and requested > 0:
            cap = min(cap, requested)
        return cap

    @contextmanager
    def ai_slot(self):
        acquired = False
        try:
            if self._cpu_count < 2:
                raise ResourceCancelled("AI inference is deferred on a single-core host to reserve CPU for other software")
            while not acquired:
                self.checkpoint()
                acquired = self._ai_lock.acquire(timeout=0.1)
            self.checkpoint()
            with self._condition:
                self._active_ai += 1
            try:
                yield
            finally:
                with self._condition:
                    self._active_ai -= 1
        finally:
            if acquired:
                self._ai_lock.release()

    def note_ai_cancelled(self):
        with self._condition:
            self._cancelled_ai += 1

    def configure(self, policy):
        policy = validate_policy(policy)
        with self._condition:
            self._policy = policy
            self._cool_samples = 0
            # A settings reload must not turn an old sample into fresh telemetry.
            self._change_state("warming")
            self._condition.notify_all()

    def begin_shutdown(self):
        with self._condition:
            self._closing = True
            self._stop.set()
            self._condition.notify_all()

    def stop(self, timeout=2):
        self.begin_shutdown()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)

    def snapshot(self):
        with self._condition:
            state = self._state_now()
            age = None if self._sample_at is None else max(0, self._clock() - self._sample_at)
            return {
                "state": state, "work_allowed": state in {"active", "throttled"},
                "cpu_percent": self._cpu, "sample_age_seconds": None if age is None else round(age, 3),
                "priority": dict(self._priority), "policy": dict(self._policy),
                "delay_seconds": round(self._delay(), 5), "cool_samples": self._cool_samples,
                "waiting_checkpoints": self._waiters, "active_ai_requests": self._active_ai,
                "cancelled_ai_requests": self._cancelled_ai, "deferred_checks": self._deferred_checks,
                "transitions": self._transitions, "ai_thread_limit": self.ai_thread_limit(),
                "ai_work_allowed": state in {"active", "throttled"} and self._cpu_count >= 2,
                "ai_defer_reason": "A single-core host has no spare core for AI inference." if self._cpu_count < 2 else None,
                "cpu_logical_count": self._cpu_count, "error": self._error,
                "scope": "This process and its cooperative work; only this application's Ollama requests are cancelled.",
            }
