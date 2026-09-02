class SystemService:
    def __init__(self, scan_service, code_steward_service):
        self.scan_service = scan_service
        self.service = code_steward_service

    def get_health(self):
        return {"status": "ok"}

    def get_status(self):
        return self.service.get_status()

    def start(self):
        self.service.start()
        return {"message": "Service started."}

    def stop(self):
        self.service.stop()
        return {"message": "Service stopping."}

    def pause(self):
        self.service.pause()
        return {"message": "Service paused."}

    def resume(self):
        self.service.resume()
        return {"message": "Service resumed."}