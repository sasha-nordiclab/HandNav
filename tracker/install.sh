#!/bin/zsh
# SPDX-License-Identifier: MIT
# Installs the HandNav tracker, the separate program that watches the camera, for this user:
#   ~/Library/Application Support/HandNav
# It downloads mediapipe and its dependencies from PyPI (about 300 MB installed) into an environment
# of its own and the hand and face detection models (about 8 MB) from Google. Nothing is installed into FreeCAD.
#
#   install.sh              install or update
#   install.sh --uninstall  remove everything again
{
set -e
SRC=${0:A:h}
DST="$HOME/Library/Application Support/HandNav"
[ "$(uname)" = Darwin ] || { echo "HandNav works on macOS only." >&2; exit 1; }
unset PYTHONHOME PYTHONPATH
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

if [ "$1" = "--uninstall" ]; then
  # only what this installer and the addon put there
  cd "$DST" 2>/dev/null || { echo "The tracker is not installed."; exit 0; }
  rm -rf .venv HandNavTracker.app .cache
  rm -f hand_tracker.py run_tracker.command requirements.txt VERSION hand_landmarker.task face_detector.tflite \
        args secret tracker.log .helper-ok .env-ok
  cd / && rmdir "$DST" 2>/dev/null || true
  echo "The HandNav tracker was removed."
  exit 0
fi

mkdir -p "$DST"
chmod 700 "$DST"
# copy next to the target and rename, so a running tracker keeps its own copy
for f in hand_tracker.py run_tracker.command requirements.txt; do
  cp "$SRC/$f" "$DST/$f.new.$$"
  mv -f "$DST/$f.new.$$" "$DST/$f"
done
chmod +x "$DST/run_tracker.command"
xattr -d com.apple.quarantine "$DST"/*(.N) 2>/dev/null || true

cd "$DST"
want=$(shasum -a 256 requirements.txt | cut -d' ' -f1)
if [ ! -x .venv/bin/python ] || [ "$(cat .env-ok 2>/dev/null)" != "$want" ]; then
  echo "Installing the tracker's Python environment…"
  rm -rf .venv
  if command -v uv >/dev/null; then
    uv venv --python 3.12 .venv
    uv pip install --python .venv/bin/python -r requirements.txt
  else
    python3 -m venv .venv
    .venv/bin/pip install -r requirements.txt
  fi
  echo "$want" > .env-ok
fi
.venv/bin/python hand_tracker.py --fetch-model
# The first import builds a font index (matplotlib, which mediapipe needs) and takes up to a minute:
# do it now instead of at the first start. It also proves that the libraries load.
echo "Preparing the libraries, this can take a minute…"
export MPLCONFIGDIR="$DST/.cache"
.venv/bin/python -c "import cv2, mediapipe" 2>/dev/null || {
  .venv/bin/python -c "import cv2, mediapipe" || true
  echo "The tracker's libraries do not load. The installation is not complete." >&2
  exit 1
}

# The windowless helper app that macOS grants the camera to. Its ad-hoc signature changes with every
# build and macOS then asks again, so it is rebuilt only when its sources changed.
want=$(cat "$SRC/helper.c" "$SRC/Info.plist" | shasum -a 256 | cut -d' ' -f1)
if [ ! -x HandNavTracker.app/Contents/MacOS/HandNavTracker ] || [ "$(cat .helper-ok 2>/dev/null)" != "$want" ]; then
  NEW="HandNavTracker-new$$.app"
  mkdir -p "$NEW/Contents/MacOS"
  cp "$SRC/Info.plist" "$NEW/Contents/Info.plist"
  if xcode-select -p >/dev/null 2>&1 &&
     xcrun clang -O2 -Wall -o "$NEW/Contents/MacOS/HandNavTracker" "$SRC/helper.c" &&
     codesign --force --sign - "$NEW"; then
    rm -rf HandNavTracker.app
    mv "$NEW" HandNavTracker.app
    echo "$want" > .helper-ok
    echo "Built the HandNav Tracker helper app."
  else
    rm -rf "$NEW"
    echo "note: could not build the helper app (it needs Apple's Command Line Tools:" >&2
    echo "      xcode-select --install). Until then HandNav runs the tracker in a Terminal window." >&2
  fi
fi
cp "$SRC/VERSION" VERSION   # last: marks the installation as complete
echo "The HandNav tracker is installed in $DST"
exit 0
}
