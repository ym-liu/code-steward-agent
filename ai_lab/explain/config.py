"""Load and check a settings file (models, device, limits) and turn menu names into model tags."""
from ..common import SETTINGS, LabError, read_json

CONFIG_KEYS = {"base_url", "models", "default_model", "device", "timeout_seconds", "num_ctx",
               "num_predict", "num_thread", "temperature", "seed", "max_source_chars", "max_files"}
# Optional keys keep older config files valid; defaults are written into run.json.
# long_file_tokens 0 keeps every line; otherwise longer files send only key lines (see source_view).
OPTIONAL_CONFIG = {"repair_attempts": 1, "long_file_tokens": 0, "long_file_keep_tokens": 2000}


def load_config(path=None):
    config = read_json(path or SETTINGS / "legacy.json")
    if not isinstance(config, dict):
        raise LabError("Config must be a JSON object.", "config_error")
    if not CONFIG_KEYS <= set(config) or set(config) - CONFIG_KEYS - set(OPTIONAL_CONFIG):
        raise LabError(f"Config keys must be: {', '.join(sorted(CONFIG_KEYS))} "
                       f"(optional: {', '.join(sorted(OPTIONAL_CONFIG))})", "config_error")
    config = {**OPTIONAL_CONFIG, **config}
    if type(config["repair_attempts"]) is not int or config["repair_attempts"] not in (0, 1):
        raise LabError("repair_attempts must be 0 or 1.", "config_error")
    limits = {"timeout_seconds": (1, 3600), "num_ctx": (1024, 32768),
              "num_predict": (64, 4096), "num_thread": (1, 128),
              "seed": (0, 2**31 - 1), "max_source_chars": (100, 100000), "max_files": (1, 1000)}
    # "auto" leaves the thread count to Ollama, which uses the physical performance cores.
    if config["num_thread"] == "auto":
        limits.pop("num_thread")
    for key, (low, high) in limits.items():
        if type(config[key]) is not int or not low <= config[key] <= high:
            raise LabError(f"{key} must be an integer from {low} to {high}.", "config_error")
    for key, low, high in (("long_file_tokens", 0, 32768), ("long_file_keep_tokens", 500, 16000)):
        if type(config[key]) is not int or not low <= config[key] <= high:
            raise LabError(f"{key} must be an integer from {low} to {high}.", "config_error")
    if config["long_file_tokens"] and config["long_file_keep_tokens"] >= config["long_file_tokens"]:
        raise LabError("long_file_keep_tokens must be smaller than long_file_tokens.", "config_error")
    if config["num_predict"] >= config["num_ctx"]:
        raise LabError("num_predict must be smaller than num_ctx.", "config_error")
    if type(config["temperature"]) not in (int, float) or not 0 <= config["temperature"] <= 2:
        raise LabError("temperature must be between 0 and 2.", "config_error")
    if config["device"] not in ("cpu", "auto"):
        raise LabError("device must be cpu or auto.", "config_error")
    models = config["models"]
    if not isinstance(models, dict) or not models or not all(isinstance(k, str) for k in models):
        raise LabError("models must contain aliases and Ollama model tags.", "config_error")
    for tag in models.values():
        validate_model(tag)
    if not isinstance(config["default_model"], str) or config["default_model"] not in models:
        raise LabError("default_model must be an alias in models.", "config_error")
    return config


def validate_model(tag):
    if not isinstance(tag, str) or not tag or len(tag) > 200 or any(c.isspace() for c in tag):
        raise LabError("Use a non-empty Ollama model tag without spaces.", "config_error")
    if "cloud" in tag.lower() or "://" in tag or any(ord(c) < 32 for c in tag):
        raise LabError("This lab accepts local Ollama models only, not cloud models/URLs.", "config_error")
    return tag


def resolve_model(config, name):
    return validate_model(config["models"].get(name, name))
