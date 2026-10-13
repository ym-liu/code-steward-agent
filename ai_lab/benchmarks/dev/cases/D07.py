import os
from pathlib import Path

target = Path(os.getenv("OUTPUT_DIR", "reports")) / "summary.txt"
print(target)
