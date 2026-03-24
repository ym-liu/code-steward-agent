class SystemService:
    def __init__(self, scan_service):
        self.scan_service = scan_service

    def get_health(self):
        return {"message": "System health service noop."}

    def get_status(self):
        return {"message": "System status service noop."}
