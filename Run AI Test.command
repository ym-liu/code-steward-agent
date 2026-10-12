#!/bin/zsh
cd -- "${0:A:h}" || exit 1
export PYTHONUTF8=1
task_python=""
for candidate in python3.14 python3.13 python3.12 python3.11 python3.10 python3 \
  "$HOME/.local/bin/python3.12" /opt/homebrew/bin/python3 /usr/local/bin/python3; do
  if command -v "$candidate" >/dev/null 2>&1 && \
     "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
    task_python="$candidate"
    break
  fi
done
if [[ -z "$task_python" ]]; then
  print "Python 3.10+ is required. Install it from https://www.python.org/downloads/ and try again."
  read "?Press Enter to close this window."
  exit 1
fi
"$task_python" ai_test.py --interactive
task_result=$?
read "?Press Enter to close this window."
exit "$task_result"
