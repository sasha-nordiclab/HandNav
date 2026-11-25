#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""HandNav tracker: webcam -> hand gestures -> UDP packets for the FreeCAD addon.

Gestures
  pinch (thumb + index) and move ...... orbit
  pinch and twist the hand ............ roll (rotate in the screen plane)
  fist and move ....................... pan
  fist or pinch to / from the camera .. zoom
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

Thresholds can be changed while running with "set TOKEN {json}" on PORT+1 (see Gestures.tune).
The tracker stops when it receives "quit TOKEN" on PORT+1, when nobody listens on PORT any more
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
FACE_PATH = os.path.join(HERE, "face_detector.tflite")
_STORE = "https://storage.googleapis.com/mediapipe-models/"
MODELS = {   # file, address, SHA-256, name
    "hand": (MODEL_PATH, _STORE + "hand_landmarker/hand_landmarker/float16/1/hand_landmarker.task",
             "fbc2a30080c3c557093b5ddfc334698132eb341044ccee322ccf8bcf3607cde1", "hand landmark (8 MB)"),
    "face": (FACE_PATH, _STORE + "face_detector/blaze_face_short_range/float16/1/blaze_face_short_range.tflite",
             "b4578f35940bf5a1a655214a1cce5cab13eba73c1297cd78e1a04c2380b0152f", "face detection (0.2 MB)"),
}

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


def palm_size(pts):
    """Palm size as a fraction of the picture height."""
    return max(_d(pts[0], pts[9]), _d(pts[5], pts[17]))


def big_enough(pts, min_size, busy):
    """A far-away (small) hand does not count; a running gesture may shrink a little further."""
    return palm_size(pts) >= min_size * (0.75 if busy else 1.0)


class FaceGuard:
    """Ignores a hand that touches the face (scratching, leaning on it, adjusting glasses).

    A hand on the face is over the face in the picture and about as far from the camera as the
    face, so its palm is clearly smaller than the face. A hand held out in front of the face
    towards the camera looks larger and still counts."""

    KEEP = 1.0          # seconds a face is remembered when it is not detected any more ...
    KEEP_COVERED = 15.0   # ... and while a hand is over it, which hides it from the detector
    ENTER, LEAVE = 0.3, 0.3   # seconds before a running gesture is stopped / a hand counts again

    def __init__(self, ratio=0.85):
        self.ratio = ratio
        self._faces, self._seen, self._covered = [], -1e9, -1e9
        self._since, self._clear, self.blocking = None, None, False
        self.measured = None   # palm size / face height of the last hand over a face

    def faces(self, boxes, now):
        """boxes: detected faces (x, y, w, h), or [] when none was found."""
        if boxes:
            self._faces, self._seen = boxes, now

    def face(self, now):
        """The face found last, while it is remembered."""
        return self._faces[0] if self._faces and now - self._seen <= self.KEEP_COVERED else None

    def touching(self, pts, now):
        if now - max(self._seen, min(self._covered, self._seen + self.KEEP_COVERED)) > self.KEEP:
            self._faces = []
        size, over = palm_size(pts), False
        limit = self.ratio * (1.15 if self.blocking else 1.0)
        touch = False
        for x, y, w, h in self._faces:
            # with hair, ears, chin and neck
            inside = sum(x - 0.3 * w <= px <= x + 1.3 * w and y - 0.45 * h <= py <= y + 1.4 * h for px, py in pts)
            if inside >= 3:
                over, self.measured = True, size / h
                touch = touch or size < limit * h
        if over:
            self._covered = now
        return touch

    def filter(self, hands, now, busy):
        """The hands that count. busy: a gesture is in progress."""
        if len(hands) != 1:
            return [pts for pts in hands if not self.touching(pts, now)]
        if self.touching(hands[0], now):
            self._clear = None
            self._since = now if self._since is None else self._since
            # nothing starts at the face; a gesture that merely passes over it goes on for a moment
            if not busy or now - self._since >= self.ENTER:
                self.blocking = True
        else:
            self._since = None
            self._clear = now if self._clear is None else self._clear
            if now - self._clear >= self.LEAVE:
                self.blocking = False
        return [] if self.blocking else hands


class FaceSearch:
    """Where to look for a face next. The detector works on a small square picture, so in the whole
    frame it only finds large faces; in parts of it, it finds those of a normal sitting distance."""

    def __init__(self):
        self._step = 0

    @staticmethod
    def _square(cx, cy, side, w, h):
        side = int(min(max(side, 0.35 * h), h))
        x = int(min(max(cx - side / 2, 0), w - side))
        y = int(min(max(cy - side / 2, 0), h - side))
        return x, y, side

    def regions(self, w, h, face=None, hand=None):
        """One or two squares (x, y, side) in pixels. face: (x, y, w, h), hand: (x, y), both in
        picture heights."""
        out = []
        if face is not None:
            out.append(self._square((face[0] + face[2] / 2) * h, (face[1] + face[3] / 2) * h, 2.6 * face[3] * h, w, h))
        elif hand is not None:
            out.append(self._square(hand[0] * h, hand[1] * h, 0.7 * h, w, h))
        grid = [(x, 0.5 * h, h) for x in (w / 2, 0.5 * h, w - 0.5 * h)]
        grid += [(x, y, 0.7 * h) for y in (0.35 * h, 0.65 * h)
                 for x in (w / 2, 0.35 * h, w - 0.35 * h, 0.35 * w, 0.65 * w)]
        out.append(self._square(*grid[self._step % len(grid)], w, h))
        self._step += 1
        return out


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


class Speed:
    """Smoothed speed, and the acceleration that follows from it: slow is finer, fast goes further."""

    LOW, HIGH = 0.5, 3.0

    def __init__(self, usual):
        self.usual = usual      # at this speed nothing changes
        self.value = None

    def update(self, distance, dt):
        v = distance / dt
        self.value = v if self.value is None else self.value + (1 - math.exp(-dt / 0.06)) * (v - self.value)

    def factor(self, accel):
        if accel <= 0 or not self.value:
            return 1.0 if accel <= 0 else self.LOW
        return min(max((self.value / self.usual) ** accel, self.LOW), self.HIGH)


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
        self.moved = 0.0

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
        self.moved = abs(self.smooth * (self._acc - self._s))   # in this frame
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
                 smooth=0.5, deadzone=0.002, debounce=2, max_jump=0.2, view_hold=0.3,
                 roll=True, push=True, squeeze=True, pinch_push=True, accel=0.0):
        self.accel = min(max(accel, 0.0), 1.0)
        self._speed = Speed(usual=0.35)       # picture heights per second
        self._turn_speed = Speed(usual=1.0)   # radians per second
        self._dt = 1 / 30
        self.use_pinch_push = pinch_push
        self.use_roll, self.use_push, self.use_squeeze = roll, push, squeeze
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
    def tune(self, values):
        """Change thresholds while running; unknown names and unusable values are ignored."""
        def num(name, lo, hi):
            v = values.get(name)
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
                return None
            return min(max(float(v), lo), hi)

        pinch, fist = num("pinch", 0.05, 0.8), num("fist", 0.4, 1.4)
        if pinch is not None:
            self.pinch_on, self.pinch_off = pinch, pinch * 1.5
        if fist is not None:
            self.fist_on, self.fist_off = fist, fist * 1.15
        smooth, hold, accel = num("smooth", 0.05, 1.0), num("view_hold", 0.1, 3.0), num("accel", 0.0, 1.0)
        self.accel = self.accel if accel is None else accel
        self.smooth = self.smooth if smooth is None else smooth
        self.view_hold = self.view_hold if hold is None else hold
        twist, push = num("twist_start", 1.0, 90.0), num("push_start", 1.0, 100.0)
        if twist is not None:   # degrees
            self._twist.engage = self._wheel.engage = math.radians(twist)
        if push is not None:    # per cent of the apparent size
            self._push.engage = math.log(1 + push / 100)

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
        before, self._now = self._now, self._now + 1 / 30 if now is None else now
        self._dt = min(max(self._now - before, 1 / 120), 0.2)
        hands = self.classify_hands(hands_pts)
        view = self._view(hands, hands_pts)
        if view:
            return view
        return self._motion(hands, hands_pts) + ([{"t": "view", "v": "fit"}] if self.use_squeeze and self._squeeze(hands) else [])

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
            want, val, aux = "orbit", pinch[0][WRIST], (twist_of(pinch[0]), size_of(pinch[0]))
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
        elif self.mode == "orbit":
            twist, size = aux
            d = self._roll(self._twist, twist) if self.use_roll else 0.0
            if d:
                extra = [{"t": "roll", "a": d}]
            if self._twist.active:
                self._push.reset(size)     # turning the hand changes its apparent size
            elif self.use_pinch_push:
                d = self._push.update(size)
                if d:
                    extra = [{"t": "depth", "d": d}]
            if self._twist.active or self._push.active:
                # the small shift that comes with a twist or a push must not orbit as well
                self._s = self._prev = val
                return extra
        elif not (self.use_push if self.mode == "pan" else self.use_roll):
            pass
        elif self.mode == "pan":
            d = self._push.update(aux)
            if d:
                extra = [{"t": "depth", "d": d}]
        else:
            d = self._roll(self._wheel, aux)
            if d:
                extra = [{"t": "roll", "a": d}]
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

    def _roll(self, axis, angle):
        d = axis.update(angle)
        self._turn_speed.update(axis.moved, self._dt)
        return d * self._turn_speed.factor(self.accel)

    def _reset_axes(self):
        self._speed.value = self._turn_speed.value = None
        for axis in (self._twist, self._wheel, self._push):
            axis.reset(None)

    @property
    def label(self):
        """What is happening now, for the preview."""
        if self.mode == "orbit" and self._twist.active:
            return "ROLL"
        if self.mode == "orbit" and self._push.active:
            return "ZOOM"
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
        was = self._s
        self._s = (self._s[0] + a * (val[0] - self._s[0]), self._s[1] + a * (val[1] - self._s[1]))
        self._speed.update(_d(self._s, was), self._dt)
        dx, dy = self._s[0] - self._prev[0], self._s[1] - self._prev[1]
        if math.hypot(dx, dy) < self.deadzone:
            return []
        prev, self._prev = self._prev, self._s
        if self.mode == "zoom":
            if prev[0] < 1e-3 or self._s[0] < 1e-3:
                return []
            return [{"t": "zoom", "k2": prev[0] / self._s[0]}]   # two hands: 1:1, without acceleration
        f = self._speed.factor(self.accel)
        dx, dy = dx * f, dy * f
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


def quit_message(token):
    return ("quit " + token).strip().encode()


def parse_set(data, token):
    """The values of a "set TOKEN {json}" datagram, or {} for anything else."""
    try:
        word, got, body = data.decode().split(" ", 2)
        values = json.loads(body)
    except ValueError:
        return {}
    return values if word == "set" and token and got == token and isinstance(values, dict) else {}


def bind_control(port, wait=15.0, waiting=None, token=""):  # nosec B107 - none when standalone
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
            probe.sendto(quit_message(token), ("127.0.0.1", port))
        except OSError:
            pass
        probe.close()
        time.sleep(0.7)


def model_ok(path, sha256):
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest() == sha256
    except OSError:
        return False


def ensure_model(say=print, which="hand"):
    path, url, sha256, what = MODELS[which]
    if model_ok(path, sha256):
        return
    import subprocess  # nosec B404
    say("Downloading the %s model from Google…" % what)
    tmp = path + ".part"
    # curl uses the system certificates; python.org builds often have none
    subprocess.run(["/usr/bin/curl", "-fsSL", "--connect-timeout", "20", "--max-time", "600",
                    "-o", tmp, url], check=True)  # nosec B603 - fixed program and address
    os.replace(tmp, path)
    if not model_ok(path, sha256):
        os.remove(path)
        raise RuntimeError("the downloaded %s model does not match its checksum" % what)


def list_cameras():
    """[(name, unique_id)] in OpenCV's index order (AVFoundation devices sorted by unique ID).
    None when the list could not be read, [] when there is no camera."""
    import subprocess  # nosec B404
    try:
        raw = subprocess.run(["/usr/sbin/system_profiler", "-json", "SPCameraDataType"],
                             capture_output=True, text=True, timeout=20).stdout  # nosec B603
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
    ap.add_argument("--fetch-model", action="store_true", help="only download the hand landmark model")
    ap.add_argument("--embed", action="store_true", help="send the preview to FreeCAD instead of opening a window")
    ap.add_argument("--two-hands", action="store_true", help="track two hands: adds the two-hand gestures")
    ap.add_argument("--no-preview", action="store_true", help="no preview window")
    ap.add_argument("--standalone", action="store_true", help="keep running without FreeCAD listening (for testing)")
    ap.add_argument("--pinch", type=float, default=0.30, help="pinch threshold (smaller = stricter)")
    ap.add_argument("--fist", type=float, default=0.90, help="fist threshold (smaller = tighter fist needed)")
    ap.add_argument("--smooth", type=float, default=0.5, help="smoothing 0.05..1 (smaller = smoother)")
    ap.add_argument("--no-roll", action="store_true", help="no roll by twisting")
    ap.add_argument("--no-push", action="store_true", help="no zoom by pushing or pulling the fist")
    ap.add_argument("--no-pinch-push", action="store_true", help="no zoom by pushing or pulling the pinch")
    ap.add_argument("--no-squeeze", action="store_true", help="no fit all by squeezing twice")
    ap.add_argument("--accel", type=float, default=0.5, help="acceleration 0..1: fast movements go further, slow ones are finer (0 = off)")
    ap.add_argument("--twist-start", type=float, default=9.0, help="degrees of twist before roll starts")
    ap.add_argument("--push-start", type=float, default=10.0, help="per cent of size change before push zoom starts")
    ap.add_argument("--no-face-guard", action="store_true", help="do not ignore a hand that touches the face")
    ap.add_argument("--face-ratio", type=float, default=0.85,
                    help="a hand over the face is ignored while its palm is smaller than this part of the face height")
    ap.add_argument("--min-hand", type=float, default=0.12,
                    help="ignore hands whose palm is smaller than this part of the picture height (0 = off)")
    ap.add_argument("--confidence", type=float, default=0.4, help="hand detection confidence (smaller = loses the hand less, more false hands)")
    ap.add_argument("--width", type=int, default=960, help="width of the picture that is analysed")
    ap.add_argument("--view-hold", type=float, default=0.3, help="seconds raised fingers must be held still")
    args = ap.parse_args()
    if args.fetch_model:   # used by the installer
        ensure_model()
        ensure_model(which="face")
        return

    token = ""  # nosec B105 - none when standalone
    if args.token_file:   # a file, not an argument: the process list is readable by every user
        try:
            with open(os.path.join(HERE, args.token_file)) as f:
                token = f.read().strip()
        except OSError as e:
            sys.exit("cannot read the session token (%s). Switch HandNav on in FreeCAD first." % e)
    link = Link(args.port, token, args.standalone)

    stop = {"why": None}
    tuned = {}   # thresholds received while running, not yet applied

    def on_signal(signum, _frame):
        stop["why"] = "stopped from the terminal"

    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, on_signal)

    def should_stop():
        if stop["why"]:
            return stop["why"]
        try:
            while True:
                data = ctl.recv(4096)
                if data.strip() == quit_message(token):
                    stop["why"] = "switched off"
                    break
                values = parse_set(data, token)
                if values:
                    tuned.update(values)
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

    ctl = bind_control(args.port + 1, waiting=still_waiting, token=token)
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
        run(args, link, stop, should_stop, say, finish, starting, tuned)
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


def run(args, link, stop, should_stop, say, finish, starting, tuned):
    """Load everything, open the camera and track until told to stop."""
    say("Loading the hand tracker…")
    import cv2
    import mediapipe as mp
    import numpy as np
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
    face_detector = None
    if not args.no_face_guard:
        try:
            ensure_model(say, "face")
            face_detector = vision.FaceDetector.create_from_options(vision.FaceDetectorOptions(
                base_options=mp_python.BaseOptions(model_asset_path=FACE_PATH),
                running_mode=vision.RunningMode.IMAGE, min_detection_confidence=0.6))
        except Exception as e:   # the rest works without it
            out("Face detection is not available (%s)" % e)
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
    g = Gestures(pinch_on=args.pinch, fist_on=args.fist, smooth=args.smooth, view_hold=args.view_hold,
                 roll=not args.no_roll, push=not args.no_push, squeeze=not args.no_squeeze,
                 pinch_push=not args.no_pinch_push, accel=args.accel)
    colors = {"idle": (160, 160, 160), "orbit": (80, 200, 255), "pan": (120, 255, 120), "zoom": (255, 160, 80), "lift": (255, 160, 80)}
    g.tune({"twist_start": args.twist_start, "push_start": args.push_start})
    guard, frames_to_face = (FaceGuard(args.face_ratio) if face_detector else None), 0
    search, picker, had = FaceSearch(), HandPicker(), False
    stats = {"t": time.monotonic(), "frames": 0, "hand": 0, "returns": 0, "rejected": 0,
             "small": 0, "face": 0, "size_sum": 0.0, "size_n": 0}
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
        if tuned:
            values = dict(tuned)
            tuned.clear()
            g.tune(values)
            ratio = values.get("face_ratio")
            if guard is not None and isinstance(ratio, (int, float)) and 0.1 <= ratio <= 3:
                guard.ratio = ratio
            size = values.get("min_hand")
            if isinstance(size, (int, float)) and 0 <= size <= 1:
                args.min_hand = size
        now = time.monotonic()
        found = [([(p.x * w / h, p.y) for p in lms], side[0].score)
                 for lms, side in zip(res.hand_landmarks, res.handedness)]
        hands = [pts for pts, score in found if plausible(pts, score)]
        shown = hands
        hands = [pts for pts in shown if big_enough(pts, args.min_hand, g.mode != "idle")]
        small = shown if shown and not hands else []
        if guard is not None:
            frames_to_face -= 1
            if frames_to_face <= 0:   # a face moves slowly: every few frames is enough
                frames_to_face = 4 if hands else 12
                for x, y, side in search.regions(w, h, guard.face(now), classify(hands[0])[1] if hands else None):
                    part = np.ascontiguousarray(rgb[y:y + side, x:x + side])
                    r = face_detector.detect(mp.Image(image_format=mp.ImageFormat.SRGB, data=part))
                    if r.detections:
                        guard.faces([((x + d.bounding_box.origin_x) / h, (y + d.bounding_box.origin_y) / h,
                                      d.bounding_box.width / h, d.bounding_box.height / h)
                                     for d in r.detections], now)
                        break
        at_face = []
        if guard is not None and hands:
            at_face, hands = hands, guard.filter(hands, now, g.mode != "idle")
            at_face = [pts for pts in at_face if pts not in hands]
        misfits = misfits + 1 if found and not hands and not at_face else 0
        if misfits >= (15 if small else 5):
            # something that is no hand (a face, say) or too small is being followed: an empty picture makes the
            # tracker drop it and search again
            misfits, ts = 0, ts + 1
            landmarker.detect_for_video(mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb * 0), ts)
        if not args.two_hands:
            hands = picker.pick(hands, now, g.mode != "idle")
        stats["frames"] += 1
        stats["hand"] += bool(hands)
        stats["small"] += bool(small)
        stats["face"] += bool(at_face)
        if shown:
            size = palm_size(shown[0])
            stats["size_sum"] += size
            stats["size_n"] += 1
        stats["rejected"] += len(found) - sum(plausible(*f) for f in found)
        if hands and not had:
            stats["returns"] += 1
        had = bool(hands)
        if now - stats["t"] >= 10:
            out("%.0f fps at %dx%d, hand in %d%% of frames, found again %d times, %d false detections, "
                "too small in %d%%, at the face in %d%%, mean palm size %.2f, face seen: %s, last palm / face over it: %s"
                % (stats["frames"] / (now - stats["t"]), w, h, 100 * stats["hand"] // stats["frames"],
                   stats["returns"], stats["rejected"],
                   100 * stats["small"] // stats["frames"],
                   100 * stats["face"] // stats["frames"], stats["size_sum"] / max(stats["size_n"], 1),
                   "yes" if guard is not None and now - guard._seen < 2 else "no",
                   "%.2f" % guard.measured if guard is not None and guard.measured else "-"))
            stats = dict.fromkeys(stats, 0)
            stats["t"] = now
        for pkt in g.update(hands, now):
            link.packet(**pkt)
        if draw:
            unit = w / 640
            for pts in hands:   # only the hands that count
                for x, y in pts:
                    cv2.circle(frame, (int(x * h), int(y * h)), int(3 * unit), colors[g.mode], -1)
            for pts in small:   # seen, but ignored: grey, with its size
                for x, y in pts:
                    cv2.circle(frame, (int(x * h), int(y * h)), int(2 * unit), (120, 120, 120), -1)
            for pts in at_face:   # seen, but ignored: at the face
                for x, y in pts:
                    cv2.circle(frame, (int(x * h), int(y * h)), int(2 * unit), (90, 90, 230), -1)
            label = g.label if not small else "TOO SMALL %.2f < %.2f" % (palm_size(small[0]), args.min_hand)
            if at_face:
                label = "FACE"
            cv2.putText(frame, label, (int(12 * unit), int(34 * unit)), cv2.FONT_HERSHEY_SIMPLEX, unit,
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
