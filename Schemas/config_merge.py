"""Merge `ConfigUpdateRequest` into a `ConfigSnapshot` without dropping unspecified nested keys."""

from __future__ import annotations

from pydantic import ValidationError

from Schemas.api_models import ConfigSnapshot, ConfigUpdateRequest, ValidationErrorItem


def apply_config_patch(current: ConfigSnapshot, patch: ConfigUpdateRequest) -> tuple[ConfigSnapshot | None, list[ValidationErrorItem]]:
    """
    Returns (new_snapshot, errors). On validation failure, new_snapshot is None.
    """
    data = current.model_dump()
    raw = patch.model_dump(exclude_unset=True)

    for key, value in raw.items():
        if key == "safety" and value is not None:
            merged = current.safety.model_dump()
            for sk, sv in value.items():
                if sv is not None:
                    merged[sk] = sv
            data["safety"] = merged
        elif key == "runtime" and value is not None:
            merged = current.runtime.model_dump()
            for rk, rv in value.items():
                if rv is not None:
                    merged[rk] = rv
            data["runtime"] = merged
        else:
            data[key] = value

    try:
        return ConfigSnapshot.model_validate(data), []
    except ValidationError as err:
        errors: list[ValidationErrorItem] = []
        for e in err.errors():
            loc = ".".join(str(x) for x in e.get("loc", ()))
            errors.append(ValidationErrorItem(field=loc or "(root)", message=e.get("msg", "Invalid value")))
        return None, errors
