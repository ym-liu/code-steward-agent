# Startup and shutdown animations

Use the additive launcher to run Code Steward with graceful browser shutdown:

```powershell
venv\Scripts\python launch.py
```

Open **http://127.0.0.1:8000/startup**. **Shut down** appears in its header,
main actions, and error dialog. The launcher also adds a fixed **Shut down**
button to the top of **http://127.0.0.1:8000/docs**. These links open
**http://127.0.0.1:8000/shutdown**.

The farewell animation uses character poses derived from the supplied thumbs-up
image. The employee lifts the chest badge, taps the reader, waits for both gate
panels to retract into their respective posts, and walks through. WebP and GIF
assets loop continuously; reduced-motion preferences select a static poster.
The supplied original portrait is preserved. Prompts and timing are documented
in `clockoff-badge-animation-prompt.txt`.

Opening the shutdown page does not stop anything. Click **Shut down Code Steward** to:

1. Stop admitting new mutating API requests and finish already accepted requests.
2. Wait for file organization, copying, undo, and planning operations to finish.
3. Call the existing background-service stop method, then wait for the scan and
   file-monitor threads to finish.
4. Release CPU-resource waits and let the agent's AI worker stop safely. Completed
   results stay saved; interrupted requests return to the queue without consuming
   an attempt. Only this application's request is cancelled. Ollama is neither
   stopped nor unloaded; see `RESOURCE_CONTROL.md` for scheduling boundaries.
5. Acknowledge the finish request and ask Uvicorn to exit gracefully. The browser
   shows the final step complete only after the server confirmed background
   cleanup and its API connection has closed.

Progress reflects those actual states. An error is shown with a retry action.
File publication and journal cleanup finish safely; worker processes are not
force-killed to make the animation finish. If the browser closes midway, the coordinator
continues waiting and exits 15 seconds after cleanup is ready. That grace period
does not bypass cleanup.

The old `python -m uvicorn CodeSteward:app` command remains usable for the existing
API and startup page. Because that command does not register an owned server-exit
callback, the shutdown page reports that full process shutdown is unsupported
and asks the user to start with `launch.py`; it does not claim a shutdown occurred.

Shutdown POST routes require a per-process nonce, a custom action header, and
local same-origin access. GET routes never start or finish shutdown. The launcher
adds the shutdown coordinator and request gate around the existing app. The
existing entry points receive the additive resource governor. Scheduling hooks
yield at safe work boundaries without replacing the scanner, model, prompts or
organization rules. The existing API controllers keep their routes.

Run regression tests:

```powershell
venv\Scripts\python -B -m unittest discover -s tests -p "test_*.py" -v
```
