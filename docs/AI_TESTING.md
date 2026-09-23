# AI model testing — quick start

Test how well local models explain a script's purpose, inputs, outputs and dependencies.
**Defaults: CPU only, six bundled samples, no pip install or FastAPI setup.**

## 1. Install and run

Install **[Python 3.10+](https://www.python.org/downloads/)** (enable “Add Python to PATH”),
**[Git](https://git-scm.com/downloads)** and **[Ollama](https://ollama.com/download)**. Open Ollama, then run:

```powershell
git clone --branch main https://github.com/ym-liu/code-steward-agent.git
cd code-steward-agent
python ai_test.py
```

Choose **1** for the smallest model, **A** to compare all presets, or **C** for a custom model.
Then choose **1** for six quick samples, **2** for 24 development cases, or **3** for 12 final cases.
Missing models download automatically. The first download needs internet; installed models run locally.

Already have the repository? Update your `main` checkout with `git pull --ff-only` first.
On Windows, use `py -3` if `python` is unavailable; on macOS/Linux, use `python3`.

## 2. Switch models or samples

| Alias | Model (4-bit instruction version) |
|---|---|
| `qwen-small` | Qwen2.5-Coder 0.5B |
| `qwen-medium` | Qwen2.5-Coder 1.5B |
| `gemma-small` | Gemma 3 1B |

```powershell
# Test one model; change the alias to switch
python ai_test.py run --model qwen-medium --pull

# Compare all three on the development test set
python ai_test.py compare --suite dev --pull

# Use your own short UTF-8 file (a folder also works)
python ai_test.py run --model qwen-small --input "C:/samples/tool.py" --pull

# Use another compatible local Ollama model
python ai_test.py run --model "qwen2.5-coder:3b-instruct-q4_K_M" --pull
```

`--pull` downloads missing models; it does not update installed ones. Custom models must support
text chat and JSON-schema output. Add `--repeat 3` for three passes. Presets and resource limits
are editable in [`ai_lab/config.json`](../ai_lab/config.json).

Use `--suite holdout` only after fixing prompts/settings. Run `python ai_test.py check-inputs`
for six offline invalid-input checks. [Dataset coverage and reference answers](../ai_lab/datasets/v1/README.md).

## 3. Review and send back results

Each run prints its folder under `ai-results/`:

| File | What to do |
|---|---|
| `summary.md` | Read answers, source snapshots and reference checklists |
| `results.csv` | Compare timing and automatic output checks |
| `review.csv` | Fill the five score columns: **0 = wrong/missing, 1 = partial, 2 = correct** |

Score purpose, inputs/outputs, dependencies, evidence and uncertainty. Fill all five for a /10 total;
record unsupported claims and notes separately. Keep the CSV headers and record IDs.
After saving and closing the CSV, refresh the report:

```powershell
python ai_test.py report "ai-results/YOUR-RUN-FOLDER"
```

**Send the whole run folder as a ZIP**, plus your CPU, RAM and any slowdown you noticed.
The folder also contains raw responses, settings and exact input files; check custom-code contents before sharing.

## 4. If something goes wrong

| Problem | Fix |
|---|---|
| Cannot connect | Open Ollama, then run `python ai_test.py doctor` |
| No Ollama installed yet | Run `python ai_test.py demo` to try the report workflow with scripted answers |
| Other models loaded | Close other AI clients; check `ollama ps` and stop the models you intend to unload |
| Timeout or invalid/cut-off output | Inspect the saved report; check Ollama before retrying. Try a shorter file or adjust limits consistently for all models |
| Input too long | Default limit is 2,500 characters; select a smaller file or edit the config |

**How to interpret results:** valid JSON is not proof of a correct explanation; review quality manually.
Case categories appear in the reports. Memory figures are Ollama allocation snapshots, not peak RAM.
Demo runs do not test AI performance. Minimum hardware support still needs testing on target PCs.
The lab reads code as text and never executes it; it is not yet connected to the scanner's background workflow.

<details>
<summary>Developer checks</summary>

Lab tests need no Ollama or downloaded models:

```sh
python -m unittest tests.test_ai_lab -v
```

Run all project tests in the existing environment after installing `requirements.txt`:

```sh
python -m unittest discover -s tests -v
```

</details>
