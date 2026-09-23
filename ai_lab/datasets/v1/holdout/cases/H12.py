import tempfile
from pathlib import Path

with tempfile.TemporaryDirectory() as root:
    marker = Path(root) / "ready.txt"
    marker.write_text("ready", encoding="utf-8")
    print(marker.read_text(encoding="utf-8"))
print("temporary directory closed")
