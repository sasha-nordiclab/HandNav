#!/usr/bin/env python3
"""HandNav tracker: webcam -> hand gestures -> UDP packets for the FreeCAD addon.

Gestures
  pinch (thumb + index) and move ...... orbit
  fist and move ....................... pan
  two pinches, move hands apart/close . zoom
  open palm / no hand ................. idle

Packets to 127.0.0.1:PORT (JSON, each carries "k": TOKEN):
  {"t":"orbit","dx":..,"dy":..}  {"t":"pan","dx":..,"dy":..}  {"t":"zoom","k2":..}
  {"t":"mode","m":"idle|orbit|pan|zoom"}  {"t":"status","msg":..}  {"t":"alive"}
  {"t":"bye","msg":..,"ok":true|false}
dx/dy are in units of frame height, +x right, +y up (mirror view).
With --embed the preview is sent to the same port as TOKEN + JPEG datagrams instead of a window.

The tracker stops when it receives "quit" on PORT+1, when nobody listens on PORT any more
(FreeCAD switched HandNav off, quit or crashed), or on Ctrl+C / closing its terminal window.
"""
import argparse
import hashlib
import json
import math
import os
import signal
import socket
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
MODEL_PATH = os.path.join(HERE, "hand_landmarker.task")
MODEL_URL = ("https://storage.googleapis.com/mediapipe-models/hand_landmarker/"
             "hand_landmarker/float16/1/hand_landmarker.task")
MODEL_SHA256 = "fbc2a30080c3c557093b5ddfc334698132eb341044ccee322ccf8bcf3607cde1"

WRIST, THUMB_TIP, INDEX_TIP = 0, 4, 8
TIPS, MCPS = (8, 12, 16, 20), (5, 9, 13, 17)
PALM = (0, 5, 9, 13, 17)


def _d(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def classify(pts, pinch_thr=0.30, fist_thr=0.90):
    """pts: 21 (x, y) points in isotropic units. Returns (gesture, palm_center).

    fist:  all four fingertips are pulled back to the palm (closer to the wrist than
           fist_thr x their knuckle). A merely relaxed or half-curled hand is not a fist.
    pinch: thumb tip and index tip closer than pinch_thr x palm size.
    """
    cx = sum(pts[i][0] for i in PALM) / len(PALM)
    cy = sum(pts[i][1] for i in PALM) / len(PALM)
    scale = max(_d(pts[0], pts[9]), _d(pts[5], pts[17]), 1e-6)
    wrist = pts[WRIST]
    if all(_d(pts[t], wrist) < fist_thr * _d(pts[m], wrist) for t, m in zip(TIPS, MCPS)):
        return "fist", (cx, cy)
    if _d(pts[THUMB_TIP], pts[INDEX_TIP]) / scale < pinch_thr:
        return "pinch", (cx, cy)
    return "open", (cx, cy)


class Gestures:
    """Turns per-frame hand landmarks into debounced, smoothed motion packets."""

    def __init__(self, pinch_on=0.30, pinch_off=None, fist_on=0.90, fist_off=None,
                 smooth=0.5, deadzone=0.002, debounce=2, max_jump=0.2):
        self.pinch_on = pinch_on
        self.pinch_off = pinch_on * 1.5 if pinch_off is None else pinch_off
        self.fist_on = fist_on
        self.fist_off = fist_on * 1.15 if fist_off is None else fist_off
        self.smooth = min(max(smooth, 0.05), 1.0)
        self.deadzone, self.debounce, self.max_jump = deadzone, max(1, debounce), max_jump
        self.mode = "idle"
        self._target, self._count, self._mismatch = None, 0, 0
        self._latch = False   # after a zoom: wait for both hands to open before anything else
        self._hands = []      # [(gesture, center)] of the previous frame, for per-hand hysteresis
        self._raw = None      # last raw control value (point, or (distance, 0) for zoom)
        self._s = None        # smoothed control value
        self._prev = None     # smoothed value at the last emitted packet

    def _held(self, center):
        """Gesture the nearest hand of the previous frame was making, or None."""
        best, best_d = None, 0.25
        for g, c in self._hands:
            dist = _d(c, center)
            if dist < best_d:
                best, best_d = g, dist
        return best

    def classify_hands(self, hands_pts):
        """Classify every hand; only a hand already holding a gesture gets the looser threshold."""
        out = []
        for pts in hands_pts:
            _, center = classify(pts, self.pinch_on, self.fist_on)
            held = self._held(center)
            out.append(classify(pts,
                                self.pinch_off if held == "pinch" else self.pinch_on,
                                self.fist_off if held == "fist" else self.fist_on))
        self._hands = out
        return out

    def _nearest(self, points):
        if self._raw is None or len(points) == 1:
            return points[0]
        return min(points, key=lambda c: _d(c, self._raw))

    def update(self, hands_pts):
        """hands_pts: list of 21-point hands. Returns the list of packets (dicts) to send."""
        hands = self.classify_hands(hands_pts)
        pinch = [c for g, c in hands if g == "pinch"]
        fist = [c for g, c in hands if g == "fist"]
        if self._latch:
            if not pinch and not fist:
                self._latch = False
            want, val = "idle", None
        elif len(pinch) >= 2:
            want, val = "zoom", (_d(pinch[0], pinch[1]), 0.0)
        elif len(pinch) == 1:
            want, val = "orbit", pinch[0]
        elif fist:
            want, val = "pan", (self._nearest(fist) if self.mode == "pan" else fist[0])
        else:
            want, val = "idle", None

        if want != self.mode:
            self._mismatch += 1
            if want == self._target:
                self._count += 1
            else:
                self._target, self._count = want, 1
            if self._count >= self.debounce:
                return self._switch(want, val)
            if self._mismatch >= 3 * self.debounce:   # noise that never settles: let go
                return self._switch("idle", None)
            return []
        self._target, self._count, self._mismatch = None, 0, 0
        if val is None:
            return []

        if _d(val, self._raw) > self.max_jump:
            # tracking jumped to the other hand or glitched: re-anchor instead of moving the view
            self._raw = self._s = self._prev = val
            return []
        self._raw = val
        a = self.smooth
        self._s = (self._s[0] + a * (val[0] - self._s[0]), self._s[1] + a * (val[1] - self._s[1]))
        dx, dy = self._s[0] - self._prev[0], self._s[1] - self._prev[1]
        if math.hypot(dx, dy) < self.deadzone:
            return []
        prev, self._prev = self._prev, self._s
        if self.mode == "zoom":
            if prev[0] < 1e-3 or self._s[0] < 1e-3:
                return []
            return [{"t": "zoom", "k2": prev[0] / self._s[0]}]
        return [{"t": self.mode, "dx": dx, "dy": -dy}]

    def _switch(self, new, val):
        self._target, self._count, self._mismatch = None, 0, 0
        if self.mode == "zoom" and new != "zoom":
            # a zoom ends one hand at a time; do not let the other hand orbit on the way out
            self._latch, new, val = True, "idle", None
        if new == self.mode:
            return []
        self.mode = new
        self._raw = self._s = self._prev = val
        return [{"t": "mode", "m": new}]


def out(msg):
    """print that survives a closed terminal window."""
    try:
        print(msg, flush=True)
    except OSError:
        pass


class Link:
    """Connected UDP socket to the FreeCAD addon. `gone` turns true once nobody listens."""

    def __init__(self, port, token, standalone=False):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 262144)  # macOS caps datagrams at 9 KB otherwise
        self.sock.connect(("127.0.0.1", port))   # connected: a closed port is reported as ConnectionRefusedError
        self.token, self.standalone, self.gone = token, standalone, False
        self._last = 0.0

    def _send(self, data):
        try:
            self.sock.send(data)
            self._last = time.monotonic()
        except ConnectionRefusedError:
            self.gone = not self.standalone
        except OSError:
            pass

    def packet(self, **pkt):
        pkt["k"] = self.token
        self._send(json.dumps(pkt).encode())

    def frame(self, jpeg):
        self._send(self.token.encode() + jpeg)

    def keepalive(self, every=0.5):
        if time.monotonic() - self._last >= every:
            self.packet(t="alive")


def bind_control(port):
    """Bind the stop port. A tracker left over from an earlier start is told to quit first."""
    for attempt in range(4):
        ctl = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            ctl.bind(("127.0.0.1", port))
            ctl.setblocking(False)
            return ctl
        except OSError:
            ctl.close()
            probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            probe.sendto(b"quit", ("127.0.0.1", port))
            probe.close()
            time.sleep(1.0)
    return None


def model_ok():
    try:
        with open(MODEL_PATH, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest() == MODEL_SHA256
    except OSError:
        return False


def ensure_model(say=print):
    if model_ok():
        return
    import subprocess
    say("Downloading the hand landmark model (about 8 MB)…")
    tmp = MODEL_PATH + ".part"
    # curl uses the system certificates; python.org builds often have none
    subprocess.run(["/usr/bin/curl", "-fsSL", "-o", tmp, MODEL_URL], check=True)
    os.replace(tmp, MODEL_PATH)
    if not model_ok():
        os.remove(MODEL_PATH)
        raise RuntimeError("the downloaded hand landmark model does not match its checksum")


def list_cameras():
    """[(name, unique_id)] in OpenCV's index order (AVFoundation devices sorted by unique ID)."""
    import subprocess
    try:
        raw = subprocess.run(["/usr/sbin/system_profiler", "-json", "SPCameraDataType"],
                             capture_output=True, text=True, timeout=20).stdout
        cams = [(c.get("_name", "?"), c.get("spcamera_unique-id", "")) for c in json.loads(raw)["SPCameraDataType"]]
    except Exception:
        return []
    return sorted(cams, key=lambda c: c[1])


def pick_camera(index, name, cams=None):
    """Camera index: explicit name match, explicit index, else the Mac's built-in camera."""
    cams = list_cameras() if cams is None else cams
    for i, (n, _) in enumerate(cams):
        print("  camera %d: %s" % (i, n), flush=True)
    if name:
        for i, (n, _) in enumerate(cams):
            if name.lower() in n.lower():
                return i
        print("Camera '%s' not found, using the built-in one." % name, flush=True)
    elif index >= 0:
        return index
    for words in (("facetime", "macbook", "built-in"), ("",)):
        for i, (n, _) in enumerate(cams):
            if any(w in n.lower() for w in words) and "iphone" not in n.lower():
                return i
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--camera", type=int, default=-1, help="camera index; -1 = the Mac's built-in camera")
    ap.add_argument("--camera-name", default="", help="part of a camera name, e.g. FUJIFILM")
    ap.add_argument("--port", type=int, default=47800)
    ap.add_argument("--token", default="", help="session token given by the FreeCAD addon")
    ap.add_argument("--embed", action="store_true", help="send the preview to FreeCAD instead of opening a window")
    ap.add_argument("--no-preview", action="store_true", help="no preview window")
    ap.add_argument("--standalone", action="store_true", help="keep running without FreeCAD listening (for testing)")
    ap.add_argument("--pinch", type=float, default=0.30, help="pinch threshold (smaller = stricter)")
    ap.add_argument("--fist", type=float, default=0.90, help="fist threshold (smaller = tighter fist needed)")
    ap.add_argument("--smooth", type=float, default=0.5, help="smoothing 0.05..1 (smaller = smoother)")
    args = ap.parse_args()

    # The stop port comes first, so a stop sent while the model loads or macOS asks about the
    # camera is not lost, and a second tracker never reaches the camera.
    ctl = bind_control(args.port + 1)
    if ctl is None:
        sys.exit("Port %d is in use by another program: HandNav cannot start. "
                 "Set a different Port in the HandNav parameters." % (args.port + 1))
    link = Link(args.port, args.token, args.standalone)

    stop = {"why": None}

    def on_signal(signum, _frame):
        stop["why"] = "stopped from the terminal"

    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, on_signal)

    def should_stop():
        if stop["why"]:
            return stop["why"]
        try:
            while True:
                if ctl.recv(64).startswith(b"quit"):
                    stop["why"] = "switched off"
                    break
        except OSError:
            pass
        link.keepalive()
        if link.gone:
            stop["why"] = "FreeCAD is not listening"
        return stop["why"]

    def say(msg):
        out(msg)
        link.packet(t="status", msg=msg)

    def finish(msg, ok=True, cap=None):
        if cap is not None:
            cap.release()
        link.packet(t="bye", msg=msg, ok=ok)
        out(("HandNav tracker stopped: " if ok else "HandNav tracker error: ") + msg)
        sys.exit(0 if ok else 1)

    # Is FreeCAD there at all? A refused datagram shows up on the send after it.
    link.packet(t="status", msg="Tracker starting…")
    time.sleep(0.2)
    link.packet(t="alive")
    if link.gone:
        finish("FreeCAD is not listening on port %d. Switch HandNav on in FreeCAD first "
               "(or use --standalone to test the tracker alone)." % args.port)

    say("Loading the hand tracker…")
    import cv2
    import mediapipe as mp
    from mediapipe.tasks import python as mp_python
    from mediapipe.tasks.python import vision

    try:
        ensure_model(say)
        landmarker = vision.HandLandmarker.create_from_options(vision.HandLandmarkerOptions(
            base_options=mp_python.BaseOptions(model_asset_path=MODEL_PATH),
            running_mode=vision.RunningMode.VIDEO, num_hands=2,
            min_hand_detection_confidence=0.6, min_tracking_confidence=0.6))
    except Exception as e:
        finish("could not load the hand landmark model (%s)" % e, ok=False)
    if should_stop():
        finish(stop["why"])

    cams = list_cameras()
    cam_index = pick_camera(args.camera, args.camera_name, cams)
    if cams and cam_index >= len(cams):
        finish("there is no camera %d (found %d: %s). Set Camera or CameraName in the HandNav parameters."
               % (cam_index, len(cams), ", ".join(n for n, _ in cams)), ok=False)
    cam_name = cams[cam_index][0] if cams else "camera %d" % cam_index
    say("Opening %s…" % cam_name)

    # The first open only triggers the macOS permission prompt and fails, so keep retrying.
    cap, ok, deadline, told = None, False, time.monotonic() + 90, False
    while not ok:
        cap = cv2.VideoCapture(cam_index, cv2.CAP_AVFOUNDATION)
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
        ok, _ = cap.read()
        if ok:
            break
        cap.release()
        if time.monotonic() > deadline:
            finish("could not open %s. Allow Terminal to use the camera in System Settings → "
                   "Privacy & Security → Camera, or pick another camera." % cam_name, ok=False)
        if not told:
            told = True
            say("Waiting for camera access: click Allow in the macOS prompt "
                "(or System Settings → Privacy & Security → Camera → Terminal)…")
        for _ in range(6):
            if should_stop():
                finish(stop["why"])
            time.sleep(0.25)

    g = Gestures(pinch_on=args.pinch, fist_on=args.fist, smooth=args.smooth)
    colors = {"idle": (160, 160, 160), "orbit": (80, 200, 255), "pan": (120, 255, 120), "zoom": (255, 160, 80)}
    labels = {"idle": "-", "orbit": "ORBIT", "pan": "PAN", "zoom": "ZOOM"}
    draw = args.embed or not args.no_preview
    window = draw and not args.embed
    say("Running on %s" % cam_name)
    out("Pinch: orbit, fist: pan, two pinches: zoom. Stop: %s."
        % ("q in the preview window, or Ctrl+C here" if window else "the HandNav button in FreeCAD, or Ctrl+C here"))
    n, ts, t0, bad_since = 0, -1, time.monotonic(), None
    while not should_stop():
        ok, frame = cap.read()
        if not ok:
            now = time.monotonic()
            bad_since = bad_since or now
            if now - bad_since > 10:
                finish("%s stopped delivering frames" % cam_name, ok=False, cap=cap)
            time.sleep(0.05)
            continue
        bad_since = None
        h, w = frame.shape[:2]
        if w > 640:
            frame = cv2.resize(frame, (640, int(h * 640 / w)))
            h, w = frame.shape[:2]
        frame = cv2.flip(frame, 1)
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        ts = max(ts + 1, int((time.monotonic() - t0) * 1000))   # must be strictly increasing
        res = landmarker.detect_for_video(mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb), ts)
        hands = [[(p.x * w / h, p.y) for p in lms] for lms in res.hand_landmarks]
        for pkt in g.update(hands):
            link.packet(**pkt)
        if draw:
            for lms in res.hand_landmarks:
                for p in lms:
                    cv2.circle(frame, (int(p.x * w), int(p.y * h)), 3, colors[g.mode], -1)
            cv2.putText(frame, labels[g.mode], (12, 34), cv2.FONT_HERSHEY_SIMPLEX, 1.0, colors[g.mode], 2)
        if args.embed:
            n += 1
            if n % 2 == 0:  # ~15 fps is plenty for a preview
                small = cv2.resize(frame, (320, int(h * 320 / w)))
                ok, buf = cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, 60])
                if ok:
                    link.frame(buf.tobytes())
        elif window:
            cv2.imshow("HandNav", cv2.resize(frame, (400, int(h * 400 / w))))
            if cv2.waitKey(1) & 0xFF in (ord("q"), 27):
                stop["why"] = "closed from the preview window"
    link.packet(t="mode", m="idle")
    finish(stop["why"], cap=cap)


if __name__ == "__main__":
    main()
