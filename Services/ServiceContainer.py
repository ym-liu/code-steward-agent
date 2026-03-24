from Services.ArtifactsService import ArtifactsService
from Services.ConfigService import ConfigService
from Services.LogsService import LogsService
from Services.ScanService import ScanService
from Services.SystemService import SystemService


class ServiceContainer:
    def __init__(self):
        self.config_service = ConfigService()
        self.logs_service = LogsService()
        self.artifacts_service = ArtifactsService()
        self.scan_service = ScanService(
            config_service=self.config_service,
            logs_service=self.logs_service,
        )
        self.system_service = SystemService(scan_service=self.scan_service)


services = ServiceContainer()
