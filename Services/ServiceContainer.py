import logging
import os
from pathlib import Path

from Services.AnalysisService import AnalysisService
from Services.ArtifactsService import ArtifactsService
import Services.ConfigService as config_file
from Services.ConfigService import ConfigService
from Services.EventAggregator import EventAggregator
from Services.ExecutionService import ExecutionService
from Services.VersionService import VersionService, ai_subject
from Services.LogsService import LogsService, configure_logging
from Services.OrganizationService import OrganizationService
from Services.ScanService import CHECKPOINT_FILE, ScanService
from Services.SystemService import SystemService
from Services.FileEventService import FileEventService
from service import CodeStewardService

PROGRAM_FOLDER = Path(__file__).resolve().parents[1]


def program_path(value):
    """Relative storage paths in config.yaml are relative to the program folder."""
    path = Path(os.path.expanduser(value))
    return path if path.is_absolute() else PROGRAM_FOLDER / path


class ServiceContainer:
    """Creates every service once and connects them. File changes flow like this:

    FileEventService (watcher) -> EventAggregator (waits, merges) -> CodeStewardService loop
    -> ScanService (checks files) -> ArtifactsService (SQLite). Full scans start in the loop.
    Every saved change also reaches AnalysisService, which explains code files with the local AI.
    OrganizationService builds the organization plan (topics and proposed folders) on request.
    """

    def __init__(self):
        self.config_service = ConfigService()
        log_path = program_path(self.config_service.get("storage", "log_path"))
        self._configure_logging(log_path)
        logging.getLogger("code-steward.config").info("Config loaded from %s", config_file.CONFIG_PATH)
        self.logs_service = LogsService(log_path)
        self.artifacts_service = ArtifactsService(str(program_path(self.config_service.get("storage", "database_path"))))
        self.aggregator = EventAggregator()
        self._configure_aggregator()
        self.scan_service = ScanService(
            config_service=self.config_service,
            logs_service=self.logs_service,
            artifacts_service=self.artifacts_service,
        )
        self._set_own_paths(log_path)
        self.scan_service.own_folder = str(PROGRAM_FOLDER)
        self._create_watched_folders()

        self.analysis_service = AnalysisService(self.config_service, self.artifacts_service)
        self.analysis_service.name_for = self._name_below_root
        self.scan_service.listeners.append(self.analysis_service)
        self.organization_service = OrganizationService(
            self.config_service, self.artifacts_service, self.analysis_service, self.scan_service)
        self.analysis_service.on_analysis_done = self.organization_service.on_analysis_done
        # The library's Git history; the local model writes each commit's subject line.
        self.version_service = VersionService(self.config_service,
                                              subject_writer=lambda facts: ai_subject(self.analysis_service, facts))
        self.execution_service = ExecutionService(
            self.config_service, self.artifacts_service, self.organization_service, self.scan_service,
            self.version_service)
        self.organization_service.placements = self.execution_service.placements  # plans keep library folders

        self.file_event_service = FileEventService(
            aggregator=self.aggregator,
            scan_service=self.scan_service,
        )

        self.code_steward_service = CodeStewardService(
            file_event_service=self.file_event_service,
            scan_service=self.scan_service,
            aggregator=self.aggregator,
            analysis_service=self.analysis_service,
        )
        # No signal handlers here: uvicorn handles Ctrl+C and shutdown, and the API's lifespan
        # (CodeSteward.py) then stops this service cleanly. Own handlers would block uvicorn's.

        self.scan_service.code_steward_service = self.code_steward_service
        self.analysis_service.code_steward_service = self.code_steward_service

        self.system_service = SystemService(
            scan_service=self.scan_service,
            code_steward_service=self.code_steward_service,
        )
        self.config_service.on_reload(self._apply_settings)

    def _name_below_root(self, path):
        """The name the AI model sees: the path below its root folder, such as lab_project/train.py."""
        root = self.scan_service.root_of(path)
        return Path(os.path.relpath(path, root)).as_posix() if root else os.path.basename(path)

    def _create_watched_folders(self):
        """Create missing watched folders inside the program folder (watched/ by default); others are left alone."""
        for root in self.scan_service.roots():
            path = Path(root)
            if not os.path.lexists(path) and path.resolve().is_relative_to(PROGRAM_FOLDER):
                path.mkdir(parents=True, exist_ok=True)
                logging.getLogger("code-steward.config").info("Created the watched folder %s", path)

    def _set_own_paths(self, log_path):
        """The agent never registers its own log (and its older copies), settings or checkpoint files."""
        own = [log_path, Path(config_file.CONFIG_PATH), Path(CHECKPOINT_FILE)]
        own += [Path(f"{log_path}.{number}") for number in range(1, 101)]
        self.scan_service.own_paths = {os.path.normcase(os.path.abspath(path)) for path in own}

    def _configure_logging(self, log_path):
        settings = self.config_service.get_config()["logging"]
        configure_logging(log_path, settings["level"], settings["max_bytes"], settings["backup_count"])

    def _configure_aggregator(self):
        events = self.config_service.get_config()["events"]
        self.aggregator.configure(events["quiet_seconds"], events["max_wait_seconds"], events["max_pending_files"])

    def _apply_settings(self, settings):
        """After POST /config/reload: new logging, waiting rules and watched folders, then a full scan.

        storage.database_path takes effect at the next start of the API.
        """
        log_path = program_path(settings["storage"]["log_path"])
        self._configure_logging(log_path)
        self.logs_service.log_file = log_path
        self._set_own_paths(log_path)
        self._configure_aggregator()
        self.analysis_service.reload_settings()
        self.organization_service.set_plan_mode(self.config_service.get("organization", "plan_mode", default="manual"), quiet=True)
        self._create_watched_folders()
        if self.code_steward_service.state.value != "stopped":
            self.file_event_service.restart()
            self.code_steward_service.request_full_scan("settings-scan")
            self.analysis_service.queue_missing()  # a new model or prompt means new explanations


services = ServiceContainer()
