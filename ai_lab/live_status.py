"""One terminal status line, with native whole-machine counters and no extra packages."""
from collections import deque
import ctypes
from pathlib import Path
import shutil
import sys
import threading
import time


def clock_text(seconds):
    seconds = max(0, int(seconds))
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes:02d}:{seconds:02d}"


class MemoryStatus(ctypes.Structure):
    _fields_ = [("length", ctypes.c_uint32), ("load", ctypes.c_uint32)] + [
        (name, ctypes.c_uint64) for name in
        ("total", "available", "page_total", "page_available", "virtual_total", "virtual_available", "extended")
    ]


class FileTime(ctypes.Structure):
    _fields_ = [("low", ctypes.c_uint32), ("high", ctypes.c_uint32)]

    @property
    def ticks(self):
        return self.low + (self.high << 32)


class SystemSampler:
    """System CPU percent (all cores), and physical RAM used/total in GiB."""
    def __init__(self):
        self.previous = None
        self.native = None
        self.total = None
        try:
            if sys.platform == "darwin":
                self.native = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
                self.native.mach_host_self.restype = ctypes.c_uint32
                self.host = self.native.mach_host_self()
                self.native.host_statistics.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_void_p,
                                                        ctypes.POINTER(ctypes.c_uint32)]
                total = ctypes.c_uint64()
                size = ctypes.c_size_t(ctypes.sizeof(total))
                self.native.sysctlbyname.argtypes = [ctypes.c_char_p, ctypes.c_void_p,
                                                    ctypes.POINTER(ctypes.c_size_t), ctypes.c_void_p, ctypes.c_size_t]
                if self.native.sysctlbyname(b"hw.memsize", ctypes.byref(total), ctypes.byref(size), None, 0) == 0:
                    self.total = total.value
                self.page_size = self.native.getpagesize()
            elif sys.platform == "win32":
                self.native = ctypes.WinDLL("kernel32", use_last_error=True)
                self.native.GetSystemTimes.argtypes = [ctypes.POINTER(FileTime)] * 3
                self.native.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(MemoryStatus)]
        except (OSError, AttributeError):
            self.native = None

    def _mac_stats(self, flavor, length):
        data = (ctypes.c_uint32 * length)()
        count = ctypes.c_uint32(length)
        if self.native.host_statistics(self.host, flavor, data, ctypes.byref(count)) != 0:
            raise OSError("System counters unavailable")
        return data

    def _cpu_ticks(self):
        if sys.platform == "darwin" and self.native:
            ticks = self._mac_stats(3, 4)  # HOST_CPU_LOAD_INFO: user, system, idle, nice.
            return ticks[2], sum(ticks)
        if sys.platform == "win32" and self.native:
            idle, kernel, user = FileTime(), FileTime(), FileTime()
            if not self.native.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
                raise OSError("System counters unavailable")
            # Kernel ticks already include idle ticks.
            return idle.ticks, kernel.ticks + user.ticks
        if sys.platform.startswith("linux"):
            values = [int(value) for value in Path("/proc/stat").read_text().splitlines()[0].split()[1:9]]
            return values[3] + values[4], sum(values)  # Guest time is already in user/nice.
        raise OSError("System counters unavailable")

    def _memory(self):
        if sys.platform == "darwin" and self.native and self.total:
            pages = self._mac_stats(2, 15)  # HOST_VM_INFO, including all legacy fields.
            # Free includes speculative pages already. Exclude inactive cache too.
            available = (pages[0] + pages[2]) * self.page_size
            return self.total - available, self.total
        if sys.platform == "win32" and self.native:
            status = MemoryStatus()
            status.length = ctypes.sizeof(status)
            if not self.native.GlobalMemoryStatusEx(ctypes.byref(status)):
                raise OSError("System counters unavailable")
            return status.total - status.available, status.total
        if sys.platform.startswith("linux"):
            values = {key: int(value.split()[0]) * 1024 for key, value in
                      (line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())}
            return values["MemTotal"] - values["MemAvailable"], values["MemTotal"]
        raise OSError("System counters unavailable")

    def sample(self):
        result = {"cpu_percent": None, "ram_used_gib": None, "ram_total_gib": None}
        try:
            current = self._cpu_ticks()
            if self.previous:
                idle, total = (current[index] - self.previous[index] for index in (0, 1))
                if total > 0 and 0 <= idle <= total:
                    result["cpu_percent"] = 100 * (1 - idle / total)
            self.previous = current
        except (OSError, ValueError, IndexError, AttributeError):
            self.previous = None
        try:
            used, total = self._memory()
            if total > 0:
                result.update(ram_used_gib=max(0, min(used, total)) / 1024**3, ram_total_gib=total / 1024**3)
        except (OSError, ValueError, KeyError, IndexError, AttributeError):
            pass
        return result

    def close(self):
        if sys.platform == "darwin" and self.native:
            # Release the host-port send right obtained by mach_host_self.
            self.native.mach_task_self.restype = ctypes.c_uint32
            self.native.mach_port_deallocate(self.native.mach_task_self(), self.host)


class LiveStatus:
    """Refresh at most once per second; never print the model's generated text."""
    def __init__(self, sampler=None, stream=None, clock=time.perf_counter):
        self.sampler = sampler or SystemSampler()
        self.stream = sys.stdout if stream is None else stream
        self.clock = clock
        self.started = clock()
        self.first_chunk = None
        self.chunks = deque(maxlen=8192)
        self.latest = {}
        self.peaks = {"system_cpu_peak_percent": None, "system_ram_peak_gib": None,
                      "system_ram_total_gib": None, "resource_samples": 0}
        self.visible = bool(getattr(self.stream, "isatty", lambda: False)())
        self.lock = threading.Lock()
        self.ended = threading.Event()
        self.thread = None
        self.width = 0

    def on_chunk(self, chunk):
        message = chunk.get("message") or {}
        if message.get("content") or message.get("thinking"):
            now = self.clock()
            with self.lock:
                if self.first_chunk is None:
                    self.first_chunk = now
                self.chunks.append(now)

    def text(self, now=None):
        now = self.clock() if now is None else now
        with self.lock:
            speed = "Preparing"
            if self.first_chunk is not None:
                elapsed = min(5.0, now - self.first_chunk)
                count = sum(max(self.first_chunk, now - 5) < stamp <= now for stamp in self.chunks)
                speed = f"~{count / elapsed:.1f} tok/s" if elapsed >= 0.25 else "Generating"
            cpu = self.latest.get("cpu_percent")
            used, total = self.latest.get("ram_used_gib"), self.latest.get("ram_total_gib")
            cpu_text = f"{cpu:.0f}%" if cpu is not None else "--"
            ram_text = f"{used:.1f}/{total:.1f} GiB" if used is not None and total is not None else "--"
            return f"    {clock_text(now - self.started)} | {speed} | CPU {cpu_text} | RAM {ram_text}"

    def refresh(self):
        try:
            snapshot = self.sampler.sample()
        except (OSError, AttributeError, ValueError):
            snapshot = {}
        with self.lock:
            self.latest = snapshot
            self.peaks["resource_samples"] += 1
            for source, target in (("cpu_percent", "system_cpu_peak_percent"), ("ram_used_gib", "system_ram_peak_gib")):
                value = snapshot.get(source)
                if value is not None:
                    previous = self.peaks[target]
                    self.peaks[target] = max(value, previous) if previous is not None else value
            self.peaks["system_ram_total_gib"] = snapshot.get("ram_total_gib") or self.peaks["system_ram_total_gib"]
        if self.visible:
            limit = max(1, shutil.get_terminal_size((80, 24)).columns - 1)
            text = self.text()[:limit]
            try:
                self.stream.write("\r" + text.ljust(min(limit, self.width)))
                self.stream.flush()
                self.width = len(text)
            except (OSError, ValueError):
                self.visible = False

    def start(self):
        self.refresh()
        def loop():
            while not self.ended.wait(1):
                self.refresh()
        self.thread = threading.Thread(target=loop, name="ai-lab-status", daemon=True)
        self.thread.start()

    def stop(self):
        self.ended.set()
        if self.thread:
            self.thread.join(timeout=2)
        if self.visible and self.width:
            try:
                self.stream.write("\r" + " " * self.width + "\r")
                self.stream.flush()
            except (OSError, ValueError):
                pass
        try:
            self.sampler.close()
        except (OSError, AttributeError):
            pass

    def summary(self):
        return {key: round(value, 2) if isinstance(value, float) else value for key, value in self.peaks.items()}
