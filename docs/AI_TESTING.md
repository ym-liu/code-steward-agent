# Test code explanations with a local AI

A local model explains each code file in `ai_lab/samples/` in short English, and the program checks the
answers against the code. Your code is read, never run. This tool is separate from the scanner and API.
How the code is organized: [ai_lab/README.md](../ai_lab/README.md).

## Quick start

Install [Python 3.10+](https://www.python.org/downloads/) and [Ollama](https://ollama.com/download) once. Then:

1. Put code files in **`ai_lab/samples/`**. Subfolders work too; there is no list to update.
2. Open Ollama, then double-click **`Run AI Test.command`** (Mac) or **`Run AI Test.bat`** (Windows).
3. Choose a model, then **1 — CPU** (press Enter) or **2 — GPU**. Everything else runs automatically and
   **`report.html`** opens when the run finishes.

If double-clicking is unavailable, run `python3 ai_test.py --interactive` (Windows: `py -3 ai_test.py --interactive`).
Python and Ollama are the only prerequisites; FastAPI and the product service packages are not needed.

| Choice | Model |
|---|---|
| 1 | Qwen3.5 4B (default) |
| 2 | Qwen3.5 9B |
| 3 | Qwen2.5-Coder 7B |
| 4 | Qwen2.5-Coder 3B |
| C | Add / use another model |

All four presets use Q4_K_M quantization and the same requested settings. Qwen3.5 has no 7B version, so the
7B option is Qwen2.5-Coder; see the official lists for [Qwen3.5](https://ollama.com/library/qwen3.5) and
[Qwen2.5-Coder](https://ollama.com/library/qwen2.5-coder). A missing model downloads on first use.
Start with 4B on memory-limited computers; performance on the target PC has not been verified.
Long files can take several minutes.

**Another model (C):** paste its Ollama name, an `ollama run ...` command or its model-page link. Press Enter at
**Keep this model in the menu next time?** to save it, or type **N** to use it once; **0** goes back. Saved choices
stay on this computer in `ai_lab/settings/models.local.json` and are not shared through Git. No JSON editing is
needed. Custom models must support local text chat and JSON-schema answers.

**CPU or GPU:** CPU is the default and the final product setting. GPU is a faster preview (Apple Silicon or a
supported GPU): its answers and speed can differ from CPU, its reports say so and `run.json` records `device: auto`.
Confirm chosen results on CPU and do not compare GPU timings with CPU runs. On the command line, add `--device auto`.

To compare models, keep the same files, prompt and settings and change only the model.

## What gets tested

Every supported code file in `ai_lab/samples/` and its subfolders: currently **118 files**
([what they are](AI_TEST_FILES.md)). Supported formats include Python, Shell, Batch, PowerShell,
JavaScript/TypeScript, C/C++, Java, Go, Rust, C#, PHP, Ruby and others, as non-empty UTF-8 text.
Notebooks are ignored, and folders such as `.git`, `node_modules`, `.venv` and `ai-results` are skipped.
Files that are too large or unreadable are reported as unfinished; source is never silently cut.

Each file is analyzed separately. Related files are described when the code refers to them, but they are not
opened as extra context. The test folder is found from the repository's location, so no paths need editing;
keep the launchers in the repository root.

## While it runs

One terminal line refreshes each second:

```text
00:42 | ~7.3 tok/s | CPU 34% | RAM 6.2/16.0 GiB
```

CPU and RAM cover the whole computer, including Ollama and other apps; missing readings show `--`.
The live speed, marked `~`, is an estimate from streamed text over the last five seconds. When a file finishes,
its time and Ollama's actual average token speed are shown. The final summary and the report show the total
time, including model setup and downloads. RAM excludes free and inactive pages on macOS, so the operating
system's memory display may differ. Redirected output keeps only the per-file results. No extra packages are needed.

## Read the results

Each run is saved in a new folder under **`ai-results/`**, named `<run ID>_<model>_<cpu|gpu>_<files>`, for example
`20261013T015306Z-6ac3a38d_qwen3.5-4b_cpu_118files`. `ai-results/README.md` lists the earlier runs and what each
one was for.

The browser opens **`report.html`** when the run finishes; double-click it to see it again. `report.md` is the same
text without formatting. Each file gets a short English explanation:

- What it does, what it needs and what it produces.
- How to use it, its dependencies and related files.
- Important missing information, and expandable code evidence.

"Explanation ready" means a complete answer was saved. The page says which files received an explanation and
which did not finish. Read important claims against the code: the format and code positions are checked, not
whether every claim is true. Technical records are saved next to the report: `summary.md`, the JSON and CSV
files, the exact prompt and schema, and `results.jsonl` with `read_ms` (reading the file) and `write_ms`
(writing the answer) for each file. Sampled CPU/RAM peaks are whole-machine samples, not the peak memory of one
process.

## How the answers are checked

Before the model answers, the program reads the code without running it and passes along plain facts: whether
anything runs when the file is started, public functions and their parameters, files read and written, the most
used imports, and names that are used but never defined or imported.

Afterwards the program compares the answer with the code. It removes `"None"` placeholders and repeated items
itself. If the model leaves the inputs, outputs or usage of a library file empty, the program fills them from the
function signatures and says so in the report. It flags run commands for files that run nothing by themselves,
command options or file names the code never mentions, the language or a class listed as a dependency, types
listed as related files, internal words such as `entry_point`, libraries the code never uses, libraries listed
in usage, several items packed into one, text cut off at the length limit, and guesses about unknown syntax.
For those, and for answers that break a format rule (such as an empty purpose with no reason), it asks the model
once to fix them, and keeps the corrected answer only when it has fewer problems and loses nothing else.
Smaller notes (empty inputs or outputs, evidence on a comment line) are shown without a second request.
Anything left is shown as **Needs checking**. These checks catch common mistakes, not all mistakes.
Set `"repair_attempts": 0` in a settings file to turn the follow-up request off (for example, to match older runs).

## Sort files into folders (dry run)

After a run, the program can label every file in the tested folder and propose an organized layout:

```sh
python3 ai_test.py organize ai-results/RUN-FOLDER
```

- Each file gets a type (launcher, script, compiled program, sample, data, document or unknown), a role for code
  (runs by itself, used by other code, test), its language, a sensitive flag for configuration and environment
  files or hard-coded passwords, exact duplicates and a confidence computed by the program, not by the model.
- Files that name each other (imports, started scripts, paths in code) stay together, and so do folders with a
  README, LICENSE or package file. The model picks one topic per group from `ai_lab/settings/topics.json`
  (editable); program keyword hints check each choice and are used alone with `--no-ai`. Disagreements are listed
  for review.
- Nothing is copied, moved or deleted. The plan is saved as `organization/plan.html` and `plan.json` in the run
  folder. Sensitive files are marked to stay out of Git, exact duplicates are recorded once and existing READMEs
  are never replaced. The topic step uses the run's device unless `--device` is given.
- `--score` compares a plan for `ai_lab/samples` with `ai_lab/answer_keys/organization_answers.json` (tuning files
  only). Versions and variants of the same tool are not grouped yet.

## Settings

Resource and model settings are in **`ai_lab/settings/default.json`**; the prompt is
`ai_lab/settings/explain_prompt.txt`. Changing either makes new results not directly comparable with older runs.

- `num_thread: "auto"` lets Ollama use all performance cores (on a 6+2-core M1 Pro about 2.4x faster than
  2 threads; adding the efficiency cores was slower). A number fixes the thread count. Answers can differ slightly
  in wording between thread counts, so compare runs that used the same setting.
- To save reading time, blank lines are left out of the source sent to the model (line numbers are kept) and the
  program facts are sent as compact JSON. A one-line JSON answer was also tried: it saved output tokens, but the
  4B model then wrote less (empty usage, packed items), so answers keep the model's normal layout.
- Optional, off by default (`long_file_tokens: 0`): files estimated above `long_file_tokens` send only key lines
  (opening comments/help, imports, definitions, parameter blocks, lines the program facts point at, main steps,
  then nearby lines) up to `long_file_keep_tokens`. Tried with 3500/2500 on the 8 longest files: 28% faster and
  mostly equal answers, but `a4.py` described its inputs less well, so every line is sent by default.
- `ai_lab/settings/legacy.json` keeps the older short-file benchmark and the six offline input checks. It is used
  for `--suite dev/holdout` and `check-inputs` when no `--config` is given.

## If it stops

- Cannot connect: open Ollama, then try again.
- First download: keep the internet connection active and wait.
- Another model is running: stop the other test first.
- Timeout: check Ollama before retrying; the server may still be processing.
- No automatic browser window: double-click `report.html` in the saved result folder.

## Developer commands

```sh
python3 ai_test.py run --model qwen-4b --pull                    # same settings as the launcher
python3 ai_test.py compare --models qwen-4b qwen-9b --pull       # several models on the same files
python3 ai_test.py run --input /path/to/code --model MODEL_TAG --pull
python3 ai_test.py compare --config ai_lab/settings/default.json --suite dev --pull
python3 ai_test.py demo                                          # preview the report layout without a model
python3 ai_test.py report ai-results/RUN-FOLDER                  # rebuild reports after filling review.csv
python3 ai_test.py organize ai-results/RUN-FOLDER --score
python3 -m unittest discover -s tests -p "test_ai_*.py"          # offline AI tests
```

Use `dev` for tuning and keep `holdout` until prompts and settings are fixed ([benchmark sets](../ai_lab/benchmarks/README.md)).
Reference answers are never sent to the model. `review.csv` keeps the five-dimension /10 manual rubric for
comparison with older runs; it does not separately score the usage and relationship fields, so review those
directly against the source. Memory snapshots are not peak process RAM, and minimum hardware support still needs
testing on target PCs.
