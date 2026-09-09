# code-steward-agent

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
    LogSvc --> Store
    AgentLoop --> LogSvc
```

Database note:

- SQLite is a good MVP default for local logs and metadata.
- Single-file DB keeps install simple and works offline.
- You can split into tables like: `artifacts`, `scan_runs`, `file_events`, `logs`, `agent_state`.
