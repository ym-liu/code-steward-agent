# Quick Start

Install Python 3.10+ and Ollama once. After that:

1. Put your code files in **ai_lab/samples**. Subfolders work too.
2. Open Ollama, then double-click **Run AI Test.command** (Mac) or **Run AI Test.bat** (Windows).
3. Choose **1 for Qwen3.5 4B** or **2 for Qwen3.5 9B**. The script tests the folder and opens the results automatically.

A missing model downloads on first use. Long files can take several minutes.
Qwen3.5 has no 7B version; 9B is the larger option. See the [official model list](https://ollama.com/library/qwen3.5).
Start with 4B on memory-limited computers. Target-PC performance has not been verified.

While a file is analyzed, one line refreshes with elapsed time, estimated token speed and system CPU/RAM.
The live speed has a `~` prefix; the completed file shows Ollama's actual average token speed.
The final summary and report show total time, including model setup. No extra monitoring packages are needed.

The test folder is found from the cloned repository's location. You do not need to edit paths.
Keep the launchers in the repository root; results are saved in that repository's **ai-results** folder.

The folder already contains **27 files**, including the complete `a4.py`, LeetCode solutions
and real project modules. See [Test files](CORPUS.md) for the list and sources.

Each file gets a short explanation: what it does, its inputs and outputs, how to run it,
dependencies, related files and important missing information. Expand code evidence when needed.
“Ready” means a complete response was saved; important claims still need checking against the code.

Results are kept in a new folder under **ai-results**. Double-click **report.html** to view them again.
There is also a short **report.md** text version. Raw answers and technical records are saved separately.

Your code is read, never executed. Common code formats are supported; notebooks are ignored.
Each file is analyzed separately. Related files are described when referenced, but are not opened automatically.
To test different code, change the files in **ai_lab/samples** and run the script again.
