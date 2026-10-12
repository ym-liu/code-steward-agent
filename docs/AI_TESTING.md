# Test your code with a local AI

1. Install [Python 3.10+](https://www.python.org/downloads/) and [Ollama](https://ollama.com/download). Open Ollama.
2. Add code to **`ai_lab/samples/`**. You can add files and subfolders; no manifest edits are needed.
3. Double-click **`Run AI Test.command`** (Mac) or **`Run AI Test.bat`** (Windows).
4. Choose **1 for Qwen3.5 4B** or **2 for Qwen3.5 9B**. Everything else runs automatically.

Models download on first use. Installed models work locally. Python and Ollama are the only prerequisites
for this testing tool; you do not need to start FastAPI or install the product service dependencies.
If double-clicking is unavailable, run `python3 ai_test.py --interactive` (Windows: `py -3 ai_test.py --interactive`).

Qwen3.5 has no 7B version; the larger menu option is 9B. Start with 4B on memory-limited computers.
Available sizes are listed in the [official model library](https://ollama.com/library/qwen3.5).

The default folder includes **27 files**: the complete 791-line `a4.py`, 18 LeetCode solutions,
two real project modules and six starter examples. The full `a4.py` fits the reading limit.
See [Test files and sources](../CORPUS.md). The expanded batch takes longer than the starter examples.

## Read the results

During analysis, one terminal line refreshes each second:

```text
00:42 | ~7.3 tok/s | CPU 34% | RAM 6.2/16.0 GiB
```

CPU/RAM cover the whole computer, including Ollama and other apps. Missing readings show `--`.
Live token speed is an estimate from streamed text pieces over the last five seconds; pieces are not
guaranteed to correspond one-to-one with tokens. Final speed uses Ollama's actual token count and
generation duration. RAM excludes free and inactive pages on macOS; OS memory displays may differ.
Redirected output keeps only the per-file results rather than repeated progress lines.
No extra packages are required for monitoring.

The browser opens **`report.html`** when the run finishes. Each code file gets a short English explanation:

- What it does, needs and produces.
- How to use it, its dependencies and related files.
- Important missing information and expandable code evidence.

The page says which files received an explanation and which did not finish. Read important claims against
the code; the tool checks response format and code positions, not whether every claim is true.
All results are saved in a new folder under **`ai-results/`**. Reopen `report.html` at any time.
`report.md` is the same short explanation as text. Technical JSON/CSV files and `summary.md` are saved separately.
Per-file time and final token speed appear beside the model name. Total time includes model setup and
downloads when needed. Sampled system CPU/RAM peaks are saved in the technical records.

## Add files or change models

Common code formats include Python, Shell, Batch, PowerShell, JavaScript/TypeScript, C/C++, Java, Go, Rust,
C#, PHP, Ruby and others. Use non-empty UTF-8 text. Notebook files are ignored.
Generated folders such as `.git`, `node_modules`, `.venv` and `ai-results` are ignored.
Files that are too large or unreadable are reported as unfinished; source is never silently truncated.
Your code is read, never executed. Each file is analyzed separately; file relationships describe visible
references, and referenced files are not automatically opened as additional context in this version.

The folder is fixed at **`ai_lab/samples/`**. Put new files there, then run again.
Model options and resource settings are in **`ai_lab/config.simple.json`**.
Custom model tags must support local text chat and JSON-schema responses.

## If it stops

- Cannot connect: open Ollama, then try again.
- First download: keep the internet connection active and wait.
- Another model is running: stop the other test first.
- Timeout: check Ollama before retrying; the server may still be processing.
- No automatic browser window: double-click `report.html` in the saved result folder.

## Optional developer commands

```sh
python3 ai_test.py compare --config ai_lab/config.simple.json --suite dev --pull
python3 ai_test.py run --config ai_lab/config.simple.json --input /path/to/code --model MODEL_TAG --pull
python3 ai_test.py demo
python3 ai_test.py report ai-results/YOUR-RUN-FOLDER
python3 -m unittest tests.test_ai_lab tests.test_ai_suites tests.test_simple_flow tests.test_realistic_corpus tests.test_live_status
```

Use dev for tuning; keep holdout for after prompts and settings are fixed. References are never sent to the model.
`review.csv` retains the existing five-dimension /10 manual rubric for comparison with older runs; it does not
separately score the new usage and relationship fields. Review those fields directly against the source.
The older `ai_lab/config.json` preserves the short-file benchmark and six offline input checks.
Memory snapshots are not peak process RAM. Minimum hardware support still needs testing on target PCs.
This tool is separate from the scanner and product analysis API.
