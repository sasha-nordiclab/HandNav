"""HandNav FreeCAD side: receives motion packets over UDP and moves the active 3D view camera."""
import json
import math
import os
import secrets
import shlex
import socket
import subprocess
import time

import FreeCAD as App
import FreeCADGui as Gui
from PySide import QtCore, QtGui, QtNetwork, QtWidgets
from pivy import coin

HERE = os.path.dirname(os.path.abspath(__file__))
TRACKER_DIR = os.path.join(HERE, "tracker")
VENV_OK = os.path.join(TRACKER_DIR, ".venv", ".handnav-ok")   # written by run_tracker.command
PARAMS = "User parameter:BaseApp/Preferences/Mod/HandNav"
COMMAND = "HandNav_Toggle"
NAV_NAME = "HandNavNavigator"
# FreeCAD.app points these into its own bundle; a child Python (and a Terminal started by
# `open`, which inherits them) breaks if they leak out.
SCRUB_ENV = ("PYTHONHOME", "PYTHONPATH", "PREFIX", "LD_LIBRARY_PATH", "SSL_CERT_FILE", "GIT_SSL_CAINFO")


def _p():
    return App.ParamGet(PARAMS)


def _status(text):
    try:
        Gui.getMainWindow().statusBar().showMessage("HandNav: " + text, 4000)
    except Exception:
        pass


def _set_button(checked):
    """Make the toolbar/menu toggle show the real state without triggering the command."""
    for action in Gui.getMainWindow().findChildren(QtGui.QAction, COMMAND):
        action.blockSignals(True)
        action.setChecked(checked)
        action.blockSignals(False)


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

    def __init__(self, parent, first_run):
        super().__init__("HandNav", parent)
        self.setObjectName("HandNavDock")
        self.label = _PreviewLabel()
        self.label.setAlignment(QtCore.Qt.AlignCenter)
        self.label.setWordWrap(True)
        self.label.setMinimumSize(160, 120)
        self.label.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Expanding)
        self.setWidget(self.label)
        self._started = time.monotonic()
        self._patience = 300.0 if first_run else 20.0
        self._last = 0.0      # time of the last packet from the tracker
        self._final = False   # the tracker said goodbye: keep its message on screen
        self.set_text("First run: the tracker is being installed in the Terminal window.\n"
                      "This can take a few minutes." if first_run else "Starting the tracker…")
        self._timer = QtCore.QTimer(self)
        self._timer.timeout.connect(self._check)
        self._timer.start(1000)

    def alive(self):
        self._last = time.monotonic()

    def set_text(self, text, final=False):
        self._final = final
        self.label.setText(text)

    def show_jpeg(self, data):
        pix = QtGui.QPixmap()
        if pix.loadFromData(data, "JPG"):
            self._final = False
            self.label.setPixmap(pix.scaled(self.label.size(), QtCore.Qt.KeepAspectRatio,
                                            QtCore.Qt.SmoothTransformation))

    def set_mode(self, text):
        self.setWindowTitle("HandNav — " + text)

    def _check(self):
        if self._final:
            return
        now = time.monotonic()
        if self._last and now - self._last > 2.5:
            self.set_text("No signal from the tracker.\nSwitch HandNav off and on to restart it.")
        elif not self._last and now - self._started > self._patience:
            self.set_text("The tracker has not started.\nLook at its Terminal window for the reason.")


class Navigator(QtCore.QObject):
    def __init__(self):
        super().__init__(Gui.getMainWindow())
        self.setObjectName(NAV_NAME)
        self.sock = None
        self.dock = None
        self.port = 47800
        self.token = ""
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
        sock = QtNetwork.QUdpSocket(self)
        if not sock.bind(QtNetwork.QHostAddress(QtNetwork.QHostAddress.LocalHost), self.port):
            msg = "port %d is busy (%s). Is HandNav on in another FreeCAD window?" % (self.port, sock.errorString())
            App.Console.PrintError("HandNav: %s\n" % msg)
            _status(msg)
            sock.deleteLater()
            return False
        sock.readyRead.connect(self._read)
        self.sock = sock
        self.token = secrets.token_hex(8)
        self._depth = None
        auto = launch_tracker and p.GetBool("AutoStartTracker", True)
        first_run = auto and not os.path.exists(VENV_OK)
        if p.GetBool("EmbedPreview", True):
            mw = Gui.getMainWindow()
            self.dock = PreviewDock(mw, first_run)
            mw.addDockWidget(QtCore.Qt.LeftDockWidgetArea, self.dock)
            mw.resizeDocks([self.dock], [270], QtCore.Qt.Vertical)
        try:
            self._write_args()
            if auto:
                self._launch(background=self.dock is not None and not first_run)
            elif self.dock is not None:
                self.dock.set_text("Waiting for the tracker.\nRun tracker/run_tracker.command to start it.")
        except OSError as e:
            App.Console.PrintError("HandNav: could not start the tracker (%s)\n" % e)
            self.stop()
            return False
        _status("on")
        return True

    def stop(self):
        if not self.running():
            return
        try:  # ask the tracker to exit; it also exits by itself once this port is closed
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.sendto(b"quit", ("127.0.0.1", self.port + 1))
            s.close()
        except OSError:
            pass
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
        args = ["--camera", str(p.GetInt("Camera", -1)), "--port", str(self.port), "--token", self.token]
        name = p.GetString("CameraName", "").strip()
        if name:
            args += ["--camera-name", name]
        if self.dock is not None:
            args.append("--embed")
        try:
            args += shlex.split(p.GetString("TrackerArgs", ""))
        except ValueError as e:
            App.Console.PrintWarning("HandNav: ignoring TrackerArgs (%s)\n" % e)
        path = os.path.join(TRACKER_DIR, "args")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write("".join(a.replace("\n", " ") + "\n" for a in args))
        os.chmod(path, 0o600)

    def _launch(self, background):
        """Run the tracker in Terminal: FreeCAD.app itself has no camera permission entry."""
        env = {k: v for k, v in os.environ.items() if k not in SCRUB_ENV}
        cmd = ["open"] + (["-g"] if background else []) + ["-a", "Terminal",
                                                           os.path.join(TRACKER_DIR, "run_tracker.command")]
        subprocess.Popen(cmd, env=env)

    # --- packets ---------------------------------------------------------
    def _read(self):
        orbit = [0.0, 0.0]
        pan = [0.0, 0.0]
        zoom = 1.0
        frame = None
        got = False
        tok = self.token.encode()
        while self.sock is not None and self.sock.hasPendingDatagrams():
            try:
                data = bytes(self.sock.receiveDatagram().data())
                if data.startswith(tok) and data[len(tok):len(tok) + 2] == b"\xff\xd8":  # JPEG preview frame
                    frame, got = data[len(tok):], True
                    continue
                m = json.loads(data)
                if not isinstance(m, dict) or m.get("k") != self.token:
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
            self.apply(orbit, pan, zoom)
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
                pass
            if depth is None:
                try:
                    act = coin.SoGetBoundingBoxAction(coin.SbViewportRegion())
                    act.apply(view.getSceneGraph())
                    box = act.getBoundingBox()
                    if not box.isEmpty():
                        depth = (box.getCenter() - pos).dot(look)
                except Exception:
                    pass
            if depth is None or not math.isfinite(depth) or (not ortho and depth <= 0):
                depth = fallback   # a perspective camera cannot orbit a point behind itself
            self._depth = depth
        return self._depth

    def apply(self, orbit=(0.0, 0.0), pan=(0.0, 0.0), zoom=1.0):
        if not (orbit[0] or orbit[1] or pan[0] or pan[1] or zoom != 1.0):
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

        if orbit[0] or orbit[1]:
            g = p.GetFloat("OrbitGain", 4.0)
            yaw = -orbit[0] * g * (-1.0 if p.GetBool("InvertX", False) else 1.0)
            pitch = orbit[1] * g * (-1.0 if p.GetBool("InvertY", False) else 1.0)
            depth = self._pivot_depth(view, pos, look, fd, ortho)
            pivot = pos + look * depth
            # Coin applies the left factor first.
            if p.GetBool("Turntable", True):
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

        if pan[0] or pan[1]:
            h = cam.height.getValue() if ortho else 2.0 * fd * math.tan(cam.heightAngle.getValue() / 2.0)
            g = p.GetFloat("PanGain", 1.5) * h
            right = rot.multVec(coin.SbVec3f(1, 0, 0))
            up = rot.multVec(coin.SbVec3f(0, 1, 0))
            pos = pos - right * (pan[0] * g) - up * (pan[1] * g)
            cam.position.setValue(pos)

        if zoom != 1.0:
            k = min(max(zoom ** p.GetFloat("ZoomGain", 1.5), 0.5), 2.0)
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
                old.stop()
            except Exception:
                pass
            old.setObjectName("")
            old.deleteLater()
            _set_button(False)
        _nav = Navigator()
    return _nav


class ToggleCommand:
    def GetResources(self):
        return {"MenuText": "HandNav: hand gesture navigation",
                "ToolTip": "Orbit, pan and zoom the 3D view with hand gestures seen by a webcam.\n"
                           "Pinch: orbit. Fist: pan. Two pinches: zoom. Open palm: pause.",
                "Pixmap": os.path.join(HERE, "handnav.svg"),
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


class _Manipulator:
    def modifyToolBars(self):
        return [{"append": COMMAND, "toolBar": "View"}]

    def modifyMenuBar(self):
        # directly in the View menu, after "Fullscreen"
        return [{"insert": COMMAND, "menuItem": "Std_ViewDockUndockFullscreen", "after": ""}]


def setup():
    Gui.addCommand(COMMAND, ToggleCommand())
    Gui.addWorkbenchManipulator(_Manipulator())
