"""Gesture logic tests. Pure Python: no camera, mediapipe or OpenCV needed.

    python3 -m unittest discover -s tests
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tracker"))
import hand_tracker as ht  # noqa: E402


def hand(cx, cy, kind):
    """Synthetic 21-point hand, wrist at the bottom, fingers pointing up (image y grows down)."""
    p = [(cx, cy)] * 21
    p[0] = (cx, cy + 0.10)
    extended = kind != "fist"
    for base, dx in {5: -0.03, 9: -0.01, 13: 0.01, 17: 0.03}.items():
        p[base] = (cx + dx, cy)
        p[base + 1] = (cx + dx, cy - 0.04)
        p[base + 2] = (cx + dx, cy - 0.07 if extended else cy - 0.01)
        p[base + 3] = (cx + dx, cy - 0.10 if extended else cy + 0.02)
    p[1], p[2], p[3] = (cx - 0.04, cy + 0.06), (cx - 0.06, cy + 0.03), (cx - 0.07, cy)
    p[4] = (cx - 0.08, cy - 0.03)
    if kind == "pinch":
        p[8] = (cx - 0.06, cy - 0.04)
        p[4] = (cx - 0.065, cy - 0.035)
    return p


def feed(g, frames):
    out = []
    for hands in frames:
        out += g.update([ht.classify(h, g.pinch_thr()) for h in hands])
    return out


class ClassifyTest(unittest.TestCase):
    def test_poses(self):
        for kind in ("open", "pinch", "fist"):
            self.assertEqual(ht.classify(hand(0.5, 0.5, kind), 0.30)[0], kind)

    def test_center_follows_hand(self):
        (x1, y1), (x2, y2) = (ht.classify(hand(x, 0.5, "open"), 0.3)[1] for x in (0.3, 0.6))
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
            k *= p["k"]
        self.assertLess(k, 0.8)  # hands apart -> k < 1 -> zoom in

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


class PickCameraTest(unittest.TestCase):
    CAMS = [("Someone's iPhone Camera", "1"), ("FaceTime HD Camera", "3"), ("FUJIFILM X Webcam 2", "B")]

    def setUp(self):
        self._orig = ht.list_cameras
        ht.list_cameras = lambda: self.CAMS
        self._stdout, sys.stdout = sys.stdout, open(os.devnull, "w")

    def tearDown(self):
        ht.list_cameras = self._orig
        sys.stdout.close()
        sys.stdout = self._stdout

    def test_auto_prefers_builtin(self):
        self.assertEqual(ht.pick_camera(-1, ""), 1)

    def test_by_name(self):
        self.assertEqual(ht.pick_camera(-1, "fuji"), 2)

    def test_explicit_index(self):
        self.assertEqual(ht.pick_camera(0, ""), 0)

    def test_unknown_name_falls_back(self):
        self.assertEqual(ht.pick_camera(-1, "nope"), 1)

    def test_no_builtin_skips_iphone(self):
        self.CAMS = [("Someone's iPhone Camera", "1"), ("FUJIFILM X Webcam 2", "B")]
        self.assertEqual(ht.pick_camera(-1, ""), 1)


if __name__ == "__main__":
    unittest.main()
