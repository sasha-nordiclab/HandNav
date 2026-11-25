#!/bin/zsh
# Install HandNav into a FreeCAD user Mod folder (macOS).
#   ./install.sh                 newest FreeCAD version found
#   ./install.sh /path/to/Mod    a specific Mod folder
set -e
SRC=${0:A:h}
if [ -n "$1" ]; then
  MOD=$1
else
  BASE="$HOME/Library/Application Support/FreeCAD"
  MOD=$(ls -d "$BASE"/v*/Mod 2>/dev/null | sort -V | tail -1)
  [ -z "$MOD" ] && MOD="$BASE/Mod"
fi
[ -d "$MOD" ] || { echo "Mod folder not found: $MOD" >&2; exit 1; }
DST="$MOD/HandNav"
mkdir -p "$DST/tracker"
cp "$SRC"/{InitGui.py,handnav_gui.py,handnav.svg,package.xml,LICENSE,README.md} "$DST/"
cp "$SRC"/tracker/{hand_tracker.py,run_tracker.command,requirements.txt} "$DST/tracker/"
chmod +x "$DST/tracker/run_tracker.command"
echo "HandNav installed to $DST"
echo "Restart FreeCAD, then press the HandNav button on the View toolbar."
