# What is in the default test folder?

The launcher now tests **27 code files** automatically:

| Group | Files | What it tests |
|---|---:|---|
| Starter cases | 6 | Basic input/output, missing config, misleading comments and unknown code |
| User's a4.py | 1 | A complete 791-line image-segmentation experiment with data, training and output stages |
| Real project modules | 2 | Medium-sized evaluation and report-generation code with package dependencies |
| LeetCode solutions | 18 | Python, Java and C++: arrays, stacks, linked lists, trees, graphs, caching, tries and dynamic programming |

The LeetCode samples are unchanged files from [neetcode-gh/leetcode](https://github.com/neetcode-gh/leetcode).
They cover 12 problems; some have alternate-language versions. Source URLs, commit and hashes are
in `ai_lab/samples/leetcode/SOURCE.json`; the original license is included next to the code.

**a4.py is loaded in full:** 32,663 characters, below the launcher's 60,000-character reading limit.
The original bytes are preserved. It contains Colab's `!cp` command, so it needs an IPython/Colab
environment to run unchanged. Static extraction uses a placeholder for that shell line, while the
AI and saved source snapshot receive the complete original code and line numbers.

LeetCode files are often method/class excerpts rather than command-line scripts. A good explanation
should describe their arguments and returned results, recognize missing harness/types/imports,
and avoid inventing files, command flags or package-install instructions.
The upstream Python islands file also has a real indentation defect; it remains unchanged.

The two project modules are development snapshots, provided as module excerpts. They do not include
their entire package environment. Provenance is in `ai_lab/samples/real_world/SOURCE.json`.

All 27 files have short reference checklists in `ai_lab/samples/manifest.json`.
These references help reviewers and never enter the model request. They are development cases,
not a new holdout benchmark or a measured accuracy score.

The workflow is unchanged: add code to `ai_lab/samples`, start the launcher, and choose a model.
The larger folder takes longer than the six-file starter run. None of the sample code is executed.
