# HandNav

Navigate the FreeCAD 3D view with one hand in front of a webcam: orbit, roll, pan, zoom and standard views, without a 3D mouse.

HandNav has two parts: this FreeCAD workbench, and a small **hand tracker** that watches the camera with [MediaPipe](https://ai.google.dev/edge/mediapipe/solutions/vision/hand_landmarker). The tracker is a separate program that you install once with one command.

**Platform:** macOS only. Developed and tested with FreeCAD 27.1 weekly on Apple Silicon; other versions and Intel Macs are untested.

**State:** all the features below work. The gesture thresholds were set on synthetic hands and tried by very few people so far, so expect to adjust the sensitivity.

## Gestures

Gestures work like grabbing the model: the view follows your hand while the gesture is held.

| Gesture | Action |
|---|---|
| Pinch (thumb + index) and move the hand | Orbit |
| Pinch and twist the hand | Roll: rotate in the plane of the screen |
| Fist and move the hand | Pan |
| Thumb + middle finger together, other fingers up; hand up / down | Zoom in / out |
| Pinch or fist, pull away from the camera / push towards it | Zoom in / out (coarse) |
| Squeeze a fist twice, quickly and in place | Fit all |
| Open or relaxed hand, or no hand in view | Pause |

Raise fingers with the hand pointing up and hold still for about 0.3 s to get a standard view. The name and progress are shown over the camera picture.

| Fingers | View | With the thumb out to the side as well |
|---|---|---|
| Index | Front | Rear |
| Index + middle | Top | Bottom |
| Index + middle + ring | Right | Left |
| Index + little finger | Isometric | (same) |
| Little finger | Fit all | (same) |
| Four fingers, thumb folded across the palm | Fit all | |

- **Re-grip:** when your hand reaches the edge of the picture, open it, move it back and grab again, like lifting a mouse off the pad.
- **Orbit centre:** the view turns around whatever is under the centre of the 3D view, or around the middle of the model when nothing is there.
- **Twist and push** start only after a deliberate movement (about 9°, or 10 % of the hand's apparent size). While they are active, orbit waits.
- **Level horizon:** orbit is a free trackball. "Keep the horizon level" in the settings makes it a turntable; roll is then off.
- **Two hands** (optional, in the settings): pinch with both hands and move them apart or together to zoom, or turn them like a wheel to roll. Open both hands before the next gesture. Two open palms held up for about 2 s fit all.

## Install

1. Install the HandNav addon: with the Addon Manager (add `https://github.com/sasha-nordiclab/HandNav`, branch `main`, as a custom repository), or copy this repository into FreeCAD's `Mod` folder as `HandNav`. Restart FreeCAD.
2. Install the tracker. The HandNav panel shows the exact command, with a button to copy it; it is `tracker/install.sh` from the addon folder, run in Terminal:

   ```bash
   zsh path/to/Mod/HandNav/tracker/install.sh
   ```

The last step prepares the libraries and can take a minute. The tracker goes into `~/Library/Application Support/HandNav`. Its installer downloads and sets up:

- a Python environment of its own with `mediapipe` and its dependencies from PyPI (about 300 MB). With [uv](https://docs.astral.sh/uv/) installed it uses Python 3.12, which uv downloads if needed; otherwise the `python3` on your `PATH`, 3.9 or newer (the one from Apple's Command Line Tools will do).
- the hand landmark model (about 8 MB) from Google's MediaPipe model storage, checked against a known SHA-256.
- HandNav Tracker, a tiny windowless helper app compiled from `tracker/helper.c`. This needs Apple's Command Line Tools (`xcode-select --install`). Without them the tracker runs in a Terminal window.

The addon itself installs, downloads and compiles nothing. To remove the tracker: `zsh path/to/Mod/HandNav/tracker/install.sh --uninstall`.

## Use

1. Choose the HandNav workbench and press the hand button. HandNav stays on when you change to another workbench. To have the button everywhere, switch on "Show the HandNav button in every workbench" in the settings, or assign a shortcut under Tools → Customize.
2. The HandNav panel appears. It shows what the tracker is doing, then the camera picture with the points found on your hand and the current action. Errors are shown there too.
3. The first time, macOS asks once whether HandNav Tracker may use the camera: allow it.
4. Press the button again to stop. The camera is released then, and also when FreeCAD quits or crashes.

## Sensitivity and settings

- **Sensitivity** in the panel opens sliders that act while you move your hand: the speed of every motion, and how easily every gesture starts. To the right is always more sensitive.
- **Acceleration**, one of the sliders, works like that of a mouse: fast movements go further, slow ones are finer. All the way to the left switches it off.
- **Gestures** in the panel shows the list of gestures.
- **Settings…** (or FreeCAD's preferences, page HandNav, once the workbench has been opened) has everything else: gestures on or off, directions, camera. Every setting explains itself in a tooltip.

Everything is stored under `BaseApp/Preferences/Mod/HandNav`. A few parameters exist only there (Tools → Edit parameters) and apply the next time HandNav is switched on:

| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `Camera` | Integer | `-1` | Camera index. `-1` picks the Mac's built-in camera. A camera chosen in the settings goes first. |
| `AutoStartTracker` | Boolean | `true` | Start the tracker with the button. With `false`, switch HandNav on and run `run_tracker.command` from the tracker's folder yourself. |
| `TrackerArgs` | String | empty | Extra tracker options, e.g. `--width 640`. They override the settings. |
| `Port` | Integer | `47800` | UDP port on 127.0.0.1. The tracker listens on the next one. |

## Privacy

- The camera is used only while HandNav is on. Pictures are analysed on your Mac, shown in the FreeCAD panel and never stored or sent anywhere by HandNav.
- HandNav itself makes no network connections. Only the tracker's installer does, to PyPI (and, with uv, to its Python downloads) and to Google's model storage.
- The addon and the tracker talk over UDP on the loopback interface (127.0.0.1). Every packet, in both directions, carries a secret kept in a file that only you can read; others are ignored. This protects against stray packets and other users of the Mac, not against programs running under your own account.

## How it works

```
camera → tracker/hand_tracker.py (MediaPipe, started by HandNavTracker.app)
            │  UDP 127.0.0.1:47800: JSON motion and status packets + JPEG preview frames
            ▼
         freecad/HandNav/gui.py (inside FreeCAD) → camera of the active 3D view
```

- `tracker/hand_tracker.py` classifies the hand, debounces changes, smooths the motion and sends it as differences.
- `freecad/HandNav/gui.py` applies the motion to the view camera. It never touches the document.
- `freecad/HandNav/prefs.py` has the settings, the sliders and the gesture guide.
- The tracker cannot run inside FreeCAD, because FreeCAD.app has no camera permission entry. `HandNavTracker.app` starts it as its child, so macOS asks for the camera on behalf of HandNav Tracker.
- The tracker notices when FreeCAD stops listening and exits.

## Troubleshooting

- **The panel shows a message instead of a picture.** It is the tracker's own status or error.
- **"The tracker has not started".** Look at `~/Library/Application Support/HandNav/tracker.log`.
- **macOS never asked about the camera.** Enable HandNav Tracker under System Settings → Privacy & Security → Camera.
- **A Terminal window opens.** The helper app was not built: install the Command Line Tools and run the tracker's installer again. Or "Run the tracker in a Terminal window" is on.
- **"TOO SMALL" over the picture.** The hand is too far from the camera. The numbers are its size and the limit; change the limit with "Distant hand" in the sliders. A running gesture continues down to 75 % of it.
- **The hand is lost near the face or against a busy background.** Lower "Detection confidence" in the settings, add light from the front, and hold the hand beside the face rather than in front of it. Every 10 s `tracker.log` records the frame rate and how often the hand was lost (numbers only).
- **"Port … is busy".** HandNav is on in another FreeCAD window, or another program uses the port. Set a different `Port`.

## Limitations

- macOS only.
- One ordinary camera gives no real depth: zooming by pushing or pulling is judged from the apparent size of the hand and is coarser than the other motions.
- No object selection.

## Development

```bash
python3 -m unittest discover -s tests
./install-dev.sh
```

The tests cover the gesture logic, the link to FreeCAD and camera selection with synthetic hands; they need no camera, MediaPipe or OpenCV. `install-dev.sh` copies the addon into the newest FreeCAD's `Mod` folder and updates the tracker.

## License

MIT, see [LICENSE](LICENSE). MediaPipe and the hand landmark model are separate works by Google under their own licences (Apache-2.0) and are not part of this repository.
