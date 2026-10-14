"""Check that the tools this repo needs are installed before onboarding a new laptop."""
import shutil
import subprocess

REQUIRED = {
    "git": ["git", "--version"],
    "python3": ["python3", "--version"],
    "node": ["node", "--version"],
    "docker": ["docker", "--version"],
    "psql": ["psql", "--version"],
}

missing = []
for name, command in REQUIRED.items():
    if shutil.which(command[0]) is None:
        missing.append(name)
        print(f"[missing] {name}")
        continue
    version = subprocess.run(command, capture_output=True, text=True).stdout.strip()
    print(f"[ok]      {name:8} {version}")

if missing:
    print("\nInstall these before continuing: " + ", ".join(missing))
    raise SystemExit(1)
