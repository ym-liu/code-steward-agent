# Code Steward Safety Policy

**Status:** Draft for TASK-044 review  
**Purpose:** Define enforceable guardrails for scanning, AI-assisted decisions, filesystem actions, privacy, and resource use.  
**Precedence:** Safety rules override completeness, convenience, and model recommendations. When a required check cannot be completed, the operation must fail closed.

## 1. Policy outcomes

Every guarded operation has one of these outcomes:

- **Allow:** all required checks pass and the action is within the request's approved scope.
- **Block:** a rule is violated or a safety check is unavailable, ambiguous, or inconclusive. No side effect may begin. Return a stable policy error with a rule ID and concise reason.
- **Defer for review:** the operation is not performed. Present the plan and evidence so a person can revise or explicitly approve it where approval is supported.

The model is an untrusted input. It may recommend or describe; it cannot grant permission, expand scope, waive a rule, or authorize its own output. Static program checks and policy checks run after model output and immediately before side effects.

## 2. Rules

| ID | Rule | Required enforcement | Current implementation |
|---|---|---|---|
| SAFE-001 | **Never execute unknown binaries.** | Never launch, import, load, or invoke a binary discovered during scanning or copied by a plan. Treat binary content as opaque. It may be identified, preserved, and documented only. | Binaries are classified and excluded from AI code analysis; the organization executor copies files and does not execute them. Keep this invariant in every new action type. |
| SAFE-002 | **Fail closed on uncertainty.** | Missing, malformed, stale, conflicting, or unverified safety data blocks the affected action. Do not fall back to a broader path, weaker rule, or permissive interpretation. Explain the failed check. | Plan IDs, source hashes, path checks, and target conflict checks are validated before execution. Policy screening is configurable in `config.yaml`; see implementation gaps below. |
| SAFE-003 | **Stay within approved roots.** | Scan only explicitly configured roots. Resolve paths before access; reject traversal, links/reparse points, protected/system locations, denied paths, and paths outside approved roots. Recheck source and destination scope immediately before each write. | Root configuration, deny paths, system-folder checks, link checks, path validation, and library/scan-root separation are enforced in the scanner and execution service. |
| SAFE-004 | **Preserve originals.** | Default file action is copy or mirror. Never delete or move source files. Never overwrite a destination that differs from the planned content. | Execution copies into the library and retains conflicting or modified targets. |
| SAFE-005 | **Dry-run before side effects.** | A plan must be inspectable before it can execute. Plans must identify sources, destinations, action types, uncertainty, and risk. AI suggestions are not approval. | Organization plans are dry runs, exposed through `/organization/plan`; execution requires the current plan ID. Plans and the HTML plan view show low/medium/high risk labels. |
| SAFE-006 | **Atomic and recoverable writes.** | Stage writes in a temporary file on the destination filesystem, verify content, then atomically publish. Record enough state to recover or undo the most recent run. Never delete or replace user-modified output during rollback. | Execution uses staged publication, journals actions, and supports undo with content checks. Recovery behavior should remain conservative if journal or Git state is inconsistent. |
| SAFE-007 | **Bound work.** | Enforce maximum files/actions/bytes per plan, daily action and resource quotas, and bounded retries/model calls. On quota exhaustion, stop before the next side effect and report what completed. | Configurable plan-action, daily-action, bytes, wall-time, Python-process CPU-time, and resident-memory limits are checked before execution actions. Scan size, AI thread/context and model-call limits also bound ingestion and analysis. CPU usage inside the separate Ollama process is not measured by the execution CPU-time quota. |
| SAFE-008 | **Protect secrets and sensitive data.** | Classify config/env files, keys, credentials, and detected secret-bearing content as sensitive. Redact secrets before any prompt or log. Do not include sensitive data in samples, diagnostic bundles, or repository commits. | Config/env, key-file, and embedded credential classification exists. Prompt and log redaction cover common credential forms; logs also redact configured scan/deny/library roots and common user home paths. No diagnostic-bundle feature exists. |
| SAFE-009 | **Minimize disclosure.** | Use local models by default. If remote processing is ever enabled, send only the minimum necessary, non-sensitive fields and require explicit configuration. Never transmit raw source, credentials, absolute paths, or denied data. | Cloud-backed Ollama models are refused. Current behavior should be re-reviewed before adding any remote AI or telemetry integration. |
| SAFE-010 | **Keep credentials secure.** | Do not store credentials in plaintext config, plans, logs, database records, or source-control history. Use the operating system's secure credential store for any future credentials. | No credential store integration is currently required for local Ollama. Any future authenticated connector must implement secure storage before use. |
| SAFE-011 | **Keep Git private and local by default.** | Never push, publish, or add a remote automatically. A remote or public operation requires explicit user configuration and a separate review of what will be sent. Never commit sensitive files. | Library Git history is local and sensitive files are excluded from commits. There is no automatic push behavior. |
| SAFE-012 | **Make decisions auditable.** | Log each perception, model decision, plan, action, and policy block with timestamp, rule ID, reason, and references to evidence or action IDs. Logs must not contain secrets or unnecessary sensitive paths. | Topic/reuse decisions, plan checks, blocks, and action results are logged; action details are also saved in run records. Some scanner perceptions do not yet have a uniform rule/evidence event format. |
| SAFE-013 | **Expose and simulate policy.** | Expose active rules and the last decision through a read-only API. Provide a simulation endpoint that evaluates a proposed operation without changing files, model state, or policy state. Simulation must use the same evaluator as real execution. | `GET /policy` exposes configured checks/limits and the latest in-process decision; `POST /policy/simulate` does no writes and shares action/path/text checks with plan enforcement. Copy simulations also inspect source-path metadata. Simulation cannot reserve quota or guarantee later filesystem state. |
| SAFE-014 | **Constrain AI decisions.** | Validate schema, allowed identifiers, evidence references, and output limits. Treat source text and model responses as untrusted data. Reject instructions embedded in scanned content. Policy violations block the output before it is saved, shown as actionable, or executed. | Structured response/evidence validation exists. Configured blocked terms are checked on explanation output and organization decisions before caching/saving and again before execution. This is a deterministic text rule set, not a semantic classifier. |
| SAFE-015 | **Prefer conservative plans.** | When confidence is low, evidence is incomplete, or paths conflict, choose the lower-impact action or defer. Risk labels must be visible in the plan and the planner must never trade safety for completeness. | Plans show risk labels and reasons. Topic confidence below 0.5 routes a group to `needs-review`; sensitive groups receive high risk and are excluded from Git. Execution still requires the current plan ID and preflight checks. |

## 3. Required decision sequence

For any operation that can read outside metadata, write, publish, or otherwise cause an external effect:

1. Identify the requested operation and its policy rule set.
2. Confirm the source is in an approved root and is not denied, protected, sensitive beyond the operation's allowance, or a link/reparse point.
3. Validate all paths, action types, quotas, file sizes, hashes, evidence, and current plan/version identifiers.
4. Evaluate AI output as untrusted input. Reject malformed or policy-violating output; do not interpret failure as approval.
5. Present a dry-run plan with evidence and risk. Require the applicable approval before execution.
6. Recheck scope, identity, hashes, conflicts, and quotas immediately before each side effect.
7. Use an atomic, journaled action. If a check fails, stop subsequent actions and preserve user data.
8. Record the outcome and any block with rule ID, reason, and relevant evidence/action IDs, without recording secrets.

## 4. Policy error contract

API policy blocks should use a consistent JSON shape, for example:

```json
{
  "error": "policy_violation",
  "rule_id": "SAFE-003",
  "detail": "Destination is outside the approved library root.",
  "decision": "blocked"
}
```

Use `4xx` for a request or plan blocked by policy and a clear failed state for asynchronous jobs. Do not return a success response for a blocked action. Do not expose raw prompts, secrets, sensitive source excerpts, or full paths when a safe relative reference is sufficient.

## 5. Review and security testing

Policy changes must be reviewable in source control. Each rule should have positive and negative tests, including boundary paths, symlinks/reparse points, stale plans, malformed model responses, quota exhaustion, secret-like values, interruption during atomic publication, and attempts to bypass checks through alternate endpoints. Simulation and execution must share the same evaluator. Security test hooks must be disabled in normal operation unless they are explicitly intended for local development.

## 6. Implementation boundary and next work

This document records the intended safety contract; it does not claim every rule is fully implemented. Resource quotas are admission checks at execution boundaries, not operating-system hard limits; the Python CPU timer excludes the separate Ollama process. Simulation checks the supplied proposal but cannot reserve quotas or predict filesystem races. `blocked_terms` is a deterministic text screen, not a semantic policy evaluator. A term match can be useful as a tripwire, but absence of a match is not proof that a decision is safe.

Before TASK-044 is marked complete, map each `SAFE-*` rule to code enforcement points and tests, and confirm which requirements are already satisfied by existing behavior versus which need implementation.
