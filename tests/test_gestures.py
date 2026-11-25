"""Tracker logic tests. Pure Python: no camera, mediapipe or OpenCV needed.

    python3 -m unittest discover -s tests
"""
import json
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
        self.assertTrue(stale.recv(64).startswith(b"quit"))
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
