class ConfigService:
    def __init__(self):
        pass

    def get_config(self):
        return {}

    def reload(self):
        return {
            "message": "Config reload noop.",
        }
