# Startup page

Start the API with the existing command, then open **http://127.0.0.1:8000/startup**.

```powershell
venv\Scripts\python launch.py
```

The file-archiving animation follows readiness checks, rather than a timed progress bar.
The counter shows confirmed checks out of six. It reaches six only when each check is
ready or intentionally disabled. A failed AI check remains unconfirmed; when the core
checks pass, the user can explicitly continue to the existing API documentation with
the basic features available. Nothing redirects automatically.

- API: the local endpoint answers.
- Service: the background loop is running and reports no current error.
- Database: the existing artifact index can be queried with a read-only SQLite connection.
- Watcher: configured roots are usable and the watcher and its emitter threads are alive.
- Scan: the current service start has a completed full scan, no full scan is pending,
  and no scan is running or reporting file errors. This is the scanner's reported
  state; it does not certify that every nested directory was readable. Inspect the
  existing runtime logs for individual skipped paths and permission warnings.
- AI: a fresh, bounded Ollama check confirms that the configured model is installed,
  local, and available under the agent's existing model-use rules. It does **not**
  run inference, load model weights, or wait for all file explanations. When AI is
  intentionally disabled in the configuration, this check is marked disabled.

Failures produce an accessible dialog with details, a suggested action, a retry
button, and a link to the existing logs API. A connection timeout is shown as an
error. A scan that remains pending for two minutes produces a notice to inspect
the logs or retry; it is not silently declared complete.

The page uses local assets only and respects reduced-motion preferences. It never
starts/stops the background service, triggers a scan, changes configuration,
downloads models, builds an organization plan, or copies files. A request can only
observe the app after Python and the API have started serving HTTP. If Python fails
before HTTP starts, no server-rendered page is available; the terminal shows that
startup error. If an already-open page loses its API connection while checking,
it reports the connection failure.

Integration adds only an import and router registration to `CodeSteward.py`.
The existing root JSON response, `/docs`, and API routes keep their current behavior.
The new controller and static assets are independent of the scanner and AI pipeline.

New routes:

| Route | Purpose |
| --- | --- |
| `GET /startup` | Startup animation and live readiness checks |
| `GET /startup/status` | Core readiness snapshot, without starting work |
| `GET /startup/ai` | Fresh Ollama and installed-model checks, without inference |
| `GET /startup/assets/startup.css` | Local styles |
| `GET /startup/assets/startup.js` | Local animation and status client |

Run the startup regression tests with the existing virtual environment:

```powershell
venv\Scripts\python -B -m unittest discover -s tests -p test_startup.py -v
```
