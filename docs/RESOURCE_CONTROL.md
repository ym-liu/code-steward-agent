# Low-priority background processing

Code Steward yields CPU to foreground software. This feature extends the existing
services; it does not replace scanning, classification, organization, models,
prompts, saved-result validation, or the startup/shutdown animations.

## Starting and inspecting it

Install `requirements.txt` (the one new dependency is `psutil==7.2.2`) and start
with `python launch.py`. The policy is installed by `ServiceContainer`, so the
existing `python -m uvicorn CodeSteward:app` entry point also receives it. It
applies to the application, not the separate experimental command-line AI labs.

Open `GET /system/resources` in `/docs`. It is read-only and reports actual system
CPU usage, sample age, verified process priority, policy, pacing delay, waiting
checkpoints, active AI requests, cancelled requests and the AI thread cap. It
never starts work. Light API controls, monitoring and safe cleanup remain usable
while heavy work waits; the whole application still has low CPU priority.
The startup page shows a CPU-wait reason when its first scan is deferred. An
intentional resource wait does not consume its two-minute scan notice timer.

## Defaults

| Condition | Behavior |
| --- | --- |
| Windows | Current Python process uses `IDLE_PRIORITY_CLASS`, verified after setting it |
| macOS/Linux | Current process uses `nice 19`, verified after setting it |
| Initial sampling | The first nonblocking sample is discarded on the monitoring thread; heavy work waits for a real sample |
| CPU below 50% | Background work proceeds at low OS priority |
| CPU 50% to below 80% | Increasing delays at safe checkpoints, up to 20 ms each |
| CPU at least 80% | Heavy work waits; this application's active AI generation connection is cancelled |
| Recovery from pressure | Three consecutive samples at or below 40% are required before work resumes |
| Sensor failure, stale sample or priority failure | Heavy work remains blocked; the status endpoint explains the state |
| Single logical CPU | File work remains governed; AI generation is deferred because no core can be reserved for other software |

System CPU is the average across all logical CPUs, including Code Steward's and
Ollama's work. A single busy core does not by itself mean the whole CPU is full.
The default sampling interval is one second; a sample older than three seconds
cannot authorize heavy work. Thresholds, intervals and delays are validated in
the new `resource_control` configuration section. Invalid reloads retain the
previous settings. There is no switch to silently disable the mandatory policy.

An operator can tune valid thresholds through `config.yaml` and the existing
`POST /config/reload`. Reloading does not relabel a cached sample as fresh.
Low process priority stays in effect even when CPU pressure is low.

## Work covered

- The service checks admission before taking events out of the aggregator.
  Events continue to accumulate, and interrupted scans request reconciliation.
- Scans yield between directories, files, deletion checks and 64 KiB hash chunks.
  A cancelled full scan cannot claim completion or run its remaining deletion
  sweep from an incomplete view of the tree.
- Native file notifications keep recording changes. The polling fallback uses
  governed snapshots and retains its last complete snapshot through a pause.
  A reconciliation scan follows its delayed initial baseline.
- Organization yields while reading files, hashing, examining source links,
  grouping units and building actions. Copies and undo yield between actions
  and 1 MiB checksum/copy chunks.
- Explanations, correction requests, organization topics and AI-written version
  subjects all use the new governed client through the existing client factory.
  At most one application generation request runs at once. Its stable thread cap
  is the smaller of two threads, logical CPU count minus one, and an explicitly
  lower existing `ai.num_thread` setting.
- Both normal application shutdown and the animation's shutdown coordinator
  cancel resource waits before waiting for operation locks. Interrupted AI jobs
  return to the queue without losing a retry attempt; partial answers are not
  saved as successful explanations. Safe filesystem and journal cleanup finishes.

## AI and scheduling boundaries

Ollama is an independent, potentially shared process. Lowering Python's priority
does not lower Ollama's priority. Code Steward therefore caps its own requests
and closes only its own generation socket when pressure, stale monitoring or
shutdown requires it. The guard also works while connection/response headers or
prompt processing are pending, rather than waiting for the first output token.
It checks at 100 ms intervals in addition to the CPU sampling interval.

Closing the connection requests cancellation; completion latency depends on the
installed Ollama version and model. This is not a hard real-time CPU reservation
or a promise of instantaneous zero CPU. The app never suspends, reprioritizes,
kills or unloads the shared Ollama service, nor closes another client's request.
Ollama's thread count is a runner-load option: a capped request may reload an
already loaded runner. It stays stable across CPU samples to avoid repeated
load-triggered runner changes. Sharing that runner can still affect other
clients' latency; a separate model server would be needed for strict isolation.

The OS can preempt the agent and its inherited low-priority child processes.
Cooperative waits occur at safe boundaries, not in the middle of atomic file
publication, rollback or journal durability steps. Post-rename integrity checks
finish without parking a temporarily missing target, still at low OS priority.
An already running Git command, filesystem/system call or snapshot-diff operation
can also finish before the next checkpoint. No CPU-intensive operation is
force-killed in an unsafe storage state. Tight thresholds can defer work for a
long time; the feature does not guarantee a task completion deadline.

## Verification

Run `python -B -m unittest discover -s tests -p "test_*.py"` after installing
requirements. Tests cover load transitions and hysteresis, real Windows Idle
priority and child inheritance in a short-lived process, per-thread baseline
sampling, failure/stale samples, cancellation, AI serialization and thread caps,
retry preservation, queued events, scan completion safety, governed polling,
configuration validation and read-only status. Disposable HTTP fixtures cover
AI cancellation before headers and during output, response limits and shared
server availability. Tests do not load a real model or stress the machine's CPU.

Native Windows priority was exercised. macOS/Linux use supported psutil APIs but
have not been exercised on those operating systems in this implementation run.
Real-model cancellation latency remains an integration measurement for the
target Ollama installation; fixture cancellation does not prove that latency.

## References

- [Microsoft process priority](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-setpriorityclass)
- [psutil CPU sampling and process priority](https://psutil.readthedocs.io/stable/)
- [Ollama runner options](https://github.com/ollama/ollama/blob/main/api/types.go)
- [Ollama request context cancellation](https://github.com/ollama/ollama/blob/main/server/routes.go)
- [Ollama runner reload decisions](https://github.com/ollama/ollama/blob/main/server/sched.go)
