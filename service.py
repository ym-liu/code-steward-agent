import time
import signal
import logging
import threading
from datetime import datetime
from enum import Enum


#SERVICE STATE

class ServiceState(Enum):
    STOPPED = "stopped"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPING = "stopping"

#LOGGER SETUP

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("service.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("code-steward.service")

#CONTROL METHODS

class CodeStewardService:

    TICK_INTERVAL_SECONDS = 5

    def __init__(self, file_event_service=None):
        self.state = ServiceState.STOPPED
        self.started_at = None
        self.tick_count = 0
        self.file_event_service = file_event_service

    def start(self):
        if self.state != ServiceState.STOPPED:
            logger.warning("Service is already running.")
            return
        logger.info("service starting...")
        self.state = ServiceState.RUNNING
        self.started_at = datetime.utcnow()
        self.tick_count = 0

        if self.file_event_service is not None:
            self.file_event_service.start()

        thread = threading.Thread(target=self.run_loop, daemon=True)
        thread.start()

    def stop(self):
        if self.state == ServiceState.STOPPED:
            logger.warning("Service is already stopped.")
            return
        logger.info("Service stopping...")
        self.state = ServiceState.STOPPING

    def pause(self):
        if self.state != ServiceState.RUNNING:
            logger.warning("Service is not running")
            return
        logger.info("Service paused")
        self.state = ServiceState.PAUSED

    def resume(self):
        if self.state != ServiceState.PAUSED:
            logger.warning("Service is not paused.")
            return
        logger.info("Service resumed")
        self.state = ServiceState.RUNNING

    def get_status(self) -> dict:
        return{
            "state": self.state.value,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "tick_count": self.tick_count,
            "uptime_seconds": (
                (datetime.utcnow() - self.started_at).total_seconds()
                if self.started_at else 0
            ),
        }

#INTERNAL LOOP

    def run_loop(self):
        logger.info("Service loop started (tick interval: %ss)", self.TICK_INTERVAL_SECONDS)

        while self.state != ServiceState.STOPPING:

            if self.state == ServiceState.PAUSED:
                time.sleep(self.TICK_INTERVAL_SECONDS)
                continue
            self.tick_count += 1
            self.do_work()

            time.sleep(self.TICK_INTERVAL_SECONDS)

        self.on_shutdown()


    def do_work(self):
        """
        we can add more function calls
        like call the scanner or classifier...

        """
        logger.info("Tick #%d - service running.", self.tick_count)

    def on_shutdown(self):
        logger.info(
            "Service shutting down after %d ticks (uptime: %.1fs)",
            self.tick_count,
            (datetime.utcnow() - self.started_at).total_seconds() if self.started_at else 0,
        )
        if self.file_event_service is not None:
            self.file_event_service.stop()
        self.state = ServiceState.STOPPED
        self.started_at = None

#SIGNAL HANDLERS

def attach_signal_handlers(service):
    def handle(sig, frame):
        sig_name = signal.Signals(sig).name
        logger.info("Received signal %s requesting shutdown", sig_name)
        service.stop()

    signal.signal(signal.SIGINT,  handle)
    signal.signal(signal.SIGTERM, handle)
    logger.info("Signal handlers registered (SIGINT, SIGTERM)")