from pathlib import Path

def write_marker(path):
    Path(path).write_text("ready", encoding="utf-8")

if __name__ == "__main__":
    print("preview only")
