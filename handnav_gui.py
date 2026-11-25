"""HandNav FreeCAD side: receives motion packets over UDP and moves the active 3D view camera."""
import json
import math
import os
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
PARAMS = "User parameter:BaseApp/Preferences/Mod/HandNav"
MODE_TEXT = {"idle": "idle", "orbit": "orbit", "pan": "pan", "zoom": "zoom"}


def _p():
    return App.ParamGet(PARAMS)


def _status(text):
    try:
        Gui.getMainWindow().statusBar().showMessage("HandNav: " + text, 3000)
    except Exception:
        pass


class _PreviewLabel(QtWidgets.QLabel):
    def sizeHint(self):  # fixed hint: the pixmap must not drive the layout
        return QtCore.QSize(320, 240)


class PreviewDock(QtWidgets.QDockWidget):
    """Camera preview sent by the tracker, shown inside the FreeCAD window."""

    def __init__(self, parent):
        super().__init__("HandNav", parent)
        self.setObjectName("HandNavDock")
        self.label = _PreviewLabel("Waiting for the tracker…")
        self.label.setAlignment(QtCore.Qt.AlignCenter)
        self.label.setMinimumSize(160, 120)
        self.label.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Expanding)
        self.setWidget(self.label)
        self._last = 0.0
        self._timer = QtCore.QTimer(self)
        self._timer.timeout.connect(self._check)
        self._timer.start(1000)

    def show_jpeg(self, data):
        pix = QtGui.QPixmap()
        if pix.loadFromData(data, "JPG"):
            self._last = time.monotonic()
            self.label.setPixmap(pix.scaled(self.label.size(), QtCore.Qt.KeepAspectRatio,
                                            QtCore.Qt.SmoothTransformation))

    def set_mode(self, text):
        self.setWindowTitle("HandNav — " + text)

    def _check(self):
        if self._last and time.monotonic() - self._last > 2.0:
            self._last = 0.0
            self.label.setText("No signal from the tracker")


class Navigator(QtCore.QObject):
    def __init__(self):
        super().__init__(Gui.getMainWindow())
        self.sock = None
        self.port = 47800
        self.dock = None
        self._depth = None  # distance from the camera to the orbit pivot, along the view axis

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
            App.Console.PrintError("HandNav: port %d is busy (%s)\n" % (self.port, sock.errorString()))
            sock.deleteLater()
            return False
        sock.readyRead.connect(self._read)
        self.sock = sock
        if p.GetBool("EmbedPreview", True):
            mw = Gui.getMainWindow()
            self.dock = PreviewDock(mw)
            mw.addDockWidget(QtCore.Qt.LeftDockWidgetArea, self.dock)
            mw.resizeDocks([self.dock], [270], QtCore.Qt.Vertical)
        if launch_tracker and p.GetBool("AutoStartTracker", True):
            self.launch_tracker()
        _status("on")
        return True

    def stop(self):
        if not self.running():
            return
        try:  # ask the tracker to exit
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

    def launch_tracker(self):
        """Run the tracker in Terminal: FreeCAD.app itself has no camera permission entry."""
        p = _p()
        args = ["--camera", str(p.GetInt("Camera", -1)), "--port", str(self.port)]
        name = p.GetString("CameraName", "").strip()
        if name:
            args += ["--camera-name", name]
        if self.dock is not None:
            args.append("--embed")
        try:
            args += shlex.split(p.GetString("TrackerArgs", ""))
        except ValueError as e:
            App.Console.PrintWarning("HandNav: ignoring TrackerArgs (%s)\n" % e)
        # one argument per line: the launcher reads them without any shell evaluation
        with open(os.path.join(TRACKER_DIR, "args"), "w") as f:
            f.write("".join(a.replace("\n", " ") + "\n" for a in args))
        # -g: with the preview embedded, Terminal stays in the background
        opts = ["-g"] if self.dock is not None else []
        subprocess.Popen(["open"] + opts + ["-a", "Terminal", os.path.join(TRACKER_DIR, "run_tracker.command")])

    # --- packets ---------------------------------------------------------
    def _read(self):
        orbit = [0.0, 0.0]
        pan = [0.0, 0.0]
        zoom = 1.0
        frame = None
        while self.sock is not None and self.sock.hasPendingDatagrams():
            try:
                data = bytes(self.sock.receiveDatagram().data())
                if data[:2] == b"\xff\xd8":  # JPEG preview frame
                    frame = data
                    continue
                m = json.loads(data)
                t = m.get("t")
                if t == "orbit":
                    orbit[0] += float(m["dx"]); orbit[1] += float(m["dy"])
                elif t == "pan":
                    pan[0] += float(m["dx"]); pan[1] += float(m["dy"])
                elif t == "zoom":
                    zoom *= float(m["k"])
                elif t == "mode":
                    self._depth = None  # new gesture: pick the pivot again
                    text = MODE_TEXT.get(m.get("m"), str(m.get("m")))
                    if self.dock is not None:
                        self.dock.set_mode(text)
                    else:
                        _status(text)
            except (ValueError, KeyError, TypeError, AttributeError):
                continue
        if frame is not None and self.dock is not None:
            self.dock.show_jpeg(frame)
        try:
            self.apply(orbit, pan, zoom)
        except Exception as e:
            App.Console.PrintWarning("HandNav: %s\n" % e)

    # --- camera ----------------------------------------------------------
    def _pivot_depth(self, view, pos, look, fallback):
        """Depth of the scene centre along the view axis, so orbit turns the model in place.
        (An orthographic camera's own focalDistance is often a meaningless 1 mm.)"""
        if self._depth is None:
            self._depth = fallback
            try:
                act = coin.SoGetBoundingBoxAction(coin.SbViewportRegion())
                act.apply(view.getSceneGraph())
                box = act.getBoundingBox()
                if not box.isEmpty():
                    self._depth = (box.getCenter() - pos).dot(look)
            except Exception:
                pass
        return self._depth

    def apply(self, orbit=(0.0, 0.0), pan=(0.0, 0.0), zoom=1.0):
        view = Gui.activeView()
        if view is None or not hasattr(view, "getCameraNode"):
            return
        cam = view.getCameraNode()
        p = _p()
        rot = cam.orientation.getValue()
        pos = cam.position.getValue()
        fd = cam.focalDistance.getValue()
        look = rot.multVec(coin.SbVec3f(0, 0, -1))
        ortho = view.getCameraType() == "Orthographic"

        if orbit[0] or orbit[1]:
            g = p.GetFloat("OrbitGain", 4.0)
            sx = -1.0 if p.GetBool("InvertX", False) else 1.0
            sy = -1.0 if p.GetBool("InvertY", False) else 1.0
            local = (coin.SbRotation(coin.SbVec3f(0, 1, 0), -orbit[0] * g * sx)
                     * coin.SbRotation(coin.SbVec3f(1, 0, 0), orbit[1] * g * sy))
            depth = self._pivot_depth(view, pos, look, fd)
            pivot = pos + look * depth
            rot = local * rot
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
    global _nav
    if _nav is None:
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
            nav.start()
        else:
            nav.stop()

    def IsActive(self):
        return True


class _Manipulator:
    def modifyToolBars(self):
        return [{"append": "HandNav_Toggle", "toolBar": "View"}]

    def modifyMenuBar(self):
        return [{"append": "HandNav_Toggle", "menuItem": "Std_ViewFitAll"}]


def setup():
    Gui.addCommand("HandNav_Toggle", ToggleCommand())
    Gui.addWorkbenchManipulator(_Manipulator())
