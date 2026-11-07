# Schwinn 170/270 Bluetooth protocol (reverse engineered)

Observed on one bike that advertises as `SCHWINN 170/270` (Device Information: manufacturer `Nautilus, Inc.`, firmware `R04.12.004P`, hardware `H44_R15`).
The bike is **not** FTMS. Everything below was read from live captures and checked against the console display; the verification notes say how well.

## Connecting
* Service `3bf58980-3a2f-11e6-9011-0002a5d5c51b`. The bike advertises (connectable) **only while awake**: press Quick Start or pedal.
* **One connection at a time.** A phone app, nRF Connect, or Home Assistant holding the link blocks everyone else.
* Signal: a link at about -84 dBm connected and then dropped during service discovery; -58 dBm and -59 dBm were fine. Keep the radio close.
* The Bluetooth advertisement (`manufacturer data 0x2E40: 71 69 aa 8b 0? 80`) does **not** reflect the selected console user.
* After connecting, write `05 03 D9 00 1F` (with response) to `1717b3c0-9803-11e3-90e1-0002a5d5c51b` (`length, seq, checksum (sum to 0), 00, command 1F`) to start the
  heart-rate stream, then subscribe to the notify characteristics below. The bike answers the command on `35ddd0a0-...` with `03 1f 00 00` (ignored).
* Nothing is streamed until a workout is started on the console, and it is silent again after End.

## Notify characteristics
| Characteristic (suffix `-9803-11e3-...-0002a5d5c51b` unless noted) | Packet | Meaning |
| --- | --- | --- |
| `5c7d82a0-...8a6c` | 17 B `11 20 00 ...` | **Workout**: cranks `b3/256 + b4 + b5*256` (1/8 revolution per packet), clock `LE16(b8,b9)` in 1/1024 s (wraps every 64 s), level `b16` (1-25). `LE48(b10..b15)` is a calorie-like counter that **drifts high: do not use**. Sent only while pedalling. |
| `5c7d82a0-...8a6c` | 5 B `05 09 <user> <state> 00` | `<user>` = console user number (1, 2, 3 ...), `<state>` = 1 workout started, 3 workout ended. |
| `a46a4a80-...8f3c` | 14 B, ~10 Hz | **Motion** (found by capture): `b2=0x61` speed `LE16(b3,b4)` in 0.01 mph; `b5=0x60` power `LE16(b6,b7)` in 0.01 W; `b8=0x3d` energy count `LE16(b9,b10)` (about kcal); `b11=0x3e` distance `LE16(b12,b13)` in 0.01 mile. `b0` is a sequence number, `b1` is unknown. Energy and distance carry a start offset: use differences within a ride. |
| `6be8f580-...ab03` | 20 B | **Heart rate and user**: valid when `b15 == 0x5A`; heart rate `b16` (0 = no reading); `b7` = console user number (also when no heart rate is detected); `b18` repeats the level; `b3` is a checksum; `b13` is a seconds counter. |
| `b8066ec0`, `d57cda20` | | subscribed, never sent anything |

Other readable characteristics: `7241b880` (20 B) and `5ec4e520` (4 B, write+read) are **running counters**, not user identifiers (they changed after a ride and increment between reads).

## Behaviour
* **Pause** freezes speed and power at their last value (not zero) and stops the 17-byte workout packets; counters freeze; the connection stays up and the other channels keep streaming.
* **End** sends the 5-byte packet with state 3 and all streams go quiet. The console then shows a cool-down screen with the totals.
* **Plain stop** (no button): speed goes to 0 within a few seconds and the workout packets stop. The bike can also go completely silent after some time without pedalling (seen: no packets for a minute): do not treat silence as "disconnected".
* **So "riding" means the crank count advances**, never "speed above zero".
* A new workout (Quick Start) restarts the energy and distance counters.
* The heart-rate stream comes from handgrip contact sensors: zeros when hands are off (about 40% of the time in a typical ride) and bursts of about 185 bpm that the console itself displays are **bogus**. Filter them (limit around the user's maximum heart rate plus a margin, and reject jumps of more than 30 bpm from the recent median).

## Verification (two sessions, about 9 minutes of pedalling plus later rides, levels 1-23)
* Speed within about 3% of the console at three different levels; cadence, level and elapsed time match.
* Distance: integrating the speed channel over a ride matches the distance counter's change (0.0099 mile per count); the console shows tenths of a mile, rounded down.
* Calories: the energy count is within 0-2 of the console's calories over 53 kcal. The raw counter in the workout packet is not.
* Power: the bike's own value cannot be checked against the console (no watts shown); it ran about 0.97 of a community polynomial estimate (0.83-1.15 depending on level). Treat as an estimate.
* Heart rate: identical to the console, with the grip caveats above.
* Console users: user numbers 1, 2 and 3 were each seen in both the heart-rate packets and the start/end packet, matching the user selected on the console.
* Not tested: levels 24-25, rides over about 10 minutes, other firmware.

## Capturing packets yourself
`tools/capture_local.py` connects from a computer with Bluetooth (needs `pip install bleak`), reads every readable characteristic, subscribes to every notification,
writes raw packets to `captures/*.jsonl` and prints decoded values once a second. `tools/watch_adverts.py` logs advertisement changes without connecting.

## Credits
The workout and heart-rate packet layouts, the start-HR command and the idea that this bike is not FTMS come from the
[TrackMyIndoorWorkout](https://github.com/TrackMyIndoorWorkout/TrackMyIndoorWorkout) project (Apache-2.0, issue #240 and `schwinn_x70.dart`) and ursoft's
`ANT_Libraries` ESP32 sketch. The motion channel, the user number and the pause/end behaviour were found with the captures described here.
