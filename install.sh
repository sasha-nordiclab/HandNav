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
  dirs=("$BASE"/v*(N/n))   # v1-1 … v27-1, numerically sorted; empty when there are none
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
mkdir -p "$DST/tracker"
# Copy next to the target and rename over it. A plain cp rewrites the file in place, and a
# launcher that is running at that moment (HandNav switched on) would go on reading the new text.
put() {
  cp "$SRC/$1" "$DST/$1.new.$$"
  mv -f "$DST/$1.new.$$" "$DST/$1"
}
for f in InitGui.py handnav_gui.py handnav.svg package.xml LICENSE README.md \
         tracker/hand_tracker.py tracker/run_tracker.command tracker/requirements.txt; do
  put "$f"
done
chmod +x "$DST/tracker/run_tracker.command"
# a ZIP downloaded with a browser is quarantined; Terminal would refuse to run the launcher
xattr -dr com.apple.quarantine "$DST" 2>/dev/null || true
echo "HandNav installed to $DST"
echo "Restart FreeCAD, then press the HandNav button on the View toolbar."
