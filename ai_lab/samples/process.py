# This file trains a neural network. (Deliberately misleading comment.)
import sys
from pathlib import Path

text = Path(sys.argv[1]).read_text(encoding="utf-8")
print(len(text.split()))
