# SPDX-License-Identifier: MIT
"""HandNav settings: one table of parameters, the preference pages built from it, the gesture guide."""
import json
import math

import FreeCAD as App
from PySide import QtCore, QtWidgets

PARAMS = "User parameter:BaseApp/Preferences/Mod/HandNav"

# name: (type, default, label, help, minimum, maximum, step)
SPEC = [
    ("Gestures", [
        ("EnableRoll", "bool", True, "Roll by twisting the pinch",
         "Twisting the hand while pinching rotates the view in the plane of the screen."),
        ("EnablePush", "bool", True, "Zoom by pushing or pulling the fist",
         "Judged from the apparent size of the hand, so it is coarse. Switch it off if panning zooms by accident."),
        ("EnablePinchPush", "bool", True, "Zoom by pushing or pulling the pinch",
         "Like the fist, while pinching. Switch it off if orbiting zooms by accident."),
        ("EnableSqueeze", "bool", True, "Fit all by squeezing the fist twice",
         "Two quick squeezes in place."),
        ("ViewGestures", "bool", True, "Standard views by raised fingers",
         "See the Gestures page for the list."),
        ("TwoHands", "bool", False, "Two-hand gestures as well",
         "Also follows the second hand: zoom by moving two pinches apart. Slower."),
        ("EnableLock", "bool", True, "Lock by holding the thumb up",
         "While locked, no gesture moves anything. Hold the thumb up again, or press Lock in the panel, to unlock."),
        ("FaceGuard", "bool", True, "Ignore a hand that touches the face",
         "Scratching your nose or leaning on your hand moves nothing. A hand held nearer to the camera\n"
         "than the face still works, also in front of it. The panel shows FACE while a hand is ignored."),
        ("Turntable", "bool", False, "Keep the horizon level",
         "Orbit around the model's vertical axis. Roll is ignored then."),
    ]),
    ("Speed and direction", [
        ("Sensitivity", "float", 1.0, "Overall sensitivity", "Multiplies all the speeds below.", 0.2, 4.0, 0.1),
        ("Acceleration", "float", 0.5, "Acceleration",
         "Fast movements go further, slow ones are finer: orbit, roll, pan and zoom up / down. 0 switches it off.",
         0.0, 1.0, 0.1),
        ("OrbitGain", "float", 4.0, "Orbit speed", "How far the view turns for a hand movement.", 0.5, 15.0, 0.5),
        ("RollGain", "float", 1.0, "Roll speed", "1 turns the view exactly as much as the hand.", 0.2, 5.0, 0.1),
        ("PanGain", "float", 1.5, "Pan speed", "How far the view moves for a hand movement.", 0.2, 6.0, 0.1),
        ("ZoomGain", "float", 1.5, "Zoom speed", "Thumb + middle finger (and two-hand) zoom.", 0.2, 6.0, 0.1),
        ("PushGain", "float", 2.0, "Push zoom speed", "Zoom by pushing or pulling the fist or the pinch.", 0.2, 8.0, 0.2),
        ("InvertX", "bool", False, "Reverse orbit left / right", ""),
        ("InvertY", "bool", False, "Reverse orbit up / down", ""),
        ("InvertRoll", "bool", False, "Reverse roll", ""),
        ("InvertPush", "bool", False, "Reverse push zoom", ""),
    ]),
    ("Recognition", [
        ("MinHandSize", "float", 0.12, "Smallest hand",
         "Palm size as a part of the picture height. Smaller (more distant) hands are ignored; 0 accepts all.\n"
         "The panel shows TOO SMALL with the measured size.", 0.0, 0.6, 0.01),
        ("FaceRatio", "float", 0.70, "Hand size at the face",
         "A hand over the face is ignored while it is seen at the same scale, i.e. its palm is smaller than\n"
         "this part of the face height (a hand on the face: about 0.55). The panel shows the measured value.\n"
         "Smaller lets hands close to the face through, larger ignores more.", 0.4, 1.2, 0.02),
        ("Pinch", "float", 0.30, "Pinch distance",
         "How close thumb and finger must come. Smaller is stricter, larger triggers more easily.", 0.10, 0.60, 0.02),
        ("Fist", "float", 0.90, "Fist tightness",
         "Smaller needs a tighter fist, larger triggers more easily.", 0.60, 1.20, 0.02),
        ("TwistStart", "float", 9.0, "Twist before roll starts (°)",
         "Smaller starts to roll more easily, larger keeps orbiting cleaner.", 2.0, 30.0, 1.0),
        ("PushStart", "float", 10.0, "Size change before push zoom starts (%)",
         "Smaller starts to zoom more easily, larger keeps panning and orbiting cleaner.", 3.0, 40.0, 1.0),
        ("ViewHold", "float", 0.3, "Hold time for views (s)",
         "How long raised fingers must be held still.", 0.1, 2.0, 0.05),
        ("Smooth", "float", 0.5, "Responsiveness",
         "Smaller is smoother but lags behind, 1 follows the hand directly.", 0.05, 1.0, 0.05),
        ("Confidence", "float", 0.4, "Detection confidence",
         "Smaller keeps hold of the hand better against a face or clutter but accepts more false hands.",
         0.1, 0.9, 0.05),
    ]),
    ("Camera and window", [
        ("CameraName", "camera", "", "Camera", "Which camera to use."),
        ("EmbedPreview", "bool", True, "Show the camera in a FreeCAD panel",
         "Otherwise a separate window opens. Applies the next time HandNav is switched on."),
        ("ShowInAllWorkbenches", "bool", False, "Show the HandNav button in every workbench",
         "Adds it to the View toolbar and the View menu. Applies after a restart of FreeCAD."),
        ("UseTerminal", "bool", False, "Run the tracker in a Terminal window",
         "For troubleshooting. Applies the next time HandNav is switched on."),
    ]),
]
# thresholds that a running tracker takes over at once: parameter -> name in the tracker
LIVE = {"MinHandSize": "min_hand", "Pinch": "pinch", "Fist": "fist", "TwistStart": "twist_start",
        "PushStart": "push_start", "ViewHold": "view_hold", "Smooth": "smooth", "Acceleration": "accel",
        "FaceRatio": "face_ratio"}
# sliders of the panel: (title, [(parameter, label, logarithmic, right end is the smaller value)])
SLIDERS = [
    ("Speed", [("Sensitivity", "Overall", True, False),
               ("Acceleration", "Acceleration", False, False), ("OrbitGain", "Orbit", True, False),
               ("RollGain", "Roll", True, False), ("PanGain", "Pan", True, False),
               ("ZoomGain", "Zoom", True, False), ("PushGain", "Push zoom", True, False)]),
    ("How easily it starts", [("Pinch", "Pinch", False, False), ("Fist", "Fist", False, False),
                              ("TwistStart", "Roll", True, True), ("PushStart", "Push zoom", True, True),
                              ("ViewHold", "Finger views", True, True),
                              ("MinHandSize", "Distant hand", False, True),
                              ("FaceRatio", "Near the face", False, True),
                              ("Smooth", "Response", False, False)]),
]
DEFAULTS = {item[0]: (item[1], item[2]) for _, items in SPEC for item in items}
RANGES = {item[0]: item[5:7] for _, items in SPEC for item in items if item[1] == "float"}
TIPS = {item[0]: item[4] for _, items in SPEC for item in items}

GUIDE = """
<table cellpadding="3">
<tr><td colspan="2"><b>Move the view</b></td></tr>
<tr><td>Pinch (thumb + index), move</td><td>Orbit</td></tr>
<tr><td>Pinch, twist the hand</td><td>Roll in the screen plane</td></tr>
<tr><td>Fist, move</td><td>Pan</td></tr>
<tr><td>Thumb + middle finger, others up;<br>hand up / down</td><td>Zoom in / out</td></tr>
<tr><td>Fist or pinch, pull back / push to the camera</td><td>Zoom in / out (coarse)</td></tr>
<tr><td>Open hand</td><td>Pause</td></tr>
<tr><td>Thumb up, hold about a second</td><td>Lock / unlock</td></tr>
<tr><td colspan="2"><b>Fit all</b></td></tr>
<tr><td>Squeeze the fist twice, quickly</td><td>Fit all</td></tr>
<tr><td>Little finger, or four fingers with the<br>thumb across the palm</td><td>Fit all</td></tr>
<tr><td colspan="2"><b>Views: raise fingers, hold still</b></td></tr>
<tr><td>Index</td><td>Front (thumb out: Rear)</td></tr>
<tr><td>Index + middle</td><td>Top (thumb out: Bottom)</td></tr>
<tr><td>Index + middle + ring</td><td>Right (thumb out: Left)</td></tr>
<tr><td>Index + little finger</td><td>Isometric</td></tr>
</table>
"""
HOW = """
<p>HandNav follows one hand with the camera. A gesture works like grabbing the model: while you hold it,
the view follows your hand; open the hand to let go, move it back and grab again.</p>
<p>Orbit turns around whatever is under the middle of the 3D view. The panel shows the camera picture with
the points found on your hand and the name of the current action. Grey points mean the hand is seen but
ignored because it is too small (too far away), red points that it touches the face.</p>
<p>Nothing is sent over the network and no picture is stored.</p>
"""


def get(name):
    """Current value of a parameter from the table."""
    kind, default = DEFAULTS[name]
    p = App.ParamGet(PARAMS)
    if kind == "bool":
        return p.GetBool(name, default)
    if kind == "float":
        return p.GetFloat(name, default)
    return p.GetString(name, default)


def put(name, value):
    kind, default = DEFAULTS[name]
    p = App.ParamGet(PARAMS)
    if value == default:   # keep only what differs, so later defaults reach the user
        {"bool": p.RemBool, "float": p.RemFloat}.get(kind, p.RemString)(name)
    elif kind == "bool":
        p.SetBool(name, bool(value))
    elif kind == "float":
        p.SetFloat(name, float(value))
    else:
        p.SetString(name, str(value))


def live_values():
    """The thresholds for a running tracker."""
    return {key: round(get(name), 4) for name, key in LIVE.items()}


def tracker_args(live=True):
    """The tracker's command-line arguments that follow from the settings; without `live` only
    those that need a restart."""
    args = []
    if live:
        for key, value in live_values().items():
            args += ["--" + key.replace("_", "-"), "%.3f" % max(value, 0.0)]
    args += ["--confidence", "%.3f" % min(max(get("Confidence"), 0.05), 0.95)]
    if get("CameraName").strip():
        args += ["--camera-name", get("CameraName").strip()]
    for name, option, when in (("EnableRoll", "--no-roll", False), ("EnablePush", "--no-push", False),
                               ("EnablePinchPush", "--no-pinch-push", False),
                               ("FaceGuard", "--no-face-guard", False), ("EnableLock", "--no-lock", False),
                               ("EnableSqueeze", "--no-squeeze", False), ("TwoHands", "--two-hands", True)):
        if get(name) == when:
            args.append(option)
    return args


class SettingsPage:
    """Preference page built from SPEC."""

    def __init__(self, parent=None):
        self.form = QtWidgets.QWidget()
        self.form.setWindowTitle("Settings")
        outer = QtWidgets.QVBoxLayout(self.form)
        self._widgets = {}
        self._proc = None
        for title, items in SPEC:
            box = QtWidgets.QGroupBox(title)
            grid = QtWidgets.QFormLayout(box)
            for item in items:
                self._add(grid, *item)
            outer.addWidget(box)
        row = QtWidgets.QHBoxLayout()
        note = QtWidgets.QLabel("Hover over a setting for an explanation.")
        reset = QtWidgets.QPushButton("Restore HandNav defaults")
        reset.clicked.connect(self._reset)
        row.addWidget(note)
        row.addStretch()
        row.addWidget(reset)
        outer.addLayout(row)
        outer.addStretch()

    def _add(self, grid, name, kind, default, label, tip, lo=0.0, hi=1.0, step=0.1):
        if kind == "bool":
            w = QtWidgets.QCheckBox(label)
            grid.addRow(w)
        elif kind == "float":
            w = QtWidgets.QDoubleSpinBox()
            w.setDecimals(2)
            w.setRange(lo, hi)
            w.setSingleStep(step)
            grid.addRow(label, w)
        else:
            w = QtWidgets.QComboBox()
            grid.addRow(label, w)
        text = tip
        if kind == "float":
            text = (tip + "\n" if tip else "") + "Default: %g" % default
        w.setToolTip(text)
        self._widgets[name] = w

    def _set(self, name, value):
        w, kind = self._widgets[name], DEFAULTS[name][0]
        if kind == "bool":
            w.setChecked(value)
        elif kind == "float":
            w.setValue(value)
        else:
            self._fill_cameras([], value)

    def _value(self, name):
        w, kind = self._widgets[name], DEFAULTS[name][0]
        if kind == "bool":
            return w.isChecked()
        if kind == "float":
            return round(w.value(), 4)
        return w.currentData() or ""

    def _fill_cameras(self, names, current):
        w = self._widgets["CameraName"]
        w.clear()
        w.addItem("Automatic (the Mac's built-in camera)", "")
        for n in names:
            w.addItem(n, n)
        if current:
            found = [i for i in range(1, w.count()) if current.lower() in w.itemText(i).lower()]
            if not found:   # not connected now, or only part of a name was given
                w.addItem(current + ("" if not names else " (not connected)"), current)
                found = [w.count() - 1]
            else:
                w.setItemData(found[0], current)
            w.setCurrentIndex(found[0])

    def _list_cameras(self):
        """Ask macOS for the cameras without holding up the dialog."""
        self._proc = QtCore.QProcess(self.form)
        self._proc.finished.connect(self._cameras_listed)
        self._proc.start("/usr/sbin/system_profiler", ["-json", "SPCameraDataType"])

    def _cameras_listed(self, *_):
        try:
            data = json.loads(bytes(self._proc.readAllStandardOutput()).decode("utf-8", "replace"))
            names = sorted(c["_name"] for c in data["SPCameraDataType"])
        except (ValueError, KeyError, TypeError):
            return
        self._fill_cameras(names, self._value("CameraName"))

    def _reset(self):
        for name, (_, default) in DEFAULTS.items():
            if name != "CameraName":
                self._set(name, default)
        self._widgets["CameraName"].setCurrentIndex(0)

    def loadSettings(self):
        for name in DEFAULTS:
            self._set(name, get(name))
        self._list_cameras()

    def saveSettings(self):
        before = tracker_args(live=False)
        for name in DEFAULTS:
            put(name, self._value(name))
        from . import gui

        gui.settings_changed(restart=tracker_args(live=False) != before)


class SensitivityPanel(QtWidgets.QWidget):
    """Sliders that act while you move your hand. To the right is always more sensitive."""

    STEPS = 100

    def __init__(self, changed, parent=None):
        super().__init__(parent)
        self._changed = changed
        self._rows = {}
        grid = QtWidgets.QGridLayout(self)
        grid.setContentsMargins(4, 4, 4, 4)
        row = 0
        for title, items in SLIDERS:
            grid.addWidget(QtWidgets.QLabel("<b>%s</b>" % title), row, 0, 1, 3)
            row += 1
            for name, label, log, flip in items:
                slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
                slider.setRange(0, self.STEPS)
                slider.setMinimumWidth(60)
                value = QtWidgets.QLabel()
                value.setMinimumWidth(36)
                for w in (slider, value):
                    w.setToolTip(TIPS[name] + "\nDefault: %g. Right: more sensitive." % DEFAULTS[name][1])
                slider.valueChanged.connect(lambda _pos, n=name: self._moved(n))
                grid.addWidget(QtWidgets.QLabel(label), row, 0)
                grid.addWidget(slider, row, 1)
                grid.addWidget(value, row, 2)
                self._rows[name] = (slider, value, log, flip)
                row += 1
        reset = QtWidgets.QPushButton("Reset sensitivity")
        reset.clicked.connect(self.reset)
        grid.addWidget(reset, row, 0, 1, 3)
        grid.setRowStretch(row + 1, 1)
        grid.setColumnStretch(1, 1)
        self.load()

    def to_value(self, name, pos):
        _, _, log, flip = self._rows[name]
        lo, hi = RANGES[name]
        t = pos / self.STEPS
        t = 1 - t if flip else t
        return round(lo * (hi / lo) ** t if log else lo + (hi - lo) * t, 3)

    def to_pos(self, name, value):
        _, _, log, flip = self._rows[name]
        lo, hi = RANGES[name]
        value = min(max(value, lo), hi)
        t = math.log(value / lo) / math.log(hi / lo) if log else (value - lo) / (hi - lo)
        return round((1 - t if flip else t) * self.STEPS)

    def load(self):
        """Show the stored values without writing anything."""
        for name, (slider, label, _, _) in self._rows.items():
            slider.blockSignals(True)
            slider.setValue(self.to_pos(name, get(name)))
            slider.blockSignals(False)
            label.setText("%.2f" % get(name))

    def _moved(self, name):
        slider, label, _, _ = self._rows[name]
        value = self.to_value(name, slider.value())
        if slider.value() == self.to_pos(name, DEFAULTS[name][1]):
            value = DEFAULTS[name][1]   # the default sits exactly on its notch
        put(name, value)
        label.setText("%.2f" % value)
        self._changed()

    def reset(self):
        for name in self._rows:
            put(name, DEFAULTS[name][1])
        self.load()
        self._changed()


class GuidePage:
    """Preference page that explains the gestures."""

    def __init__(self, parent=None):
        self.form = QtWidgets.QWidget()
        self.form.setWindowTitle("Gestures")
        layout = QtWidgets.QVBoxLayout(self.form)
        text = QtWidgets.QLabel(HOW + GUIDE)
        text.setTextFormat(QtCore.Qt.RichText)
        text.setWordWrap(True)
        text.setAlignment(QtCore.Qt.AlignTop)
        layout.addWidget(text)
        layout.addStretch()

    def loadSettings(self):
        pass

    def saveSettings(self):
        pass
