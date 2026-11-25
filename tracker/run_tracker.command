#!/bin/zsh
# SPDX-License-Identifier: MIT
# Runs the HandNav tracker from its installed folder (see install.sh). Normally started without a
# window by HandNavTracker.app, with its output in tracker.log; it also works in a terminal.
#
# The whole script is one { ... } group ending in exit: zsh then parses all of it before
# running anything, so an upgrade cannot change it under a running tracker.
{
cd "${0:A:h}" || exit 1
# FreeCAD points these into its own bundle; they must not reach the tracker's Python.
unset PYTHONHOME PYTHONPATH PREFIX LD_LIBRARY_PATH SSL_CERT_FILE GIT_SSL_CAINFO FONTCONFIG_FILE FONTCONFIG_PATH
export MPLCONFIGDIR="$PWD/.cache"   # filled by install.sh
pause() { [ -t 0 ] && { echo "You can close this window."; read -k 1; }; return 0; }   # only in a terminal

if [ ! -x .venv/bin/python ]; then
  echo "The tracker is not installed here. Run tracker/install.sh of the HandNav addon."
  pause
  exit 1
fi
args=()
[ -s args ] && args=("${(@f)$(<args)}")  # one argument per line, written by the FreeCAD addon
.venv/bin/python hand_tracker.py "${args[@]}" "$@" 2> >(grep -v -E "^(W0000|I0000|INFO:)" >&2)
rc=$?
if [ $rc -ne 0 ]; then echo; echo "The tracker exited with an error ($rc)."; pause; fi
exit $rc
}
