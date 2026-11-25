# HandNav

Navigate the FreeCAD 3D view with your hand in front of a webcam: orbit, pan and zoom without a 3D mouse.

A small tracker process watches the camera with [MediaPipe](https://ai.google.dev/edge/mediapipe/solutions/vision/hand_landmarker) hand landmarks and sends motion to a FreeCAD addon, which moves the camera of the active 3D view.

**Platform:** macOS only for now (tested with FreeCAD 27.1 weekly on Apple Silicon).

## Gestures

Gestures work like grabbing the model: the view follows your hand while the gesture is held.

| Gesture | Action |
|---|---|
| Pinch (thumb + index) and move the hand | Orbit |
| Fist and move the hand | Pan |
| Pinch with both hands, move them apart | Zoom in |
| Pinch with both hands, move them together | Zoom out |
| Open palm, or no hand in view | Pause |

To keep going when your hand reaches the edge of the frame, open it, move it back and grab again, like lifting a mouse off the pad.

Orbit turns the view around the centre of the screen at the depth of the model. To orbit around a particular part, pan it to the centre first.

## Install

```bash
./install.sh
```

This copies the addon into the `Mod` folder of the newest FreeCAD version found under `~/Library/Application Support/FreeCAD`. Pass a `Mod` folder as an argument to install somewhere else. Restart FreeCAD afterwards.

The tracker needs Python 3.9 or newer; [uv](https://docs.astral.sh/uv/) is used when available, otherwise `python3 -m venv`. On first start the tracker creates its own environment inside the installed addon folder, installs `mediapipe` and `opencv-python`, and downloads the hand landmark model (about 8 MB) from Google.

## Use

1. Press the HandNav button (a hand) on the View toolbar.
2. Terminal opens in the background and runs the tracker. The first time, macOS asks to let Terminal use the camera: allow it.
3. A HandNav panel appears in the FreeCAD window with the camera picture, the tracked hand points and the current mode (`ORBIT`, `PAN`, `ZOOM`).
4. Press the button again to stop. The tracker exits and the panel is removed.

The tracker runs in Terminal rather than inside FreeCAD because FreeCAD.app has no camera permission entry, so macOS would refuse it the camera.

## Settings

Tools → Edit parameters → `BaseApp/Preferences/Mod/HandNav`. Create the entry with the given type if it is missing. Camera and tracker settings apply the next time HandNav is switched on; gains apply immediately.

| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `Camera` | Integer | `-1` | Camera index. `-1` picks the Mac's built-in camera and avoids an iPhone Continuity Camera. |
| `CameraName` | String | empty | Part of a camera name, e.g. `FUJIFILM`. Overrides `Camera`. |
| `EmbedPreview` | Boolean | `true` | Show the preview in a FreeCAD panel. `false` opens a separate window instead. |
| `OrbitGain` | Float | `4.0` | Orbit sensitivity, radians per frame height of hand travel. |
| `PanGain` | Float | `1.5` | Pan sensitivity, view heights per frame height of hand travel. |
| `ZoomGain` | Float | `1.5` | Zoom sensitivity (exponent). |
| `InvertX`, `InvertY` | Boolean | `false` | Reverse the orbit direction. |
| `AutoStartTracker` | Boolean | `true` | Start the tracker with the button. With `false`, run `tracker/run_tracker.command` yourself. |
| `TrackerArgs` | String | empty | Extra tracker options, e.g. `--pinch 0.25 --smooth 0.4`. |
| `Port` | Integer | `47800` | UDP port on 127.0.0.1. The tracker also listens on the next port for the stop signal. |

The tracker lists the cameras it sees, with their indexes, in its Terminal window.

Tracker options (`tracker/hand_tracker.py --help`): `--pinch` sets how close thumb and index must be to count as a pinch (smaller is stricter), `--smooth` sets motion smoothing from 0 to 1 (smaller is smoother and slower).

## How it works

```
camera → tracker/hand_tracker.py (MediaPipe, own Python env, runs in Terminal)
            │  UDP 127.0.0.1:47800: JSON motion packets + JPEG preview frames
            ▼
         handnav_gui.py (inside FreeCAD) → Coin camera of the active 3D view
```

- `tracker/hand_tracker.py` classifies each hand as pinch, fist or open, debounces mode changes, smooths the motion and sends deltas in units of frame height.
- `handnav_gui.py` listens on a `QUdpSocket`, accumulates the packets that arrived since the last event-loop pass and applies them to the view camera. It never touches the document.
- `InitGui.py` registers the toggle command and adds it to the View toolbar and menu.

Nothing leaves the machine: the socket is bound to the loopback interface and the camera picture is only sent to the FreeCAD panel.

## Troubleshooting

- **Nothing happens after pressing the button.** Look at the Terminal window: it shows the camera list and any error. If macOS never asked about the camera, enable Terminal under System Settings → Privacy & Security → Camera.
- **Wrong camera.** Set `Camera` or `CameraName` (see Settings) and switch HandNav off and on.
- **The panel says "No signal from the tracker".** The tracker stopped. Switch HandNav off and on.
- **Gestures trigger too easily or not at all.** Tune `--pinch` through `TrackerArgs`, and keep the whole hand in the frame with reasonable light.

## Limitations

- macOS only: the launcher uses Terminal, AVFoundation and `system_profiler`.
- One ordinary camera gives no real depth, so there are three motions (orbit, pan, zoom), not the six axes of a 3D mouse.
- No roll around the view axis, no standard views and no object selection yet.

## Development

```bash
python3 -m unittest discover -s tests
```

The tests cover the gesture logic and camera selection with synthetic hands; they need no camera, MediaPipe or OpenCV.

## License

MIT, see [LICENSE](LICENSE).
