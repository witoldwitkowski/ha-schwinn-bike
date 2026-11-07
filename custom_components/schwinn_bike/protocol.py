"""Schwinn 170/270 (Nautilus) Bluetooth protocol: UUIDs and packet parsers. Pure Python, no Home Assistant imports, so it is unit-testable offline.

Protocol notes (reverse engineered; see docs/PROTOCOL.md): the bike is not FTMS. The `a46a4a80` motion channel (speed, power, distance, calories) and the
console-user number were found by live capture and checked against the console display.
"""
from __future__ import annotations

from dataclasses import dataclass

SERVICE_UUID = "3bf58980-3a2f-11e6-9011-0002a5d5c51b"
CONTROL_UUID = "1717b3c0-9803-11e3-90e1-0002a5d5c51b"  # write the HR-stream start command here
WORKOUT_UUID = "5c7d82a0-9803-11e3-8a6c-0002a5d5c51b"  # 17-byte workout packets
HR_UUID = "6be8f580-9803-11e3-ab03-0002a5d5c51b"  # 20-byte packets, HR when b15 == 0x5A
START_HR_STREAM = bytes([0x05, 0x03, 0xD9, 0x00, 0x1F])

TICKS_PER_S = 1024
TIME_WRAP = 65536
CRANK_WRAP = 65536
CAL_DIVISOR = 2097152  # LE48 of b10..b15 / this = kcal

@dataclass(frozen=True)
class Workout:
    cranks: float  # revolutions, 1/8 rev resolution
    ticks: int  # device clock, 1/1024 s, wraps at 65536
    kcal: float  # cumulative console calories
    resistance: int  # 1..25


def parse_workout(data: bytes) -> Workout | None:
    """17-byte packet starting 11 20 00. Anything else (e.g. the 5-byte 05 09 01 01 00 at workout start) returns None."""
    if len(data) != 17 or data[0] != 0x11 or data[1] != 0x20 or data[2] != 0x00:
        return None
    cranks = data[3] / 256 + data[4] + data[5] * 256
    ticks = data[8] | (data[9] << 8)
    kcal = int.from_bytes(data[10:16], "little") / CAL_DIVISOR
    return Workout(cranks, ticks, kcal, data[16])


MOTION_UUID = "a46a4a80-9803-11e3-8f3c-0002a5d5c51b"  # 14-byte packets, ~10 Hz, undocumented elsewhere (found by live capture 2026-11-06)


@dataclass(frozen=True)
class Motion:
    """The bike's own speed / power / distance / calories-candidate (14-byte packet on MOTION_UUID).

    Layout: b0 seq, b1 unknown (not a simple checksum), then fixed-position tags: b2=0x61 speed LE16 (b3,b4) in 0.01 mph,
    b5=0x60 power LE16 (b6,b7) in 0.01 W, b8=0x3d LE16 (b9,b10) = calories-candidate (counts, ~kcal), b11=0x3e LE16 (b12,b13) = distance in 0.01 mi.
    Distance and calories carry a start offset (about 7 and 1 at workout start): use DIFFERENCES within a ride.
    Speed verified vs console (3 points, within ~5%); distance verified by integrating speed (0.0099 mi/count); power vs community table within ~5%;
    calories UNCONFIRMED (console 5/10/29 vs counts 7/11/~30).
    """

    speed_mph: float
    power_w: float
    energy_count: int
    distance_mi: float


def parse_motion(data: bytes) -> Motion | None:
    if len(data) != 14 or data[2] != 0x61 or data[5] != 0x60 or data[8] != 0x3D or data[11] != 0x3E:
        return None
    return Motion(
        speed_mph=(data[3] | data[4] << 8) / 100,
        power_w=(data[6] | data[7] << 8) / 100,
        energy_count=data[9] | data[10] << 8,
        distance_mi=(data[12] | data[13] << 8) / 100,
    )


def parse_state(data: bytes) -> tuple[int, int] | None:
    """The 5-byte workout packet `05 09 <user> <state> 00` -> (console user number, state). State 1 = workout started, 3 = ended.
    (Byte 2 is the console USER, found by live capture: 1, 2, 3 ... as selected on the console. It looks like a constant if only one user is ever used.)"""
    if len(data) != 5 or data[0] != 0x05 or data[1] != 0x09 or data[4] != 0x00:
        return None
    return data[2], data[3]


def parse_user(data: bytes) -> int | None:
    """Console user number from ANY 20-byte packet on the HR characteristic (byte 7), also when no heart rate is detected."""
    if len(data) != 20 or data[15] != 0x5A or data[7] == 0:
        return None
    return data[7]


def parse_user_map(text: str) -> dict[int, str]:
    """'1=alice, 2=bob' -> {1: 'alice', 2: 'bob'} (console user number -> the name used in the ride log and events). Bad pieces are skipped."""
    out: dict[int, str] = {}
    for part in (text or "").replace(";", ",").split(","):
        k, _, v = part.partition("=")
        k, v = k.strip(), v.strip().lower()
        if k.isdigit() and v:
            out[int(k)] = v
    return out


def parse_hr(data: bytes) -> int | None:
    """20-byte packet; HR is b16 only when b15 == 0x5A. 0 means 'no reading'."""
    if len(data) != 20 or data[15] != 0x5A or data[16] == 0:
        return None
    return data[16]
