import yaml
import logging
import os

logger = logging.getLogger("code-steward.config")

CONFIG_PATH = "config.yaml"

class ConfigService:
    def __init__(self):
        self._config = {}
        self._load()
        
    def _load(self):
        if not os.path.exists(CONFIG_PATH):
            logger.warning("config.yaml not found, using empty config.")
            self._config = {}
            return
        with open(CONFIG_PATH, "r") as f:
            self._config = yaml.safe_load(f) or {}
        logger.info("Config loaded form %s", CONFIG_PATH)

    def get_config(self):
         return self._config
        
    def get(self, *keys, default=None):
        """
        Safely get a nested config value.
        Exemple: config_service.get("scanning", "root_paths", default=[])
        """
        value = self._config
        for key in keys:
            if not isinstance(value, dict):
                return default
            value = value.get(key, default)
        return value
    

    def reload(self):
        self._load()
        return {"message": "Config reload successfully."}
