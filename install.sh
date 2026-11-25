#!/bin/zsh
# Install HandNav into a FreeCAD user Mod folder (macOS).
#   ./install.sh                 the newest FreeCAD version that has a settings folder
#   ./install.sh /path/to/Mod    a specific Mod folder
set -e
SRC=${0:A:h}
if [ -n "$1" ]; then
  MOD=$1
  [ -d "$MOD" ] || { echo "Mod folder not found: $MOD" >&2; exit 1; }
else
  BASE="$HOME/Library/Application Support/FreeCAD"
  dirs=("$BASE"/v<->-<->(N/n))   # v1-1 … v27-1, numerically sorted; empty when there are none
  if (( ${#dirs} )); then
    MOD="${dirs[-1]}/Mod"
  elif [ -d "$BASE" ]; then
    MOD="$BASE/Mod"        # FreeCAD releases without per-version folders
  else
    echo "No FreeCAD settings folder at $BASE. Start FreeCAD once, or pass a Mod folder." >&2
    exit 1
  fi
  mkdir -p "$MOD"
fi
DST="$MOD/HandNav"
APP=tracker/HandNavTracker.app
mkdir -p "$DST/tracker"
# Copy next to the target and rename over it. A plain cp rewrites the file in place, and a
# launcher that is running at that moment (HandNav switched on) would go on reading the new text.
put() {
  cp "$SRC/$1" "$DST/$1.new.$$"
  mv -f "$DST/$1.new.$$" "$DST/$1"
}
for f in InitGui.py handnav_gui.py handnav_prefs.py handnav.svg preferences-handnav.svg package.xml LICENSE README.md \
         tracker/hand_tracker.py tracker/run_tracker.command tracker/requirements.txt \
         tracker/helper.c tracker/Info.plist; do
  put "$f"
done
chmod +x "$DST/tracker/run_tracker.command"
# a ZIP downloaded with a browser is quarantined; Terminal would refuse to run the launcher
xattr -dr com.apple.quarantine "$DST" 2>/dev/null || true

# Build the windowless helper app that macOS grants the camera to. Its ad-hoc signature changes
# with every build and macOS then asks again, so rebuild only when its sources changed.
BIN="$DST/$APP/Contents/MacOS/HandNavTracker"
want=$(cat "$SRC/tracker/helper.c" "$SRC/tracker/Info.plist" | shasum -a 256 | cut -d' ' -f1)
if [ ! -x "$BIN" ] || [ "$(cat "$DST/tracker/.helper-ok" 2>/dev/null)" != "$want" ]; then
  NEW="$DST/tracker/HandNavTracker-new$$.app"
  mkdir -p "$NEW/Contents/MacOS"
  cp "$SRC/tracker/Info.plist" "$NEW/Contents/Info.plist"
  if xcode-select -p >/dev/null 2>&1 &&
     xcrun clang -O2 -Wall -o "$NEW/Contents/MacOS/HandNavTracker" "$SRC/tracker/helper.c" &&
     codesign --force --sign - "$NEW"; then
    rm -rf "$DST/$APP"
    mv "$NEW" "$DST/$APP"
    echo "$want" > "$DST/tracker/.helper-ok"
    echo "Built the HandNav Tracker helper app."
  else
    rm -rf "$NEW"
    echo "note: could not build the helper app (it needs Apple's Command Line Tools:" >&2
    echo "      xcode-select --install). Until then HandNav runs the tracker in a Terminal window." >&2
  fi
fi
echo "HandNav installed to $DST"
echo "Restart FreeCAD, then press the HandNav button on the View toolbar."
