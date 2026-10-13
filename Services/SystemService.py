import os
import sys
import threading
import time


class SystemService:
    def __init__(self, scan_service, code_steward_service):
        self.scan_service = scan_service
        self.service = code_steward_service
        self._last_cpu = None

    def get_health(self):
        return {"status": "ok"}

    def get_status(self):
        return self.service.get_status()

    # Each control returns "changed": False when the service was already in another state;
    # the API reports that as HTTP 409 with the message.

    def start(self):
        changed = self.service.start()
        return self._result(changed, "Service started.", "Service is already running.")

    def stop(self):
        changed = self.service.stop()
        return self._result(changed, "Service stopping.", "Service is already stopped.")

    def pause(self):
        changed = self.service.pause()
        return self._result(changed, "Service paused.", "Service is not running, so it cannot be paused.")

    def resume(self):
        changed = self.service.resume()
        return self._result(changed, "Service resumed.", "Service is not paused, so it cannot be resumed.")

    def _result(self, changed, done, refused):
        return {"changed": changed, "message": done if changed else refused, "state": self.service.state.value}

    def get_metrics(self):
        """Resource use of this process, for measuring idle and busy load without extra tools."""
        times = os.times()
        cpu_seconds = times.user + times.system
        now = time.monotonic()
        cpu_percent = None
        if self._last_cpu is not None and now > self._last_cpu[1]:
            # Share of one CPU core used since the previous call.
            cpu_percent = round((cpu_seconds - self._last_cpu[0]) / (now - self._last_cpu[1]) * 100, 1)
        self._last_cpu = (cpu_seconds, now)
        return {
            "cpu_seconds_total": round(cpu_seconds, 2),
            "cpu_percent_since_last_call": cpu_percent,
            "memory_peak_mb": _peak_memory_mb(),
            "threads": threading.active_count(),
            "service_state": self.service.state.value,
            "waiting_changes": self.service.aggregator.status() if self.service.aggregator is not None else None,
            "is_scanning": self.scan_service.get_status()["is_scanning"],
        }


def _peak_memory_mb():
    """Peak memory of this process in MB, or None where the platform does not report it."""
    try:
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes

            class Counters(ctypes.Structure):
                _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                            ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                            ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]
            counters = Counters()
            counters.cb = ctypes.sizeof(counters)
            process = ctypes.windll.kernel32.GetCurrentProcess()
            if ctypes.windll.psapi.GetProcessMemoryInfo(process, ctypes.byref(counters), counters.cb):
                return round(counters.PeakWorkingSetSize / 1024 / 1024, 1)
            return None
        import resource
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return round(peak / 1024 / 1024 if sys.platform == "darwin" else peak / 1024, 1)  # bytes on macOS, KB on Linux
    except (ImportError, OSError, AttributeError):
        return None
