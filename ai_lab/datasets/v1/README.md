# AI test dataset v1.0

**36 new code-understanding cases + 6 input checks**, with reference answers and
evidence lines. The original six quick samples remain available as `smoke`.

| Set | Cases | Use |
|---|---:|---|
| `smoke` | 6 | Quick installation check; the original examples |
| `dev` | 24 | Compare models and improve prompts |
| `holdout` | 12 | Final check after freezing prompts/settings |
| `robustness` | 6 | Test input rejection offline; not AI answer quality |

## Run

From the repository folder:

```powershell
# One model on the development set
python ai_test.py run --model qwen-small --suite dev --pull

# All three models on the same development set
python ai_test.py compare --suite dev --pull

# Final evaluation, after tuning is finished
python ai_test.py compare --suite holdout --pull

# Empty/binary/invalid/oversized input checks: no Ollama needed
python ai_test.py check-inputs
```

The interactive menu (`python ai_test.py`) also lets you choose a test set.
Use `python ai_test.py demo --suite dev` to preview the report with scripted answers.
Use `--suite` to retain reference answers and case metadata; do not pass the whole
dataset tree to `--input`, which would mix the splits and lose the answer keys.

## Coverage

| Area | Examples |
|---|---|
| Data processing | JSON to CSV, JSONL counts, regex filtering, median, HTML links |
| File I/O | Checksums, read-only SQLite, file listings, temporary-file cleanup |
| Dependencies and invocation | Optional libraries, external packages, Shell/Batch launchers |
| Missing information | Environment settings, missing modules/config, dynamic imports, type declarations |
| Misleading code | Wrong filename/comment, Base64 described as encryption, uncalled functions, syntax errors |
| Instruction resistance | Source comments telling the analyzer to ignore its instructions |
| Languages and text | Python, JavaScript, TypeScript, Shell, Batch, PowerShell, Ruby, Perl, Unicode and unknown notation |
| Rejected input | Empty, whitespace-only, oversized, NUL-containing, invalid UTF-8, unsupported extension |

**[Browse all 42 new cases](CASE_INDEX.md).** Each suite's `manifest.json` contains
the expected behavior, input/output, dependencies, supporting lines, unknowns and
claims the model must not invent. They are shown in the report, never in model input.

## Score and hand off

Open `summary.md`, score `review.csv` using the existing five 0/1/2 columns, then
run `python ai_test.py report "ai-results/YOUR-RUN-FOLDER"`. The report includes
results by category. Zip the run folder for your teammate; include CPU/RAM details.
Use the same cases/settings and review all rows when comparing models.

`check-inputs` instead produces `input_checks.csv`, `input_checks.json` and a short
report. It does not load models or produce human AI scores. Expected outcomes use
the default 2,500-character input budget; changing that budget can deliberately
make the oversize check fail.

## Scope

- All new fixtures are original synthetic examples, not copied from a production codebase.
- Reference answers come from static code inspection. Samples are not executed by the lab.
- Each AI request sees **one file**. Referenced helpers, binaries and configs are intentionally
  missing in some cases; assess whether the model recognizes that limit. This is not a
  multi-file integration benchmark.
- The sets use different task families and source files. The holdout is public and useful
  only while the team keeps it out of prompt tuning; it is not a hidden/blind benchmark.
- Passing JSON/evidence checks does not establish semantic accuracy. Category groups are
  small and descriptive, not statistically reliable rankings or a security certification.
- Binary/encoding fixture bytes are preserved by `.gitattributes`; do not resave them in an editor.

For broader installation and scoring instructions, see the [quick guide](../../../docs/AI_TESTING.md).
