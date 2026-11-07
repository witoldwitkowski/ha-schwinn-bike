#!/usr/bin/env python3
"""Passive: log every CHANGE in the Schwinn 170/270's Bluetooth advertisement (manufacturer data, name, service data) with timestamps.
Never connects, so it cannot disturb Home Assistant. Used to find out which advertisement byte (if any) reflects console state such as the selected user.
    python watch_adverts.py --seconds 600
"""
import argparse
import asyncio
import datetime as dt
import time

SERVICE = "3bf58980-3a2f-11e6-9011-0002a5d5c51b"


async def main(a):
    from bleak import BleakScanner

    last = {}
    t0 = time.monotonic()

    def cb(dev, adv):
        if SERVICE not in [u.lower() for u in adv.service_uuids] and not (dev.name or "").startswith("SCHWINN"):
            return
        mfr = {k: v.hex() for k, v in adv.manufacturer_data.items()}
        key = (adv.local_name, tuple(sorted(mfr.items())), tuple(sorted((k, v.hex()) for k, v in adv.service_data.items())))
        if key != last.get("k"):
            last["k"] = key
            print(f"{dt.datetime.now():%H:%M:%S} +{time.monotonic() - t0:5.0f}s rssi={adv.rssi} name={adv.local_name} mfr={mfr} svc_data={ {k: v.hex() for k, v in adv.service_data.items()} }", flush=True)
        last["seen"] = time.monotonic()

    async with BleakScanner(cb):
        end = time.monotonic() + a.seconds
        while time.monotonic() < end:
            await asyncio.sleep(5)
            if time.monotonic() - last.get("seen", t0) > 15 and not last.get("gone"):
                last["gone"] = True
                print(f"{dt.datetime.now():%H:%M:%S} (no advertisement for 15 s: bike asleep or connected to something)", flush=True)
            elif time.monotonic() - last.get("seen", 0) < 15:
                last["gone"] = False


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=int, default=600)
    asyncio.run(main(ap.parse_args()))
