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
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                loaded = yaml.safe_load(f)
            if loaded is None:
                loaded = {}
            if not isinstance(loaded, dict):
                raise ValueError("Configuration must be a YAML mapping.")
        except (OSError, yaml.YAMLError, ValueError):
            logger.exception("Configuration load failed: %s; previous settings retained.", CONFIG_PATH)
            raise
        self._config = loaded
        logger.info("Config loaded from %s", CONFIG_PATH)

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
