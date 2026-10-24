# Code Steward Agent

Code Steward runs on your own computer and looks after the code files in folders you choose:

1. **Finds and records** every file in those folders and notices new, changed, renamed and deleted files.
2. **Explains code files with a local AI model**: what each file does, its inputs and outputs, how to use
   it, what it depends on and which files it works with.
3. **Proposes an organization plan**: files that belong together stay together, the AI picks a topic for
   each group (for example `machine-learning/` or `setup-and-deployment/`), and you review the plan first.
4. **Copies the files into an organized library** (the `library` folder) when you approve the plan, with
   an undo, and keeps the library's **version history in Git**.

Everything stays on your computer. Your original files are only read: Code Steward never changes, moves,
deletes or runs them. You control it through a REST API; the easiest way is the browser page
`http://localhost:8000/docs`.

**[How it works: every file and how data moves between them](docs/HOW_IT_WORKS.md)**

---

## What you need

| | Why | Get it |
|---|---|---|
| **Python 3.10 or newer** (3.12 recommended) | Runs Code Steward | [python.org/downloads](https://www.python.org/downloads/) |
| **Ollama** and the model **qwen3.5:4b-q4_K_M** (3.3 GB download) | The local AI model | [ollama.com/download](https://ollama.com/download) |
| **Git** | Version history of the library | Mac: included with the command line tools · Windows: [git-scm.com](https://git-scm.com/download/win) |

It was tested on a Mac with 16 GB of memory. Computers with 8 GB have not been tested yet. The model runs
on the CPU by default; on that Mac most code files took 10 to 60 seconds each (27 seconds on average), very
long files a few minutes.

---

## 1. Install (once)

### Mac

Open **Terminal** and run these lines one at a time.

1. Check Python and Git. Both should print a version; Python must be 3.10 or newer.
   ```bash
   python3 --version
   git --version
   ```
   If `git` is missing, macOS offers to install the command line tools; accept, then run it again.
2. Install **Ollama** from [ollama.com/download](https://ollama.com/download) and open it once. Then download
   the model (only the first time; it takes a few minutes):
   ```bash
   ollama pull qwen3.5:4b-q4_K_M
   ```
3. Download Code Steward and go into its folder:
   ```bash
   git clone https://github.com/ym-liu/code-steward-agent.git
   cd code-steward-agent
   ```
4. Create a private Python environment for it and install the libraries it needs:
   ```bash
   python3 -m venv venv
   venv/bin/python -m pip install -r requirements.txt
   ```

### Windows

Open **PowerShell** and run these lines one at a time.

1. Install **Python** from [python.org](https://www.python.org/downloads/) (on the first installer screen,
   tick **Add python.exe to PATH**) and **Git** from [git-scm.com](https://git-scm.com/download/win). Check both:
   ```powershell
   py --version
   git --version
   ```
2. Install **Ollama** from [ollama.com/download](https://ollama.com/download) and open it once. Then download
   the model (only the first time):
   ```powershell
   ollama pull qwen3.5:4b-q4_K_M
   ```
3. Download Code Steward and go into its folder:
   ```powershell
   git clone https://github.com/ym-liu/code-steward-agent.git
   cd code-steward-agent
   ```
4. Create a private Python environment and install the libraries:
   ```powershell
   py -m venv venv
   venv\Scripts\python -m pip install -r requirements.txt
   ```

---

## 2. Start Code Steward

1. Make sure **Ollama is open** (Mac: the llama icon in the menu bar; Windows: in the system tray).
2. In Terminal/PowerShell, inside the `code-steward-agent` folder, start it:
   ```bash
   venv/bin/python -m uvicorn CodeSteward:app            # Mac
   ```
   ```powershell
   venv\Scripts\python -m uvicorn CodeSteward:app        # Windows
   ```
3. Wait for `Uvicorn running on http://127.0.0.1:8000`. Leave this window open: closing it stops Code Steward.
4. Open **[http://localhost:8000/docs](http://localhost:8000/docs)** in your browser.
5. To stop, press **Ctrl+C** in that window. It finishes the current step first.

Everything Code Steward learns is kept in `artifacts.sqlite3` in the program folder, so after a restart it
continues instead of starting over.

---

## 3. The `watched` folder and its test files

Code Steward watches one folder and copies the organized files into another. Both are inside the
`code-steward-agent` folder, the same on Windows and Mac:

```text
code-steward-agent/
├── watched/     <- the files Code Steward looks after; 146 test files come with the project
└── library/     <- the organized copies and their Git history (created by the first organization)
```

`watched` already contains test material: 146 files, 118 of them code in 12 languages (Python, shell,
JavaScript, TypeScript, Java, C++, Go, Ruby, PowerShell, batch, SQL, R).

| Folder in `watched` | What it is |
|---|---|
| `real_world/` | A complete course assignment (`a4.py`) and modules of a real project |
| `leetcode/` | LeetCode solutions in several languages |
| `open_source/` | Unchanged examples from 15 open-source projects; each keeps its `LICENSE`, and `SOURCE.json` records where it came from |
| `simulated/` | Imitations of messy personal script folders: a machine-learning lab project, a data pipeline, office automation, developer tools and a "misc" folder with copies and old versions |
| Top-level files | Small tricky cases: a misleading comment, a launcher, a script with a missing settings file, text that tries to give the AI instructions, a file of unknown type |

At the first start Code Steward explains all 118 code files in the background, one at a time: about **50
minutes** on a CPU (27 seconds per file on average on a Mac M1 Pro). You can use everything else meanwhile;
a plan uses the explanations that are ready so far. The answers are kept, so later starts only explain new
or changed files.

You can also put **copies** of your own code into `watched`. New files you add there stay on your
computer: `.gitignore` keeps them out of this project's Git repository (the bundled test files were added
on purpose). To undo your edits of the bundled files, run `git checkout -- watched`.

To watch other folders as well, add them under `scanning.root_paths` in `config.yaml`: an absolute path
such as `C:\Code` or `/Users/you/Code`, or `~/Code` for a folder in your home folder. If a folder is itself
one project, add its **parent** folder so the project stays together. The library and a watched folder
must not contain each other.

```yaml
scanning:
  root_paths:
    - watched          # code-steward-agent/watched on every system
```

---

## 4. Use it, step by step

In **http://localhost:8000/docs**, click an endpoint, then **Try it out**, fill in the values, and
**Execute**. The answer appears under **Response body**. (Each step also shows the same request with
`curl` for the Mac Terminal.)

### Step 1: Is it running?

`GET /system/status` should show `"state": "running"`.

```bash
curl http://localhost:8000/system/status
```

### Step 2: See the files it found

`GET /artifacts` lists every file with its `id`, path, size, type (`script`, `wrapper` = launcher,
`binary`, `sample`, `data`, `documentation`, `unknown`), language, and whether it looks `sensitive`
(passwords, keys). Add, edit, rename or delete a file in `watched`: a few seconds later the list
shows the change.

```bash
curl "http://localhost:8000/artifacts?limit=20"
```

### Step 3: Read the AI explanations

`GET /analysis/status` shows the progress: `explaining_now`, `waiting_files`, `explained_contents` and
`estimated_minutes_left`. `GET /analysis/{artifact_id}` (use an `id` from step 2) shows one file's
explanation: purpose, inputs, outputs, how to use it, dependencies, related files, what is unclear, and
line numbers as evidence.

```bash
curl http://localhost:8000/analysis/status
curl http://localhost:8000/analysis/PASTE_ARTIFACT_ID
```

### Step 4: Build an organization plan and read it

1. `POST /organization/plan` starts building a plan (the AI chooses a topic per group).
2. `GET /organization/status` until `"state": "ready"`. It also shows the plan's `plan_id`.
   `partial` means a plan was saved but reuse checks remain unfinished; the response explains why.
   Build again to retry them using the completed assessments already saved.
3. Open **[http://localhost:8000/organization/plan/page](http://localhost:8000/organization/plan/page)**:
   the groups, their topics and proposed folders, and a "Check these first" list. Nothing is copied yet.

Build the plan after most files are explained (step 3); files that are not explained yet are planned
with less information. The first plan asks the AI about every group (a few seconds each); later plans
only ask about groups that changed.

```bash
curl -X POST http://localhost:8000/organization/plan
curl http://localhost:8000/organization/status
```

### Additional topic placements

The workflow is **build plan -> preflight -> execute**. AI can assign up to two additional topics
based on a file's saved explanation. Extra copies go under those topic folders, with supporting
local files where needed. No project compatibility comparisons or `_reuse` project folders are
created by new plans. Existing project grouping rules are unchanged.

Topic hints shortlist candidates; AI decides which additional labels fit. Sharing the same primary
topic does not create an extra copy. An ML-only test collection can correctly have no extra topics.
Completed decisions and explanations are cached. `organization.suggest_reuse: false` disables extra
placements; `reuse_batch_size` controls batching. Failed assessments can be retried by rebuilding.
Old executed copies are retained until explicitly undone; a new plan does not delete old output.

### Step 5: Check the plan before copying (preflight)

`POST /organization/preflight` with the body `{"plan_id": "<plan_id from step 4>"}`. It reports what would
happen now (`would-copy`, `would-update`, `already-done`, `conflict`, `skipped`), the space needed and
the free space. It writes nothing.

```bash
curl -X POST http://localhost:8000/organization/preflight -H "Content-Type: application/json" -d '{"plan_id": "PASTE_PLAN_ID"}'
```

### Step 6: Copy the files into the library

`POST /organization/execute` with the same body. The answer contains a `run_id`. Then
`GET /organization/runs/{run_id}` until `"state": "complete"`; `summary` counts the results. Open the
`library` folder to see the topic folders. Each group gets a `README.md` written
from the AI explanations (`CODE_STEWARD_NOTES.md` when the group already has its own README), and sensitive
files are listed in a `.gitignore`.

If the plan changed since you read it, execute answers `409`: read the new plan and use its `plan_id`.

```bash
curl -X POST http://localhost:8000/organization/execute -H "Content-Type: application/json" -d '{"plan_id": "PASTE_PLAN_ID"}'
curl http://localhost:8000/organization/runs/PASTE_RUN_ID
```

### Step 7: Look at the library's version history

`GET /organization/repository` shows the Git branch, the last commits and any problems. Every execution
and undo is one commit by "Code Steward". The subject starts with what happened ("Organize", "Update",
"Undo"), and the AI adds which kinds of code it was about. You can also look with Git directly:

```bash
git -C library log --stat
```

### Step 8: Change a file and organize again

Edit a code file in `watched`. Its explanation is updated (step 3). Build a new plan (step 4),
preflight and execute it (steps 5 and 6): the library copy becomes `updated`, and the old version stays in
the Git history. Files that are already in the library keep their folder, even if the AI would now name the
group differently; new files of that group join the same folder. If **you** edited a copy inside the
library, Code Steward never overwrites it; it reports a `conflict` instead. To organize a group again from
scratch, undo the run that copied it (step 9) and build a new plan.

### Step 9: Undo

`POST /organization/undo` with `{"run_id": "<run_id from step 6>"}` removes the copies that run made (or puts
back the previous version of updated files) and records a commit. Copies you edited are kept.

```bash
curl -X POST http://localhost:8000/organization/undo -H "Content-Type: application/json" -d '{"run_id": "PASTE_RUN_ID"}'
```

### Step 10: Read the log

`GET /logs` shows what Code Steward did, newest first. Filter with `level=WARNING` or `search=...`.

```bash
curl "http://localhost:8000/logs?limit=50"
```

---

## 5. What to test and how to report it

- **Finding files**: create, edit, rename and delete files (also whole folders). Does `GET /artifacts`
  follow within a few seconds? Is each file's type sensible?
- **AI explanations**: is the purpose right? Does it invent usage, inputs or dependencies?
- **Plan**: are the groups and topics sensible? Which files end up in `needs-review/`?
- **Library**: are the copies complete and identical? Are your originals untouched?
- **Updates and safety**: change a source file and organize again (the copy should be `updated`); edit a
  copy in the library first (it should be a `conflict` and stay as you edited it); try undo.

When something looks wrong, note: your system (Windows/Mac) and Python version, what you did, what you
expected and what happened, and the related lines from `GET /logs`.

---

## Settings (`config.yaml`)

Change values in `config.yaml`, then call `POST /config/reload` (or restart). Invalid values are
rejected with the reason, and the previous settings stay in use.

| Setting | What it does |
|---|---|
| `scanning.root_paths` | Folders to watch (`watched`); a relative path is inside the program folder |
| `scanning.deny_paths` | Folders that must never be read (system folders are always protected) |
| `scanning.exclude_folders` | Folder names to skip, such as `.git`, `node_modules`, `venv` |
| `scanning.include_extensions` | Only these file types (empty = all files) |
| `scanning.max_file_size_bytes` | Bigger files are listed but not read or hashed (10 MB) |
| `scanning.scan_interval_seconds` | How often a full check runs besides the file watcher (300 s) |
| `ai.enabled`, `ai.model` | AI explanations on/off, and the model (`qwen-4b`, `qwen-9b`, `qwen-coder-7b`, `qwen-coder-3b` or any installed Ollama model) |
| `ai.device`, `ai.num_thread` | `cpu` (default) or `auto` (GPU when available); CPU threads |
| `ai.analyze_types` | Which file types get an explanation (`script`, `wrapper`) |
| `organization.library_path` | Where the organized copies go (`library`, inside the program folder) |
| `organization.git` | Keep the library's version history in Git (`true`) |
| `organization.topics_file` | The list of topic folders the AI chooses from (`ai/settings/topics.json`) |
| `storage.database_path`, `storage.log_path` | The database and log file (in the program folder) |
| `logging.level` | How much the log records (`INFO`) |
| `testing.allow_event_injection` | Allows `POST /events/inject` to simulate file changes for validation (`false`) |

The AI's limits and prompts are in `ai/settings/`. To use another settings file for yourself, start with
`CODE_STEWARD_CONFIG=/path/to/my-config.yaml` set in the environment.

---

## API reference

All answers are JSON; errors look like `{"detail": "..."}`. Details and examples are in `/docs`.

| Endpoint | What it does |
|---|---|
| `GET /system/health` | Health check |
| `GET /system/status` | Service state, waiting file changes, file watcher, last background error |
| `GET /system/metrics` | CPU time, CPU share, peak memory and threads of Code Steward |
| `POST /system/start`, `/stop`, `/pause`, `/resume` | Control the background service (409 when not possible) |
| `GET /scan/status` | Running scan, last scan counts, root folder availability |
| `POST /scan/trigger` | Ask for a full scan now (202) |
| `GET /artifacts` | Saved files, a page at a time: `limit`, `offset`; filters `artifact_type`, `language`, `sensitive`, `path_contains` |
| `GET /artifacts/{artifact_id}` | One file with its classification and exact duplicates |
| `GET /analysis/status` | AI explanations: model, the file being explained, files waiting, time left |
| `GET /analysis/{artifact_id}` | One file's AI explanation, or its place in the queue |
| `POST /analysis/{artifact_id}` | Explain one file again (202) |
| `POST /organization/plan` | Build a new plan in the background (202) |
| `GET /organization/status` | Plan state (building, ready, partial, failed), its `plan_id` and summary |
| `GET /organization/plan` | The latest plan as JSON |
| `GET /organization/plan/page` | The latest plan as a readable page |
| `GET /organization/topics` | The topic folders the plan chooses from |
| `GET /organization/reuse/candidates` | Optional candidate pairs, without AI |
| `POST /organization/reuse/check` | Optional check of one project pair |
| `GET /organization/reuse/status` | Optional check result and new plan ID |
| `POST /organization/reuse/review` | Optional override of automatic reuse copies |
| `POST /organization/preflight` | What executing a plan would do now; writes nothing |
| `POST /organization/execute` | Copy a reviewed plan into the library (202) |
| `GET /organization/execution/status` | The running operation and the last run |
| `GET /organization/runs/{run_id}` | One run: the plan used and every action's result |
| `POST /organization/undo` | Undo the newest run that still has copies (202) |
| `GET /organization/repository` | The library's Git history and problems |
| `POST /organization/repository/repair` | Fix what interrupted Git commands left behind |
| `GET /events/status` | File changes waiting to be checked, and how folders are watched |
| `POST /events/inject` | Simulate a file change (only with `testing.allow_event_injection: true`) |
| `GET /logs` | Recent log entries: `limit`, `level`, `search` |
| `GET /config` | Current settings (passwords and tokens shown as `***`) |
| `POST /config/reload` | Read `config.yaml` again |

---

## Troubleshooting

| You see | Do this |
|---|---|
| `GET /analysis/status` says *waiting: Ollama is not running* | Open Ollama. Code Steward tries again by itself. |
| *Model is not installed* | Run `ollama pull qwen3.5:4b-q4_K_M` (or the model named in `ai.model`). |
| *Other models are loaded* | Another program is using a different model. Wait, or close it; Code Steward never stops another program's model. |
| `address already in use` at start | Something else uses port 8000: add `--port 8001` and open `http://localhost:8001/docs`. |
| `TypeError: unsupported operand type(s) for \|` at start | Python is older than 3.10; install a newer one and create `venv` again. |
| Execute answers 409 *The plan changed* | Read the new plan and send its `plan_id`. |
| Execute answers 409 *Start or resume the service* | `POST /system/resume` or `POST /system/start`, then try again. |
| Many actions are `failed` on Windows | Put the library on a normal NTFS drive (USB sticks formatted as FAT/exFAT cannot be used for the library). |
| Want to start from zero | Stop Code Steward, delete `artifacts.sqlite3` (records, explanations, plans and runs) and the `library` folder, and run `git checkout -- watched` to restore the bundled test files. |

---

## Privacy and safety

- The AI model runs locally through Ollama; no code or file leaves the computer, and cloud models are refused.
- Passwords, keys and tokens written in code are replaced with `<hidden>` before a file is sent to the model.
- Original files are only read. Copies in the library are never overwritten when someone changed them.
- Sensitive files are never committed to the library's Git history, and the history has no remote.
- System folders and folders in `deny_paths` are never read; links are not followed.
