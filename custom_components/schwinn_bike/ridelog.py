"""Append finished rides to a CSV that the weekly receipt reads. Pure Python.

One row per ride. `hr_hist` is JSON ({"110": seconds, ...} in 10 bpm bins) so heart-rate zones are computed at REPORT time from the current max HR.
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import os

FIELDS = [
    "ts_start", "ts_end", "person", "active_min", "kcal", "miles", "avg_mph", "max_mph", "avg_w", "max_w", "avg_rpm", "max_rpm",
    "avg_level", "max_level", "avg_hr", "max_hr", "hr_s", "hr_hist", "reason",
]


def row_from_summary(person: str, s: dict) -> dict:
    row = {k: s.get(k) for k in FIELDS}
    row["person"] = person
    row["hr_hist"] = json.dumps(s.get("hr_hist") or {}, separators=(",", ":"))
    for k in ("avg_hr", "max_hr"):
        row[k] = "" if row[k] is None else row[k]
    return row


def append_row(path: str, row: dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    new = not os.path.exists(path) or os.path.getsize(path) == 0
    with open(path, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerow(row)


def read_rows(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def summary_from_row(row: dict) -> dict | None:
    """A logged CSV row back into the summary shape the tracker/entities use (numbers, not strings); None if the row is unusable."""
    try:
        dt.datetime.fromisoformat(row["ts_start"])  # a damaged line (no real timestamp) is not a ride
        dt.datetime.fromisoformat(row["ts_end"])

        def num(k, cast=float, default=0):
            v = row.get(k)
            return cast(v) if v not in (None, "") else default

        return {
            "ts_start": row["ts_start"], "ts_end": row["ts_end"], "person": row.get("person") or None,
            "active_min": num("active_min"), "kcal": int(num("kcal")), "miles": num("miles"), "avg_mph": num("avg_mph"), "max_mph": num("max_mph"),
            "avg_w": int(num("avg_w")), "max_w": int(num("max_w")), "avg_rpm": int(num("avg_rpm")), "max_rpm": int(num("max_rpm")),
            "avg_level": num("avg_level"), "max_level": int(num("max_level")),
            "avg_hr": int(num("avg_hr")) if row.get("avg_hr") else None, "max_hr": int(num("max_hr")) if row.get("max_hr") else None,
            "hr_s": int(num("hr_s")), "hr_hist": json.loads(row.get("hr_hist") or "{}"), "reason": row.get("reason") or "",
        }
    except (KeyError, ValueError, TypeError):
        return None


def last_logged(path: str) -> dict | None:
    """The most recent ride in the log (any person), so the 'Last ride' sensor survives a restart."""
    best = None
    for row in read_rows(path):
        s = summary_from_row(row)
        if s and (best is None or dt.datetime.fromisoformat(s["ts_start"]) > dt.datetime.fromisoformat(best["ts_start"])):
            best = s
    return best
