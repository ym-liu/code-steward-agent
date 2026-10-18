# Code Kamaji

A local background agent that finds and organizes scripts and other code artifacts, controlled through a REST API.

## Stack

| Tool    | Role                                                                         |
| ------- | ---------------------------------------------------------------------------- |
| uv      | Package manager that handles Python, dependencies and `.venv/`               |
| Python  | Programming language the project is written in                               |
| FastAPI | Web framework that defines the REST API (maps URLs to Python functions)      |
| Uvicorn | Web server that runs the API locally (receives requests at `localhost:8000`) |

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
uv run uvicorn main:app --reload
```

- `uv run`: runs the command inside `.venv/`, no activation needed
- `main:app`: the `app` object in `main.py`
- `--reload`: restarts the server when code changes

Then open:

- http://localhost:8000: the API
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
```

- `ruff check`: finds bugs, unused imports and style problems (`--fix` fixes the easy ones)
- `ruff format`: rewrites the code in a consistent style

Both also run on every commit. To run all pre-commit hooks on the whole repo:

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
