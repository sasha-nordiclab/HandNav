# HandNav: guide for agents

FreeCAD workbench (macOS) that moves the 3D view with hand gestures seen by a webcam. Users read `README.md`; this file is for whoever changes the code.

## Rules

- Everything in the repository is in English: code, UI, docs, commit messages.
- One change: code + test + `README.md` (if visible to users) + `CHANGELOG.md` + `<version>` and `<date>` in `package.xml`. Commit locally; push, release, or submit to the addon index only when the user says so.
- Every source file starts with `SPDX-License-Identifier: MIT`.
- The repository is public: no personal paths, names of people, e-mail addresses, camera pictures or tokens.
- The addon must not install, download or compile anything, and must not modify `sys.path` (FreeCAD addon index rules). That is the job of `tracker/install.sh`, which the user runs.
- Commands, toolbars and menus stay in the HandNav workbench. `ShowInAllWorkbenches` is the only, opt-in, exception. `init_gui.py` runs at every start of FreeCAD: keep it minimal and import `gui` lazily.
- No pop-up windows: messages go to the HandNav panel.
- Do not look at or store the user's camera picture. To judge recognition, use the numbers in the log.

## Map

| Path | What |
|---|---|
| `freecad/HandNav/init_gui.py` | Declares the workbench |
| `freecad/HandNav/gui.py` | UDP listener, camera motion, panel (`PreviewDock`), commands, start and stop of the tracker |
| `freecad/HandNav/prefs.py` | `SPEC` (all settings), `LIVE` (changed without restart), `SLIDERS`, preference pages, gesture guide |
| `tracker/hand_tracker.py` | Separate process: camera, MediaPipe, gesture logic. Everything above `main()` is pure Python and tested |
| `tracker/install.sh` | Installs the tracker into `~/Library/Application Support/HandNav`; `--uninstall` removes it |
| `tracker/helper.c`, `Info.plist` | Windowless app that holds the camera permission |
| `tracker/run_tracker.command` | Launcher run by the app |
| `tests/test_gestures.py` | Tests with synthetic hands |
| `install-dev.sh` | Copies the addon into the newest FreeCAD `Mod` folder and updates the tracker |

Inside `hand_tracker.py`, one frame passes through: `HandLandmarker` → `plausible` → `big_enough` → `FaceGuard.filter` → `HandPicker.pick` → `Gestures.update` (`classify`, lock, views, `_motion`) → `Link.packet`.

## Protocol

UDP on 127.0.0.1. Tracker → addon on `Port` (47800), addon → tracker on `Port + 1`. The shared secret is in `secret` (0600).

- To the addon, JSON with `"k": secret`: `orbit`/`pan` (`dx`, `dy` in picture heights, +x right, +y up), `roll` (`a`, radians, counter-clockwise on screen), `zoom` (`k2`, below 1 zooms in), `depth` (`d`, log of hand size), `view`, `lock`, `mode`, `status`, `alive`, `bye`. Preview: secret + JPEG bytes.
- To the tracker: `quit SECRET`, `set SECRET {json}` (thresholds, `locked`).
- The tracker exits when the addon's port is closed; a new tracker tells an old one to quit.

**Raise `tracker/VERSION` and `TRACKER_VERSION` in `gui.py` together** whenever the tracker's arguments or the protocol change. Otherwise an updated addon starts an old tracker.

## How to

- **Test:** `python3 -W error -m unittest discover -s tests` (no camera, MediaPipe or OpenCV needed).
- **Try:** `./install-dev.sh`, then restart FreeCAD. In a running FreeCAD, stop the navigator, reload `prefs` then `gui`, set `gui._registered = True`, and start again.
- **Add a setting:** a row in `SPEC`; if the tracker needs it, also `LIVE` (and `Gestures.tune`) or an option in `tracker_args()`, the argument in `main()`, and a slider in `SLIDERS` if it is worth one.
- **Add a gesture:** a pure function on 21 points, its place in `Gestures.update`, tests that it fires, that it does not fire for every other pose, and that it can be switched off; then `GUIDE` in `prefs.py` and the README table.
- **Scan:** `uvx bandit -q -r freecad tracker` should stay clean.
- **Log:** `~/Library/Application Support/HandNav/tracker.log` has a line of numbers every 10 s: frame rate, how often the hand was found, lost, too small or at the face, palm / face ratio (low, middle, high). Read it with `grep -a`.

## Things that were learned the hard way

- **Camera permission** belongs to the responsible process. FreeCAD.app cannot have it. A script as the app's executable makes macOS hold Python responsible and refuse silently, so the helper is a native binary. Its ad-hoc signature changes with every build and macOS asks again: do not touch `helper.c` or `Info.plist` without need. Children of the app inherit the permission, so the app must stay alive as their parent.
- **FreeCAD's environment** (`PYTHONHOME` and others, see `SCRUB_ENV`) breaks any child Python. It is scrubbed in `gui.py` and again in the launcher.
- **Shell scripts that may be replaced while running** are one `{ ... exit; }` group, and installers copy next to the target and rename. An in-place overwrite once made a running shell execute the new text.
- **macOS limits UDP datagrams to 9 KB** unless `SO_SNDBUF` is raised (preview frames).
- **A connected UDP socket** reports a closed port on the *next* send; that is how the tracker notices that FreeCAD is gone.
- **`num_hands=2` halves the frame rate**: palm detection (about 26 ms) then runs on every frame instead of only when the hand is lost. One hand is the default.
- **Power:** without a hand, palm detection on the whole picture costs about 20 ms per frame and dominated the load (75 % of a core). `Pace` keeps the full rate for gestures only; the camera is off while FreeCAD is in the background (`standby`), and the preview is sent only while the panel is visible. Now about 30 % with a hand in view, 0 % in the background.
- **MediaPipe pulls in matplotlib**, whose first import builds a font index (30 to 46 s). The installer does it (`MPLCONFIGDIR`).
- **`mediapipe` depends on `opencv-contrib-python`**: never add `opencv-python` next to it.
- **Face detection** (BlazeFace short range) misses faces below about 100 px in the whole 960×540 frame, hence the square parts in `FaceSearch`. MediaPipe's world landmarks give the scale of the hand independent of its pose (`true_palm`): about 0.55 of the face height for a hand on the face, 0.8 and more in front of it.
- **OpenCV's camera order** on macOS is the cameras sorted by unique ID.
- **pivy returns references** (`cam.position.getValue()`) that change with the camera: copy them. In Coin, `a * b` applies `a` first. An orthographic camera's `focalDistance` can be a meaningless 1 mm.
- **FreeCAD reads a command's texts once**; a changed menu text needs a restart.

## State and open points

- Verified: FreeCAD 27.1 weekly on Apple Silicon, installation by the Addon Manager (ZIP and git) into a clean profile, tracker installation with uv and with Apple's Python 3.9, hard crash of FreeCAD, update and uninstall.
- Not verified: FreeCAD 1.x, Intel Macs, a Mac without Command Line Tools, other people's hands. Gesture thresholds come from synthetic hands plus a little live tuning.
- Addon index: the repository has to be at least 30 days old (from 2026-12-25). The maintainer address in `package.xml` is a GitHub noreply address that receives no mail; it needs a real one before submitting.
- Custom-repository and index rules: <https://freecad.github.io/Addon-Academy/Topics/Addon-Index/Index/Qualities.html>
