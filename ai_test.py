"""Beginner-friendly entry point for the standalone local AI evaluation lab."""
import sys

if sys.version_info < (3, 10):
    raise SystemExit("Please install Python 3.10 or newer, then run this command again.")

from ai_lab.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
