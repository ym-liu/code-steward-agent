# ai_lab: local AI code explanations

This folder explains code files with a local model (through Ollama) and checks the answers. For now it is a
test lab, separate from the scanner and API; the product integration is meant to reuse `explain/`.
It starts from `ai_test.py` or the `Run AI Test` launchers in the repository root. Code is read, never run.
How to use it: [docs/AI_TESTING.md](../docs/AI_TESTING.md).

## Folders

| Folder | What is inside | Change it when |
|---|---|---|
| `settings/` | Run settings, the prompts and the topic list | You want other models, limits or prompt wording |
| `samples/` | The default test folder: every supported code file here is explained | You want to test other code |
| `answer_keys/` | Reference answers for `samples/`; never sent to a model | You add samples that need a reference |
| `benchmarks/` | Small labeled sets `dev`, `holdout` and `robustness` | Rarely; never tune on `holdout` |
| `explain/` | Code: explain one file | You change what the model receives or how answers are checked |
| `testing/` | Code: test many files and write reports | You change runs, live status or reports |

Results go to `ai-results/` in the repository root (one folder per run, ignored by Git).
[What the test files are](../docs/AI_TEST_FILES.md).

## Settings

| File | Used by |
|---|---|
| `settings/default.json` | The double-click menu and every run of `samples/`: models, CPU/GPU, threads, limits |
| `settings/legacy.json` | The old short-file benchmark; default for `--suite dev/holdout` and `check-inputs` without `--config` |
| `settings/explain_prompt.txt` | The instruction sent with every file |
| `settings/topic_prompt.txt`, `settings/topics.json` | The organize step: topic instruction and the list of topics |
| `settings/models.local.json` | Personal models saved from the menu (option C); created when needed, ignored by Git |

Each run saves its settings, prompt hash and format versions in `run.json`. Changing the prompt, the facts or
the checks makes new results not directly comparable with older ones: raise `INPUT_VERSION`
(`explain/request.py`) or `CHECKS_VERSION` (`explain/checks.py`) when you change what they produce.

## How one file is explained (`explain/`)

1. `files.py` finds the supported files in a folder and reads each one: text, hash and facts.
2. `facts.py` reads facts from the code without AI: imports, definitions, options, files read and written,
   commands run, names never defined, and whether anything runs when the file is started.
3. `request.py` builds what the model receives: the prompt, the facts and the numbered source.
4. `ollama_client.py` sends it to the local model (CPU by default, thinking off, JSON answer).
5. `answer.py` checks the eight answer fields and their line numbers.
6. `checks.py` compares the answer with the code, cleans small problems and asks the model once to fix
   bigger ones (invented commands, files or dependencies, and similar). Remaining issues are shown to people.

`config.py` loads and checks a settings file.

The product (`Services/AnalysisService.py`) explains files with `files.snapshot` and
`testing/runner.result_for`, using `settings/default.json` by default. Keep their inputs and results the same,
or update the product with them; the product tests (`tests/test_analysis.py`) check this connection.

## Testing many files (`testing/`)

| File | Job |
|---|---|
| `runner.py` | Explains every file with each chosen model, saves results as it goes and times each step |
| `live_status.py` | The one-line live display: elapsed time, token speed, CPU and RAM |
| `suites.py` | Loads the `benchmarks/` sets and runs the offline input checks |
| `report_page.py` | `report.html` and `report.md` for people |
| `report_records.py` | `summary.md`, the CSV files and the manual review sheet |

## Other files

| File | Job |
|---|---|
| `cli.py` | Menu and commands: `python3 ai_test.py --help` |
| `organize.py` | Labels the files of a finished run and proposes folders (dry run: nothing is moved). `plan_for` (groups, topics, folders) is shared with the product's `Services/OrganizationService.py` |
| `common.py` | Folder locations, the error type and JSON helpers shared by everything above |

## Tests

```sh
python3 -m unittest discover -s tests -p "test_ai_*.py"
```

The AI tests run offline with a fake model server; they never download or call a real model.
