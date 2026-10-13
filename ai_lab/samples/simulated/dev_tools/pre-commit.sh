#!/bin/sh
# Git hook: copy to .git/hooks/pre-commit and make it executable.
# Blocks the commit when lint or the quick tests fail.
staged=$(git diff --cached --name-only --diff-filter=ACM | grep '\.py$')
[ -z "$staged" ] && exit 0

echo "flake8 on staged files..."
flake8 $staged || { echo "lint failed"; exit 1; }

echo "quick tests..."
python -m pytest -q -m "not slow" || { echo "tests failed"; exit 1; }
