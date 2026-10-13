# What is in the default test folder?

The launcher explains every supported code file in `ai_lab/samples/`: currently **118 code files**.
None of them is ever run.

| Folder | Code files | What it is | Origin |
|---|---:|---|---|
| top level | 6 | Starter cases: a CSV filter, a launcher whose program is missing, a missing settings file, a misleading comment, an instruction hidden in a comment, unknown text | Written for this lab |
| `real_world/` | 3 | The user's complete 791-line `a4.py` (image-segmentation experiment) and two development snapshots of this lab's own modules | User / this project |
| `leetcode/` | 18 | 12 LeetCode problems in Python, Java and C++ | [neetcode-gh/leetcode](https://github.com/neetcode-gh/leetcode), MIT |
| `open_source/` | 54 | Unchanged files from 15 projects: PyTorch, Keras and scikit-learn examples, the Flask tutorial, Express, Go examples, nvm, the Postgres Docker image, vcpkg, PowerShell, VS Code extension samples and TheAlgorithms in four languages | Pinned commits; each folder keeps its upstream `LICENSE` |
| `simulated/` | 37 | Imitations of personal script folders: a research project, office automation, a data pipeline, developer tools and messy leftovers, with deliberate duplicates and variants | Written by Claude Code; names, hosts and values are fictional |

Languages: Python 62, Shell 11, JavaScript 8, C++ 6, Java 6, Go 5, PowerShell 5, Ruby 3, Windows batch 3,
TypeScript 3, SQL 2, R 1 and 3 plain-text files.

The folder also holds 28 files that are not explained: 16 licenses, 4 `SOURCE.json` provenance files,
one upstream README, and 7 non-code files in `simulated/` for the file-type tests of the organize step
(an `.env` with placeholder values, result files, a sample input/expected-output pair, a small compiled program).

## Notes on particular files

- **`real_world/a4.py` is sent in full**: 32,663 characters, below the 60,000-character reading limit.
  It contains Colab's `!cp` shell line. The program's own fact extraction uses a placeholder for that line;
  the model receives the complete original code with line numbers.
- **LeetCode files** are often method or class excerpts rather than scripts. A good explanation describes
  their arguments and results, notices the missing harness, types or imports, and does not invent files,
  command options or install steps. The upstream Python islands file has a real indentation error; it is kept.
- **The two project modules** are snapshots of earlier versions of this lab's runner and readable report,
  provided as module excerpts without their package.

## Reference answers (`ai_lab/answer_keys/`)

These are never sent to a model.

| File | Contents |
|---|---|
| `explanation_checklists.json` | Short reference checklists for the original 27 files (starter, `real_world/`, `leetcode/`) |
| `tuning_vs_sealed.json` | Which code files may be read while tuning prompts and checks (72) and which are only measured at the end (46) |
| `organization_answers.json` | Expected file types, groups and folder plan for the organize step |

Sources, commits and file hashes are in the `SOURCE.json` file of each folder. To test other code, add or remove
files in `ai_lab/samples/`; no list needs updating. Adding files changes what a run covers, so compare runs
on the same files.
