import copy
import logging
import ntpath
import os
from pathlib import Path, PureWindowsPath
import posixpath
import re
import sys

import yaml

from Services.ClassificationService import TYPES

logger = logging.getLogger("code-steward.config")

# The file next to the program is used no matter which folder the server is started from.
# Set CODE_STEWARD_CONFIG to use another file (for example personal settings).
PROGRAM_FOLDER = Path(__file__).resolve().parents[1]
CONFIG_PATH = os.environ.get("CODE_STEWARD_CONFIG") or str(PROGRAM_FOLDER / "config.yaml")

# Every setting and its default. A value missing from config.yaml uses the default below.
DEFAULTS = {
    "service": {"auto_start": True, "tick_seconds": 5},
    "scanning": {
        "root_paths": ["watched"],
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
        "settings_file": "ai/settings/default.json",
        "analyze_types": ["script", "wrapper"],
        "max_attempts": 3,
    },
    "organization": {"topics_file": "ai/settings/topics.json", "library_path": "library", "git": True,
                     "suggest_reuse": True, "reuse_max_groups": 12, "reuse_max_pairs": 1, "reuse_batch_size": 3},
}
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
SECRET_WORDS = ("password", "passwd", "secret", "token", "api_key", "apikey", "credential", "private_key")
SYSTEM_KEYS = ("windows", "mac", "linux")


def system_key(platform=None):
    """The config branch for this operating system (platform: check another system's settings)."""
    platform = sys.platform if platform is None else platform
    return "windows" if platform == "win32" else "mac" if platform == "darwin" else "linux"


def _path_branch(value, setting, multiple):
    if multiple:
        values = [value] if isinstance(value, str) else value
        if not isinstance(values, list) or not all(isinstance(item, str) and item.strip() for item in values):
            raise ValueError(f"{setting} must be a path or a list of nonempty paths")
        return values
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{setting} must be a nonempty path")
    return [value]


def _select_paths(value, platform, setting, multiple):
    system = system_key(platform)
    if isinstance(value, dict):
        unknown = [str(key) for key in value if key not in SYSTEM_KEYS]
        if unknown:
            raise ValueError(f"{setting} has unknown system keys: {', '.join(unknown)}; use windows, mac or linux")
        # Check every branch's structure, but a foreign system's paths are not interpreted here.
        for key, branch in value.items():
            _path_branch(branch, f"{setting}.{key}", multiple)
        if system not in value:
            if multiple:
                return []
            raise ValueError(f"{setting} needs a {system} path for this operating system")
        value = value[system]
    paths = _path_branch(value, setting, multiple)
    path_module = ntpath if system == "windows" else posixpath
    selected = []
    for path in paths:
        expanded = path_module.expanduser(path)
        if "\0" in expanded or _foreign(expanded, system):
            raise ValueError(f"{setting} path {path!r} is not a {system} path; write a folder inside the program "
                             f"folder (for example watched) or an absolute {system} path")
        absolute = PureWindowsPath(expanded).is_absolute() if system == "windows" else posixpath.isabs(expanded)
        if not absolute:
            # A relative path is a folder inside the program folder: the same folder on every system.
            expanded = path_module.join(str(PROGRAM_FOLDER), expanded)
        selected.append(path_module.normpath(expanded))
    return selected


def _foreign(path, system):
    """True for a path written for another system: a drive or network path off Windows; on Windows a path
    that starts with a slash but has no drive, or a drive without a slash (C:tools)."""
    if system == "windows":
        windows = PureWindowsPath(path)
        return not windows.is_absolute() and bool(windows.drive or windows.root)
    return bool(re.match(r"^[A-Za-z]:", path)) or path.startswith("\\\\")


def select_system_paths(value, *, platform=None, setting="paths"):
    """Select and expand a list/string or per-system mapping without changing the raw settings.

    Relative paths are folders inside the program folder; absolute paths are used as written."""
    return _select_paths(value, platform, setting, multiple=True)


def select_system_path(value, *, platform=None, setting="path"):
    """Select and expand a single path, optionally written as a per-system mapping."""
    return _select_paths(value, platform, setting, multiple=False)[0]


def _contains(folder, path, platform=None):
    """True when path is folder or inside it; Windows and macOS compare without case."""
    system = system_key(platform)
    module = ntpath if system == "windows" else posixpath
    folder, path = module.normpath(folder), module.normpath(path)
    if system in ("windows", "mac"):
        folder, path = folder.casefold(), path.casefold()
    return path == folder or path.startswith(folder.rstrip(module.sep) + module.sep)


def validate(loaded, platform=None):
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
    for key in ("root_paths", "deny_paths"):
        value = config["scanning"][key]
        try:
            select_system_paths(value, platform=platform, setting=f"scanning.{key}")
        except ValueError as error:
            problems.append(str(error))
        if isinstance(value, str):
            config["scanning"][key] = [value]  # retain the legacy single-folder form
    for key in ("include_extensions", "exclude_folders"):
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
    try:
        library = select_system_path(config["organization"]["library_path"], platform=platform,
                                     setting="organization.library_path")
        roots = select_system_paths(config["scanning"]["root_paths"], platform=platform, setting="scanning.root_paths")
    except ValueError as error:
        problems.append(str(error))
    else:
        for root in roots:
            if _contains(library, root, platform):
                problems.append(f"scanning.root_paths {root!r} is inside organization.library_path; "
                                "the library is never scanned, so choose folders that do not contain each other")
    flag("organization", "git")
    flag("organization", "suggest_reuse")
    number("organization", "reuse_batch_size", 1, 6, whole=True)
    number("organization", "reuse_max_pairs", 0, 10, whole=True)
    number("organization", "reuse_max_groups", 1, 200, whole=True)
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
