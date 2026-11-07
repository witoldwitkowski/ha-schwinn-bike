"""Ride tracking for the Schwinn 170/270. Pure Python (no Home Assistant imports) so it is unit-testable offline.

Findings it encodes (verified against console photos/video, 2026-11-06):
* "riding" means the crank count in the 17-byte workout packets is advancing. Speed alone is NOT a riding signal: when the console is paused the bike
  freezes speed/power at their last value and the workout packets stop.
* speed / power / distance / calories come from the 14-byte motion packet (see protocol.parse_motion). Distance and calories carry a start offset,
  so a ride uses differences against the first reading of that ride.
* heart rate comes from grip sensors: 0 = hands off, and bursts of ~185 are bogus. Filtered here.
* the 5-byte packet `05 09 01 <state> 00` marks the workout: state 1 = started, 3 = ended.
"""
from __future__ import annotations

import datetime as dt
import statistics
import time
from collections import deque
from collections.abc import Callable

from .protocol import Motion, Workout

MOTION_GAP_S = 1.0  # one motion packet's worth of time at most (they arrive ~10 Hz)
WORKOUT_GAP_S = 3.0  # a bigger hole between workout packets re-baselines the cadence window


class RideTracker:
    def __init__(
        self,
        max_hr: int = 177,
        idle_end_s: float = 180.0,
        min_active_s: float = 60.0,
        merge_wait_s: float = 180.0,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], dt.datetime] | None = None,
    ) -> None:
        self.max_hr = max_hr
        self.idle_end_s = idle_end_s
        self.min_active_s = min_active_s
        self.merge_wait_s = merge_wait_s
        self._clock = clock
        self._wall = wall or (lambda: dt.datetime.now().astimezone())
        # live inputs
        self.level = 0
        self._last_w: Workout | None = None
        self._cum_cranks = 0.0
        self._cum_s = 0.0
        self._win: list[tuple[float, float, float]] = []
        self._cadence = 0.0
        self._last_move_at: float | None = None
        self._last_workout_at: float | None = None
        self._motion: Motion | None = None
        self._motion_at: float | None = None
        self._hr: int | None = None
        self._hr_at: float | None = None
        self._hr_recent: deque[tuple[float, int]] = deque(maxlen=40)
        self.hr_rejected = 0
        self.workout_state: int | None = None
        self.user: int | None = None  # console user (1, 2, ...) as reported by the bike
        self.max_hr_by_user: dict[int, int] = {}  # heart-rate filter limit per console user (falls back to max_hr)
        self._base_max_hr = max_hr
        self._ride_user: int | None = None
        # ride accumulators
        self.last_summary: dict | None = None
        self._finished: list[dict] = []  # FINAL rides (merged, long enough) since the last pop_finished()
        self._pending: dict | None = None  # the last ride(s), waiting merge_wait_s to see whether another ride follows
        self._pending_at = 0.0
        self._open = False
        self._reset_ride()

    # ---- inputs ------------------------------------------------------------------------------
    def feed_workout(self, w: Workout, now: float | None = None) -> None:
        now = self._clock() if now is None else now
        self._last_workout_at = now
        self.level = w.resistance
        last = self._last_w
        self._last_w = w
        if last is None:
            self._win = [(now, self._cum_cranks, self._cum_s)]
            return
        dticks = (w.ticks - last.ticks) % 65536
        if dticks == 0:
            return
        dt_s = dticks / 1024
        if dt_s > WORKOUT_GAP_S:  # we were paused/stopped: don't average across the hole
            self._win = [(now, self._cum_cranks, self._cum_s)]
            self._cadence = 0.0
            return
        dcr = (w.cranks - last.cranks) % 65536
        self._cum_cranks += dcr
        self._cum_s += dt_s
        self._win.append((now, self._cum_cranks, self._cum_s))
        while len(self._win) > 1 and now - self._win[0][0] > 5.0:
            self._win.pop(0)
        _, c0, s0 = self._win[0]
        span = self._cum_s - s0
        if span >= 1.0:  # a shorter window (right after a pause) gives wild numbers
            self._cadence = (self._cum_cranks - c0) / span * 60
        if dcr > 0:
            self._last_move_at = now

    def feed_motion(self, m: Motion, now: float | None = None) -> None:
        now = self._clock() if now is None else now
        prev, prev_at = self._motion, self._motion_at
        self._motion, self._motion_at = m, now
        if prev is not None and (m.energy_count < prev.energy_count or m.distance_mi < prev.distance_mi - 1e-9):
            # counters went backwards: a new workout was started on the console without us seeing "ended"
            self.finish("reset", now)
        if not self.riding(now):
            return
        if not self._open:
            self._begin(now, m)
        if prev_at is not None:
            dt_s = min(now - prev_at, MOTION_GAP_S)
            if dt_s > 0:
                self._accumulate(dt_s, m, now)
        self._e1, self._d1 = m.energy_count, m.distance_mi

    def feed_hr(self, raw: int | None, now: float | None = None) -> None:
        """raw = parse_hr() result (None/0 means no reading). Bogus values (above max+5, or a jump >30 bpm from the recent median) are dropped."""
        now = self._clock() if now is None else now
        if not raw:
            return
        recent = [v for t, v in self._hr_recent if now - t <= 15]
        if raw < 40 or raw > self.max_hr + 5 or (len(recent) >= 2 and abs(raw - statistics.median(recent)) > 30):
            self.hr_rejected += 1
            return
        self._hr, self._hr_at = raw, now
        self._hr_recent.append((now, raw))

    def configure_max_hr(self, default: int, by_user: dict[int, int]) -> None:
        self._base_max_hr = default
        self.max_hr_by_user = dict(by_user)
        self.max_hr = by_user.get(self.user, default) if self.user else default

    def feed_user(self, user: int | None, now: float | None = None) -> None:
        """Console user number (protocol.parse_user / parse_state). Sets that person's heart-rate limit."""
        if not user:
            return
        self.user = user
        self.max_hr = self.max_hr_by_user.get(user, self._base_max_hr)

    def feed_state(self, state: int, now: float | None = None, user: int | None = None) -> dict | None:
        """The 5-byte packet's state byte (1 started, 3 ended). Returns the finished ride summary on 'ended'."""
        now = self._clock() if now is None else now
        self.feed_user(user)
        self.workout_state = state
        if state == 3:
            return self.finish("ended", now)
        return None

    def tick(self, now: float | None = None) -> dict | None:
        """Call about once a second: ends a ride after idle_end_s without pedalling. Returns the summary if it just ended."""
        now = self._clock() if now is None else now
        out = None
        if self._open and self._last_move_at is not None and now - self._last_move_at > self.idle_end_s:
            out = self.finish("idle", now)
        self._expire_pending(now)
        return out

    def disconnected(self, now: float | None = None) -> dict | None:
        """Call ONLY for a deliberate disconnect (idle). After an unexpected link drop do nothing: the counters carry on and the ride resumes."""
        now = self._clock() if now is None else now
        return self.finish("disconnect", now)

    # ---- live values -------------------------------------------------------------------------
    def riding(self, now: float | None = None) -> bool:
        now = self._clock() if now is None else now
        return self._last_move_at is not None and now - self._last_move_at <= 3.0

    def cadence(self, now: float | None = None) -> float:
        now = self._clock() if now is None else now
        if not self.riding(now) or self._last_workout_at is None or now - self._last_workout_at > 2.0:
            return 0.0
        return self._cadence

    def speed_mph(self, now: float | None = None) -> float:
        now = self._clock() if now is None else now
        return self._motion.speed_mph if (self._motion and self.riding(now)) else 0.0

    def power_w(self, now: float | None = None) -> float:
        now = self._clock() if now is None else now
        return self._motion.power_w if (self._motion and self.riding(now)) else 0.0

    def heart_rate(self, now: float | None = None) -> int | None:
        now = self._clock() if now is None else now
        return self._hr if (self._hr_at is not None and now - self._hr_at <= 5.0) else None

    # ---- ride accounting ---------------------------------------------------------------------
    def _reset_ride(self) -> None:
        self._start_wall: dt.datetime | None = None
        self._e0 = self._e1 = 0
        self._d0 = self._d1 = 0.0
        self._active = 0.0
        self._sums = {"mph": 0.0, "w": 0.0, "rpm": 0.0, "lvl": 0.0, "hr": 0.0}
        self._hr_s = 0.0
        self._hist: dict[int, float] = {}
        self._max = {"mph": 0.0, "w": 0.0, "rpm": 0.0, "lvl": 0, "hr": 0}

    def _begin(self, now: float, m: Motion) -> None:
        if self._pending is not None and (now - self._pending_at >= self.merge_wait_s or self._pending.get("user") != self.user):
            self._expire_pending(now, force=True)  # the wait is over, or it was somebody else's ride: that ride is final, this one is a new ride
        self._reset_ride()
        self._ride_user = self.user
        self._open = True
        self._start_wall = self._wall()
        self._e0 = self._e1 = m.energy_count
        self._d0 = self._d1 = m.distance_mi

    def _accumulate(self, dt_s: float, m: Motion, now: float) -> None:
        self._active += dt_s
        cad = self.cadence(now)
        s = self._sums
        s["mph"] += m.speed_mph * dt_s
        s["w"] += m.power_w * dt_s
        s["rpm"] += cad * dt_s
        s["lvl"] += self.level * dt_s
        mx = self._max
        mx["mph"], mx["w"], mx["rpm"], mx["lvl"] = max(mx["mph"], m.speed_mph), max(mx["w"], m.power_w), max(mx["rpm"], cad), max(mx["lvl"], self.level)
        hr = self.heart_rate(now)
        if hr:
            s["hr"] += hr * dt_s
            self._hr_s += dt_s
            b = hr // 10 * 10
            self._hist[b] = self._hist.get(b, 0.0) + dt_s
            mx["hr"] = max(mx["hr"], hr)

    def summary(self, reason: str = "live") -> dict:
        a = self._active
        s = self._sums
        return {
            "ts_start": self._start_wall.isoformat(timespec="seconds") if self._start_wall else None,
            "ts_end": self._wall().isoformat(timespec="seconds"),
            "active_min": round(a / 60, 2),
            "kcal": self._e1 - self._e0,
            "miles": round(self._d1 - self._d0, 2),
            "avg_mph": round(s["mph"] / a, 1) if a else 0.0,
            "max_mph": round(self._max["mph"], 1),
            "avg_w": round(s["w"] / a) if a else 0,
            "max_w": round(self._max["w"]),
            "avg_rpm": round(s["rpm"] / a) if a else 0,
            "max_rpm": round(self._max["rpm"]),
            "avg_level": round(s["lvl"] / a, 1) if a else 0.0,
            "max_level": self._max["lvl"],
            "avg_hr": round(s["hr"] / self._hr_s) if self._hr_s >= 10 else None,
            "max_hr": self._max["hr"] or None,
            "hr_s": round(self._hr_s),
            "hr_hist": {str(k): round(v) for k, v in sorted(self._hist.items())},
            "reason": reason,
            "user": self._ride_user,
        }

    def finish(self, reason: str, now: float | None = None) -> dict | None:
        """Close the open ride and hold it as 'pending' for merge_wait_s: if another ride starts in that time the two are merged (so two short
        rides can add up to one that is long enough). Returns this ride's own summary. The final, merged ride is released by tick()/flush()."""
        now = self._clock() if now is None else now
        if not self._open:
            return None
        self._open = False
        self._last_move_at = None  # an ended ride is not "riding": a stray packet in the next 3 s must not open a phantom ride
        summ = self.summary(reason)
        summ["parts"] = 1
        if self._pending is not None and self._pending.get("user") != summ.get("user"):
            self._expire_pending(now, force=True)  # never merge two different people's rides
        self._pending = merge_summaries(self._pending, summ) if self._pending else summ
        self._pending_at = now
        return summ

    def _expire_pending(self, now: float, force: bool = False) -> None:
        if self._pending is None or self._open:
            return
        if not force and now - self._pending_at < self.merge_wait_s:
            return
        final, self._pending = self._pending, None
        if final["active_min"] * 60 >= self.min_active_s:  # the minimum applies to the MERGED ride
            self.last_summary = final
            self._finished.append(final)

    def flush(self, now: float | None = None) -> None:
        """Shutting down / unloading: close any open ride and make the pending one final right now (no waiting)."""
        now = self._clock() if now is None else now
        if self._open:
            self.finish("shutdown", now)
        self._expire_pending(now, force=True)

    def pop_finished(self) -> list[dict]:
        """Final rides (merged, long enough) released since the last call: the caller logs and prints them. Safe to call after every feed/tick."""
        out, self._finished = self._finished, []
        return out

    @property
    def ride_open(self) -> bool:
        return self._open

    def snapshot(self, now: float | None = None) -> dict:
        """Everything the entities show. Session values keep their last numbers after a ride ends, until the next ride starts."""
        now = self._clock() if now is None else now
        sess = self.summary()
        return {
            "riding": self.riding(now),
            "speed_mph": self.speed_mph(now),
            "power_w": self.power_w(now),
            "cadence_rpm": self.cadence(now),
            "level": self.level,
            "heart_rate": self.heart_rate(now),
            "session_kcal": sess["kcal"],
            "session_miles": sess["miles"],
            "session_minutes": sess["active_min"],
            "session_avg_hr": sess["avg_hr"],
            "ride_open": self._open,
            "user": self.user,
            "last_ride": self.last_summary,
        }


def merge_summaries(a: dict, b: dict) -> dict:
    """Combine two ride summaries (a first, b after it) into one: sums for totals, time-weighted averages, max of maxes, heart-rate histogram added."""
    ta, tb = a["active_min"], b["active_min"]
    tot = ta + tb

    def wavg(key, wa, wb, nd=0):
        if (a.get(key) is None and b.get(key) is None) or (wa + wb) == 0:
            return None
        va, vb = a.get(key), b.get(key)
        num = (va or 0) * (wa if va is not None else 0) + (vb or 0) * (wb if vb is not None else 0)
        den = (wa if va is not None else 0) + (wb if vb is not None else 0)
        return round(num / den, nd) if nd else round(num / den)

    hist: dict[str, int] = {}
    for h in (a.get("hr_hist") or {}, b.get("hr_hist") or {}):
        for k, v in h.items():
            hist[k] = hist.get(k, 0) + v
    hr_s = (a.get("hr_s") or 0) + (b.get("hr_s") or 0)
    return {
        "ts_start": a["ts_start"], "ts_end": b["ts_end"], "active_min": round(tot, 2), "kcal": a["kcal"] + b["kcal"], "miles": round(a["miles"] + b["miles"], 2),
        "avg_mph": wavg("avg_mph", ta, tb, 1) or 0.0, "max_mph": max(a["max_mph"], b["max_mph"]),
        "avg_w": wavg("avg_w", ta, tb) or 0, "max_w": max(a["max_w"], b["max_w"]),
        "avg_rpm": wavg("avg_rpm", ta, tb) or 0, "max_rpm": max(a["max_rpm"], b["max_rpm"]),
        "avg_level": wavg("avg_level", ta, tb, 1) or 0.0, "max_level": max(a["max_level"], b["max_level"]),
        "avg_hr": wavg("avg_hr", a.get("hr_s") or 0, b.get("hr_s") or 0), "max_hr": max([h for h in (a.get("max_hr"), b.get("max_hr")) if h], default=None),
        "hr_s": hr_s, "hr_hist": dict(sorted(hist.items(), key=lambda kv: int(kv[0]))), "reason": b["reason"], "parts": a.get("parts", 1) + b.get("parts", 1), "user": a.get("user"),
    }
