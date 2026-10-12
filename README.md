# code-steward-agent

## AI model testing

This standalone lab tests how well local AI models explain code. Each request contains one file's
source with line numbers and basic facts extracted from the code. The [prompt](ai_lab/prompt.txt)
asks for brief English JSON: purpose, inputs, outputs, usage, dependencies, related files,
important unknowns and code evidence. It tells the model to avoid guessing missing details.
The lab is separate from the project's scanner and API.

1. Install **[Python 3.10+](https://www.python.org/downloads/)** and **[Ollama](https://ollama.com/download)** once. Open Ollama.
2. Put your code files in **`ai_lab/samples/`**. Subfolders work too.
3. Double-click **`Run AI Test.command`** on Mac or **`Run AI Test.bat`** on Windows, then choose **Qwen3.5 4B** or **Qwen3.5 9B**.

The script tests every supported code file in that folder and opens **`report.html`** automatically.
The report presents each explanation with expandable source-line evidence.
Missing models download automatically. The lab reads code; it does not run your scripts.
One status line shows live token speed and system CPU/RAM; each file shows its time and final speed,
and the finished run shows total time. Live speed is marked `~` because it is an estimate.

The folder includes **27 files**: the complete 791-line `a4.py`, 18 LeetCode solutions in Python,
Java and C++, two real project modules and six starter examples.

**[Short guide](docs/AI_TESTING.md)** · **[One-page guide](QUICK_START.md)** · **[Test files](CORPUS.md)**

<details>
<summary>Command-line options for developers</summary>

```sh
# Same model settings as the double-click launcher
python3 ai_test.py run --config ai_lab/config.simple.json --model qwen-4b --pull

# Compare Qwen3.5 4B and 9B on the same development cases
python3 ai_test.py compare --config ai_lab/config.simple.json --suite dev --pull

# Test another folder or a different local model
python3 ai_test.py run --config ai_lab/config.simple.json --input /path/to/code --model MODEL_TAG --pull

# Preview the result layout without a model
python3 ai_test.py demo
```

The launcher uses `ai_lab/config.simple.json`: CPU only, thinking off, complete source text,
up to 60,000 characters per file, 32K context and a 2,048-token generation ceiling.
Actual explanations are kept short by the prompt and field limits. Long files can take several minutes.
These settings have not been certified for an 8GB PC.
The older `ai_lab/config.json` remains available for the existing short-file benchmark and input checks.
The input folder is fixed at `ai_lab/samples/`; change files there, not the manifest.
Add model aliases to `config.simple.json` to change the menu.

`report.html` and `report.md` are readable results. `summary.md`, JSON files and CSV files retain technical
records and optional manual scoring. A generated explanation is not an automatic correctness score.
[Dataset information](ai_lab/datasets/v1/README.md).

</details>

## Dependencies

- **Python**
- **FastAPI**: API framework
  - defines the API
- **Uvicorn**: starts a server (`localhost:8000`), listens for requests, passes them to FastAPI, returns responses
  - runs the API

## How to install and run

### Create virtual env

```
python3 -m venv venv
source venv/bin/activate    # Mac/Linux
venv\Scripts\activate       # Windows
```

### Install dependencies

```
pip install -r requirements.txt
```

### Run the server

```
uvicorn CodeSteward:app --reload
```

- CodeSteward = filename
- app = FastAPI instance
- --reload = auto-restart on changes

### Send requests with curl

The default URL is `http://127.0.0.1:8000` or `http://localhost:8000`.

E.g.,

```
curl http://localhost:8000
```

```
curl http://127.0.0.1:8000/system/health
```

## Generated docs

http://localhost:8000/docs

## MVP API Skeleton

Current endpoint scaffolding:

- `GET /system/health`
- `GET /system/status`
- `GET /scan/status`
- `POST /scan/trigger`
- `GET /artifacts`
- `GET /artifacts/{artifact_id}`
- `GET /logs?limit=100`
- `GET /config`
- `POST /config/reload`

## Verify artifact classification

1. Start the API and call `POST /system/start` from `/docs`.
2. Create, edit, or move a supported file inside a configured `root_paths` directory.
3. Call `GET /artifacts` to see its current name, path, type, size, hash, and change type.

The artifact ID stays the same when a known file is edited or moved. Its hash changes
when its contents change.

## Saved artifact records

Artifact records are saved automatically in `artifacts.sqlite3` in the project
directory. Restarting the API keeps the records and their IDs. SQLite is included
with Python, so the existing installation commands still work.

The existing `ArtifactsService` handles saving and querying. `ScanService` uses
these saved records to check which files have changed. The API endpoints and
response fields stay the same.

When upgrading from the previous version, start the service and trigger one scan
to populate the database. The old `scan_checkpoint.json` does not contain complete
artifact records or IDs, so it is ignored when the artifact service is connected.
The scanner still supports that JSON file when used without an artifact service.

To check persistence:

1. Set `scanning.root_paths` in `config.yaml` to your test folder.
2. Start the API, call `POST /system/start`, then `POST /scan/trigger`.
3. Call `GET /artifacts` and note a file's ID.
4. Stop and restart the API. Call `GET /artifacts` again: the file and ID remain.
5. Start the service and scan again. Unchanged files stay in the list even when
   the scan reports `files_found: 0` (this count means new or changed files).

The database contains the latest file information. File contents stay in their
original locations. Keep the database to keep these records; Git ignores the
database and its temporary files. The scanner skips its own database files.

This change also lets a scan continue when a file disappears while its size or
modification time is being read. Other scanning work is still pending: preserving
events during pause or a busy scan, detecting deletions missed by the watcher,
handling consecutive renames, periodic scans, and avoiding repeated full hashing.

Run the regression tests from the project directory:

```sh
python -m unittest discover -s tests -v
```

## Runtime logs

`GET /logs` reads real application events from `service.log` in the project
directory. Entries survive server restarts and are returned newest first.
The existing plain-text history is also readable. New entries include a UTC
timestamp, severity, source module and message; old entries retain their original
local timestamps and have `source: null` when no source was recorded.
Multiline exception details stay with the event that produced them.

Query parameters:

- `limit`: maximum matching entries, from 1 to 1000; default 100.
- `level`: optional exact level: DEBUG, INFO, WARNING, ERROR or CRITICAL.
- `search`: optional case-insensitive message text, up to 200 characters.

Filters apply before the limit. `count` is the number returned, not the total
number of historical entries. No matches return an empty list with HTTP 200.
Invalid parameters return HTTP 422. An unreadable log file returns HTTP 503
with a short explanation; a missing log file is treated as an empty history.

```sh
curl "http://127.0.0.1:8000/logs?limit=20"
curl "http://127.0.0.1:8000/logs?limit=20&level=WARNING&search=blocked"
```

The logger captures scanning requests and results, file changes, service
controls, skipped paths, blocked scans and errors. File-type exclusions are
summarized per scan. Idle ticks are DEBUG messages and are not recorded at the
default INFO level, so routine heartbeats do not bury useful events. This
version uses the existing local log file; it does not add log rotation or an
AI reasoning module. Filters with no matches may need to scan the full history.

To verify in `/docs`, open `GET /logs`, choose **Try it out**, set `limit` to 20,
then choose **Execute**. Look under **Server response / Response body**.
Pause the service, request a scan, and query with `level=WARNING` and
`search=blocked`: the rejected scan and its reason should appear. Resume the
service after this check. Reading logs does not itself generate application
log records. Execute the query again to refresh; the page does not auto-update.

The automated suite includes temporary-file log tests and real HTTP tests of
the logs controller. Run `python -m unittest discover -s tests -v` after installing
the existing requirements; no additional test dependency is needed.

## MVP: A Local-First Architecture

```mermaid
flowchart LR
    subgraph UserMachine["Windows Host (Local Only)"]
        API["FastAPI REST API"]
        AgentLoop["Runtime Agent Loop\n(low priority background process)"]
        EventSensors["File Event Sensors\n(create/modify/delete/rename)"]
        PeriodicScan["Periodic Full Scanner"]
        ArtifactSvc["Artifacts Service"]
        LogSvc["Logs Service"]
        LogFile["Human-readable service.log"]
        ConfigSvc["Config Service"]
        Store["SQLite Database\n(artifacts, scans, logs, state)"]
        ConfigFile["Config File (JSON/YAML)"]
    end

    API --> ArtifactSvc
    API --> LogSvc
    API --> ConfigSvc
    API --> AgentLoop
    ConfigFile --> ConfigSvc
    ConfigSvc --> AgentLoop
    AgentLoop --> EventSensors
    AgentLoop --> PeriodicScan
    EventSensors --> ArtifactSvc
    PeriodicScan --> ArtifactSvc
    ArtifactSvc --> Store
    LogSvc --> LogFile
    AgentLoop --> LogSvc
```

Database note:

- SQLite is a good MVP default for local logs and metadata.
- Single-file DB keeps install simple and works offline.
- You can split into tables like: `artifacts`, `scan_runs`, `file_events`, `logs`, `agent_state`.
