class LogsService:
    def __init__(self):
        pass

    def append_log(self, level: str, message: str, context=None):
        return {"message": "Append log noop."}

    def list_logs(self, limit: int = 100):
        return {"items": [], "limit": limit}
