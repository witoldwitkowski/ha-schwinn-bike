#!/usr/bin/env python3
"""Log the raw packets of a Schwinn 170/270 using THIS computer's Bluetooth (bleak). No Home Assistant involved.

    pip install bleak
    python tools/capture_local.py --seconds 300 [--label user2]

What it does: finds the bike (it only advertises while awake: press Quick Start or pedal), connects, lists the services, reads every readable
characteristic, writes the start-HR-stream command, subscribes to every notifiable characteristic of the Schwinn service and writes each raw packet to
captures/<timestamp>[-label]-local.jsonl. Once a second it prints the decoded values (the same decoder the integration uses) so you can compare them with the console.

The bike accepts ONE connection: close phone apps and pause the Home Assistant integration first. At weak signal (below about -80 dBm) the link drops
during service discovery: put the computer next to the bike.
"""
import argparse
import asyncio
import datetime as dt
import importlib
import json
import os
import sys
import time
import types

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.normpath(os.path.join(HERE, "..", "custom_components", "schwinn_bike"))


def load(name):
    """import the integration's pure modules without running its __init__ (which needs Home Assistant)"""
    if "schwinn_pkg" not in sys.modules:
        pkg = types.ModuleType("schwinn_pkg")
        pkg.__path__ = [PKG]
        sys.modules["schwinn_pkg"] = pkg
    return importlib.import_module(f"schwinn_pkg.{name}")


pr = load("protocol")
tr = load("tracker")


async def main(a):
    from bleak import BleakClient, BleakScanner

    print("scanning for the bike (wake it: Quick Start / pedal) ...", flush=True)
    dev = await BleakScanner.find_device_by_filter(
        lambda d, adv: pr.SERVICE_UUID in [u.lower() for u in adv.service_uuids] or (d.name or "").startswith("SCHWINN"), timeout=a.scan_timeout
    )
    if not dev:
        sys.exit("bike not found - is it awake?")
    print(f"found {dev.name} {dev.address}", flush=True)
    os.makedirs(os.path.join(HERE, "..", "captures"), exist_ok=True)
    label = f"-{a.label}" if a.label else ""
    path = os.path.join(HERE, "..", "captures", dt.datetime.now().strftime("%Y%m%d-%H%M%S") + label + "-local.jsonl")
    out = open(path, "w", buffering=1)  # line-buffered: the file is always current
    t0 = time.monotonic()
    tracker = tr.RideTracker(clock=time.monotonic, min_active_s=0, merge_wait_s=0)
    motion = {"m": None}
    seen, cnt = {}, {}

    def make_cb(uuid):
        def cb(_, data):
            now = time.monotonic()
            b = bytes(data)
            out.write(json.dumps({"t": round(now - t0, 3), "wall": dt.datetime.now().isoformat(timespec="milliseconds"), "uuid": uuid, "hex": b.hex()}) + "\n")
            k = (uuid[:8], len(b), b[0] if b else None)
            seen[k] = seen.get(k, 0) + 1
            cnt[uuid[:8]] = cnt.get(uuid[:8], 0) + 1
            if uuid == pr.WORKOUT_UUID:
                w = pr.parse_workout(b)
                st = pr.parse_state(b)
                if w:
                    tracker.feed_workout(w, now)
                elif st:
                    tracker.feed_state(st[1], now, user=st[0])
            elif uuid == pr.HR_UUID:
                tracker.feed_user(pr.parse_user(b))
                tracker.feed_hr(pr.parse_hr(b), now)
            elif uuid == pr.MOTION_UUID:
                m = pr.parse_motion(b)
                if m:
                    motion["m"] = m
                    tracker.feed_motion(m, now)

        return cb

    async with BleakClient(dev, timeout=a.connect_timeout) as cl:
        print("CONNECTED", flush=True)
        for s in cl.services:
            print(f"service {s.uuid}")
            for c in s.characteristics:
                print(f"   char {c.uuid} props={','.join(c.properties)}")
                if "read" in c.properties:
                    try:
                        v = bytes(await cl.read_gatt_char(c))
                        print(f"      value {v.hex()}  {v.decode('ascii', 'replace') if v and all(32 <= x < 127 for x in v) else ''}")
                        out.write(json.dumps({"t": round(time.monotonic() - t0, 3), "wall": dt.datetime.now().isoformat(timespec="milliseconds"), "read": c.uuid.lower(), "hex": v.hex()}) + "\n")
                    except Exception as e:  # noqa: BLE001
                        print(f"      read failed: {e}")
        await cl.write_gatt_char(pr.CONTROL_UUID, pr.START_HR_STREAM, response=True)
        for c in next(s for s in cl.services if s.uuid.lower() == pr.SERVICE_UUID).characteristics:
            if "notify" in c.properties or "indicate" in c.properties:
                await cl.start_notify(c, make_cb(c.uuid.lower()))
        print(f"logging to {os.path.normpath(path)} for {a.seconds}s. START PEDALING NOW.", flush=True)
        end = time.monotonic() + a.seconds
        while time.monotonic() < end and cl.is_connected:
            await asyncio.sleep(1)
            now = time.monotonic()
            m = motion["m"]
            bike = f"SPD={m.speed_mph:5.2f}mph PWR={m.power_w:6.1f}W DIST={m.distance_mi:4.2f}mi ENERGY={m.energy_count:3d}" if m else "SPD=- (no motion packet yet)"
            s = tracker.summary()
            print(
                f"{dt.datetime.now():%H:%M:%S} user={tracker.user or '-'} cad={tracker.cadence(now):5.1f}rpm lvl={tracker.level:2d} hr={tracker.heart_rate(now) or '-':>3} "
                f"ride={s['active_min']:.2f}min {s['kcal']}kcal {s['miles']}mi | {bike}",
                flush=True,
            )
        print("\npacket kinds seen (char, length, byte0) -> count:")
        for k, v in sorted(seen.items(), key=lambda kv: -kv[1]):
            print("  ", k, v)
    out.close()
    print("disconnected")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=int, default=300)
    ap.add_argument("--scan-timeout", type=float, default=30)
    ap.add_argument("--connect-timeout", type=float, default=30)
    ap.add_argument("--label", default="", help="added to the file name, e.g. the console user")
    asyncio.run(main(ap.parse_args()))
