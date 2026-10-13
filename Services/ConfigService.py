import copy
import logging
import os
from pathlib import Path

import yaml

from Services.ClassificationService import TYPES

logger = logging.getLogger("code-steward.config")

# The file next to the program is used no matter which folder the server is started from.
# Set CODE_STEWARD_CONFIG to use another file (for example in tests).
CONFIG_PATH = os.environ.get("CODE_STEWARD_CONFIG") or str(Path(__file__).resolve().parents[1] / "config.yaml")

# Every setting and its default. A value missing from config.yaml uses the default below.
DEFAULTS = {
    "service": {"auto_start": True, "tick_seconds": 5},
    "scanning": {
        "root_paths": [],
        "include_extensions": [],
        "exclude_folders": [],
        "deny_paths": [],
        "max_file_size_bytes": 10485760,
        "scan_interval_seconds": 300,
        "watch_mode": "auto",
        "polling_interval_seconds": 30,
    },
    "events": {"quiet_seconds": 2, "max_wait_seconds": 30, "max_pending_files": 10000},
    "logging": {"level": "INFO", "max_bytes": 5242880, "backup_count": 3},
    "storage": {"database_path": "artifacts.sqlite3", "log_path": "service.log"},
    "testing": {"allow_event_injection": False},
    "ai": {
        "enabled": True,
        "model": "qwen-4b",
        "device": "cpu",
        "num_thread": "auto",
        "settings_file": "ai_lab/settings/default.json",
        "analyze_types": ["script", "wrapper"],
        "max_attempts": 3,
    },
    "organization": {"topics_file": "ai_lab/settings/topics.json"},
}
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
SECRET_WORDS = ("password", "passwd", "secret", "token", "api_key", "apikey", "credential", "private_key")


def validate(loaded):
    """Return the settings with defaults filled in, or raise ValueError listing every problem."""
    config = copy.deepcopy(loaded)
    problems = []
    for section, defaults in DEFAULTS.items():
        values = config.get(section)
        if values is None:
            values = {}
        if not isinstance(values, dict):
            problems.append(f"{section} must be a section of settings")
            values = {}
        config[section] = {**copy.deepcopy(defaults), **values}

    def number(section, key, low, high=None, whole=False):
        value = config[section][key]
        kinds = int if whole else (int, float)
        if isinstance(value, bool) or not isinstance(value, kinds) or value < low or (high is not None and value > high):
            limit = f"from {low} to {high}" if high is not None else f"of at least {low}"
            problems.append(f"{section}.{key} must be a {'whole number' if whole else 'number'} {limit}")

    def flag(section, key):
        if not isinstance(config[section][key], bool):
            problems.append(f"{section}.{key} must be true or false")

    def text_list(section, key):
        value = config[section][key]
        if isinstance(value, str):
            value = config[section][key] = [value]  # one folder written on the same line
        if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
            problems.append(f"{section}.{key} must be a list of text values, one per line starting with '- '")

    def choice(section, key, options):
        if config[section][key] not in options:
            problems.append(f"{section}.{key} must be one of: {', '.join(options)}")

    flag("service", "auto_start")
    number("service", "tick_seconds", 0.5, 3600)
    for key in ("root_paths", "include_extensions", "exclude_folders", "deny_paths"):
        text_list("scanning", key)
    extensions = config["scanning"]["include_extensions"]
    if isinstance(extensions, list) and all(isinstance(item, str) for item in extensions):
        if any(not item.startswith(".") for item in extensions):
            problems.append("scanning.include_extensions must start with a dot, for example .py")
        config["scanning"]["include_extensions"] = [item.lower() for item in extensions]
    number("scanning", "max_file_size_bytes", 0, whole=True)
    number("scanning", "scan_interval_seconds", 10)
    choice("scanning", "watch_mode", ("auto", "events", "polling"))
    number("scanning", "polling_interval_seconds", 1)
    number("events", "quiet_seconds", 0, 600)
    number("events", "max_wait_seconds", 0, 3600)
    number("events", "max_pending_files", 10, whole=True)
    if not problems and config["events"]["max_wait_seconds"] < config["events"]["quiet_seconds"]:
        problems.append("events.max_wait_seconds must not be smaller than events.quiet_seconds")
    choice("logging", "level", LOG_LEVELS)
    number("logging", "max_bytes", 0, whole=True)
    number("logging", "backup_count", 0, 100, whole=True)
    for key in ("database_path", "log_path"):
        if not isinstance(config["storage"][key], str) or not config["storage"][key].strip():
            problems.append(f"storage.{key} must be a file name or path")
    flag("testing", "allow_event_injection")
    flag("ai", "enabled")
    for key in ("model", "settings_file"):
        if not isinstance(config["ai"][key], str) or not config["ai"][key].strip():
            problems.append(f"ai.{key} must be a name or path")
    choice("ai", "device", ("cpu", "auto"))
    if config["ai"]["num_thread"] != "auto":
        number("ai", "num_thread", 1, 128, whole=True)
    text_list("ai", "analyze_types")
    if isinstance(config["ai"]["analyze_types"], list) and set(config["ai"]["analyze_types"]) - set(TYPES):
        problems.append("ai.analyze_types must use these types: " + ", ".join(TYPES))
    number("ai", "max_attempts", 1, 10, whole=True)
    if not isinstance(config["organization"]["topics_file"], str) or not config["organization"]["topics_file"].strip():
        problems.append("organization.topics_file must be a file name or path")
    if problems:
        raise ValueError("Invalid configuration: " + "; ".join(problems) + ".")
    return config


def masked(value):
    """Copy of the settings with passwords, tokens and keys hidden, for showing through the API."""
    if isinstance(value, dict):
        return {key: "***" if any(word in str(key).lower() for word in SECRET_WORDS) and item not in (None, "")
                else masked(item) for key, item in value.items()}
    if isinstance(value, list):
        return [masked(item) for item in value]
    return value


class ConfigService:
    def __init__(self):
        self._config = {}
        self._listeners = []
        self._load()

    def _load(self):
        path = CONFIG_PATH
        if not os.path.exists(path):
            logger.warning("%s not found, using default settings.", path)
            self._config = validate({})
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                loaded = yaml.safe_load(f)
            if loaded is None:
                loaded = {}
            if not isinstance(loaded, dict):
                raise ValueError("Configuration must be a YAML mapping.")
            config = validate(loaded)
        except (OSError, yaml.YAMLError, ValueError):
            logger.exception("Configuration load failed: %s; previous settings retained.", path)
            raise
        self._config = config
        logger.info("Config loaded from %s", path)

    def get_config(self):
        return self._config

    def public_config(self):
        return masked(self._config)

    def get(self, *keys, default=None):
        """
        Safely get a nested config value.
        Example: config_service.get("scanning", "root_paths", default=[])
        """
        value = self._config
        for key in keys:
            if not isinstance(value, dict):
                return default
            value = value.get(key, default)
        return value

    def on_reload(self, callback):
        """Call callback(settings) after every successful reload, e.g. to watch new folders."""
        self._listeners.append(callback)

    def reload(self):
        self._load()
        for callback in self._listeners:
            try:
                callback(self._config)
            except Exception:
                logger.exception("Applying the reloaded configuration failed.")
        return {"message": "Config reload successfully."}
