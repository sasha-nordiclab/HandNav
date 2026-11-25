#!/bin/zsh
# Starts the HandNav tracker; creates its Python environment on first run.
#
# The whole script is one { ... } group ending in exit: zsh then parses all of it before
# running anything, so replacing this file while a tracker is running (an upgrade) cannot
# make the running shell continue into the new file's text.
{
cd "${0:A:h}" || exit 1
# FreeCAD points these into its own bundle; they must not reach the tracker's Python.
unset PYTHONHOME PYTHONPATH PREFIX LD_LIBRARY_PATH SSL_CERT_FILE GIT_SSL_CAINFO
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

# The environment is complete only when the marker holds the hash of requirements.txt,
# so an interrupted or failed install is redone instead of being reused.
want=$(shasum -a 256 requirements.txt | cut -d' ' -f1)
if [ ! -x .venv/bin/python ] || [ "$(cat .venv/.handnav-ok 2>/dev/null)" != "$want" ]; then
  echo "Setting up the tracker's Python environment (mediapipe and its dependencies)…"
  rm -rf .venv
  if command -v uv >/dev/null; then
    uv venv --python 3.12 .venv && uv pip install --python .venv/bin/python -r requirements.txt
  else
    python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
  fi || { echo; echo "Setup failed. Fix the problem above, then switch HandNav off and on."; read -k 1; exit 1; }
  echo "$want" > .venv/.handnav-ok
fi

args=()
[ -s args ] && args=("${(@f)$(<args)}")  # one argument per line, written by the FreeCAD addon
.venv/bin/python hand_tracker.py "${args[@]}" "$@" 2> >(grep -v -E "^(W0000|I0000|INFO:)" >&2)
rc=$?
if [ $rc -ne 0 ]; then echo; echo "The tracker exited with an error ($rc). You can close this window."; read -k 1; fi
exit $rc
}
