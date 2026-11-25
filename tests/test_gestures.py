# SPDX-License-Identifier: MIT
"""Tracker logic tests. Pure Python: no camera, mediapipe or OpenCV needed.

    python3 -m unittest discover -s tests
"""
import json
import math
import os
import socket
import subprocess
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tracker"))
import hand_tracker as ht  # noqa: E402


def hand(cx, cy, kind):
    """Synthetic 21-point hand, wrist at the bottom, fingers pointing up (image y grows down).

    open            fingers straight, thumb away
    relaxed         fingers half curled, nothing touching
    fist            all four fingertips pulled back onto the palm
    pinch           thumb and index touching, other fingers straight (an OK sign)
    natural_pinch   thumb and index touching, other fingers loosely curled
    gap             thumb and index close but not touching (0.38 x palm size apart)
    """
    p = [(cx, cy)] * 21
    p[0] = (cx, cy + 0.10)
    tip_y = {"fist": cy + 0.02, "relaxed": cy - 0.03, "natural_pinch": cy - 0.02}.get(kind, cy - 0.10)
    for base, dx in {5: -0.03, 9: -0.01, 13: 0.01, 17: 0.03}.items():
        p[base] = (cx + dx, cy)
        p[base + 1] = (cx + dx, cy - 0.04)
        p[base + 2] = (cx + dx, (cy - 0.04 + tip_y) / 2)
        p[base + 3] = (cx + dx, tip_y)
    p[1], p[2], p[3] = (cx - 0.04, cy + 0.06), (cx - 0.06, cy + 0.03), (cx - 0.07, cy)
    p[4] = (cx - 0.08, cy - 0.03)
    if kind in ("pinch", "natural_pinch"):
        p[8] = (cx - 0.06, cy - 0.04)
        p[4] = (cx - 0.065, cy - 0.035)
    elif kind == "gap":
        p[8] = (cx - 0.042, cy - 0.03)
    return p


def feed(g, frames):
    out = []
    for hands in frames:
        out += g.update(hands)
    return out


def kinds(packets):
    return [p["t"] for p in packets]


class ClassifyTest(unittest.TestCase):
    def test_poses(self):
        expected = {"open": "open", "pinch": "pinch", "fist": "fist",
                    "relaxed": "open",            # a half-curled hand is not a fist
                    "natural_pinch": "pinch",     # a pinch with loosely curled fingers is not a fist
                    "gap": "open"}
        for kind, want in expected.items():
            self.assertEqual(ht.classify(hand(0.5, 0.5, kind))[0], want, kind)

    def test_gap_is_a_pinch_only_with_the_loose_threshold(self):
        self.assertEqual(ht.classify(hand(0.5, 0.5, "gap"), 0.45)[0], "pinch")

    def test_center_follows_hand(self):
        (x1, y1), (x2, y2) = (ht.classify(hand(x, 0.5, "open"))[1] for x in (0.3, 0.6))
        self.assertAlmostEqual(x2 - x1, 0.3)
        self.assertAlmostEqual(y1, y2)


class GesturesTest(unittest.TestCase):
    def test_orbit_right_and_up(self):
        g = ht.Gestures()
        pk = feed(g, [[hand(0.3 + 0.01 * i, 0.6 - 0.005 * i, "pinch")] for i in range(30)])
        self.assertEqual(pk[0], {"t": "mode", "m": "orbit"})
        orbit = [p for p in pk if p["t"] == "orbit"]
        self.assertGreater(sum(p["dx"] for p in orbit), 0.2)   # hand moved right
        self.assertGreater(sum(p["dy"] for p in orbit), 0.1)   # hand moved up -> positive dy
        self.assertEqual(len(pk), len(orbit) + 1)

    def test_fist_pans(self):
        g = ht.Gestures()
        pk = feed(g, [[hand(0.5 + 0.01 * i, 0.5, "fist")] for i in range(20)])
        self.assertEqual(pk[0], {"t": "mode", "m": "pan"})
        self.assertGreater(sum(p.get("dx", 0) for p in pk), 0.1)

    def test_two_pinches_zoom(self):
        g = ht.Gestures()
        apart = [[hand(0.4 - 0.005 * i, 0.5, "pinch"), hand(0.6 + 0.005 * i, 0.5, "pinch")] for i in range(20)]
        pk = feed(g, apart)
        self.assertEqual(pk[0], {"t": "mode", "m": "zoom"})
        k = 1.0
        for p in pk[1:]:
            k *= p["k2"]
        self.assertLess(k, 0.8)  # hands apart -> factor < 1 -> zoom in

    def test_open_palm_releases(self):
        g = ht.Gestures()
        feed(g, [[hand(0.5, 0.5, "pinch")]] * 5)
        pk = feed(g, [[hand(0.5, 0.5, "open")]] * 5)
        self.assertEqual(pk, [{"t": "mode", "m": "idle"}])
        self.assertEqual(feed(g, [[]] * 5), [])

    def test_single_frame_flicker_is_ignored(self):
        g = ht.Gestures()
        feed(g, [[hand(0.5, 0.5, "pinch")]] * 5)
        pk = feed(g, [[hand(0.5, 0.5, "open")], [hand(0.5, 0.5, "pinch")], [hand(0.5, 0.5, "pinch")]])
        self.assertEqual(pk, [])
        self.assertEqual(g.mode, "orbit")

    def test_debounce_needs_the_same_gesture_twice_in_a_row(self):
        g = ht.Gestures()
        feed(g, [[hand(0.5, 0.5, "pinch")]] * 5)
        feed(g, [[hand(0.5, 0.5, "open")], [hand(0.5, 0.5, "fist")]])   # two different one-frame readings
        self.assertEqual(g.mode, "orbit")
        feed(g, [[hand(0.5, 0.5, "fist")]])
        self.assertEqual(g.mode, "pan")

    def test_noise_that_never_settles_lets_go(self):
        g = ht.Gestures()
        feed(g, [[hand(0.5, 0.5, "pinch")]] * 5)
        feed(g, [[hand(0.5, 0.5, "open")], [hand(0.5, 0.5, "fist")]] * 3)
        self.assertEqual(g.mode, "idle")

    def test_still_hand_sends_nothing(self):
        g = ht.Gestures()
        pk = feed(g, [[hand(0.5, 0.5, "pinch")]] * 20)
        self.assertEqual(pk, [{"t": "mode", "m": "orbit"}])

    def test_regrip_does_not_jump(self):
        g = ht.Gestures()
        feed(g, [[hand(0.2, 0.5, "pinch")]] * 5)
        feed(g, [[hand(0.2, 0.5, "open")]] * 3)
        pk = feed(g, [[hand(0.8, 0.5, "pinch")]] * 5)   # grabbed again far away
        self.assertEqual([p for p in pk if p["t"] == "orbit"], [])

    def test_held_pinch_survives_a_slightly_open_grip(self):
        g = ht.Gestures()
        feed(g, [[hand(0.5, 0.5, "pinch")]] * 5)
        feed(g, [[hand(0.5, 0.5, "gap")]] * 5)   # fingers drift apart a little while holding
        self.assertEqual(g.mode, "orbit")

    def test_idle_second_hand_does_not_turn_orbit_into_zoom(self):
        g = ht.Gestures()
        frames = [[hand(0.3 + 0.005 * i, 0.5, "pinch"), hand(0.8, 0.5, "gap")] for i in range(20)]
        pk = feed(g, frames)
        self.assertEqual(g.mode, "orbit")
        self.assertNotIn("zoom", kinds(pk))

    def test_zoom_does_not_end_in_an_orbit_kick(self):
        g = ht.Gestures()
        feed(g, [[hand(0.4, 0.5, "pinch"), hand(0.6, 0.5, "pinch")]] * 5)
        self.assertEqual(g.mode, "zoom")
        # one hand lets go first while the other, still pinched, keeps moving
        pk = feed(g, [[hand(0.4 - 0.01 * i, 0.5, "pinch"), hand(0.6, 0.5, "open")] for i in range(10)])
        self.assertEqual(pk, [{"t": "mode", "m": "idle"}])
        # only after both hands opened can a new gesture start
        feed(g, [[hand(0.3, 0.5, "open"), hand(0.6, 0.5, "open")]] * 4)
        pk = feed(g, [[hand(0.3 + 0.01 * i, 0.5, "pinch")] for i in range(6)])
        self.assertEqual(pk[0], {"t": "mode", "m": "orbit"})

    def test_zoom_latch_survives_a_lost_frame(self):
        g = ht.Gestures()
        feed(g, [[hand(0.4, 0.5, "pinch"), hand(0.6, 0.5, "pinch")]] * 5)
        held = [hand(0.4, 0.5, "pinch"), hand(0.6, 0.5, "open")]
        pk = feed(g, [held] * 3 + [[]] + [held] * 5)   # the pinched hand is undetected for one frame
        self.assertEqual(pk, [{"t": "mode", "m": "idle"}])

    def test_loose_threshold_does_not_leak_to_a_neighbouring_hand(self):
        g = ht.Gestures()
        feed(g, [[hand(0.4, 0.5, "pinch")]] * 5)
        feed(g, [[hand(0.4, 0.5, "pinch"), hand(0.58, 0.5, "gap")]] * 5)
        self.assertEqual(g.mode, "orbit")

    def test_held_pinch_survives_a_lost_frame(self):
        g = ht.Gestures()
        feed(g, [[hand(0.5, 0.5, "pinch")]] * 5)
        feed(g, [[]] + [[hand(0.5, 0.5, "gap")]] * 5)
        self.assertEqual(g.mode, "orbit")

    def test_pan_follows_the_same_hand_when_the_order_swaps(self):
        g = ht.Gestures()
        a = lambda i: hand(0.2 + 0.005 * i, 0.5, "fist")   # noqa: E731  the moving hand
        b = hand(0.8, 0.5, "fist")                          # a resting second fist
        pk = feed(g, [[a(i), b] for i in range(6)] + [[b, a(i)] for i in range(6, 12)])
        dxs = [p["dx"] for p in pk if p["t"] == "pan"]
        self.assertTrue(dxs)
        self.assertLess(max(abs(d) for d in dxs), 0.02)

    def test_tracking_glitch_is_reanchored(self):
        g = ht.Gestures()
        feed(g, [[hand(0.2, 0.5, "pinch")]] * 5)
        pk = feed(g, [[hand(0.7, 0.5, "pinch")]] * 5)   # the control point teleports
        self.assertEqual(pk, [])

    def test_smooth_is_clamped(self):
        self.assertGreater(ht.Gestures(smooth=0).smooth, 0)
        self.assertEqual(ht.Gestures(smooth=5).smooth, 1.0)


def turned(pts, deg, about=None):
    """Rotate a hand counter-clockwise on screen (about the wrist by default)."""
    ox, oy = about or pts[0]
    c, n = math.cos(math.radians(deg)), math.sin(math.radians(deg))
    return [(ox + (x - ox) * c + (y - oy) * n, oy - (x - ox) * n + (y - oy) * c) for x, y in pts]


def scaled(pts, k):
    ox, oy = ht.classify(pts)[1]
    return [(ox + (x - ox) * k, oy + (y - oy) * k) for x, y in pts]


class SixAxisTest(unittest.TestCase):
    def test_twisting_a_pinch_rolls_counter_clockwise(self):
        g = ht.Gestures()
        pk = feed(g, [[turned(hand(0.5, 0.5, "pinch"), 2 * i)] for i in range(30)])
        roll = sum(p["a"] for p in pk if p["t"] == "roll")
        self.assertGreater(roll, math.radians(25))
        self.assertLess(roll, math.radians(58))
        self.assertEqual(g.label, "ROLL")
        self.assertNotIn("orbit", kinds(pk))      # a twist about the wrist does not orbit

    def test_clockwise_is_negative(self):
        g = ht.Gestures()
        pk = feed(g, [[turned(hand(0.5, 0.5, "pinch"), -2 * i)] for i in range(30)])
        self.assertLess(sum(p["a"] for p in pk if p["t"] == "roll"), 0)

    def test_twist_across_the_half_turn_does_not_jump(self):
        g = ht.Gestures()
        pk = feed(g, [[turned(hand(0.5, 0.5, "pinch"), 60 + 2 * i)] for i in range(40)])
        self.assertLess(max(abs(p["a"]) for p in pk if p["t"] == "roll"), 0.1)

    def test_orbit_with_a_small_wobble_does_not_roll(self):
        g = ht.Gestures()
        pk = feed(g, [[turned(hand(0.2 + 0.01 * i, 0.5, "pinch"), 4 * math.sin(i))] for i in range(40)])
        self.assertNotIn("roll", kinds(pk))
        self.assertIn("orbit", kinds(pk))

    def test_orbit_resumes_after_a_twist(self):
        g = ht.Gestures()
        feed(g, [[turned(hand(0.4, 0.5, "pinch"), 2 * i)] for i in range(20)])
        feed(g, [[turned(hand(0.4, 0.5, "pinch"), 38)]] * 20)
        pk = feed(g, [[turned(hand(0.4 + 0.01 * i, 0.5, "pinch"), 38)] for i in range(10)])
        self.assertIn("orbit", kinds(pk))

    def test_fist_towards_the_camera_is_positive_depth(self):
        for step, sign in ((0.02, 1), (-0.02, -1)):
            g = ht.Gestures()
            pk = feed(g, [[scaled(hand(0.5, 0.5, "fist"), 1 + step * i)] for i in range(25)])
            self.assertGreater(sign * sum(p["d"] for p in pk if p["t"] == "depth"), 0.1)

    def test_pinch_towards_the_camera_zooms_and_does_not_orbit(self):
        for step, sign in ((0.02, 1), (-0.02, -1)):
            g = ht.Gestures()
            pk = feed(g, [[scaled(hand(0.5, 0.5, "pinch"), 1 + step * i)] for i in range(25)])
            self.assertGreater(sign * sum(p["d"] for p in pk if p["t"] == "depth"), 0.1)
            self.assertEqual(g.label, "ZOOM")
            moved = sum(abs(p["dx"]) + abs(p["dy"]) for p in pk if p["t"] == "orbit")
            self.assertLess(moved, 0.05)

    def test_orbit_does_not_zoom(self):
        g = ht.Gestures()
        pk = feed(g, [[scaled(hand(0.2 + 0.01 * i, 0.5, "pinch"), 1 + 0.03 * math.sin(i))] for i in range(40)])
        self.assertNotIn("depth", kinds(pk))
        self.assertIn("orbit", kinds(pk))

    def test_twist_does_not_zoom(self):
        g = ht.Gestures()
        pk = feed(g, [[scaled(turned(hand(0.5, 0.5, "pinch"), 2 * i), 1 + 0.004 * i)] for i in range(30)])
        self.assertIn("roll", kinds(pk))
        self.assertNotIn("depth", kinds(pk))

    def test_pinch_zoom_can_be_switched_off(self):
        g = ht.Gestures(pinch_push=False)
        pk = feed(g, [[scaled(hand(0.5, 0.5, "pinch"), 1 + 0.02 * i)] for i in range(25)])
        self.assertNotIn("depth", kinds(pk))

    def test_plain_pan_does_not_zoom(self):
        g = ht.Gestures()
        pk = feed(g, [[scaled(hand(0.2 + 0.01 * i, 0.5, "fist"), 1 + 0.03 * math.sin(i))] for i in range(40)])
        self.assertNotIn("depth", kinds(pk))

    def test_two_pinches_turned_like_a_wheel_roll(self):
        g = ht.Gestures()
        mid = (0.5, 0.5)
        frames = []
        for i in range(25):
            x, y = 0.15 * math.cos(math.radians(2 * i)), 0.15 * math.sin(math.radians(2 * i))
            two = [hand(mid[0] + x, mid[1] - y, "pinch"), hand(mid[0] - x, mid[1] + y, "pinch")]
            frames.append(two if i % 2 else two[::-1])    # MediaPipe may swap the order
        pk = feed(g, frames)
        self.assertGreater(sum(p["a"] for p in pk if p["t"] == "roll"), 0.2)


def fingers(cx, cy, raised, thumb):
    """Hand with the given (index, middle, ring, pinky) raised and the thumb out or tucked."""
    p = hand(cx, cy, "fist")
    for base, r in zip((5, 9, 13, 17), raised):
        if r:
            x = p[base][0]
            p[base + 1], p[base + 2], p[base + 3] = (x, cy - 0.04), (x, cy - 0.07), (x, cy - 0.10)
    p[4] = (cx - 0.12, cy - 0.01) if thumb else (cx, cy - 0.01)
    return p


def lift(cx, cy, index_up=True, others_up=True):
    """Thumb touching the middle fingertip."""
    p = fingers(cx, cy, (index_up, 0, others_up, others_up), False)
    p[12] = (cx - 0.05, cy - 0.02)
    p[4] = (cx - 0.055, cy - 0.015)
    return p


class OneHandZoomTest(unittest.TestCase):
    def _factor(self, dy):
        g = ht.Gestures()
        pk = feed(g, [[lift(0.5, 0.5 + dy * i)] for i in range(25)])
        self.assertEqual(pk[0], {"t": "mode", "m": "zoom"})
        self.assertEqual(set(kinds(pk[1:])), {"zoom"})
        return math.prod(p["k2"] for p in pk[1:])

    def test_up_zooms_in_and_down_zooms_out(self):
        self.assertLess(self._factor(-0.01), 0.8)    # image y grows down
        self.assertGreater(self._factor(0.01), 1.25)

    def test_sideways_does_not_zoom(self):
        g = ht.Gestures()
        pk = feed(g, [[lift(0.2 + 0.01 * i, 0.5)] for i in range(25)])
        self.assertEqual(math.prod(p.get("k2", 1) for p in pk), 1)

    def test_pointing_with_the_thumb_on_the_middle_finger_is_the_front_view(self):
        g = ht.Gestures()
        pk = feed(g, [[lift(0.5, 0.5, others_up=False)]] * 30)
        self.assertEqual(pk, [{"t": "view", "v": "front"}])
        self.assertEqual(ht.classify(lift(0.5, 0.5, others_up=False))[0], "open")

    def test_other_poses_are_not_a_lift(self):
        for kind in ("open", "relaxed", "fist", "pinch", "natural_pinch", "gap"):
            self.assertNotEqual(ht.classify(hand(0.5, 0.5, kind))[0], "lift", kind)
        for raised, thumb, name in ViewTest.CASES:
            self.assertEqual(ht.classify(fingers(0.5, 0.5, raised, thumb))[0], "open", name)

    def test_zoom_then_orbit_with_the_same_hand_needs_no_pause(self):
        g = ht.Gestures()
        feed(g, [[lift(0.5, 0.5)]] * 5)
        feed(g, [[hand(0.5, 0.5, "pinch")]] * 5)
        self.assertEqual(g.mode, "orbit")

    def test_four_fingers_with_the_thumb_folded_fit_all(self):
        g = ht.Gestures()
        p = fingers(0.5, 0.5, (1, 1, 1, 1), False)
        p[4] = (0.51, 0.52)
        self.assertEqual(feed(g, [[p]] * 60), [{"t": "view", "v": "fit"}])


class RobustnessTest(unittest.TestCase):
    def test_hand_poses_are_plausible(self):
        for kind in ("open", "relaxed", "fist", "pinch", "natural_pinch", "gap"):
            self.assertTrue(ht.plausible(hand(0.5, 0.5, kind), 0.9), kind)
        for raised, thumb, name in ViewTest.CASES:
            self.assertTrue(ht.plausible(fingers(0.5, 0.5, raised, thumb), 0.9), name)

    def test_false_hands_are_rejected(self):
        good = hand(0.5, 0.5, "open")
        self.assertFalse(ht.plausible(good, 0.4))
        self.assertFalse(ht.plausible(scaled(good, 0.2), 0.9))
        stretched = list(good)
        stretched[8] = (1.2, 0.1)
        self.assertFalse(ht.plausible(stretched, 0.9))

    def test_small_hand_is_ignored_but_a_running_gesture_may_shrink_a_little(self):
        h = hand(0.5, 0.5, "pinch")          # palm 0.10
        self.assertAlmostEqual(ht.palm_size(h), 0.10, 2)
        self.assertTrue(ht.big_enough(h, 0.09, False))
        self.assertFalse(ht.big_enough(h, 0.12, False))
        self.assertTrue(ht.big_enough(h, 0.12, True))
        self.assertFalse(ht.big_enough(h, 0.15, True))
        self.assertTrue(ht.big_enough(h, 0, False))

    def test_picker_keeps_the_hand_of_a_running_gesture(self):
        k, a, b = ht.HandPicker(), hand(0.3, 0.6, "pinch"), hand(0.9, 0.3, "open")
        self.assertEqual(k.pick([a], 0.0, False), [a])
        self.assertEqual(k.pick([b, a], 0.03, True), [a])
        self.assertEqual(k.pick([b], 0.06, True), [])      # a is lost: do not jump to b
        self.assertEqual(k.pick([b], 1.0, False), [b])

    def test_picker_prefers_the_raised_hand_when_idle(self):
        k, desk, raised = ht.HandPicker(), hand(0.3, 0.9, "open"), hand(0.8, 0.4, "open")
        k.pick([desk], 0.0, False)
        self.assertEqual(k.pick([desk, raised], 0.03, False), [raised])

    def test_gesture_survives_a_short_loss_without_a_jump(self):
        g = ht.Gestures()
        feed(g, [[hand(0.5, 0.5, "pinch")]] * 5)
        pk = feed(g, [[]] * 5 + [[hand(0.62, 0.5, "pinch")]] * 5)
        self.assertEqual(pk, [])
        self.assertEqual(g.mode, "orbit")
        pk = feed(g, [[hand(0.62 + 0.01 * i, 0.5, "pinch")] for i in range(6)])
        self.assertIn("orbit", kinds(pk))

    def test_gesture_ends_when_the_hand_is_gone(self):
        g = ht.Gestures()
        feed(g, [[hand(0.5, 0.5, "pinch")]] * 5)
        self.assertEqual(feed(g, [[]] * 20), [{"t": "mode", "m": "idle"}])


class AccelerationTest(unittest.TestCase):
    def _orbit(self, accel, seconds, fps=30, kind="pinch"):
        """Move the hand 0.4 to the right in the given time; returns the distance sent."""
        g, t, pk, n = ht.Gestures(accel=accel), 0.0, [], int(seconds * fps)
        path = [0.3] * 5 + [0.3 + 0.4 * i / n for i in range(n + 1)] + [0.7] * 15
        for x in path:
            t += 1 / fps
            pk += g.update([hand(x, 0.5, kind)], t)
        return sum(p.get("dx", 0) for p in pk)

    def test_fast_goes_further_than_slow(self):
        for kind in ("pinch", "fist"):
            slow, fast = self._orbit(0.5, 3.0, kind=kind), self._orbit(0.5, 0.35, kind=kind)
            self.assertLess(slow, 0.33, kind)
            self.assertGreater(fast, 0.45, kind)

    def test_off_means_the_same_distance(self):
        self.assertAlmostEqual(self._orbit(0, 3.0), self._orbit(0, 0.35), 2)
        self.assertAlmostEqual(self._orbit(0, 1.0), 0.4, 2)

    def test_stronger_setting_accelerates_more(self):
        self.assertLess(self._orbit(0.3, 0.35), self._orbit(0.9, 0.35))

    def test_frame_rate_does_not_matter_much(self):
        a, b = self._orbit(0.5, 0.6, fps=30), self._orbit(0.5, 0.6, fps=15)
        self.assertLess(abs(a - b) / a, 0.2)

    def test_factor_is_limited_and_neutral_at_the_usual_speed(self):
        s = ht.Speed(usual=0.5)
        for v, want in ((0.5, 1.0), (1000.0, s.HIGH), (1e-9, s.LOW)):
            s.value = v
            self.assertAlmostEqual(s.factor(0.5), want)
        s.value = None
        self.assertEqual(s.factor(0), 1.0)

    def test_fast_twist_rolls_further(self):
        def roll(step):
            g, n = ht.Gestures(accel=0.6), int(60 / step)
            pk = feed(g, [[turned(hand(0.5, 0.5, "pinch"), step * i)] for i in range(n + 1)]
                      + [[turned(hand(0.5, 0.5, "pinch"), 60)]] * 10)
            return sum(p["a"] for p in pk if p["t"] == "roll")
        self.assertGreater(roll(6), 1.3 * roll(1.5))


class FaceGuardTest(unittest.TestCase):
    FACE = (0.4, 0.3, 0.18, 0.18)      # x, y, w, h; a hand at the same distance has a palm of about 0.10

    def _guard(self, now=0.0):
        guard = ht.FaceGuard()
        guard.faces([self.FACE], now)
        return guard

    def test_hand_on_the_face_is_ignored(self):
        for kind in ("open", "fist", "pinch", "relaxed"):
            self.assertEqual(self._guard().filter([hand(0.48, 0.38, kind)], 0.1, False), [], kind)
        self.assertEqual(self._guard().filter([hand(0.48, 0.50, "fist")], 0.1, False), [])    # chin, neck

    def test_hand_held_out_in_front_of_the_face_counts(self):
        near = scaled(hand(0.48, 0.38, "pinch"), 1.5)
        self.assertEqual(self._guard().filter([near], 0.1, False), [near])

    def test_only_the_same_scale_is_ignored_whatever_the_pose(self):
        on, size = hand(0.48, 0.38, "fist"), self.FACE[3]
        for ratio, counts in ((0.45, False), (0.55, False), (0.65, False), (0.80, True), (1.2, True)):
            got = self._guard().filter([on], 0.1, False, lambda pts: ratio * size)
            self.assertEqual(got, [on] if counts else [], ratio)

    def test_true_palm_does_not_depend_on_the_pose(self):
        # the same metric hand seen at the same scale, palm on and tilted: the picture shrinks alike
        metric = [(x - 0.5, y - 0.5) for x, y in hand(0.5, 0.5, "open")]
        tilted = [(x, y * 0.4) for x, y in metric]
        for world in (metric, tilted):
            pts = [(0.3 + 2 * x, 0.6 + 2 * y) for x, y in world]
            self.assertAlmostEqual(ht.true_palm(pts, world), 2 * ht.PALM_METRES)
        self.assertEqual(ht.true_palm(hand(0.5, 0.5, "open"), []), ht.palm_size(hand(0.5, 0.5, "open")))

    def test_hand_moved_towards_the_camera_counts_at_once(self):
        guard, on = self._guard(), hand(0.48, 0.38, "pinch")
        size = lambda r: (lambda pts: r * self.FACE[3])   # noqa: E731
        guard.filter([on], 0.1, False, size(0.55))
        guard.filter([on], 0.2, False, size(0.9))
        self.assertEqual(guard.filter([on], 0.4, False, size(0.9)), [on])

    def test_hand_beside_the_face_counts(self):
        for x in (0.1, 0.9):
            h = hand(x, 0.4, "pinch")
            self.assertEqual(self._guard().filter([h], 0.1, False), [h])

    def test_without_a_face_everything_counts(self):
        guard, h = ht.FaceGuard(), hand(0.48, 0.38, "fist")
        guard.faces([], 0.0)
        self.assertEqual(guard.filter([h], 0.1, False), [h])

    def test_running_gesture_may_pass_over_the_face_but_not_stay(self):
        guard, h = self._guard(), hand(0.48, 0.38, "pinch")
        self.assertEqual(guard.filter([h], 0.1, True), [h])
        self.assertEqual(guard.filter([h], 0.3, True), [h])
        self.assertEqual(guard.filter([h], 0.5, True), [])

    def test_hand_counts_again_a_moment_after_leaving(self):
        guard, on, away = self._guard(), hand(0.48, 0.38, "fist"), hand(0.9, 0.4, "fist")
        guard.filter([on], 0.1, False)
        self.assertEqual(guard.filter([away], 0.2, False), [])
        self.assertEqual(guard.filter([away], 0.6, False), [away])

    def test_face_hidden_by_the_hand_is_remembered_then_forgotten(self):
        guard, on = self._guard(), hand(0.48, 0.38, "fist")
        t = 0.0
        while t < 5:                       # not detected any more because the hand covers it
            t += 0.1
            self.assertEqual(guard.filter([on], t, False), [], t)
        guard2, away = self._guard(), hand(0.9, 0.4, "open")
        guard2.filter([away], 0.5, False)  # nobody covers it and it is gone: the person left
        self.assertEqual(guard2.filter([on], 2.0, False), [on])

    def test_hidden_face_is_not_remembered_for_ever(self):
        guard, on, t = self._guard(), hand(0.48, 0.38, "fist"), 0.0
        while t < 20:
            t += 0.1
            last = guard.filter([on], t, False)
        self.assertEqual(last, [on])


class FaceSearchTest(unittest.TestCase):
    def test_squares_stay_inside_and_the_round_covers_the_picture(self):
        w, h, s, seen = 960, 540, ht.FaceSearch(), set()
        for _ in range(13):
            for x, y, side in s.regions(w, h):
                self.assertTrue(0 <= x and x + side <= w and 0 <= y and y + side <= h)
                seen |= {(i, j) for i in range(x // 20, (x + side) // 20) for j in range(y // 20, (y + side) // 20)}
        self.assertEqual(len(seen), (w // 20) * (h // 20))

    def test_known_face_goes_first_then_the_hand(self):
        s = ht.FaceSearch()
        x, y, side = s.regions(960, 540, (0.5, 0.2, 0.2, 0.2), (0.1, 0.9))[0]
        self.assertTrue(x < 0.6 * 540 < x + side and y < 0.3 * 540 < y + side)
        x, y, side = s.regions(960, 540, None, (1.5, 0.5))[0]
        self.assertTrue(x < 1.5 * 540 < x + side)

    def test_tall_or_tiny_pictures_do_not_break(self):
        for w, h in ((640, 480), (320, 240), (960, 720)):
            for _ in range(13):
                for x, y, side in ht.FaceSearch().regions(w, h, (0, 0, 0.01, 0.01)):
                    self.assertTrue(x >= 0 and y >= 0 and side > 0 and x + side <= w and y + side <= h)


def thumb(cx, cy):
    """Fist seen from the side with the thumb stretched up: wrist to the right, knuckles to the left."""
    p = turned(hand(cx, cy, "fist"), 90, (cx, cy))
    top = min(y for _, y in p)
    p[2], p[3], p[4] = (cx - 0.01, top), (cx - 0.01, top - 0.04), (cx - 0.01, top - 0.08)
    return p


class LockTest(unittest.TestCase):
    THUMB, OPEN = [[thumb(0.5, 0.5)]], [[hand(0.5, 0.5, "open")]]
    PINCH = [[hand(0.3 + 0.01 * i, 0.5, "pinch")] for i in range(15)]

    def test_thumb_is_told_apart(self):
        self.assertTrue(ht.thumb_up(thumb(0.5, 0.5)))
        for kind in ("open", "relaxed", "fist", "pinch", "natural_pinch", "gap"):
            self.assertFalse(ht.thumb_up(hand(0.5, 0.5, kind)), kind)
            self.assertFalse(ht.thumb_up(turned(hand(0.5, 0.5, kind), 90)), kind)
        for raised, out, name in ViewTest.CASES:
            self.assertFalse(ht.thumb_up(fingers(0.5, 0.5, raised, out)), name)
        self.assertFalse(ht.thumb_up(turned(thumb(0.5, 0.5), 180)))   # thumb down

    def test_lock_stops_everything_and_unlock_brings_it_back(self):
        g = ht.Gestures()
        self.assertEqual(feed(g, self.THUMB * 40), [{"t": "lock", "on": True}])
        self.assertEqual(g.label, "LOCKED")
        f, o = SqueezeTest.FIST, SqueezeTest.OPEN
        views = [[fingers(0.5, 0.5, (1, 0, 0, 0), False)]] * 30
        self.assertEqual(feed(g, self.OPEN * 5 + self.PINCH + views + o * 5 + f * 6 + o * 5 + f * 6 + o * 10), [])
        self.assertEqual(feed(g, self.THUMB * 40), [{"t": "lock", "on": False}])
        self.assertIn("orbit", kinds(feed(g, self.OPEN * 6 + self.PINCH)))

    def test_holding_on_does_not_toggle_again_and_short_does_nothing(self):
        g = ht.Gestures()
        self.assertEqual(feed(g, self.THUMB * 200), [{"t": "lock", "on": True}])
        self.assertEqual(feed(ht.Gestures(), self.THUMB * 15 + self.OPEN * 10), [])

    def test_moving_thumb_does_not_lock_or_pan(self):
        self.assertEqual(feed(ht.Gestures(), [[thumb(0.2 + 0.02 * i, 0.5)] for i in range(30)]), [])

    def test_lock_ends_a_running_gesture(self):
        g = ht.Gestures()
        feed(g, [[hand(0.5, 0.5, "pinch")]] * 5)
        self.assertEqual(g.set_locked(True), [{"t": "mode", "m": "idle"}, {"t": "lock", "on": True}])
        self.assertEqual(g.mode, "idle")

    def test_raising_the_thumb_out_of_a_still_fist_locks(self):
        g = ht.Gestures()
        feed(g, [[hand(0.5, 0.5, "fist")]] * 5)
        self.assertEqual(g.mode, "pan")
        self.assertIn({"t": "lock", "on": True}, feed(g, self.THUMB * 40))
        self.assertEqual(g.mode, "idle")

    def test_can_be_switched_off(self):
        self.assertNotIn("lock", kinds(feed(ht.Gestures(lock=False), self.THUMB * 60)))


class TuneTest(unittest.TestCase):
    def test_thresholds_change_while_running(self):
        g = ht.Gestures()
        feed(g, [[hand(0.5, 0.5, "gap")]] * 5)
        self.assertEqual(g.mode, "idle")
        g.tune({"pinch": 0.5})
        feed(g, [[hand(0.5, 0.5, "gap")]] * 5)
        self.assertEqual(g.mode, "orbit")

    def test_twist_start(self):
        small = [[turned(hand(0.5, 0.5, "pinch"), i)] for i in range(13)]
        for degrees, rolls in ((3, True), (25, False)):
            g = ht.Gestures()
            g.tune({"twist_start": degrees})
            feed(g, small)
            self.assertEqual(g._twist.active, rolls, degrees)

    def test_nonsense_is_ignored(self):
        g = ht.Gestures()
        before = (g.pinch_on, g.fist_on, g.smooth, g.view_hold, g._twist.engage, g._push.engage)
        g.tune({"pinch": float("nan"), "fist": "x", "smooth": None, "view_hold": True, "no_such": 1})
        self.assertEqual(before, (g.pinch_on, g.fist_on, g.smooth, g.view_hold, g._twist.engage, g._push.engage))

    def test_stop_needs_the_secret(self):
        self.assertEqual(ht.quit_message("abc"), b"quit abc")
        self.assertEqual(ht.quit_message(""), b"quit")   # standalone, without a secret

    def test_only_the_session_may_tune(self):
        self.assertEqual(ht.parse_set(b'set abc {"pinch": 0.2}', "abc"), {"pinch": 0.2})
        for data, token in ((b'set bad {"pinch": 0.2}', "abc"), (b'set  {"pinch": 0.2}', ""), (b"set abc [1]", "abc"),
                            (b"set abc {", "abc"), (b"quit", "abc"), (b"\xff\xfe", "abc")):
            self.assertEqual(ht.parse_set(data, token), {})


class SwitchedOffTest(unittest.TestCase):
    def test_roll(self):
        pk = feed(ht.Gestures(roll=False), [[turned(hand(0.5, 0.5, "pinch"), 2 * i)] for i in range(30)])
        self.assertNotIn("roll", kinds(pk))

    def test_push(self):
        pk = feed(ht.Gestures(push=False), [[scaled(hand(0.5, 0.5, "fist"), 1 + 0.02 * i)] for i in range(25)])
        self.assertNotIn("depth", kinds(pk))

    def test_squeeze(self):
        f, o = SqueezeTest.FIST, SqueezeTest.OPEN
        self.assertNotIn("view", kinds(feed(ht.Gestures(squeeze=False), o * 5 + f * 6 + o * 5 + f * 6 + o * 20)))


class SqueezeTest(unittest.TestCase):
    FIST, OPEN = [[hand(0.5, 0.5, "fist")]], [[hand(0.5, 0.5, "open")]]

    def _views(self, frames):
        return [p for p in feed(ht.Gestures(), frames) if p["t"] == "view"]

    def test_two_quick_squeezes_fit_all_once(self):
        f, o = self.FIST, self.OPEN
        self.assertEqual(self._views(o * 5 + f * 6 + o * 5 + f * 6 + o * 20), [{"t": "view", "v": "fit"}])

    def test_one_squeeze_or_two_slow_ones_do_nothing(self):
        f, o = self.FIST, self.OPEN
        self.assertEqual(self._views(o * 5 + f * 6 + o * 40), [])
        self.assertEqual(self._views(o * 5 + f * 6 + o * 30 + f * 6 + o * 20), [])

    def test_regripping_while_panning_does_nothing(self):
        o = self.OPEN
        pan = [[hand(0.3 + 0.01 * i, 0.5, "fist")] for i in range(25)]
        back = [[hand(0.55 - 0.05 * i, 0.5, "open")] for i in range(5)]
        self.assertEqual(self._views(pan + back + pan + back + pan + o * 10), [])

    def test_works_at_a_low_frame_rate(self):
        g, t, pk = ht.Gestures(), 0.0, []
        for frames in (self.OPEN * 3, self.FIST * 3, self.OPEN * 3, self.FIST * 3, self.OPEN * 6):
            for hands in frames:
                t += 1 / 12
                pk += g.update(hands, t)
        self.assertEqual([p for p in pk if p["t"] == "view"], [{"t": "view", "v": "fit"}])


class ViewTest(unittest.TestCase):
    CASES = [((1, 0, 0, 0), False, "front"), ((1, 0, 0, 0), True, "rear"),
             ((1, 1, 0, 0), False, "top"), ((1, 1, 0, 0), True, "bottom"),
             ((1, 1, 1, 0), False, "right"), ((1, 1, 1, 0), True, "left"),
             ((1, 0, 0, 1), False, "iso"), ((0, 0, 0, 1), False, "fit")]

    def test_each_pose_gives_its_view_exactly_once(self):
        for raised, thumb, name in self.CASES:
            g = ht.Gestures()
            pk = feed(g, [[fingers(0.5, 0.5, raised, thumb)]] * 60)
            self.assertEqual(pk, [{"t": "view", "v": name}], name)

    def test_two_open_palms_fit_all_once(self):
        g = ht.Gestures()
        two = [fingers(0.3, 0.5, (1, 1, 1, 1), True), fingers(0.8, 0.5, (1, 1, 1, 1), True)]
        self.assertEqual(feed(g, [two] * 120), [{"t": "view", "v": "fit"}])

    def test_one_open_palm_is_only_a_pause(self):
        g = ht.Gestures()
        self.assertEqual(feed(g, [[fingers(0.5, 0.5, (1, 1, 1, 1), True)]] * 120), [])

    def test_letting_go_of_a_zoom_does_not_fit_all(self):
        g = ht.Gestures()
        feed(g, [[hand(0.3, 0.5, "pinch"), hand(0.8, 0.5, "pinch")]] * 5)
        two = [fingers(0.3, 0.5, (1, 1, 1, 1), True), fingers(0.8, 0.5, (1, 1, 1, 1), True)]
        self.assertNotIn("view", kinds(feed(g, [two] * 40)))

    def test_short_or_moving_pose_gives_nothing(self):
        g = ht.Gestures()
        self.assertEqual(feed(g, [[fingers(0.5, 0.5, (1, 0, 0, 0), False)]] * 6 + [[]] * 5), [])
        self.assertEqual(feed(g, [[fingers(0.2 + 0.01 * i, 0.5, (1, 0, 0, 0), False)] for i in range(50)]), [])

    def test_open_palm_fist_pinch_and_relaxed_hand_are_no_views(self):
        for kind in ("open", "relaxed", "fist", "pinch", "natural_pinch"):
            g = ht.Gestures()
            self.assertNotIn("view", kinds(feed(g, [[hand(0.5, 0.5, kind)]] * 60)), kind)

    def test_view_comes_after_the_hold_time_whatever_the_frame_rate(self):
        for fps in (12, 30):
            g, n = ht.Gestures(), 0
            while not g.update([fingers(0.5, 0.5, (1, 1, 0, 0), False)], 10 + n / fps):
                n += 1
            self.assertLess(n / fps, 0.4, fps)
            self.assertGreaterEqual(n / fps, 0.25, fps)

    def test_an_unclear_frame_does_not_restart_the_hold(self):
        g = ht.Gestures()
        one = [[fingers(0.5, 0.5, (1, 0, 0, 0), False)]]
        blur = [[fingers(0.5, 0.5, (1, 0, 1, 0), False)]]
        self.assertEqual(feed(g, one * 5 + blur + one * 5), [{"t": "view", "v": "front"}])

    def test_hand_pointing_sideways_is_no_view(self):
        g = ht.Gestures()
        self.assertEqual(feed(g, [[turned(fingers(0.5, 0.5, (1, 0, 0, 0), False), 80)]] * 60), [])

    def test_same_view_again_after_lowering_the_fingers(self):
        g = ht.Gestures()
        one = [[fingers(0.5, 0.5, (1, 1, 0, 0), False)]] * 30
        pk = feed(g, one + [[hand(0.5, 0.5, "open")]] * 5 + one)
        self.assertEqual(pk, [{"t": "view", "v": "top"}] * 2)

    def test_no_view_while_the_other_hand_is_working(self):
        g = ht.Gestures()
        pk = feed(g, [[hand(0.2, 0.5, "pinch"), fingers(0.8, 0.5, (1, 0, 0, 0), False)]] * 60)
        self.assertNotIn("view", kinds(pk))


class LinkTest(unittest.TestCase):
    def _listener(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind(("127.0.0.1", 0))
        s.settimeout(2)
        return s, s.getsockname()[1]

    def test_packets_and_frames_carry_the_token(self):
        s, port = self._listener()
        try:
            link = ht.Link(port, "abc123")
            self.addCleanup(link.sock.close)
            link.packet(t="zoom", k2=0.9)
            self.assertEqual(json.loads(s.recv(65535)), {"t": "zoom", "k2": 0.9, "k": "abc123"})
            link.frame(b"\xff\xd8" + b"x" * 20000)   # larger than macOS's default datagram limit
            data = s.recv(65535)
            self.assertTrue(data.startswith(b"abc123\xff\xd8"))
            self.assertEqual(len(data), 6 + 20002)
            self.assertFalse(link.gone)
        finally:
            s.close()

    def test_missing_listener_is_noticed(self):
        s, port = self._listener()
        s.close()   # nobody listens: this is FreeCAD switched off, quit or crashed
        link = ht.Link(port, "t")
        self.addCleanup(link.sock.close)
        for _ in range(5):
            link.packet(t="alive")
            time.sleep(0.05)
        self.assertTrue(link.gone)

    def test_standalone_ignores_missing_listener(self):
        s, port = self._listener()
        s.close()
        link = ht.Link(port, "t", standalone=True)
        self.addCleanup(link.sock.close)
        for _ in range(5):
            link.packet(t="alive")
            time.sleep(0.05)
        self.assertFalse(link.gone)

    def test_control_port_evicts_a_stale_tracker(self):
        old = ht.bind_control(0)
        port = old.getsockname()[1]
        old.close()
        stale = ht.bind_control(port)
        self.assertIsNotNone(stale)
        # a second tracker cannot bind while the first one lives, and tells it to quit
        t0 = time.monotonic()
        self.assertIsNone(ht.bind_control(port, wait=1.2))
        self.assertGreater(time.monotonic() - t0, 1.0)
        self.assertEqual(stale.recv(64), b"quit")
        stale.close()
        fresh = ht.bind_control(port)
        self.assertIsNotNone(fresh)
        fresh.close()


class TrackerProcessTest(unittest.TestCase):
    """Runs the real tracker process; it exits before the imports that need mediapipe."""

    def _run(self, *args):
        r = subprocess.run([sys.executable, ht.__file__, *args], capture_output=True, text=True, timeout=40)
        return r.returncode, r.stdout + r.stderr

    def _port(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind(("127.0.0.1", 0))
        self.addCleanup(s.close)
        s.settimeout(1)
        return s, s.getsockname()[1]

    def test_exits_when_freecad_is_not_listening(self):
        s, port = self._port()
        s.close()
        code, text = self._run("--port", str(port))
        self.assertEqual(code, 0, text)
        self.assertIn("not listening", text)

    def test_missing_token_file(self):
        code, text = self._run("--token-file", "no-such-file")
        self.assertEqual(code, 1)
        self.assertIn("session token", text)

    def test_stop_or_failure_while_starting_says_bye(self):
        s, port = self._port()
        proc = subprocess.Popen([sys.executable, ht.__file__, "--port", str(port), "--camera", "99"],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.addCleanup(proc.kill)
        s.recv(65535)                       # first status packet: the tracker is up
        for _ in range(10):                 # keep asking until the control port is bound
            s.sendto(b"quit", ("127.0.0.1", port + 1))
            time.sleep(0.05)
        text, _ = proc.communicate(timeout=30)
        msgs = []
        try:
            while True:
                msgs.append(json.loads(s.recv(65535)))
        except OSError:
            pass
        self.assertIn("bye", [m["t"] for m in msgs], text)


class PickCameraTest(unittest.TestCase):
    CAMS = [("Someone's iPhone Camera", "1"), ("FaceTime HD Camera", "3"), ("FUJIFILM X Webcam 2", "B")]

    def setUp(self):
        self._stdout, sys.stdout = sys.stdout, open(os.devnull, "w")

    def tearDown(self):
        sys.stdout.close()
        sys.stdout = self._stdout

    def test_auto_prefers_builtin(self):
        self.assertEqual(ht.pick_camera(-1, "", self.CAMS), 1)

    def test_by_name(self):
        self.assertEqual(ht.pick_camera(-1, "fuji", self.CAMS), 2)

    def test_explicit_index(self):
        self.assertEqual(ht.pick_camera(0, "", self.CAMS), 0)

    def test_unknown_name_falls_back(self):
        self.assertEqual(ht.pick_camera(-1, "nope", self.CAMS), 1)

    def test_no_builtin_skips_iphone(self):
        cams = [("Someone's iPhone Camera", "1"), ("FUJIFILM X Webcam 2", "B")]
        self.assertEqual(ht.pick_camera(-1, "", cams), 1)

    def test_no_camera_list(self):
        self.assertEqual(ht.pick_camera(-1, "", []), 0)


if __name__ == "__main__":
    unittest.main()
