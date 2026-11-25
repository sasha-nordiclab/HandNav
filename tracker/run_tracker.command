#!/bin/zsh
# Starts the HandNav tracker; creates its Python environment on first run.
cd "$(dirname "$0")" || exit 1
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
if [ ! -x .venv/bin/python ]; then
  echo "First run: installing mediapipe and opencv…"
  if command -v uv >/dev/null; then
    uv venv --python 3.12 .venv && uv pip install --python .venv/bin/python -r requirements.txt || exit 1
  else
    python3 -m venv .venv && .venv/bin/pip install -r requirements.txt || exit 1
  fi
fi
args=()
[ -s args ] && args=("${(@f)$(<args)}")  # one argument per line, written by the FreeCAD addon
.venv/bin/python hand_tracker.py "${args[@]}" "$@" 2> >(grep -v -E "^(W0000|I0000|INFO:)" >&2)
rc=$?
if [ $rc -ne 0 ]; then echo; echo "The tracker exited with an error ($rc). You can close this window."; read -k 1; fi
