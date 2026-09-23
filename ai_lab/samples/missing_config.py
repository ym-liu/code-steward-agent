import json
from pathlib import Path

settings = json.loads(Path("settings.json").read_text(encoding="utf-8"))
source = Path(settings["source"])
destination = Path(settings["destination"])
destination.write_text(source.read_text(encoding="utf-8").upper(), encoding="utf-8")
