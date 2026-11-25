#!/usr/bin/env python3
"""HandNav tracker: webcam -> hand gestures -> UDP packets for the FreeCAD addon.

Gestures
  pinch (thumb + index) and move ...... orbit
  pinch and twist the hand ............ roll (rotate in the screen plane)
  fist and move ....................... pan
  fist towards / away from the camera . zoom
  thumb + middle finger, up / down .... zoom (index, ring and little finger up)
  with --two-hands also: two pinches, move apart/close: zoom; turn like a wheel: roll
  fist squeezed twice quickly ......... fit all
  fingers held up and still ........... standard view (see POSES)
  open palm / no hand ................. idle

Packets to 127.0.0.1:PORT (JSON, each carries "k": TOKEN):
  {"t":"orbit","dx":..,"dy":..}  {"t":"pan","dx":..,"dy":..}  {"t":"zoom","k2":..}
  {"t":"roll","a":radians, counter-clockwise on screen}  {"t":"depth","d":log of hand size change}
  {"t":"view","v":"front|rear|top|bottom|right|left|iso|fit"}
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
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
MODEL_PATH = os.path.join(HERE, "hand_landmarker.task")
MODEL_URL = ("https://storage.googleapis.com/mediapipe-models/hand_landmarker/"
             "hand_landmarker/float16/1/hand_landmarker.task")
MODEL_SHA256 = "fbc2a30080c3c557093b5ddfc334698132eb341044ccee322ccf8bcf3607cde1"

WRIST, THUMB_TIP, INDEX_TIP = 0, 4, 8
TIPS, PIPS, MCPS = (8, 12, 16, 20), (6, 10, 14, 18), (5, 9, 13, 17)
PALM = (0, 5, 9, 13, 17)


def _d(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def classify(pts, pinch_thr=0.30, fist_thr=0.90, lift_thr=0.30):
    """pts: 21 (x, y) points in isotropic units. Returns (gesture, palm_center).

    fist:  all four fingertips are pulled back to the palm (closer to the wrist than
           fist_thr x their knuckle). A merely relaxed or half-curled hand is not a fist.
    pinch: thumb tip and index tip closer than pinch_thr x palm size.
    lift:  thumb tip and middle tip closer than lift_thr x palm size while the index is clearly
           away and ring and little finger are not folded (pointing with the index, thumb resting
           on the folded middle finger, is not a lift).
    """
    cx = sum(pts[i][0] for i in PALM) / len(PALM)
    cy = sum(pts[i][1] for i in PALM) / len(PALM)
    scale = max(_d(pts[0], pts[9]), _d(pts[5], pts[17]), 1e-6)
    wrist = pts[WRIST]
    if all(_d(pts[t], wrist) < fist_thr * _d(pts[m], wrist) for t, m in zip(TIPS, MCPS)):
        return "fist", (cx, cy)
    index = _d(pts[THUMB_TIP], pts[INDEX_TIP]) / scale
    if index < pinch_thr:
        return "pinch", (cx, cy)
    if (_d(pts[THUMB_TIP], pts[12]) / scale < lift_thr and index > 0.5
            and all(_d(pts[t], wrist) > _d(pts[p], wrist) for t, p in ((16, 14), (20, 18)))):
        return "lift", (cx, cy)
    return "open", (cx, cy)


# raised (index, middle, ring, pinky), thumb out -> view; None: the thumb does not matter
POSES = {
    (1, 0, 0, 0, False): "front", (1, 0, 0, 0, True): "rear",
    (1, 1, 0, 0, False): "top", (1, 1, 0, 0, True): "bottom",
    (1, 1, 1, 0, False): "right", (1, 1, 1, 0, True): "left",
    (1, 0, 0, 1, None): "iso",
    (0, 0, 0, 1, None): "fit",
    (1, 1, 1, 1, True): "palm",   # no view by itself; two of them: fit
}


def pose_of(pts):
    """Standard view shown by a hand with raised fingers, or None. Anything unclear is None."""
    wrist = pts[WRIST]
    up, side = wrist[1] - pts[9][1], abs(wrist[0] - pts[9][0])
    if up <= 0 or side > 1.2 * up:   # only a hand that points upwards
        return None
    raised = []
    for t, p in zip(TIPS, PIPS):
        ratio = _d(pts[t], wrist) / max(_d(pts[p], wrist), 1e-6)
        if 1.0 <= ratio <= 1.12:     # half bent
            return None
        raised.append(1 if ratio > 1.12 else 0)
    scale = max(_d(pts[0], pts[9]), _d(pts[5], pts[17]), 1e-6)
    out_by = _d(pts[THUMB_TIP], pts[5]) / scale
    thumb = True if out_by > 0.70 else False if out_by < 0.55 else None
    key = tuple(raised)
    if key == (1, 1, 1, 1) and _d(pts[THUMB_TIP], pts[13]) / scale < 0.45:
        return "fit"   # only with the thumb clearly folded across the palm; an open hand is a pause
    if key + (None,) in POSES:
        return POSES[key + (None,)]
    return None if thumb is None else POSES.get(key + (thumb,))


def twist_of(pts):
    """In-plane direction of the hand (wrist to middle knuckle), counter-clockwise on screen."""
    return math.atan2(pts[WRIST][1] - pts[9][1], pts[9][0] - pts[WRIST][0])


def size_of(pts):
    """Log of the apparent palm size: grows as the hand comes closer to the camera."""
    return math.log(max(_d(pts[0], pts[9]), _d(pts[5], pts[17]), 1e-3))


def plausible(pts, score):
    """False for detections that are no usable hand (often a face or clutter)."""
    length, width = _d(pts[0], pts[9]), _d(pts[5], pts[17])
    palm = max(length, width)
    # bones of a hand are comparable to its palm; a misfit stretches or collapses
    longest = max(_d(pts[i], pts[i + 1]) for i in (5, 6, 7, 9, 10, 11, 13, 14, 15, 17, 18, 19))
    return score >= 0.55 and 0.04 <= palm <= 0.9 and longest < 1.3 * palm


class HandPicker:
    """One-hand mode: stay with the working hand when a second hand (or a face) is detected too."""

    NEAR, FORGET = 0.3, 0.4

    def __init__(self):
        self._at, self._seen = None, -1e9

    def pick(self, hands, now, busy):
        """hands: plausible hands; busy: a gesture is in progress. Returns [] or [hand]."""
        if now - self._seen > self.FORGET:
            self._at = None
        best, best_key = None, None
        for pts in hands:
            c = classify(pts)[1]
            near = self._at is not None and _d(c, self._at) < self.NEAR
            if busy and self._at is not None:
                if not near:
                    continue       # never hand a running gesture over to another hand
                key = _d(c, self._at)
            else:
                key = c[1] - (0.15 if near else 0.0)   # the raised hand, with a bonus for the current one
            if best is None or key < best_key:
                best, best_key = pts, key
        if best is None:
            return []
        self._at, self._seen = classify(best)[1], now
        return [best]


class Axis:
    """A secondary motion (twist, push) that only engages on a deliberate movement, so that plain
    orbiting or panning does not leak into it, and lets go again when the movement ends."""

    def __init__(self, engage, hold, step, max_jump, period=None, smooth=0.35):
        self.engage, self.hold, self.step, self.max_jump = engage, hold, step, max_jump
        self.period, self.smooth = period, smooth
        self.reset(None)

    def reset(self, value):
        self._raw, self._acc, self._s, self._ref = value, 0.0, 0.0, 0.0
        self._recent, self.active = [], False

    def update(self, value):
        """Returns the change to apply for this frame (0.0 while not engaged)."""
        if self._raw is None:
            self.reset(value)
            return 0.0
        d = value - self._raw
        if self.period:   # shortest way round
            d -= self.period * round(d / self.period)
        self._raw = value
        if abs(d) > self.max_jump:   # tracking glitch or another hand
            self.reset(value)
            return 0.0
        self._acc += d
        self._s += self.smooth * (self._acc - self._s)
        self._recent = (self._recent + [self._s])[-12:]
        if not self.active:
            self._ref += 0.05 * (self._s - self._ref)   # forget slow drift of the grip
            if abs(self._s - self._ref) > self.engage:
                self.active, self._ref, self._recent = True, self._s, [self._s]
            return 0.0
        if len(self._recent) == 12 and max(self._recent) - min(self._recent) < self.hold:
            self.reset(value)
            return 0.0
        change = self._s - self._ref
        if abs(change) < self.step:
            return 0.0
        self._ref = self._s
        return change


class Gestures:
    """Turns per-frame hand landmarks into debounced, smoothed motion packets."""

    def __init__(self, pinch_on=0.30, pinch_off=None, fist_on=0.90, fist_off=None,
                 smooth=0.5, deadzone=0.002, debounce=2, max_jump=0.2, view_hold=0.3):
        self.pinch_on = pinch_on
        self.pinch_off = pinch_on * 1.5 if pinch_off is None else pinch_off
        self.fist_on = fist_on
        self.fist_off = fist_on * 1.15 if fist_off is None else fist_off
        self.smooth = min(max(smooth, 0.05), 1.0)
        self.deadzone, self.debounce, self.max_jump = deadzone, max(1, debounce), max_jump
        self.mode = "idle"
        self._target, self._count, self._mismatch = None, 0, 0
        self._latch = False   # after a zoom: wait for both hands to open before anything else
        self._calm = 0        # frames in a row without any gesture, while latched
        self._hands = []      # [(gesture, center, frames unseen)], for per-hand hysteresis
        self._raw = None      # last raw control value (point, or (distance, 0) for zoom)
        self._s = None        # smoothed control value
        self._prev = None     # smoothed value at the last emitted packet
        self.view_hold = min(max(view_hold, 0.1), 3.0)
        self._now = 0.0
        self._lost_at = None      # since when no hand is detected during a gesture
        self._idle_since = None   # since when hands are in view without any gesture
        self._pose, self._pose_at, self._pose_t, self._pose_n = None, None, 0.0, 0
        self._pose_miss, self._pose_done = 0, False
        self._sq_on = self._sq_from = self._sq_at = None   # current fist: start time, start and last place
        self._sq_end = self._sq_end_at = None              # end of a first short squeeze
        self._sq_second = False
        self._twist = Axis(engage=0.15, hold=0.04, step=0.008, max_jump=0.6, period=2 * math.pi)
        self._wheel = Axis(engage=0.15, hold=0.04, step=0.008, max_jump=0.6, period=math.pi)
        self._push = Axis(engage=0.10, hold=0.03, step=0.006, max_jump=0.35)

    LIFT_GAIN = 2.0
    POSE_STILL = 0.07
    PALMS_AFTER = 1.5   # idle seconds before two open palms count: letting go of a zoom is not "fit"
    SQUEEZE = (0.05, 0.45)      # seconds a squeeze may last; a longer fist is a pan
    SQUEEZE_GAP = (0.05, 0.50)  # seconds the hand is open between the two
    SQUEEZE_STILL = 0.12
    LOST = 0.25       # seconds a gesture survives while no hand is detected
    MEMORY = 8        # frames a hand keeps its held gesture while undetected
    MATCH = 0.25      # how far a hand can move between frames and still be the same hand

    def _match(self, centers):
        """Pair current hands with remembered ones, nearest pairs first, each used once."""
        pairs = sorted((_d(c, old[1]), i, j) for i, c in enumerate(centers)
                       for j, old in enumerate(self._hands))
        held, used = {}, set()
        for dist, i, j in pairs:
            if dist < self.MATCH and i not in held and j not in used:
                held[i] = self._hands[j][0]
                used.add(j)
        return held, used

    def classify_hands(self, hands_pts):
        """Classify every hand; only a hand already holding a gesture gets the looser threshold."""
        centers = [classify(pts)[1] for pts in hands_pts]
        held, used = self._match(centers)
        out = [classify(pts,
                        self.pinch_off if held.get(i) == "pinch" else self.pinch_on,
                        self.fist_off if held.get(i) == "fist" else self.fist_on,
                        self.pinch_off if held.get(i) == "lift" else self.pinch_on)
               for i, pts in enumerate(hands_pts)]
        unseen = [(g, c, n + 1) for j, (g, c, n) in enumerate(self._hands)
                  if j not in used and n + 1 <= self.MEMORY]
        self._hands = [(g, c, 0) for g, c in out] + unseen
        return out

    def _nearest(self, points):
        if self._raw is None or len(points) == 1:
            return points[0]
        return min(points, key=lambda c: _d(c, self._raw))

    def update(self, hands_pts, now=None):
        """hands_pts: list of 21-point hands, now: seconds (default: 30 frames per second).
        Returns the list of packets (dicts) to send."""
        self._now = self._now + 1 / 30 if now is None else now
        hands = self.classify_hands(hands_pts)
        view = self._view(hands, hands_pts)
        if view:
            return view
        return self._motion(hands, hands_pts) + ([{"t": "view", "v": "fit"}] if self._squeeze(hands) else [])

    def _motion(self, hands, hands_pts):
        if not hands_pts and self.mode not in ("idle", "zoom"):
            if self._lost_at is None:
                self._lost_at = self._now
            if self._now - self._lost_at < self.LOST:
                return []
        elif self._lost_at is not None and hands_pts:
            if self._now - self._lost_at > 0.1:
                self._raw = None   # it moved unseen: go on from where it is now, without a jump
            self._lost_at = None
        pinch = [pts for (g, _), pts in zip(hands, hands_pts) if g == "pinch"]
        fists = {c: pts for (g, c), pts in zip(hands, hands_pts) if g == "fist"}
        fist = list(fists)
        lift = [c for g, c in hands if g == "lift"]
        aux = None
        if self._latch:
            # a hand lost for one frame is not an open hand: ask for several calm frames
            self._calm = 0 if pinch or fist or lift else self._calm + 1
            if self._calm > self.debounce:
                self._latch = False
            want, val = "idle", None
        elif len(pinch) >= 2:
            a, b = classify(pinch[0])[1], classify(pinch[1])[1]
            want, val, aux = "zoom", (_d(a, b), 0.0), math.atan2(a[1] - b[1], b[0] - a[0])
        elif len(pinch) == 1:
            # the wrist, because twisting the hand moves the palm but hardly the wrist
            want, val, aux = "orbit", pinch[0][WRIST], twist_of(pinch[0])
        elif fist:
            want, val = "pan", (self._nearest(fist) if self.mode == "pan" else fist[0])
            aux = size_of(fists[val])
        elif lift:
            want, val = "lift", (self._nearest(lift) if self.mode == "lift" else lift[0])
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

        if self._raw is None or _d(val, self._raw) > self.max_jump:
            # tracking jumped to the other hand or glitched: re-anchor instead of moving the view
            self._raw = self._s = self._prev = val
            self._reset_axes()
            return []
        self._raw = val
        extra = []
        if self.mode == "lift":
            pass
        elif self.mode == "pan":
            d = self._push.update(aux)
            if d:
                extra = [{"t": "depth", "d": d}]
        else:
            axis = self._twist if self.mode == "orbit" else self._wheel
            d = axis.update(aux)
            if d:
                extra = [{"t": "roll", "a": d}]
            if axis is self._twist and axis.active:
                # rolling: the small shift that comes with a twist must not orbit as well
                self._s = self._prev = val
                return extra
        return self._move(val) + extra

    def _squeeze(self, hands):
        """True once when a fist was closed twice, quickly and in place."""
        fist = next((c for g, c in hands if g == "fist"), None)
        now = self._now
        if fist is not None:
            if self._sq_on is None:
                self._sq_second = (self._sq_end is not None
                                   and self.SQUEEZE_GAP[0] <= now - self._sq_end <= self.SQUEEZE_GAP[1]
                                   and _d(fist, self._sq_end_at) < self.SQUEEZE_STILL)
                self._sq_on, self._sq_from = now, fist
            self._sq_at = fist
            return False
        if self._sq_on is None:
            return False
        short = (self.SQUEEZE[0] <= now - self._sq_on <= self.SQUEEZE[1]
                 and _d(self._sq_at, self._sq_from) < self.SQUEEZE_STILL)
        self._sq_on = None
        if short and self._sq_second:
            self._sq_end = None
            return True
        self._sq_end, self._sq_end_at = (now, self._sq_at) if short else (None, None)
        return False

    def _view(self, hands, hands_pts):
        """Raised fingers held still, while nothing else is going on, ask once for a standard view."""
        pose, at = None, None
        calm = self.mode == "idle" and bool(hands) and all(g == "open" for g, _ in hands)
        if not calm:
            self._idle_since = None
        elif self._idle_since is None:
            self._idle_since = self._now
        if calm:
            shown = [(pose_of(pts), c) for (_, c), pts in zip(hands, hands_pts)]
            palms = [c for p, c in shown if p == "palm"]
            shown = [x for x in shown if x[0] and x[0] != "palm"]
            if len(shown) == 1 and not palms:
                pose, at = shown[0]
            elif len(palms) == 2 and not shown and self._now - self._idle_since >= self.PALMS_AFTER:
                a, b = palms
                pose, at = "fit", ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
        if calm and pose is None and self._pose and self._pose_miss < 2:
            self._pose_miss += 1   # one or two unclear frames do not start the count again
            return []
        self._pose_miss = 0
        if pose is None or pose != self._pose or _d(at, self._pose_at) > self.POSE_STILL:
            # a view is given once per pose: moving the same raised fingers does not repeat it
            self._pose_done = self._pose_done and pose is not None and pose == self._pose
            self._pose, self._pose_at, self._pose_t, self._pose_n = pose, at, self._now, 0
            return []
        self._pose_n += 1
        if self._pose_done or self._pose_n < 3 or self._now - self._pose_t < self.view_hold:
            return []
        self._pose_done = True
        return [{"t": "view", "v": pose}]

    def _reset_axes(self):
        for axis in (self._twist, self._wheel, self._push):
            axis.reset(None)

    @property
    def label(self):
        """What is happening now, for the preview."""
        if self.mode == "orbit" and self._twist.active:
            return "ROLL"
        if self.mode == "idle" and self._pose and (self._pose_done or self._pose_n > 1):
            part = min(99, int(100 * (self._now - self._pose_t) / self.view_hold))
            return self._pose.upper() + ("" if self._pose_done else " %d%%" % part)
        text = {"idle": "-", "orbit": "ORBIT", "pan": "PAN", "zoom": "ZOOM", "lift": "ZOOM"}[self.mode]
        if self.mode == "pan" and self._push.active:
            text += " + ZOOM"
        if self.mode == "zoom" and self._wheel.active:
            text += " + ROLL"
        return text

    def _move(self, val):
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
        if self.mode == "lift":   # hand up: zoom in
            return [{"t": "zoom", "k2": math.exp(self.LIFT_GAIN * dy)}] if dy else []
        return [{"t": self.mode, "dx": dx, "dy": -dy}]

    def _switch(self, new, val):
        self._target, self._count, self._mismatch = None, 0, 0
        if self.mode == "zoom" and new != "zoom":
            # a zoom ends one hand at a time; do not let the other hand orbit on the way out
            self._latch, self._calm, new, val = True, 0, "idle", None
        if new == self.mode:
            return []
        self.mode, self._lost_at = new, None
        self._reset_axes()
        self._raw = self._s = self._prev = val
        return [{"t": "mode", "m": "zoom" if new == "lift" else new}]


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


def bind_control(port, wait=15.0, waiting=None):
    """Bind the stop port. A tracker left over from an earlier start is told to quit first;
    `waiting()` is called about once a second meanwhile and may return True to give up."""
    deadline = time.monotonic() + wait
    while True:
        ctl = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            ctl.bind(("127.0.0.1", port))
            ctl.setblocking(False)
            return ctl
        except OSError:
            ctl.close()
        if time.monotonic() >= deadline or (waiting and waiting()):
            return None
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.sendto(b"quit", ("127.0.0.1", port))
        except OSError:
            pass
        probe.close()
        time.sleep(0.7)


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
    subprocess.run(["/usr/bin/curl", "-fsSL", "--connect-timeout", "20", "--max-time", "600",
                    "-o", tmp, MODEL_URL], check=True)
    os.replace(tmp, MODEL_PATH)
    if not model_ok():
        os.remove(MODEL_PATH)
        raise RuntimeError("the downloaded hand landmark model does not match its checksum")


def list_cameras():
    """[(name, unique_id)] in OpenCV's index order (AVFoundation devices sorted by unique ID).
    None when the list could not be read, [] when there is no camera."""
    import subprocess
    try:
        raw = subprocess.run(["/usr/sbin/system_profiler", "-json", "SPCameraDataType"],
                             capture_output=True, text=True, timeout=20).stdout
        cams = [(c.get("_name", "?"), c.get("spcamera_unique-id", "")) for c in json.loads(raw)["SPCameraDataType"]]
    except Exception:
        return None
    return sorted(cams, key=lambda c: c[1])


def pick_camera(index, name, cams):
    """Camera index: explicit name match, explicit index, else the Mac's built-in camera."""
    cams = cams or []
    for i, (n, _) in enumerate(cams):
        out("  camera %d: %s" % (i, n))
    if name:
        for i, (n, _) in enumerate(cams):
            if name.lower() in n.lower():
                return i
        out("Camera '%s' not found, using the built-in one." % name)
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
    ap.add_argument("--token-file", default="", help="file with the session token written by the FreeCAD addon")
    ap.add_argument("--embed", action="store_true", help="send the preview to FreeCAD instead of opening a window")
    ap.add_argument("--two-hands", action="store_true", help="track two hands: adds the two-hand gestures")
    ap.add_argument("--no-preview", action="store_true", help="no preview window")
    ap.add_argument("--standalone", action="store_true", help="keep running without FreeCAD listening (for testing)")
    ap.add_argument("--pinch", type=float, default=0.30, help="pinch threshold (smaller = stricter)")
    ap.add_argument("--fist", type=float, default=0.90, help="fist threshold (smaller = tighter fist needed)")
    ap.add_argument("--smooth", type=float, default=0.5, help="smoothing 0.05..1 (smaller = smoother)")
    ap.add_argument("--confidence", type=float, default=0.4, help="hand detection confidence (smaller = loses the hand less, more false hands)")
    ap.add_argument("--width", type=int, default=960, help="width of the picture that is analysed")
    ap.add_argument("--view-hold", type=float, default=0.3, help="seconds raised fingers must be held still")
    args = ap.parse_args()

    token = ""
    if args.token_file:   # a file, not an argument: the process list is readable by every user
        try:
            with open(os.path.join(HERE, args.token_file)) as f:
                token = f.read().strip()
        except OSError as e:
            sys.exit("cannot read the session token (%s). Switch HandNav on in FreeCAD first." % e)
    link = Link(args.port, token, args.standalone)

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
        if link.gone and not stop["why"]:
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

    # The stop port comes before the slow steps and the camera, so a stop sent meanwhile is not
    # lost and a second tracker never reaches the camera.
    def still_waiting():
        link.packet(t="status", msg="Waiting for the previous tracker to stop…")
        return bool(link.gone or stop["why"])

    ctl = bind_control(args.port + 1, waiting=still_waiting)
    if ctl is None:
        if link.gone or stop["why"]:
            finish(stop["why"] or "FreeCAD is not listening")
        finish("UDP port %d is still in use, by an earlier tracker or another program. Try again, "
               "or set a different Port in the HandNav parameters" % (args.port + 1), ok=False)

    # Imports, download and camera opening block for seconds: this thread keeps the panel informed
    # and obeys a stop meanwhile. Nothing needs cleaning up yet; the system releases the camera.
    starting = threading.Event()
    starting.set()

    def startup_watch():
        while starting.is_set():
            if should_stop() and starting.is_set():
                link.packet(t="bye", msg=stop["why"], ok=True)
                out("HandNav tracker stopped: " + stop["why"])
                os._exit(0)
            time.sleep(0.25)

    threading.Thread(target=startup_watch, daemon=True).start()
    try:
        run(args, link, stop, should_stop, say, finish, starting)
    except SystemExit:
        raise
    except Exception as e:
        import traceback
        traceback.print_exc()
        finish("unexpected error: %s: %s" % (type(e).__name__, e), ok=False)


class Camera:
    """Reads frames in the background, so that analysing one and waiting for the next overlap."""

    def __init__(self, cap):
        self._cap, self._frame, self._seq, self._stop = cap, None, 0, False
        self._cond = threading.Condition()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self):
        while not self._stop:
            ok, frame = self._cap.read()
            if not ok:
                time.sleep(0.02)
                continue
            with self._cond:
                self._frame, self._seq = frame, self._seq + 1
                self._cond.notify()

    def next(self, seen, timeout=0.1):
        """(sequence number, newest frame) newer than `seen`, or (seen, None) after the timeout."""
        with self._cond:
            if self._cond.wait_for(lambda: self._seq > seen, timeout):
                return self._seq, self._frame
        return seen, None

    def release(self):
        self._stop = True
        self._thread.join(1.0)
        self._cap.release()


def run(args, link, stop, should_stop, say, finish, starting):
    """Load everything, open the camera and track until told to stop."""
    say("Loading the hand tracker…")
    import cv2
    import mediapipe as mp
    from mediapipe.tasks import python as mp_python
    from mediapipe.tasks.python import vision

    try:
        ensure_model(say)
        landmarker = vision.HandLandmarker.create_from_options(vision.HandLandmarkerOptions(
            base_options=mp_python.BaseOptions(model_asset_path=MODEL_PATH),
            running_mode=vision.RunningMode.VIDEO, # one hand: once found it is only followed, without searching the whole picture every frame,
            # which is what takes the time
            num_hands=2 if args.two_hands else 1,
            min_hand_detection_confidence=args.confidence, min_hand_presence_confidence=args.confidence,
            min_tracking_confidence=args.confidence))
    except Exception as e:
        finish("could not load the hand landmark model (%s)" % e, ok=False)
    if should_stop():
        finish(stop["why"])

    cams = list_cameras()
    if cams == []:
        finish("no camera found. Connect a camera and switch HandNav off and on", ok=False)
    cam_index = pick_camera(args.camera, args.camera_name, cams)
    if cams and cam_index >= len(cams):
        finish("there is no camera %d (found %d: %s). Set Camera or CameraName in the HandNav parameters."
               % (cam_index, len(cams), ", ".join(n for n, _ in cams)), ok=False)
    cam_name = cams[cam_index][0] if cams else "camera %d" % cam_index
    say("Opening %s…" % cam_name)

    # The first open only triggers the macOS permission prompt and fails, so keep retrying.
    cap, ok, deadline, told = None, False, time.monotonic() + 90, False
    while not ok:
        if should_stop():
            finish(stop["why"])
        cap = cv2.VideoCapture(cam_index, cv2.CAP_AVFOUNDATION)
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
        ok, _ = cap.read()
        if ok:
            break
        cap.release()
        if time.monotonic() > deadline:
            finish("could not open %s. Allow HandNav Tracker (or Terminal, if you run it there) in "
                   "System Settings → Privacy & Security → Camera, or pick another camera." % cam_name, ok=False)
        if not told:
            told = True
            say("Waiting for camera access: click Allow in the macOS prompt, or enable HandNav Tracker "
                "in System Settings → Privacy & Security → Camera…")
        for _ in range(6):
            if should_stop():
                finish(stop["why"])
            time.sleep(0.25)

    starting.clear()   # from here on the capture loop itself checks for a stop
    g = Gestures(pinch_on=args.pinch, fist_on=args.fist, smooth=args.smooth, view_hold=args.view_hold)
    colors = {"idle": (160, 160, 160), "orbit": (80, 200, 255), "pan": (120, 255, 120), "zoom": (255, 160, 80), "lift": (255, 160, 80)}
    picker, had = HandPicker(), False
    stats = {"t": time.monotonic(), "frames": 0, "hand": 0, "returns": 0, "rejected": 0}
    draw = args.embed or not args.no_preview
    window = draw and not args.embed
    say("Running on %s" % cam_name)
    out("Pinch: orbit, twist: roll, fist: pan, thumb + middle finger up/down: zoom, raised fingers: views. Stop: %s."
        % ("q in the preview window, or Ctrl+C here" if window else "the HandNav button in FreeCAD, or Ctrl+C here"))
    n, ts, t0, bad_since = 0, -1, time.monotonic(), None
    cap, seq, misfits = Camera(cap), 0, 0
    while not should_stop():
        seq, frame = cap.next(seq)
        if frame is None:
            now = time.monotonic()
            bad_since = bad_since or now
            if now - bad_since > 10:
                finish("%s stopped delivering frames" % cam_name, ok=False, cap=cap)
            continue
        bad_since = None
        h, w = frame.shape[:2]
        if w > args.width:
            frame = cv2.resize(frame, (args.width, int(h * args.width / w)), interpolation=cv2.INTER_LINEAR)
            h, w = frame.shape[:2]
        frame = cv2.flip(frame, 1)
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        ts = max(ts + 1, int((time.monotonic() - t0) * 1000))   # must be strictly increasing
        res = landmarker.detect_for_video(mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb), ts)
        now = time.monotonic()
        found = [([(p.x * w / h, p.y) for p in lms], side[0].score)
                 for lms, side in zip(res.hand_landmarks, res.handedness)]
        hands = [pts for pts, score in found if plausible(pts, score)]
        misfits = misfits + 1 if found and not hands else 0
        if misfits >= 5:
            # something that is no hand (a face, say) is being followed: an empty picture makes the
            # tracker drop it and search again
            misfits, ts = 0, ts + 1
            landmarker.detect_for_video(mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb * 0), ts)
        if not args.two_hands:
            hands = picker.pick(hands, now, g.mode != "idle")
        stats["frames"] += 1
        stats["hand"] += bool(hands)
        stats["rejected"] += len(found) - sum(plausible(*f) for f in found)
        if hands and not had:
            stats["returns"] += 1
        had = bool(hands)
        if now - stats["t"] >= 10:
            out("%.0f fps at %dx%d, hand in %d%% of frames, found again %d times, %d false detections"
                % (stats["frames"] / (now - stats["t"]), w, h, 100 * stats["hand"] // stats["frames"],
                   stats["returns"], stats["rejected"]))
            stats = dict.fromkeys(stats, 0)
            stats["t"] = now
        for pkt in g.update(hands, now):
            link.packet(**pkt)
        if draw:
            unit = w / 640
            for pts in hands:   # only the hands that count
                for x, y in pts:
                    cv2.circle(frame, (int(x * h), int(y * h)), int(3 * unit), colors[g.mode], -1)
            cv2.putText(frame, g.label, (int(12 * unit), int(34 * unit)), cv2.FONT_HERSHEY_SIMPLEX, unit,
                        colors[g.mode], int(2 * unit))
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
