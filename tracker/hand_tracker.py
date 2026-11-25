#!/usr/bin/env python3
"""HandNav tracker: webcam -> hand gestures -> UDP packets for the FreeCAD addon.

Gestures
  pinch (thumb + index) and move ...... orbit
  fist and move ....................... pan
  two pinches, move hands apart/close . zoom
  open palm / no hand ................. idle

Packets (JSON, UDP 127.0.0.1:PORT):
  {"t":"orbit","dx":..,"dy":..}  {"t":"pan","dx":..,"dy":..}  {"t":"zoom","k":..}
  {"t":"mode","m":"idle|orbit|pan|zoom"}
dx/dy are in units of frame height, +x right, +y up (mirror view).
With --embed the preview is sent to the same port as JPEG datagrams instead of opening a window.
A datagram "quit" on PORT+1 stops the tracker.
"""
import argparse
import json
import math
import os
import socket
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
MODEL_PATH = os.path.join(HERE, "hand_landmarker.task")
MODEL_URL = ("https://storage.googleapis.com/mediapipe-models/hand_landmarker/"
             "hand_landmarker/float16/1/hand_landmarker.task")

WRIST, THUMB_TIP, INDEX_TIP = 0, 4, 8
TIPS, PIPS = (8, 12, 16, 20), (6, 10, 14, 18)
PALM = (0, 5, 9, 13, 17)


def _d(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def classify(pts, pinch_thr):
    """pts: 21 (x, y) points in isotropic units. Returns (gesture, palm_center)."""
    cx = sum(pts[i][0] for i in PALM) / len(PALM)
    cy = sum(pts[i][1] for i in PALM) / len(PALM)
    scale = max(_d(pts[0], pts[9]), _d(pts[5], pts[17]), 1e-6)
    wrist = pts[WRIST]
    if all(_d(pts[t], wrist) < _d(pts[p], wrist) for t, p in zip(TIPS, PIPS)):
        return "fist", (cx, cy)
    if _d(pts[THUMB_TIP], pts[INDEX_TIP]) / scale < pinch_thr:
        return "pinch", (cx, cy)
    return "open", (cx, cy)


class Gestures:
    """Turns per-frame hand classifications into debounced, smoothed motion packets."""

    def __init__(self, pinch_on=0.30, pinch_off=0.45, smooth=0.5, deadzone=0.002, debounce=2):
        self.pinch_on, self.pinch_off = pinch_on, pinch_off
        self.smooth, self.deadzone, self.debounce = smooth, deadzone, debounce
        self.mode = "idle"
        self._pending = 0
        self._s = None      # smoothed control value (point, or distance for zoom)
        self._prev = None   # last value a packet was emitted for

    def pinch_thr(self):
        return self.pinch_off if self.mode in ("orbit", "zoom") else self.pinch_on

    def update(self, hands):
        """hands: list of (gesture, center). Returns list of packets (dicts)."""
        pinch = [c for g, c in hands if g == "pinch"]
        fist = [c for g, c in hands if g == "fist"]
        if len(pinch) >= 2:
            want, val = "zoom", (_d(pinch[0], pinch[1]), 0.0)
        elif len(pinch) == 1:
            want, val = "orbit", pinch[0]
        elif fist:
            want, val = "pan", fist[0]
        else:
            want, val = "idle", None

        if want != self.mode:
            self._pending += 1
            if self._pending < self.debounce:
                return []
            self.mode, self._pending = want, 0
            self._s = self._prev = val
            return [{"t": "mode", "m": want}]
        self._pending = 0
        if val is None:
            return []

        a = self.smooth
        self._s = (self._s[0] + a * (val[0] - self._s[0]), self._s[1] + a * (val[1] - self._s[1]))
        dx, dy = self._s[0] - self._prev[0], self._s[1] - self._prev[1]
        if math.hypot(dx, dy) < self.deadzone:
            return []
        prev, self._prev = self._prev, self._s
        if self.mode == "zoom":
            if prev[0] < 1e-3 or self._s[0] < 1e-3:
                return []
            return [{"t": "zoom", "k": prev[0] / self._s[0]}]
        return [{"t": self.mode, "dx": dx, "dy": -dy}]


def ensure_model():
    if os.path.exists(MODEL_PATH):
        return
    import subprocess
    print("Downloading the hand landmark model (about 8 MB)…", flush=True)
    tmp = MODEL_PATH + ".part"
    # curl uses the system certificates; python.org builds often have none
    subprocess.run(["/usr/bin/curl", "-fsSL", "-o", tmp, MODEL_URL], check=True)
    os.replace(tmp, MODEL_PATH)


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


def pick_camera(index, name):
    """Camera index: explicit name match, explicit index, else the Mac's built-in camera."""
    cams = list_cameras()
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
    ap.add_argument("--no-preview", action="store_true", help="no preview window")
    ap.add_argument("--embed", action="store_true", help="send the preview to FreeCAD instead of opening a window")
    ap.add_argument("--pinch", type=float, default=0.30, help="pinch threshold (smaller = stricter)")
    ap.add_argument("--smooth", type=float, default=0.5, help="smoothing 0..1 (smaller = smoother)")
    args = ap.parse_args()

    import cv2
    import mediapipe as mp
    from mediapipe.tasks import python as mp_python
    from mediapipe.tasks.python import vision

    ensure_model()
    landmarker = vision.HandLandmarker.create_from_options(vision.HandLandmarkerOptions(
        base_options=mp_python.BaseOptions(model_asset_path=MODEL_PATH),
        running_mode=vision.RunningMode.VIDEO, num_hands=2,
        min_hand_detection_confidence=0.6, min_tracking_confidence=0.6))

    cam_index = pick_camera(args.camera, args.camera_name)
    print("Using camera %d" % cam_index, flush=True)
    # The first open only triggers the macOS permission prompt and fails, so keep retrying.
    cap, ok, deadline, told = None, False, time.monotonic() + 90, False
    while not ok and time.monotonic() < deadline:
        cap = cv2.VideoCapture(cam_index, cv2.CAP_AVFOUNDATION)
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
        ok, _ = cap.read()
        if not ok:
            cap.release()
            if not told:
                told = True
                print("Waiting for camera access: click Allow in the macOS prompt "
                      "(or System Settings → Privacy & Security → Camera → Terminal)…",
                      flush=True)
            time.sleep(1.5)
    if not ok:
        sys.exit("Could not open the camera. Check Terminal's camera access or pick another one: --camera 1")

    out = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    out.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 262144)  # macOS caps datagrams at 9 KB otherwise
    ctl = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    ctl.bind(("127.0.0.1", args.port + 1))
    ctl.setblocking(False)

    g = Gestures(pinch_on=args.pinch, pinch_off=args.pinch * 1.5, smooth=args.smooth)
    colors = {"idle": (160, 160, 160), "orbit": (80, 200, 255), "pan": (120, 255, 120), "zoom": (255, 160, 80)}
    labels = {"idle": "-", "orbit": "ORBIT", "pan": "PAN", "zoom": "ZOOM"}
    print("HandNav is running. Pinch: orbit, fist: pan, two pinches: zoom. Quit: q or the HandNav button", flush=True)
    draw = args.embed or not args.no_preview
    n = 0
    t0 = time.monotonic()
    while True:
        try:
            if ctl.recv(64).startswith(b"quit"):
                break
        except BlockingIOError:
            pass
        ok, frame = cap.read()
        if not ok:
            time.sleep(0.05)
            continue
        h, w = frame.shape[:2]
        if w > 640:
            frame = cv2.resize(frame, (640, int(h * 640 / w)))
            h, w = frame.shape[:2]
        frame = cv2.flip(frame, 1)
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        res = landmarker.detect_for_video(mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb),
                                          int((time.monotonic() - t0) * 1000))
        hands = []
        for lms in res.hand_landmarks:
            pts = [(p.x * w / h, p.y) for p in lms]
            hands.append(classify(pts, g.pinch_thr()))
            if draw:
                for p in lms:
                    cv2.circle(frame, (int(p.x * w), int(p.y * h)), 3, colors[g.mode], -1)
        for pkt in g.update(hands):
            out.sendto(json.dumps(pkt).encode(), ("127.0.0.1", args.port))
        if draw:
            cv2.putText(frame, labels[g.mode], (12, 34), cv2.FONT_HERSHEY_SIMPLEX, 1.0, colors[g.mode], 2)
        if args.embed:
            n += 1
            if n % 2 == 0:  # ~15 fps is plenty for a preview
                small = cv2.resize(frame, (320, int(h * 320 / w)))
                ok, buf = cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, 60])
                if ok:
                    try:
                        out.sendto(buf.tobytes(), ("127.0.0.1", args.port))
                    except OSError:
                        pass
        elif draw:
            cv2.imshow("HandNav", cv2.resize(frame, (400, int(h * 400 / w))))
            if cv2.waitKey(1) & 0xFF in (ord("q"), 27):
                break
    out.sendto(json.dumps({"t": "mode", "m": "idle"}).encode(), ("127.0.0.1", args.port))
    cap.release()


if __name__ == "__main__":
    main()
