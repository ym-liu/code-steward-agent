"""Watchdog polling that defers complete snapshots while the CPU is busy.

Native filesystem event capture remains unchanged. Polling retains the previous
complete snapshot during pauses, so the next diff observes accumulated changes.
"""
from functools import partial
import os

from watchdog.events import DirDeletedEvent
from watchdog.observers.api import BaseObserver
from watchdog.observers.polling import PollingEmitter

from Services.ResourceGovernor import ResourceCancelled


class GovernedPollingEmitter(PollingEmitter):
    def __init__(self, event_queue, watch, *, governor, on_initial_snapshot=None, **kwargs):
        self.resource_governor = governor
        self._on_initial_snapshot = on_initial_snapshot
        self._snapshot_ready = False
        super().__init__(event_queue, watch, stat=self._stat, listdir=self._listdir, **kwargs)

    def _checkpoint(self):
        self.resource_governor.checkpoint(cancel=self.stopped_event.is_set)

    def _stat(self, path):
        self._checkpoint()
        return os.stat(path)

    def _listdir(self, path):
        with os.scandir(path) as entries:
            for entry in entries:
                self._checkpoint()
                yield entry

    def on_thread_start(self):
        # Watchdog calls this synchronously from start(). Delay initial traversal
        # to the worker so a busy CPU never blocks API/application startup.
        pass

    def queue_events(self, timeout):
        if self._snapshot_ready and self.stopped_event.wait(timeout):
            return
        try:
            self._checkpoint()
            if not self._snapshot_ready:
                self._snapshot = self._take_snapshot()
                self._snapshot_ready = True
                if self._on_initial_snapshot is not None:
                    # Reconcile changes between the service's startup scan and
                    # this possibly CPU-delayed first polling baseline.
                    self._on_initial_snapshot()
                return
            super().queue_events(0)
        except ResourceCancelled:
            self.stop()
        except OSError:
            self.queue_event(DirDeletedEvent(self.watch.path))
            self.stop()


class GovernedPollingObserver(BaseObserver):
    def __init__(self, governor, *, timeout=30, on_initial_snapshot=None):
        super().__init__(partial(GovernedPollingEmitter, governor=governor,
                                 on_initial_snapshot=on_initial_snapshot), timeout=timeout)
