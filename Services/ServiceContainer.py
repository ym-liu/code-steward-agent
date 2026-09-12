from Services.ArtifactsService import ArtifactsService
from Services.ConfigService import ConfigService
from Services.LogsService import LogsService
from Services.ScanService import ScanService
from Services.SystemService import SystemService
from Services.FileEventService import FileEventService
from service import CodeStewardService, attach_signal_handlers


class ServiceContainer:
    def __init__(self):
        self.config_service = ConfigService()
        self.logs_service = LogsService()
        self.artifacts_service = ArtifactsService()
        self.scan_service = ScanService(
            config_service=self.config_service,
            logs_service=self.logs_service,
            artifacts_service=self.artifacts_service,
        )

        root_paths = self.config_service.get("scanning", "root_paths", default=[])
        self.file_event_service = FileEventService(
            scan_service=self.scan_service,
            target_directories=root_paths,
        )

        self.code_steward_service = CodeStewardService(
            file_event_service=self.file_event_service,
        )
        attach_signal_handlers(self.code_steward_service)

        self.scan_service.code_steward_service = self.code_steward_service

        self.system_service = SystemService(
            scan_service=self.scan_service,
            code_steward_service=self.code_steward_service,
        )


services = ServiceContainer()
