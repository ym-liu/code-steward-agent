"""Configuration: in-memory snapshot (file IO later). PATCH merges; reload is explicit noop hook."""

from __future__ import annotations

from Schemas.api_models import ConfigReloadResponse, ConfigSnapshot, ConfigUpdateRequest, ConfigUpdateResponse
from Schemas.config_merge import apply_config_patch


class ConfigService:
    def __init__(self, logs_service):
        self._logs = logs_service
        self._config = ConfigSnapshot()

    def get_config(self) -> ConfigSnapshot:
        return self._config

    def patch_config(self, body: ConfigUpdateRequest) -> ConfigUpdateResponse:
        new_snapshot, errors = apply_config_patch(self._config, body)
        if new_snapshot is None:
            self._logs.append_log(
                level="ERROR",
                message="Configuration update rejected (validation failed).",
                category="config",
            )
            return ConfigUpdateResponse(ok=False, message="Validation failed.", errors=errors, config=self._config)
        self._config = new_snapshot
        self._logs.append_log(
            level="INFO",
            message="Configuration updated.",
            category="config",
        )
        return ConfigUpdateResponse(ok=True, message="Configuration updated.", errors=[], config=self._config)

    def reload(self) -> ConfigReloadResponse:
        # Future: re-read JSON from disk, validate, then swap in-memory snapshot.
        return ConfigReloadResponse(reloaded=False, config=self._config)
