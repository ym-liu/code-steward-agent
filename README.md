# Code Kamaji

A local background agent that finds and organizes scripts and other code artifacts, controlled through a REST API.

## Stack

| Tool    | Role                                                                         |
| ------- | ---------------------------------------------------------------------------- |
| uv      | Package manager that handles Python, dependencies and `.venv/`               |
| Python  | Programming language the project is written in                               |
| FastAPI | Web framework that defines the REST API (maps URLs to Python functions)      |
| Uvicorn | Web server that runs the API locally (receives requests at `localhost:8000`) |

## Docs

- **[System context](docs/architecture/context.md)**: who uses Code Kamaji and what it talks to
- **[Data flow](docs/architecture/data-flow.md)**: how file changes become saved artifacts
- **[State machines](docs/architecture/states.md)**: service, artifact and scan states
- **[Domain model](docs/architecture/domain.md)**: the core concepts and their fields
- **[Code layers](docs/architecture/layers.md)**: folders, imports and where code goes
- **[File indexers](docs/research/file-indexers.md)**: how real indexers work, and what we borrow

## Setup

**1. Install [uv](https://docs.astral.sh/uv/getting-started/installation/)**, then restart your terminal so `uv` is on your PATH:

```bash
# Windows
winget install --id=astral-sh.uv -e

# macOS/Linux
curl -LsSf https://astral.sh/uv/install.sh | sh
```

**2. Install everything else:**

```bash
uv sync
uv run pre-commit install
```

- `uv sync`: installs Python 3.12 if missing, then the exact dependency versions from `uv.lock` into `.venv/`
- `pre-commit install`: runs ruff and basic file checks on every `git commit`

## Run

Start the server:

```bash
uv run uvicorn app.main:app --reload
```

- `uv run`: runs the command inside `.venv/`, no activation needed
- `app.main:app`: the `app` object in `app/main.py`
- `--reload`: restarts the server when code changes

Then open:

- http://localhost:8000/system/health: checks the API is up
- http://localhost:8000/docs: interactive docs to try every endpoint

## Test

Run the tests:

```bash
uv run pytest
```

## Lint

Check and format the code:

```bash
uv run ruff check --fix .
uv run ruff format .
uv run lint-imports
```

- `ruff check`: finds bugs, unused imports and style problems (`--fix` fixes the easy ones)
- `ruff format`: rewrites the code in a consistent style
- `lint-imports`: checks the [layer rules](docs/architecture/layers.md), e.g. `domain/` never imports `integrations/`

Ruff also runs on every commit, and all three run in CI. To run all pre-commit hooks on the whole repo:

```bash
uv run pre-commit run --all-files
```

## Dependencies

Add or remove a package:

```bash
uv add <package>
uv remove <package>
```

- `pyproject.toml`: lists your direct dependencies
- `uv.lock`: pins the exact versions of everything

Both commands update both files. Commit them together, and run `uv sync` after pulling someone else's dependency changes.
