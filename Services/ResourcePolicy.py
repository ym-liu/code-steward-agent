"""Validated defaults for the agent's mandatory background CPU policy."""
import math


DEFAULTS = {
    "sample_seconds": 1.0,
    "slow_cpu_percent": 50.0,
    "pause_cpu_percent": 80.0,
    "resume_cpu_percent": 40.0,
    "resume_samples": 3,
    "max_sample_age_seconds": 3.0,
    "max_delay_seconds": 0.02,
    "ai_max_threads": 2,
}


def validate_policy(value=None):
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ValueError("resource_control must be a section of settings")
    unknown = set(value) - set(DEFAULTS)
    if unknown:
        raise ValueError("Unknown resource_control settings: " + ", ".join(sorted(map(str, unknown))))
    policy = {**DEFAULTS, **value}
    ranges = {
        "sample_seconds": (0.2, 5), "slow_cpu_percent": (1, 99),
        "pause_cpu_percent": (2, 100), "resume_cpu_percent": (0, 98),
        "resume_samples": (2, 30), "max_sample_age_seconds": (0.4, 30),
        "max_delay_seconds": (0.001, 0.5), "ai_max_threads": (1, 4),
    }
    for name, (low, high) in ranges.items():
        number = policy[name]
        if (isinstance(number, bool) or not isinstance(number, (int, float))
                or not math.isfinite(number) or not low <= number <= high
                or (name in {"resume_samples", "ai_max_threads"} and not isinstance(number, int))):
            raise ValueError(f"resource_control.{name} must be a finite number from {low} to {high}"
                             + (" (whole number)" if name in {"resume_samples", "ai_max_threads"} else ""))
    if not policy["resume_cpu_percent"] < policy["slow_cpu_percent"] < policy["pause_cpu_percent"]:
        raise ValueError("resource_control thresholds must satisfy resume < slow < pause")
    if policy["max_sample_age_seconds"] < 2 * policy["sample_seconds"]:
        raise ValueError("resource_control.max_sample_age_seconds must be at least twice sample_seconds")
    return policy
