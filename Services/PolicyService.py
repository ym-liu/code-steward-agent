"""Shared policy evaluator for AI outputs, plans, simulation, and audit records."""
from datetime import datetime, timezone
import logging
import re
import os
from pathlib import Path
from pathlib import PurePosixPath
import sys


class PolicyViolation(ValueError):
    """An AI output or proposed action violates a configured policy rule."""

    def __init__(self, message, status_code=422, rule_id="SAFE-014"):
        super().__init__(message)
        self.status_code = status_code
        self.rule_id = rule_id


class PolicyService:
    def __init__(self, config_service):
        self.config_service = config_service
        self.last_decision = None

    def check_text(self, value, *, context="AI output", audit=True):
        """Reject text matching configured prohibited terms; report the rule, not the model output."""
        policy = self.config_service.get("policy", default={}) or {}
        if not policy.get("enabled", True) or not isinstance(value, str):
            return
        for term in policy.get("blocked_terms", []):
            if re.search(re.escape(term), value, re.IGNORECASE):
                if audit:
                    self._audit("blocked", "SAFE-014", context, f"Configured prohibited term matched: {term!r}")
                raise PolicyViolation(f"Policy blocked {context}: prohibited term rule matched ({term!r}).")

    def rules(self):
        policy = self.config_service.get("policy", default={}) or {}
        rules = [
            ("SAFE-001", "Never execute discovered binaries", "enforced"),
            ("SAFE-002", "Fail closed on uncertainty", "partial"),
            ("SAFE-003", "Stay within approved locations", "enforced"),
            ("SAFE-004", "Preserve originals; copy only", "enforced"),
            ("SAFE-005", "Dry-run review and visible risk", "enforced"),
            ("SAFE-006", "Atomic and recoverable writes", "enforced"),
            ("SAFE-007", "Bound actions, bytes, runtime, CPU and memory", "enforced"),
            ("SAFE-008", "Protect secrets and sensitive data", "partial"),
            ("SAFE-009", "Minimize data disclosure", "enforced"),
            ("SAFE-010", "Secure credential storage", "not_applicable_local_only"),
            ("SAFE-011", "Keep Git local and private by default", "enforced"),
            ("SAFE-012", "Audit decisions and actions", "partial"),
            ("SAFE-013", "Inspect and simulate policy", "enforced"),
            ("SAFE-014", "Validate AI decisions and prohibited terms", "partial"),
            ("SAFE-015", "Prefer conservative plans", "enforced"),
        ]
        return {"enabled": policy.get("enabled", True), "rules": [
            {"id": rule_id, "name": name, "status": status,
             "enabled": status != "not_applicable_local_only" and (rule_id != "SAFE-014" or policy.get("enabled", True))}
            for rule_id, name, status in rules], "blocked_terms": list(policy.get("blocked_terms", [])),
            "limits": {key: policy[key] for key in ("max_plan_actions", "max_daily_actions", "max_plan_bytes",
                                                     "max_run_seconds", "max_cpu_seconds_per_run",
                                                     "max_process_memory_mb")}}

    @staticmethod
    def process_memory_bytes():
        """Return current resident memory where supported; fail closed if measurement is unavailable."""
        try:
            if sys.platform == "win32":
                import ctypes
                from ctypes import wintypes

                class Counters(ctypes.Structure):
                    _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                                ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                                ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]
                counters = Counters()
                counters.cb = ctypes.sizeof(counters)
                kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
                psapi = ctypes.WinDLL("psapi", use_last_error=True)
                kernel32.GetCurrentProcess.restype = wintypes.HANDLE
                psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
                psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
                process = kernel32.GetCurrentProcess()
                if not psapi.GetProcessMemoryInfo(process, ctypes.byref(counters), counters.cb):
                    return None
                return counters.WorkingSetSize
            with open("/proc/self/statm", "r", encoding="ascii") as stream:
                resident_pages = int(stream.read().split()[1])
            return resident_pages * os.sysconf("SC_PAGE_SIZE")
        except (OSError, AttributeError, IndexError, ValueError):
            try:
                import resource
                usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
                return int(usage if sys.platform == "darwin" else usage * 1024)
            except (ImportError, AttributeError, OSError):
                return None

    def _audit(self, decision, rule_id, context, reason):
        self.last_decision = {"at": datetime.now(timezone.utc).isoformat(), "decision": decision,
                              "rule_id": rule_id, "context": context, "reason": reason}
        logger = logging.getLogger("code-steward.policy")
        log = logger.warning if decision == "blocked" else logger.info
        log("Policy decision: %s rule=%s context=%s reason=%s", decision, rule_id, context, reason)

    def simulate(self, decision):
        """Evaluate a JSON decision payload without side effects using the same checks as enforcement."""
        try:
            if not isinstance(decision, dict):
                raise PolicyViolation("Decision must be a JSON object.")
            proposed_actions = decision.get("actions")
            if proposed_actions is not None:
                if not isinstance(proposed_actions, list):
                    raise PolicyViolation("Proposed actions must be a list.")
                for proposed in proposed_actions:
                    self._check_action(proposed)
            elif decision.get("action") is not None:
                self._check_action(decision)
            actions = proposed_actions if proposed_actions is not None else [decision]
            for proposed in actions:
                if proposed.get("action") == "copy":
                    source = proposed.get("source_path")
                    scanning = self.config_service.get("scanning", default={}) or {}
                    try:
                        from Services.ConfigService import select_system_paths
                        roots = select_system_paths(scanning.get("root_paths", []), setting="scanning.root_paths")
                        denied = select_system_paths(scanning.get("deny_paths", []), setting="scanning.deny_paths")
                    except ValueError as error:
                        raise PolicyViolation("Policy cannot verify configured source roots.", rule_id="SAFE-003") from error
                    if (not isinstance(source, str) or not os.path.isabs(source)
                            or os.path.islink(source) or not os.path.isfile(source)):
                        raise PolicyViolation("Copy simulation needs an absolute, non-link source path.", rule_id="SAFE-003")
                    resolved = os.path.realpath(source)
                    inside_root = any(self._inside(resolved, root) for root in roots)
                    inside_denied = any(self._inside(resolved, path) for path in denied)
                    if not inside_root or inside_denied:
                        raise PolicyViolation("Copy source is outside approved roots or inside a denied path.", rule_id="SAFE-003")
            self._check_strings(decision, audit=False)
            return {"decision": "allow", "rule_id": None, "detail": "No configured rule blocked this payload."}
        except PolicyViolation as error:
            return {"decision": "blocked", "rule_id": error.rule_id, "detail": str(error)}

    @staticmethod
    def _check_action(action):
        if not isinstance(action, dict):
            raise PolicyViolation("Each proposed action must be an object.")
        name = action.get("action")
        if not isinstance(name, str):
            raise PolicyViolation("Action name must be text.")
        if name in {"execute", "run", "launch", "delete"}:
            raise PolicyViolation(f"Policy blocks action {name!r}.", rule_id=
                                  "SAFE-001" if name in {"execute", "run", "launch"} else "SAFE-004")
        if name in {"move", "push", "publish"}:
            raise PolicyViolation(f"Policy blocks action {name!r}.", rule_id="SAFE-004")
        if name not in {"copy", "write", "skip", "keep-out-of-git", "read", "explain", "plan"}:
            raise PolicyViolation(f"Policy blocks unknown action {name!r}.")
        target = action.get("to", action.get("target", action.get("path")))
        if name in {"copy", "write", "keep-out-of-git"}:
            path = PurePosixPath(target) if isinstance(target, str) else None
            if (path is None or path.is_absolute() or ".." in path.parts or "\\" in target
                    or not path.parts or any(part in ("", ".") for part in path.parts)):
                raise PolicyViolation("Policy blocks an invalid or out-of-scope destination.", rule_id="SAFE-003")
            if any(part.endswith((".", " ")) or any(char in '<>:"|?*' for char in part)
                   or part.casefold() == ".git" or part.split(".")[0].upper() in
                   {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
                   for part in path.parts):
                raise PolicyViolation("Policy blocks a reserved or unsupported destination name.", rule_id="SAFE-003")

    @staticmethod
    def _inside(path, root):
        try:
            return os.path.commonpath([os.path.normcase(path), os.path.normcase(os.path.realpath(root))]) == \
                os.path.normcase(os.path.realpath(root))
        except ValueError:
            return False

    def _check_strings(self, value, audit=True):
        if isinstance(value, str):
            self.check_text(value, context="simulated AI decision", audit=audit)
        elif isinstance(value, dict):
            for key, item in value.items():
                if key in {"source", "source_path", "source_code", "evidence", "quote", "action", "target", "path", "id"}:
                    continue
                self._check_strings(item, audit=audit)
        elif isinstance(value, list):
            for item in value:
                self._check_strings(item, audit=audit)

    def check_ai_output(self, value, *, context="AI decision"):
        """Inspect model-authored text recursively, excluding source/evidence supplied for grounding."""
        if isinstance(value, str):
            self.check_text(value, context=context)
        elif isinstance(value, dict):
            for key, item in value.items():
                if key not in {"evidence", "source_evidence", "target_evidence", "quote", "id", "target", "topic"}:
                    self.check_ai_output(item, context=context)
        elif isinstance(value, list):
            for item in value:
                self.check_ai_output(item, context=context)

    def check_plan(self, plan):
        """Check only model-authored decision fields, not source code or user-provided evidence."""
        for unit in plan.get("units", []):
            for field in ("ai_reason", "ai_folder_name"):
                self.check_text(unit.get(field), context=f"AI topic decision ({field})")
            for suggestion in unit.get("additional_topics", []):
                if isinstance(suggestion, dict):
                    self.check_text(suggestion.get("reason"), context="AI topic decision (additional reason)")
            if unit.get("ai_topic"):
                self._audit("allow", "SAFE-014", f"topic:{unit.get('id')}",
                            f"topic={unit.get('ai_topic')}; confidence={unit.get('topic_confidence')}")
        for index, action in enumerate(plan.get("actions", [])):
            self._check_action(action)
            self._audit("allow", "SAFE-003", f"plan-action:{index}",
                        f"action={action.get('action')} passed relative-path validation")
        reuse = plan.get("reuse", {})
        for suggestion in reuse.get("suggestions", []):
            for field in ("reason", "requirements", "unknowns"):
                value = suggestion.get(field)
                if isinstance(value, list):
                    for item in value:
                        self.check_text(item, context=f"AI reuse decision ({field})")
                else:
                    self.check_text(value, context=f"AI reuse decision ({field})")
            self._audit("allow", "SAFE-014", f"reuse:{suggestion.get('id')}",
                        "evidence_ids=" + ",".join(str(item.get("evidence_id", ""))
                                                    for item in suggestion.get("evidence", [])))
        self._audit("allow", "SAFE-014", "organization plan", f"Validated plan {plan.get('plan_id', 'pending-id')}")

    def assess_plan_risk(self, plan):
        """Annotate visible plan/unit risk from existing confidence, sensitivity and conflicts."""
        for unit in plan.get("units", []):
            confidence = unit.get("topic_confidence", 0)
            risk = "low" if isinstance(confidence, (int, float)) and confidence >= .7 else (
                "medium" if isinstance(confidence, (int, float)) and confidence >= .5 else "high")
            members = set(unit.get("members", []))
            member_records = [file for file in plan.get("files", []) if file.get("path") in members]
            sensitive = any(file.get("sensitive") for file in member_records)
            uncertain = any(not isinstance(file.get("confidence"), (int, float))
                            or file.get("confidence", 1) < .6 or file.get("type") == "unknown"
                            for file in member_records)
            if sensitive or uncertain:
                risk = "high"
            unit["risk"] = risk
            reasons = []
            if sensitive:
                reasons.append("Sensitive content is in this group.")
            if uncertain:
                reasons.append("File classification confidence is low or unknown.")
            if risk == "high" and confidence < .5:
                reasons.append("Topic confidence is below the conservative planning threshold.")
            unit["risk_reasons"] = reasons
        action_count = len([a for a in plan.get("actions", []) if a.get("action") in ("copy", "write")])
        plan["risk"] = "high" if plan["summary"].get("needs_review", 0) or any(
            u.get("risk") == "high" for u in plan.get("units", [])) else (
            "medium" if any(u.get("risk") == "medium" for u in plan.get("units", [])) else "low")
        plan["risk_reasons"] = ["One or more groups require review."] if plan["risk"] == "high" else []
        plan["policy_summary"] = {"action_count": action_count, "evaluated_at": datetime.now(timezone.utc).isoformat()}
        if plan["risk"] == "high":
            plan["summary"]["needs_review"] = max(plan["summary"].get("needs_review", 0), 1)
