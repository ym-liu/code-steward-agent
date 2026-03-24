class ScanService:
    def __init__(self, config_service, logs_service):
        self.config_service = config_service
        self.logs_service = logs_service

    def get_status(self):
        return {"message": "Scan status service noop."}

    def enqueue_manual_scan(self):
        return {"message": "Manual scan trigger noop."}
