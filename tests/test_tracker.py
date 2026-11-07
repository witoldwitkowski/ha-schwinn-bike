import csv
import glob
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(__file__))
from _load import load  # noqa: E402

pr = load("protocol")
tr = load("tracker")
rl = load("ridelog")

HERE = os.path.dirname(os.path.abspath(__file__))


def W(cranks8, ticks, level=5):
    return pr.Workout(cranks8 / 8, ticks % 65536, 0.0, level)


def M(mph=16.0, w=90.0, energy=10, dist=0.5):
    return pr.Motion(mph, w, energy, dist)


class Rig:
    """feeds 8 Hz workout packets and 10 Hz motion packets for N seconds at a steady rpm"""

    def __init__(self, tracker=None, **kw):
        self.t = 0.0
        self.tr = tracker or tr.RideTracker(clock=lambda: self.t, **kw)
        self.cr8 = 0
        self.tick = 0.0
        self.energy = 10
        self.dist = 0.5

    def pedal(self, seconds, rpm=60, mph=16.0, watts=90.0, level=5, hr=None, kcal_per_s=0.1, mph_to_mi_per_s=None):
        step = 0.1
        n = int(round(seconds / step))
        per_rev = 60 / rpm  # seconds per revolution
        for _ in range(n):
            self.t += step
            self.tick += step * 1024
            self.cr8 += step / per_rev * 8
            self.tr.feed_workout(W(int(self.cr8), int(self.tick), level), self.t)
            self.energy_f = getattr(self, "energy_f", float(self.energy)) + kcal_per_s * step
            self.energy = int(self.energy_f)
            self.dist += mph * step / 3600
            self.tr.feed_motion(M(mph, watts, self.energy, round(self.dist, 2)), self.t)
            if hr is not None:
                self.tr.feed_hr(hr, self.t)

    def idle(self, seconds, frozen_mph=0.0):
        """no workout packets (stopped or paused); motion packets keep coming"""
        step = 0.1
        for _ in range(int(round(seconds / step))):
            self.t += step
            self.tr.feed_motion(M(frozen_mph, 0.0 if frozen_mph == 0 else 3.5, self.energy, round(self.dist, 2)), self.t)
            self.tr.tick(self.t)


class TrackerTests(unittest.TestCase):
    def test_cadence_and_riding(self):
        r = Rig()
        r.pedal(8, rpm=60)
        self.assertAlmostEqual(r.tr.cadence(r.t), 60, delta=3)
        self.assertTrue(r.tr.riding(r.t))
        self.assertEqual(r.tr.speed_mph(r.t), 16.0)

    def test_ride_totals(self):
        r = Rig()
        r.pedal(90, rpm=60, mph=16.0, watts=90, level=5, kcal_per_s=0.1)
        s = r.tr.summary()
        self.assertAlmostEqual(s["active_min"], 1.5, delta=0.1)
        self.assertAlmostEqual(s["miles"], 16.0 * 90 / 3600, delta=0.03)
        self.assertAlmostEqual(s["kcal"], 9, delta=1)  # counter delta, not the absolute count (which started at 10)
        self.assertEqual((s["avg_mph"], s["avg_w"], s["max_level"]), (16.0, 90, 5))

    def test_pause_freezes_values_but_not_the_ride(self):
        """console paused: speed/power freeze at the last coasting value, workout packets stop, counters stop"""
        r = Rig()
        r.pedal(30)
        before = r.tr.summary()
        r.idle(20, frozen_mph=2.36)
        self.assertFalse(r.tr.riding(r.t))
        self.assertEqual(r.tr.speed_mph(r.t), 0.0)  # NOT the frozen 2.36
        self.assertEqual(r.tr.cadence(r.t), 0.0)
        after = r.tr.summary()
        self.assertAlmostEqual(after["active_min"], before["active_min"], delta=0.06)  # only the first <=3 s of coasting may add
        self.assertEqual(after["kcal"], before["kcal"])
        r.pedal(30)  # resume: same ride continues
        self.assertTrue(r.tr.ride_open)
        self.assertAlmostEqual(r.tr.summary()["active_min"], 1.0, delta=0.12)

    def test_end_packet_finishes_and_keeps_totals(self):
        r = Rig()
        r.pedal(75, hr=None)
        self.assertIsNone(r.tr.feed_state(1, r.t))
        s = r.tr.feed_state(3, r.t)
        self.assertEqual(s["reason"], "ended")
        self.assertGreater(s["active_min"], 1.0)
        snap = r.tr.snapshot(r.t + 5)
        self.assertEqual(snap["session_kcal"], s["kcal"])  # shown until the next ride starts
        self.assertFalse(snap["ride_open"])

    def test_short_ride_not_kept(self):
        r = Rig()
        r.pedal(20)
        self.assertIsNotNone(r.tr.feed_state(3, r.t))  # the ride itself closes ...
        r.tr.tick(r.t + 200)  # ... and when the merge wait is over it is thrown away
        self.assertEqual(r.tr.pop_finished(), [])
        self.assertIsNone(r.tr.last_summary)

    def test_five_minute_minimum(self):
        """the integration's rule: under 5 minutes of PEDALLING is thrown away; pauses do not count and do not split a ride"""
        MIN = 300.0
        r = Rig(min_active_s=MIN)
        r.pedal(280)  # 4 min 40 s
        r.tr.feed_state(3, r.t)
        r.tr.tick(r.t + 200)
        self.assertEqual(r.tr.pop_finished(), [])
        self.assertIsNone(r.tr.last_summary)
        r2 = Rig(min_active_s=MIN)
        r2.pedal(150)
        r2.idle(120, frozen_mph=2.0)  # a 2 minute pause in the middle
        r2.pedal(160)  # 150 + 160 = 310 s of pedalling
        r2.tr.feed_state(3, r2.t)
        r2.tr.tick(r2.t + 200)
        done = r2.tr.pop_finished()
        self.assertEqual(len(done), 1)
        self.assertTrue(5.0 <= done[0]["active_min"] <= 5.4, done[0]["active_min"])
        r3 = Rig(min_active_s=MIN)
        r3.pedal(150)
        r3.idle(400, frozen_mph=0.0)  # 6+ minutes of wall time but only 2.5 min pedalling: still dropped
        r3.tr.tick(r3.t + 200)
        self.assertEqual(r3.tr.pop_finished(), [])

    def test_two_short_rides_merge_into_one(self):
        """the user's rule: wait a couple of minutes after a ride; if another starts, they are ONE ride (so two 3-minute rides count, together 6 min)"""
        r = Rig(min_active_s=300.0, merge_wait_s=180.0)
        r.pedal(180, mph=15.0, watts=80, level=4, hr=120)
        a = r.tr.feed_state(3, r.t)
        self.assertEqual(r.tr.pop_finished(), [], "nothing is released while we wait to see if another ride follows")
        r.idle(90, frozen_mph=0.0)  # 90 s later the user starts again (console counters restarted)
        r.energy, r.energy_f, r.dist = 0, 0.0, 0.0
        r.pedal(180, mph=18.0, watts=120, level=8, hr=140)
        b = r.tr.feed_state(3, r.t)
        self.assertEqual(r.tr.pop_finished(), [])
        r.tr.tick(r.t + 100)  # still inside the wait
        self.assertEqual(r.tr.pop_finished(), [])
        r.tr.tick(r.t + 181)
        done = r.tr.pop_finished()
        self.assertEqual(len(done), 1, "one merged ride, not two")
        m = done[0]
        self.assertEqual(m["parts"], 2)
        self.assertAlmostEqual(m["active_min"], a["active_min"] + b["active_min"], delta=0.02)
        self.assertEqual(m["kcal"], a["kcal"] + b["kcal"])
        self.assertAlmostEqual(m["miles"], a["miles"] + b["miles"], delta=0.02)
        self.assertEqual((m["max_level"], m["max_hr"]), (8, 140))
        self.assertTrue(16.0 <= m["avg_mph"] <= 17.0, m["avg_mph"])  # weighted between 15 and 18
        self.assertTrue(125 <= m["avg_hr"] <= 135, m["avg_hr"])
        self.assertEqual(sum(m["hr_hist"].values()), a["hr_s"] + b["hr_s"])
        self.assertEqual(m["ts_start"], a["ts_start"])

    def test_rides_further_apart_than_the_wait_stay_separate(self):
        r = Rig(min_active_s=60.0, merge_wait_s=180.0)
        r.pedal(90)
        r.tr.feed_state(3, r.t)
        r.idle(200)  # the wait expires (tick runs inside idle)
        first = r.tr.pop_finished()
        self.assertEqual(len(first), 1)
        r.energy, r.energy_f, r.dist = 0, 0.0, 0.0
        r.pedal(90)
        r.tr.feed_state(3, r.t)
        r.tr.tick(r.t + 200)
        second = r.tr.pop_finished()
        self.assertEqual(len(second), 1)
        self.assertEqual((first[0]["parts"], second[0]["parts"]), (1, 1))

    def test_pending_expired_but_tick_late_is_not_merged(self):
        r = Rig(min_active_s=60.0, merge_wait_s=180.0)
        r.pedal(90)
        r.tr.feed_state(3, r.t)
        r.t += 400  # no tick ran (e.g. HA was busy); the next ride starts well after the wait
        r.energy, r.energy_f, r.dist = 0, 0.0, 0.0
        r.pedal(90)
        self.assertEqual(len(r.tr.pop_finished()), 1, "the first ride became final when the second started")
        r.tr.feed_state(3, r.t)
        r.tr.tick(r.t + 200)
        self.assertEqual(len(r.tr.pop_finished()), 1)

    def test_rides_of_different_users_never_merge(self):
        r = Rig(min_active_s=60.0, merge_wait_s=180.0)
        r.tr.feed_user(1)
        r.pedal(90)
        r.tr.feed_state(3, r.t, user=1)
        r.idle(30)  # Bob starts 30 s later, well inside the merge wait
        r.tr.feed_user(2)
        r.energy, r.energy_f, r.dist = 0, 0.0, 0.0
        r.pedal(90)
        r.tr.feed_state(3, r.t, user=2)
        r.tr.tick(r.t + 200)
        done = r.tr.pop_finished()
        self.assertEqual([(d["user"], d["parts"]) for d in done], [(1, 1), (2, 1)])

    def test_same_user_still_merges(self):
        r = Rig(min_active_s=100.0, merge_wait_s=180.0)
        r.tr.feed_user(2)
        r.pedal(60)
        r.tr.feed_state(3, r.t, user=2)
        r.idle(30)
        r.energy, r.energy_f, r.dist = 0, 0.0, 0.0
        r.pedal(60)
        r.tr.feed_state(3, r.t, user=2)
        r.tr.tick(r.t + 200)
        done = r.tr.pop_finished()
        self.assertEqual([(d["user"], d["parts"]) for d in done], [(2, 2)])

    def test_hr_limit_follows_the_console_user(self):
        t = tr.RideTracker(clock=lambda: 0.0)
        t.configure_max_hr(177, {1: 177, 2: 200})
        t.feed_user(1)
        t.feed_hr(185, 1.0)
        self.assertIsNone(t.heart_rate(1.0), "185 is above W's limit (max+5)")
        t.feed_user(2)
        t.feed_hr(185, 2.0)
        self.assertEqual(t.heart_rate(2.0), 185, "...but fine for a child with max HR 200")

    def test_flush_releases_pending_now(self):
        r = Rig(min_active_s=60.0)
        r.pedal(90)
        r.tr.feed_state(3, r.t)
        self.assertEqual(r.tr.pop_finished(), [])
        r.tr.flush(r.t + 1)  # unload / HA stopping: no waiting
        self.assertEqual(len(r.tr.pop_finished()), 1)

    def test_flush_closes_an_open_ride_too(self):
        r = Rig(min_active_s=60.0)
        r.pedal(90)
        r.tr.flush(r.t + 1)
        done = r.tr.pop_finished()
        self.assertEqual((len(done), done[0]["reason"]), (1, "shutdown"))

    def test_idle_ends_ride(self):
        r = Rig()
        r.pedal(70)
        self.assertIsNone(r.tr.tick(r.t + 100))
        s = r.tr.tick(r.t + 200)
        self.assertEqual(s["reason"], "idle")

    def test_counter_reset_starts_new_ride(self):
        r = Rig(min_active_s=60.0)
        r.pedal(70)
        r.energy, r.energy_f, r.dist = 0, 0.0, 0.0  # console workout restarted without us seeing "ended"
        r.pedal(5)
        self.assertTrue(r.tr.ride_open, "a new ride is open")
        self.assertEqual(r.tr._pending["reason"], "reset", "the old ride was closed by the counter reset and waits for the merge window")
        self.assertEqual(r.tr.pop_finished(), [])

    def test_hr_filter(self):
        t = tr.RideTracker(max_hr=177, clock=lambda: 0.0)
        for i, (raw, expect) in enumerate([(0, None), (125, 125), (185, 125), (186, 125), (126, 126), (160, 126)]):
            t.feed_hr(raw, 10.0 + i)
            self.assertEqual(t.heart_rate(10.0 + i), expect, raw)
        self.assertEqual(t.hr_rejected, 3)  # 185, 186 (above max+5) and 160 (a +34 jump)
        self.assertIsNone(t.heart_rate(30.0))  # stale after 5 s

    def test_hr_in_summary(self):
        r = Rig()
        r.pedal(70, hr=124)
        s = r.tr.summary()
        self.assertEqual((s["avg_hr"], s["max_hr"]), (124, 124))
        self.assertEqual(list(s["hr_hist"]), ["120"])

    def test_replay_of_real_ride(self):
        files = sorted(glob.glob(os.path.join(HERE, "fixtures", "ride_sample.jsonl")))
        if not files:
            self.skipTest("fixture not present")
        t = tr.RideTracker(max_hr=177, clock=lambda: 0.0)
        got = None
        with open(files[0]) as fh:
            lines = fh.readlines()
        for line in lines:
            r = json.loads(line)
            b, now = bytes.fromhex(r["hex"]), r["t"]
            if r["uuid"].startswith("5c7d82a0"):
                w = pr.parse_workout(b)
                if w:
                    t.feed_workout(w, now)
                elif len(b) == 5 and b[:3] == bytes([5, 9, 1]):
                    got = t.feed_state(b[3], now) or got
            elif r["uuid"].startswith("a46a4a80"):
                m = pr.parse_motion(b)
                if m:
                    t.feed_motion(m, now)
                    t.tick(now)
            elif r["uuid"].startswith("6be8f580"):
                t.feed_hr(pr.parse_hr(b), now)
        self.assertIsNotNone(got, "ride should end on the 05 09 01 03 00 packet")
        self.assertEqual(got["reason"], "ended")
        # console said 53 cal at the end; the capture joined at count 3 -> delta about 50
        self.assertTrue(47 <= got["kcal"] <= 53, got["kcal"])
        self.assertTrue(1.9 <= got["miles"] <= 2.3, got["miles"])  # counter 2.31 at the end, 0.22 at join
        self.assertTrue(6.5 <= got["active_min"] <= 8.6, got["active_min"])
        self.assertTrue(110 <= got["avg_hr"] <= 135, got["avg_hr"])
        self.assertLessEqual(got["max_rpm"], 140)  # the real sprint peaked near 125 rpm; just a sanity bound
        self.assertLessEqual(got["max_hr"], 177)  # the console's 185 bursts must not get in
        self.assertGreaterEqual(got["max_level"], 20)
        print("\nREAL RIDE REPLAY:", json.dumps(got))


class RideLogTests(unittest.TestCase):
    def test_last_ride_survives_a_restart(self):
        a = {"ts_start": "2026-11-06T17:34:31-05:00", "ts_end": "2026-11-06T17:40:51-05:00", "active_min": 5.31, "kcal": 31, "miles": 1.41, "avg_mph": 15.8,
             "max_mph": 17.4, "avg_w": 85, "max_w": 108, "avg_rpm": 65, "max_rpm": 75, "avg_level": 6.1, "max_level": 7, "avg_hr": 124, "max_hr": 130,
             "hr_s": 222, "hr_hist": {"120": 201}, "reason": "ended"}
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "bike_log.csv")
            self.assertIsNone(rl.last_logged(path))
            rl.append_row(path, rl.row_from_summary("alice", a))
            rl.append_row(path, rl.row_from_summary("bob", {**a, "ts_start": "2026-11-07T10:00:00-05:00", "kcal": 40}))
            rl.append_row(path, rl.row_from_summary("alice", {**a, "ts_start": "2026-11-05T10:00:00-05:00", "kcal": 99}))  # older, logged last
            with open(path, "a") as f:
                f.write("garbage,line\n")
            last = rl.last_logged(path)
        self.assertEqual((last["person"], last["kcal"], last["avg_hr"], last["miles"]), ("bob", 40, 124, 1.41))
        self.assertEqual(last["hr_hist"], {"120": 201})


    def test_roundtrip(self):
        s = {"ts_start": "2026-11-06T16:55:00-05:00", "ts_end": "2026-11-06T17:04:15-05:00", "active_min": 7.8, "kcal": 50, "miles": 2.09,
             "avg_mph": 15.1, "max_mph": 24.3, "avg_w": 100, "max_w": 262, "avg_rpm": 62, "max_rpm": 75, "avg_level": 6.5, "max_level": 23,
             "avg_hr": 122, "max_hr": 146, "hr_s": 200, "hr_hist": {"110": 50, "120": 150}, "reason": "ended"}
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "sub", "bike_log.csv")
            rl.append_row(p, rl.row_from_summary("alice", s))
            rl.append_row(p, rl.row_from_summary("alice", {**s, "avg_hr": None, "max_hr": None}))
            rows = rl.read_rows(p)
        self.assertEqual(len(rows), 2)
        self.assertEqual((rows[0]["person"], rows[0]["kcal"], rows[0]["max_level"]), ("alice", "50", "23"))
        self.assertEqual(json.loads(rows[0]["hr_hist"]), {"110": 50, "120": 150})
        self.assertEqual(rows[1]["avg_hr"], "")


if __name__ == "__main__":
    unittest.main()
