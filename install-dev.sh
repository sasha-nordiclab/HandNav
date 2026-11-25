#!/bin/zsh
# SPDX-License-Identifier: MIT
# For development: copy the addon into a FreeCAD user Mod folder and install the tracker (macOS).
#   ./install-dev.sh                 the newest FreeCAD version that has a settings folder
#   ./install-dev.sh /path/to/Mod    a specific Mod folder
{
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
    MOD="$BASE/Mod"
  else
    echo "No FreeCAD settings folder at $BASE. Start FreeCAD once, or pass a Mod folder." >&2
    exit 1
  fi
  mkdir -p "$MOD"
fi
DST="$MOD/HandNav"
cd "$SRC"
for f in package.xml LICENSE README.md CHANGELOG.md freecad/HandNav/*.py freecad/HandNav/resources/icons/*.svg \
         tracker/{hand_tracker.py,run_tracker.command,requirements.txt,helper.c,Info.plist,VERSION,install.sh}; do
  mkdir -p "$DST/${f:h}"
  cp "$f" "$DST/$f.new.$$"
  mv -f "$DST/$f.new.$$" "$DST/$f"
done
# files of versions before 0.9, which would be loaded as well
rm -f "$DST"/{InitGui.py,handnav_gui.py,handnav_prefs.py,handnav.svg,preferences-handnav.svg}
echo "HandNav installed to $DST"
zsh "$DST/tracker/install.sh"
echo "Restart FreeCAD and choose the HandNav workbench."
exit 0
}
