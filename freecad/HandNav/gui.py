# SPDX-License-Identifier: MIT
"""HandNav FreeCAD side: receives motion packets over UDP and moves the active 3D view camera."""
import json
import math
import os
import secrets
import shlex
import socket
import subprocess  # nosec B404 - only starts the tracker the user installed
import sys
import time

import FreeCAD as App
import FreeCADGui as Gui
from PySide import QtCore, QtGui, QtNetwork, QtWidgets
from pivy import coin

from . import prefs

HERE = os.path.dirname(os.path.abspath(__file__))
ICONS = os.path.join(HERE, "resources", "icons")
# The tracker is a separate program that the user installs with tracker/install.sh.
TRACKER_DIR = os.path.expanduser("~/Library/Application Support/HandNav")
TRACKER_VERSION = "2"   # must match tracker/VERSION
INSTALLER = os.path.join(os.path.dirname(os.path.dirname(HERE)), "tracker", "install.sh")
HELPER_APP = os.path.join(TRACKER_DIR, "HandNavTracker.app")
TRACKER_LOG = os.path.join(TRACKER_DIR, "tracker.log")        # output of the windowless tracker
PARAMS = prefs.PARAMS
COMMAND = "HandNav_Toggle"
COMMANDS = [COMMAND, "HandNav_Settings"]
NAV_NAME = "HandNavNavigator"
# FreeCAD.app points these into its own bundle; a child Python (and a Terminal started by
# `open`, which inherits them) breaks if they leak out.
SCRUB_ENV = ("PYTHONHOME", "PYTHONPATH", "PREFIX", "LD_LIBRARY_PATH", "SSL_CERT_FILE", "GIT_SSL_CAINFO",
             "FONTCONFIG_FILE", "FONTCONFIG_PATH")


# only these can be asked for by a packet
VIEWS = {"front": "viewFront", "rear": "viewRear", "top": "viewTop", "bottom": "viewBottom",
         "right": "viewRight", "left": "viewLeft", "iso": "viewIsometric", "fit": "fitAll"}


def _p():
    return App.ParamGet(PARAMS)


def _status(text):
    try:
        Gui.getMainWindow().statusBar().showMessage("HandNav: " + text, 4000)
    except Exception:
        pass  # nosec B110 - a nicety that must never get in the way


def _set_button(checked):
    """Make the toolbar/menu toggle show the real state without triggering the command."""
    for action in Gui.getMainWindow().findChildren(QtGui.QAction, COMMAND):
        action.blockSignals(True)
        action.setChecked(checked)
        action.blockSignals(False)


def tracker_problem():
    """Why the tracker cannot be started, or "" when it is installed and fits this addon."""
    if sys.platform != "darwin":
        return "HandNav works on macOS only."
    if not os.access(os.path.join(TRACKER_DIR, ".venv", "bin", "python"), os.X_OK):
        return "The HandNav tracker is not installed."
    try:
        with open(os.path.join(TRACKER_DIR, "VERSION")) as f:
            version = f.read().strip()
    except OSError:
        version = ""
    return "" if version == TRACKER_VERSION else "The installed HandNav tracker does not fit this version."


def install_command():
    return "zsh " + shlex.quote(INSTALLER)


def _secret():
    """Shared secret of addon and tracker, readable by this user only; made on first use."""
    path = os.path.join(TRACKER_DIR, "secret")
    try:
        with open(path) as f:
            token = f.read().strip()
    except OSError:
        token = ""  # nosec B105 - none yet
    if len(token) < 16:
        token = secrets.token_hex(16)
        _write_private("secret", token + "\n")
    return token


def _use_terminal():
    """Only when asked for, or when the helper app could not be built."""
    return (_p().GetBool("UseTerminal", False)
            or not os.access(os.path.join(HELPER_APP, "Contents", "MacOS", "HandNavTracker"), os.X_OK))


def _write_private(name, text):
    path = os.path.join(TRACKER_DIR, name)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.chmod(path, 0o600)


def _log_tail(since, lines=2, width=160):
    """Last lines the windowless tracker wrote since `since` (epoch seconds), or ""."""
    try:
        if os.path.getmtime(TRACKER_LOG) < since:
            return ""
        with open(TRACKER_LOG, "rb") as f:
            f.seek(max(0, os.path.getsize(TRACKER_LOG) - 4000))
            text = f.read().decode("utf-8", "replace")
    except OSError:
        return ""
    rows = [r.strip() for r in text.replace("\r", "\n").split("\n") if r.strip()]
    return "\n".join(r[:width] for r in rows[-lines:])


def _num(m, key, lo, hi):
    v = float(m[key])
    if not math.isfinite(v):
        raise ValueError(key)
    return min(max(v, lo), hi)


class _PreviewLabel(QtWidgets.QLabel):
    def sizeHint(self):  # fixed hint: the pixmap must not drive the layout
        return QtCore.QSize(320, 240)


class PreviewDock(QtWidgets.QDockWidget):
    """Camera preview and tracker status, shown inside the FreeCAD window."""

    def __init__(self, parent, terminal, manual):
        super().__init__("HandNav", parent)
        self._where = "its Terminal window" if terminal else TRACKER_LOG
        self._tail = not terminal and not manual   # without a window, show what the tracker writes while it starts
        self._wall = time.time()
        self.setObjectName("HandNavDock")
        self.label = _PreviewLabel()
        self.label.setAlignment(QtCore.Qt.AlignCenter)
        self.label.setWordWrap(True)
        self.label.setMinimumSize(160, 120)
        self.label.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Expanding)
        self.guide = QtWidgets.QLabel(prefs.GUIDE)
        self.guide.setTextFormat(QtCore.Qt.RichText)
        self.guide.setAlignment(QtCore.Qt.AlignTop)
        self.guide_area = QtWidgets.QScrollArea()
        self.guide_area.setWidget(self.guide)
        self.guide_area.setWidgetResizable(True)
        self.guide_area.setMinimumHeight(120)
        self.sliders = prefs.SensitivityPanel(lambda: settings_changed(restart=False, reload=False))
        self.sliders_area = QtWidgets.QScrollArea()
        self.sliders_area.setWidget(self.sliders)
        self.sliders_area.setWidgetResizable(True)
        self.sliders_area.setMinimumHeight(120)
        self._sections = {}
        buttons = QtWidgets.QHBoxLayout()
        for name, text, tip, area in (
                ("guide", "Gestures", "Show or hide the list of gestures", self.guide_area),
                ("sliders", "Sensitivity", "Show or hide the sensitivity sliders", self.sliders_area)):
            button = QtWidgets.QToolButton()
            button.setText(text)
            button.setToolTip(tip)
            button.setCheckable(True)
            button.clicked.connect(lambda on, n=name: self.show_section(n if on else ""))
            buttons.addWidget(button)
            self._sections[name] = (button, area)
        settings_button = QtWidgets.QToolButton()
        settings_button.setText("Settings…")
        settings_button.setToolTip("Open the HandNav settings")
        settings_button.clicked.connect(lambda: Gui.runCommand("HandNav_Settings", 0))
        buttons.addWidget(settings_button)
        buttons.addStretch()
        body = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(body)
        layout.setContentsMargins(2, 2, 2, 2)
        self.copy_button = QtWidgets.QPushButton("Copy the install command")
        self.copy_button.clicked.connect(
            lambda: QtWidgets.QApplication.clipboard().setText(install_command()))
        self.copy_button.hide()
        layout.addWidget(self.label, 3)
        layout.addWidget(self.copy_button)
        layout.addLayout(buttons)
        layout.addWidget(self.guide_area, 2)
        layout.addWidget(self.sliders_area, 2)
        for button, area in self._sections.values():
            area.hide()
        self.setWidget(body)
        self._started = time.monotonic()
        # how long the start may stay silent before the panel says so; never when the user starts it
        self._patience = math.inf if manual else 20.0
        self._streaming = False   # preview frames have arrived
        self._last = 0.0      # time of the last packet from the tracker
        self._final = False   # the tracker said goodbye: keep its message on screen
        self.set_text("Waiting for the tracker.\nRun run_tracker.command in\n%s" % TRACKER_DIR
                      if manual else "Starting the tracker…")
        self._timer = QtCore.QTimer(self)
        self._timer.timeout.connect(self._check)
        self._timer.start(1000)

    def show_missing(self, problem):
        """The tracker has to be installed by the user: say how."""
        self.set_text(problem + "\n\nIt is a separate program. To install it, run this command in Terminal, "
                      "then switch HandNav off and on:\n\n" + install_command(), final=True)
        self.label.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        self.copy_button.show()

    def show_section(self, name):
        """Show one section ("guide", "sliders") under the picture, or none ("")."""
        for n, (button, area) in self._sections.items():
            button.setChecked(n == name)
            area.setVisible(n == name)
        _p().SetString("PanelSection", name)
        self.fit_height()

    def fit_height(self):
        if not self.isFloating():
            Gui.getMainWindow().resizeDocks([self], [560 if _p().GetString("PanelSection", "") else 270],
                                            QtCore.Qt.Vertical)

    def alive(self):
        self._last = time.monotonic()

    def set_text(self, text, final=False):
        self._final = final
        self.label.setText(text)

    def show_jpeg(self, data):
        pix = QtGui.QPixmap()
        if pix.loadFromData(data, "JPG"):
            self._final, self._streaming = False, True
            self.label.setPixmap(pix.scaled(self.label.size(), QtCore.Qt.KeepAspectRatio,
                                            QtCore.Qt.SmoothTransformation))

    def set_mode(self, text):
        self.setWindowTitle("HandNav — " + text)

    def _check(self):
        if self._final:
            return
        now = time.monotonic()
        # loading and opening the camera can be silent for a while; a running stream cannot
        if self._last and now - self._last > (2.5 if self._streaming else 60.0):
            self.set_text("No signal from the tracker.\nSwitch HandNav off and on to restart it.")
        elif not self._last:
            tail = _log_tail(self._wall) if self._tail else ""
            if tail:
                self.set_text(tail)
            elif now - self._started > self._patience:
                self.set_text("The tracker has not started.\nSee %s for the reason." % self._where)


class Navigator(QtCore.QObject):
    def __init__(self):
        super().__init__(Gui.getMainWindow())
        self.setObjectName(NAV_NAME)
        self.sock = None
        self.dock = None
        self.port = 47800
        self.token = ""  # nosec B105 - none yet
        self._depth = None  # distance from the camera to the orbit pivot, along the view axis
        app = QtCore.QCoreApplication.instance()
        if app is not None:  # do not leave the camera on when FreeCAD quits
            app.aboutToQuit.connect(self.stop)

    # --- lifecycle -------------------------------------------------------
    def running(self):
        return self.sock is not None

    def start(self, launch_tracker=True):
        if self.running():
            return True
        p = _p()
        self.port = p.GetInt("Port", 47800)
        if not 1024 <= self.port <= 65534:
            msg = "Port must be between 1024 and 65534, not %d" % self.port
            App.Console.PrintError("HandNav: %s\n" % msg)
            _status(msg)
            return False
        sock = QtNetwork.QUdpSocket(self)
        if not sock.bind(QtNetwork.QHostAddress(QtNetwork.QHostAddress.LocalHost), self.port):
            msg = "port %d is busy (%s). Is HandNav on in another FreeCAD window?" % (self.port, sock.errorString())
            App.Console.PrintError("HandNav: %s\n" % msg)
            _status(msg)
            sock.deleteLater()
            return False
        sock.readyRead.connect(self._read)
        self.sock = sock
        self._depth = None
        auto = launch_tracker and p.GetBool("AutoStartTracker", True)
        problem = tracker_problem()
        if p.GetBool("EmbedPreview", True):
            mw = Gui.getMainWindow()
            self.dock = PreviewDock(mw, _use_terminal(), not auto)
            mw.addDockWidget(QtCore.Qt.LeftDockWidgetArea, self.dock)
            self.dock.show_section(p.GetString("PanelSection", ""))
        if problem:
            App.Console.PrintWarning("HandNav: %s Run in Terminal: %s\n" % (problem, install_command()))
            if self.dock is not None:
                self.dock.show_missing(problem)
            else:
                _status(problem)
            return True
        try:
            self.token = _secret()
            self._write_args()
            if auto:
                self._launch(_use_terminal())
        except OSError as e:
            App.Console.PrintError("HandNav: could not start the tracker (%s)\n" % e)
            self.stop()
            return False
        _status("on")
        return True

    def stop(self):
        if not self.running():
            return
        self._quit_tracker()   # it also exits by itself once this port is closed
        self.sock.close()
        self.sock.deleteLater()
        self.sock = None
        if self.dock is not None:
            Gui.getMainWindow().removeDockWidget(self.dock)
            self.dock.deleteLater()
            self.dock = None
        _status("off")

    def _write_args(self):
        """Tracker arguments, one per line: the launcher reads them without any shell evaluation."""
        p = _p()
        args = ["--camera", str(p.GetInt("Camera", -1)), "--port", str(self.port), "--token-file", "secret"]
        if self.dock is not None:
            args.append("--embed")
        args += prefs.tracker_args()
        try:
            args += shlex.split(p.GetString("TrackerArgs", ""))
        except ValueError as e:
            App.Console.PrintWarning("HandNav: ignoring TrackerArgs (%s)\n" % e)
        _write_private("args", "".join(a.replace("\n", " ") + "\n" for a in args))

    def restart_tracker(self):
        """Start the tracker again with the current settings, keeping the panel as it is."""
        if not self.running() or not self.token or not _p().GetBool("AutoStartTracker", True):
            return
        self._quit_tracker()
        if self.dock is not None:
            self.dock.set_text("Applying the settings…")
        self._write_args()
        self._launch(_use_terminal())

    def tune_tracker(self):
        """Hand the current thresholds to the running tracker."""
        if not self.token:
            return
        values = json.dumps(prefs.live_values())
        self._to_tracker(("set %s %s" % (self.token, values)).encode())
        try:
            self._write_args()   # for a tracker that is still starting, or started again by hand
        except OSError:
            pass

    def _quit_tracker(self):
        if self.token:
            self._to_tracker(("quit " + self.token).encode())

    def _to_tracker(self, data):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.sendto(data, ("127.0.0.1", self.port + 1))
            s.close()
        except OSError:
            pass

    def _launch(self, terminal):
        """Start the tracker outside FreeCAD's process tree: FreeCAD.app has no camera permission
        entry, so macOS would refuse the camera to anything FreeCAD runs directly.

        Default: the windowless HandNav Tracker helper app, which holds the camera permission
        itself. UseTerminal=true runs the launcher in a Terminal window instead."""
        env = {k: v for k, v in os.environ.items() if k not in SCRUB_ENV}
        if terminal:
            cmd = ["/usr/bin/open"] + (["-g"] if self.dock is not None else []) + [
                "-a", "Terminal", os.path.join(TRACKER_DIR, "run_tracker.command")]
        else:
            # -n: a new instance even if the previous one is still shutting down; -g: stay in the background
            cmd = ["/usr/bin/open", "-n", "-g", HELPER_APP]
        subprocess.Popen(cmd, env=env)  # nosec B603 - fixed program, no shell

    # --- packets ---------------------------------------------------------
    def _read(self):
        orbit = [0.0, 0.0]
        pan = [0.0, 0.0]
        zoom = 1.0
        roll = 0.0
        push = 0.0
        frame = None
        got = False
        tok = self.token.encode()
        while self.sock is not None and self.sock.hasPendingDatagrams():
            try:
                data = bytes(self.sock.receiveDatagram().data())
                if tok and data.startswith(tok) and data[len(tok):len(tok) + 2] == b"\xff\xd8":  # JPEG preview frame
                    frame, got = data[len(tok):], True
                    continue
                m = json.loads(data)
                if not self.token or not isinstance(m, dict) or m.get("k") != self.token:
                    continue  # not from the tracker this session started
                got = True
                t = m.get("t")
                if t == "orbit":
                    orbit[0] += _num(m, "dx", -1, 1)
                    orbit[1] += _num(m, "dy", -1, 1)
                elif t == "pan":
                    pan[0] += _num(m, "dx", -1, 1)
                    pan[1] += _num(m, "dy", -1, 1)
                elif t == "zoom":
                    zoom *= _num(m, "k2", 0.5, 2.0)
                elif t == "roll":
                    roll += _num(m, "a", -1, 1)
                elif t == "depth":
                    push += _num(m, "d", -1, 1)
                elif t == "view":
                    self._set_view(str(m.get("v")))
                elif t == "mode":
                    self._depth = None  # new gesture: pick the pivot again
                    self._show_mode(str(m.get("m")))
                elif t == "status":
                    self._show_text(str(m.get("msg", "")))
                elif t == "bye":
                    self._bye(str(m.get("msg", "")), bool(m.get("ok", True)))
            except (ValueError, KeyError, TypeError, AttributeError):
                continue
        if self.dock is not None:
            if got:
                self.dock.alive()
            if frame is not None:
                self.dock.show_jpeg(frame)
        try:
            self.apply(orbit, pan, zoom, roll, push)
        except Exception as e:
            App.Console.PrintWarning("HandNav: %s\n" % e)

    def _show_mode(self, mode):
        if self.dock is not None:
            self.dock.set_mode(mode)
        else:
            _status(mode)

    def _show_text(self, text):
        if self.dock is not None:
            self.dock.set_text(text)
        else:
            _status(text)

    def _bye(self, msg, ok):
        if not ok:
            App.Console.PrintError("HandNav tracker: %s\n" % msg)
        text = ("Tracker stopped: %s." if ok else "Tracker error: %s") % msg
        if self.dock is not None:
            self.dock.set_text(text + "\nSwitch HandNav off and on to restart it.", final=True)
        else:
            _status(text)

    def _set_view(self, name):
        view = Gui.activeView()
        if name not in VIEWS or not _p().GetBool("ViewGestures", True) or not hasattr(view, "getCameraNode"):
            return
        self._depth = None
        try:
            getattr(view, VIEWS[name])()
        except Exception as e:
            App.Console.PrintWarning("HandNav: %s\n" % e)
            return
        self._show_mode(name)

    # --- camera ----------------------------------------------------------
    def _pivot_depth(self, view, pos, look, fallback, ortho):
        """Depth of the orbit pivot along the view axis, chosen once per gesture: the geometry
        under the centre of the view, else the centre of the scene. (An orthographic camera's
        own focalDistance is often a meaningless 1 mm.)"""
        if self._depth is None:
            depth = None
            try:
                w, h = view.getSize()
                hit = view.getObjectInfo((w // 2, h // 2))
                if hit:
                    depth = (coin.SbVec3f(hit["x"], hit["y"], hit["z"]) - pos).dot(look)
            except Exception:
                pass  # nosec B110 - a nicety that must never get in the way
            if depth is None:
                try:
                    act = coin.SoGetBoundingBoxAction(coin.SbViewportRegion())
                    act.apply(view.getSceneGraph())
                    box = act.getBoundingBox()
                    if not box.isEmpty():
                        depth = (box.getCenter() - pos).dot(look)
                except Exception:
                    pass  # nosec B110 - a nicety that must never get in the way
            if depth is None or not math.isfinite(depth) or (not ortho and depth <= 0):
                depth = fallback   # a perspective camera cannot orbit a point behind itself
            self._depth = depth
        return self._depth

    def apply(self, orbit=(0.0, 0.0), pan=(0.0, 0.0), zoom=1.0, roll=0.0, push=0.0):
        if not (orbit[0] or orbit[1] or pan[0] or pan[1] or zoom != 1.0 or roll or push):
            return
        view = Gui.activeView()
        if view is None or not hasattr(view, "getCameraNode"):
            return
        cam = view.getCameraNode()
        p = _p()
        # copies: pivy hands out references that change with the camera
        rot = coin.SbRotation(*cam.orientation.getValue().getValue())
        pos = coin.SbVec3f(*cam.position.getValue().getValue())
        fd = cam.focalDistance.getValue()
        look = rot.multVec(coin.SbVec3f(0, 0, -1))
        ortho = view.getCameraType() == "Orthographic"
        turntable = p.GetBool("Turntable", False)
        overall = min(max(p.GetFloat("Sensitivity", 1.0), 0.05), 10.0)

        if orbit[0] or orbit[1]:
            g = p.GetFloat("OrbitGain", 4.0) * overall
            yaw = -orbit[0] * g * (-1.0 if p.GetBool("InvertX", False) else 1.0)
            pitch = orbit[1] * g * (-1.0 if p.GetBool("InvertY", False) else 1.0)
            depth = self._pivot_depth(view, pos, look, fd, ortho)
            pivot = pos + look * depth
            # Coin applies the left factor first.
            if turntable:
                # yaw about the world Z axis, pitch about the camera's X axis: the horizon stays level
                if rot.multVec(coin.SbVec3f(0, 1, 0))[2] < 0:
                    yaw = -yaw   # camera is upside down
                rot = (coin.SbRotation(coin.SbVec3f(1, 0, 0), pitch) * rot
                       * coin.SbRotation(coin.SbVec3f(0, 0, 1), yaw))
            else:
                # free trackball: both rotations about the camera's own axes
                rot = (coin.SbRotation(coin.SbVec3f(0, 1, 0), yaw)
                       * coin.SbRotation(coin.SbVec3f(1, 0, 0), pitch) * rot)
            look = rot.multVec(coin.SbVec3f(0, 0, -1))
            cam.orientation.setValue(rot)
            pos = pivot - look * depth
            cam.position.setValue(pos)

        if roll and not turntable:
            # the model turns with the hand, so the camera turns the other way about its own view
            # axis; the pivot lies on that axis and the position stays
            a = -roll * p.GetFloat("RollGain", 1.0) * overall * (-1.0 if p.GetBool("InvertRoll", False) else 1.0)
            rot = coin.SbRotation(coin.SbVec3f(0, 0, 1), a) * rot
            cam.orientation.setValue(rot)

        if pan[0] or pan[1]:
            h = cam.height.getValue() if ortho else 2.0 * fd * math.tan(cam.heightAngle.getValue() / 2.0)
            g = p.GetFloat("PanGain", 1.5) * overall * h
            right = rot.multVec(coin.SbVec3f(1, 0, 0))
            up = rot.multVec(coin.SbVec3f(0, 1, 0))
            pos = pos - right * (pan[0] * g) - up * (pan[1] * g)
            cam.position.setValue(pos)

        if zoom != 1.0 or push:
            # pushing the hand towards the screen moves the model away, pulling brings it closer
            away = push * p.GetFloat("PushGain", 2.0) * overall * (-1.0 if p.GetBool("InvertPush", False) else 1.0)
            k = min(max(zoom ** (p.GetFloat("ZoomGain", 1.5) * overall) * math.exp(away), 0.5), 2.0)
            if ortho:
                cam.height.setValue(cam.height.getValue() * k)
            else:
                focal = pos + look * fd
                cam.position.setValue(focal - look * (fd * k))
                cam.focalDistance.setValue(fd * k)
                self._depth = None


_nav = None


def navigator():
    """The one Navigator of this FreeCAD session (survives a reload of this module)."""
    global _nav
    if _nav is None:
        old = Gui.getMainWindow().findChild(QtCore.QObject, NAV_NAME)
        if old is not None:  # left by an earlier copy of this module: shut it down
            try:
                was_on = old.running()
                old.stop()
            except Exception:
                was_on = True
            old.setObjectName("")
            old.deleteLater()
            if was_on:
                _set_button(False)
        _nav = Navigator()
    return _nav


class ToggleCommand:
    def GetResources(self):
        return {"MenuText": "HandNav: hand gesture navigation",
                "ToolTip": "Orbit, roll, pan and zoom the 3D view with hand gestures seen by a webcam.\n"
                           "Pinch: orbit, twist to roll, push or pull to zoom. Fist: pan. Thumb + middle finger, up or down: zoom.\n"
                           "Raised fingers: standard views. Open hand: pause.",
                "Pixmap": os.path.join(ICONS, "HandNav.svg"),
                "Checkable": False}

    def Activated(self, checked=0):
        nav = navigator()
        if checked:
            if not nav.start():
                # FreeCAD has already drawn the button as pressed: put it back
                QtCore.QTimer.singleShot(0, lambda: _set_button(False))
        else:
            nav.stop()

    def IsActive(self):
        return True


class SettingsCommand:
    def GetResources(self):
        return {"MenuText": "HandNav settings…",
                "ToolTip": "Sensitivity, gestures, camera",
                "Pixmap": "preferences-system"}

    def Activated(self, index=0):
        Gui.showPreferences("HandNav", 0)

    def IsActive(self):
        return True


class _Manipulator:
    def modifyToolBars(self):
        return [{"append": COMMAND, "toolBar": "View"}]

    def modifyMenuBar(self):
        # directly in the View menu, after "Fullscreen"
        return [{"insert": COMMAND, "menuItem": "Std_ViewDockUndockFullscreen", "after": ""}]


def settings_changed(restart=False, reload=True):
    """Called when settings changed: pass thresholds to the running tracker, or restart it."""
    if _nav is None or not _nav.running():
        return
    if restart:
        _nav.restart_tracker()
    else:
        _nav.tune_tracker()
    if reload and _nav.dock is not None:
        _nav.dock.sliders.load()


_registered = False


def register():
    """Commands and preference pages; called when the workbench is first used."""
    global _registered
    if _registered:
        return
    _registered = True
    Gui.addCommand(COMMAND, ToggleCommand())
    Gui.addCommand("HandNav_Settings", SettingsCommand())
    Gui.addIconPath(ICONS)   # the group's icon is looked up as preferences-handnav
    Gui.addPreferencePage(prefs.SettingsPage, "HandNav")
    Gui.addPreferencePage(prefs.GuidePage, "HandNav")


_manipulator = None


def show_in_all_workbenches():
    """Only when the user asked for it: the button on the View toolbar of every workbench."""
    global _manipulator
    if _manipulator is None:
        _manipulator = _Manipulator()
        Gui.addWorkbenchManipulator(_manipulator)
