# HandNav

Navigate the FreeCAD 3D view with your hand in front of a webcam: orbit, pan and zoom without a 3D mouse.

A small tracker process watches the camera with [MediaPipe](https://ai.google.dev/edge/mediapipe/solutions/vision/hand_landmarker) hand landmarks and sends motion to a FreeCAD addon, which moves the camera of the active 3D view.

**Platform:** macOS only. Developed and tested with FreeCAD 27.1 weekly on Apple Silicon; FreeCAD 1.x is untested.

## Gestures

Gestures work like grabbing the model: the view follows your hand while the gesture is held.

| Gesture | Action |
|---|---|
| Pinch (thumb + index) and move the hand | Orbit |
| Pinch and twist the hand | Roll: rotate in the plane of the screen |
| Fist and move the hand | Pan |
| Fist, pull away from the camera / push towards it | Zoom in / out |
| Pinch with both hands, move them apart / together | Zoom in / out |
| Pinch with both hands, turn them like a wheel | Roll |
| Open or relaxed hand, or no hand in view | Pause |

So a pinch rotates about three axes and a fist moves along three, like a 3D mouse.

Raise fingers with the hand pointing up and hold still for about 0.6 s to get a standard view. The name and progress are shown over the camera picture.

| Fingers | View | With the thumb out to the side as well |
|---|---|---|
| Index | Front | Rear |
| Index + middle | Top | Bottom |
| Index + middle + ring | Right | Left |
| Index + little finger | Isometric | (same) |
| Little finger | Fit all | (same) |

- **Re-grip:** when your hand reaches the edge of the frame, open it, move it back and grab again, like lifting a mouse off the pad.
- **After a zoom:** open both hands before starting the next gesture. Until then nothing moves, so letting go with one hand first does not spin the view.
- **Orbit centre:** the view turns around whatever is under the centre of the 3D view, or around the middle of the model when nothing is there. To orbit around a particular part, pan it to the centre first.
- **Twist and push** start only after a deliberate movement (about 9°, or 10 % of the hand's apparent size), so ordinary orbiting and panning stay clean. While a twist is active, orbit waits.
- **Level horizon:** orbit is a free trackball. Set `Turntable` to `true` to keep the horizon level around the model's Z axis; roll is then off.

## Install

```bash
./install.sh
```

This copies the addon into the `Mod` folder of the newest FreeCAD version that has a settings folder under `~/Library/Application Support/FreeCAD`. Pass a `Mod` folder as an argument to install somewhere else. Restart FreeCAD afterwards.

The installer also compiles a tiny windowless helper app, HandNav Tracker (`tracker/helper.c`), which needs Apple's Command Line Tools (`xcode-select --install`). Without them HandNav falls back to running the tracker in a Terminal window.

The first time you switch HandNav on, the tracker sets itself up inside the installed addon folder:

- a Python environment with `mediapipe` and its dependencies (OpenCV, NumPy and others, about 300 MB). With [uv](https://docs.astral.sh/uv/) installed this is a Python 3.12 environment, which uv downloads if you do not have one; without uv it uses the `python3` on your `PATH`.
- the hand landmark model (about 8 MB) from Google's MediaPipe model storage, checked against a known SHA-256.

## Use

1. Press the HandNav button (a hand) on the View toolbar, or View → HandNav.
2. A HandNav panel appears in the FreeCAD window. It shows what the tracker is doing (on the first run, the installation progress), then the camera picture with the tracked hand points and the current mode (`ORBIT`, `ROLL`, `PAN`, `ZOOM` or a view name). If the tracker fails, the reason is shown there. No other window opens.
3. The first time, macOS asks once whether HandNav Tracker may use the camera: allow it.
4. Press the button again to stop.

The camera is released whenever HandNav stops listening: when you press the button again, when FreeCAD quits, and also when FreeCAD crashes.

The tracker runs under the helper app rather than inside FreeCAD because FreeCAD.app has no camera permission entry, so macOS would refuse it the camera. macOS asks again only when the helper app itself is rebuilt, which `install.sh` does only when its source changed.

## Settings

Tools → Edit parameters → `BaseApp/Preferences/Mod/HandNav`. Create the entry with the given type if it is missing. Camera and tracker settings apply the next time HandNav is switched on; gains apply immediately.

| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `Camera` | Integer | `-1` | Camera index. `-1` picks the Mac's built-in camera and avoids an iPhone Continuity Camera. |
| `CameraName` | String | empty | Part of a camera name, e.g. `FUJIFILM`. Overrides `Camera`. |
| `EmbedPreview` | Boolean | `true` | Show the preview in a FreeCAD panel. `false` opens a separate window instead. |
| `Turntable` | Boolean | `false` | Keep the horizon level while orbiting and ignore roll. `false` rotates freely about the screen axes. |
| `OrbitGain` | Float | `4.0` | Orbit sensitivity, radians per frame height of hand travel. |
| `PanGain` | Float | `1.5` | Pan sensitivity, view heights per frame height of hand travel. |
| `ZoomGain` | Float | `1.5` | Two-hand zoom sensitivity (exponent). |
| `PushGain` | Float | `2.0` | Sensitivity of zooming with the fist. |
| `RollGain` | Float | `1.0` | Roll per twist; `1.0` turns the view as much as the hand. |
| `InvertRoll`, `InvertPush` | Boolean | `false` | Reverse the roll or the fist zoom. |
| `ViewGestures` | Boolean | `true` | React to raised fingers with standard views. |
| `InvertX`, `InvertY` | Boolean | `false` | Reverse the orbit direction. |
| `UseTerminal` | Boolean | `false` | Run the tracker in a Terminal window instead of the helper app. Terminal then needs the camera permission. |
| `AutoStartTracker` | Boolean | `true` | Start the tracker with the button. With `false`, switch HandNav on first and then run `tracker/run_tracker.command` from the installed addon folder yourself. |
| `TrackerArgs` | String | empty | Extra tracker options, e.g. `--pinch 0.25 --smooth 0.4`. |
| `Port` | Integer | `47800` | UDP port on 127.0.0.1. The tracker also listens on the next port for the stop signal. |

The tracker lists the cameras it sees, with their indexes, in `tracker/tracker.log` in the installed addon folder.

Tracker options (`tracker/hand_tracker.py --help`):

- `--pinch` (default 0.30): how close thumb and index must be to count as a pinch, as a fraction of the palm size. Smaller is stricter.
- `--fist` (default 0.90): how far the fingertips must be pulled back to count as a fist. Smaller needs a tighter fist.
- `--smooth` (default 0.5, range 0.05 to 1): motion smoothing. Smaller is smoother and slower.

The gesture thresholds are first estimates: tune `--pinch` and `--fist` if a gesture triggers too easily or not at all.

## How it works

```
camera → tracker/hand_tracker.py (MediaPipe, own Python env, started by HandNavTracker.app)
            │  UDP 127.0.0.1:47800: JSON motion and status packets + JPEG preview frames
            ▼
         handnav_gui.py (inside FreeCAD) → Coin camera of the active 3D view
```

- `tracker/hand_tracker.py` classifies each hand as pinch, fist or open, debounces mode changes, smooths the motion and sends deltas in units of frame height.
- `handnav_gui.py` listens on a `QUdpSocket`, accumulates the packets that arrived since the last event-loop pass and applies them to the view camera. It never touches the document.
- `InitGui.py` registers the toggle command and adds it to the View toolbar and the View menu.
- The tracker sends over a connected UDP socket, so it notices as soon as FreeCAD stops listening and exits. A tracker left over from an earlier start is told to quit by the next one.
- `HandNavTracker.app` (built from `tracker/helper.c` and `tracker/Info.plist`) has no window or Dock icon. It starts `run_tracker.command` as its child so that macOS attributes the camera to it, and writes the output to `tracker/tracker.log`.

**Privacy and trust.** Nothing leaves the machine: both sockets are on the loopback interface and the camera picture goes only to the FreeCAD panel. Each session uses a random token, kept in a file only you can read, and FreeCAD ignores packets without it. This protects the view against stray packets and other users of the Mac; it is not a defence against other programs running under your own account. The stop signal needs no token, so any local program can switch the tracker off.

## Troubleshooting

- **The panel shows a message instead of a picture.** It is the tracker's own status or error, for example waiting for camera access, or a camera index that does not exist.
- **"The tracker has not started".** Look at `tracker/tracker.log` in the installed addon folder: the environment setup may have failed. Fix the cause and switch HandNav off and on; an incomplete environment is rebuilt automatically.
- **macOS never asked about the camera.** Enable HandNav Tracker under System Settings → Privacy & Security → Camera.
- **A Terminal window opens.** The helper app was not built. Install the Command Line Tools and run `./install.sh` again, and check that `UseTerminal` is not set.
- **Wrong camera.** Set `Camera` or `CameraName` (see Settings) and switch HandNav off and on.
- **"Port … is busy".** HandNav is already on in another FreeCAD window, or another program uses the port. Set a different `Port`.
- **Gestures trigger too easily or not at all.** Tune `--pinch` and `--fist` through `TrackerArgs`, and keep the whole hand in the frame with reasonable light.

## Limitations

- macOS only: it uses a macOS helper app, AVFoundation and `system_profiler`.
- One ordinary camera gives no real depth: zooming with the fist is judged from the apparent size of the hand and is coarser than the other motions.
- No object selection yet.
- Gesture thresholds have been tuned on synthetic hand poses, not on a wide range of real hands.

## Development

```bash
python3 -m unittest discover -s tests
```

The tests cover the gesture logic, the link to FreeCAD and camera selection with synthetic hands; they need no camera, MediaPipe or OpenCV.

After changing the code, run `./install.sh` again and restart FreeCAD.

## License

MIT, see [LICENSE](LICENSE).
