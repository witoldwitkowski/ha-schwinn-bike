# Schwinn 170/270 exercise bike for Home Assistant

[![Tests](https://github.com/witoldwitkowski/ha-schwinn-bike/actions/workflows/tests.yml/badge.svg)](https://github.com/witoldwitkowski/ha-schwinn-bike/actions/workflows/tests.yml)
[![Validate](https://github.com/witoldwitkowski/ha-schwinn-bike/actions/workflows/validate.yml/badge.svg)](https://github.com/witoldwitkowski/ha-schwinn-bike/actions/workflows/validate.yml)
[![HACS custom repository](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://hacs.xyz)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Live data and a ride log from the **Schwinn 170 / 270** exercise bikes (they advertise as `SCHWINN 170/270`; made by Nautilus), straight into Home Assistant over
Bluetooth. **Local push, no cloud, no phone app.** It uses Home Assistant's Bluetooth stack, so a built-in adapter or an
[ESPHome Bluetooth proxy](https://esphome.io/components/bluetooth_proxy.html) (set `active: true`) near the bike both work.

These bikes are **not** FTMS, so the usual fitness-machine integrations cannot talk to them. This integration speaks the bike's own protocol, which is documented in
[docs/PROTOCOL.md](docs/PROTOCOL.md): along the way it turned out the bike also reports speed, power, distance, calories **and which console user is riding**, none of
which the existing open-source decoders used.

## What you get
* **Live sensors:** speed, cadence, resistance level, power, heart rate, and the running totals of the current ride.
* **Rides, not just numbers:** it works out when a ride starts and ends, **merges rides that follow each other** (so two short sessions count as one), **throws away
  very short ones**, and appends each finished ride to a CSV.
* **An event when a ride finishes** (`schwinn_bike_ride_finished`) with the whole summary: use it for notifications, a printed receipt, a spreadsheet, Strava-style
  tracking, anything.
* **Several riders:** the bike's USER profiles are recognised, and every ride is logged under the right person.
* **Heart rate you can trust:** the handgrip sensor's zeros and bogus bursts are filtered out.

## Install
**HACS:** [![Open your Home Assistant instance and open this repository in HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=witoldwitkowski&repository=ha-schwinn-bike&category=integration)
or *HACS > Custom repositories > add `https://github.com/witoldwitkowski/ha-schwinn-bike` as an *Integration**, install, restart Home Assistant.
**Manual:** copy `custom_components/schwinn_bike` into `<config>/custom_components/` and restart.

**Set up:** press **Quick Start** on the console (the bike only advertises while awake). It then shows up under *Settings > Devices & services* as discovered, or use
*Add integration > Schwinn 170/270 Bike*. Close any phone app connected to the bike first: **it accepts one connection at a time**.

Requires Home Assistant 2026.10 or newer and a Bluetooth adapter or proxy with a decent signal to the bike (better than about -80 dBm).

## Entities
Device **SCHWINN 170/270**:

| Entity | Notes |
| --- | --- |
| Speed (mph), Cadence (rpm), Resistance level (1-25) | live; unavailable while the bike is not connected |
| Power (bike estimate), W | the bike's own number; nothing was available to calibrate it, so treat it as an estimate |
| Heart rate (bpm) | from the handgrips: empty when hands are off |
| Ride calories / distance / time | the current ride; they keep their last value until the next ride starts |
| Last ride calories | its attributes carry the whole summary (start/end, averages, maxima, heart-rate histogram) |
| Rider | the console user who is riding (known once a workout starts) |
| Moving (binary) | true while the pedal count advances |

## How a ride is detected
```
Quick Start ──► pedalling ──► (pause, coast ...) ──► End  ───────────────┐
                                                      or 3 min idle      │ ride closes
                                                                         ▼
              another ride by the SAME user within 3 min? ── yes ──► merge into it, wait again
                                       │ no
                                       ▼
              less than 5 min of actual pedalling? ── yes ──► discarded
                                       │ no
                                       ▼
              append to bike_log.csv  +  fire schwinn_bike_ride_finished
```
"Riding" means the pedal count is advancing; the bike's speed alone is not a signal (it freezes at its last value when the console is paused). The thresholds are
constants in `custom_components/schwinn_bike/const.py` (`MERGE_WAIT_S` 180, `MIN_RIDE_S` 300, `IDLE_DISCONNECT_S` 60).

The ride log is `<config>/weight/bike_log.csv`, one row per ride: `ts_start, ts_end, person, active_min, kcal, miles, avg_mph, max_mph, avg_w, max_w, avg_rpm,
max_rpm, avg_level, max_level, avg_hr, max_hr, hr_s, hr_hist, reason`. (`hr_hist` is JSON: seconds per 10 bpm bin.)

### Use the event
```yaml
automation:
  - alias: Bike ride finished
    triggers:
      - trigger: event
        event_type: schwinn_bike_ride_finished
    actions:
      - action: notify.notify
        data:
          message: >
            {{ trigger.event.data.person }}: {{ trigger.event.data.active_min | round(1) }} min,
            {{ trigger.event.data.kcal }} kcal, {{ trigger.event.data.miles }} mi
```
Event data: the same fields as the CSV (numbers, `hr_hist` as a dict) plus `person` and `parts` (how many sessions were merged).

## Several riders (console users)
The bike has user profiles (the **USER** button). It reports the active one **once a workout starts** (before that it sends nothing), and the integration maps that
number to a name. Set it under the integration's **Configure**: *Console users -> names*, default `1=user1, 2=user2, 3=user3`, for example `1=alice, 2=bob`.
Rides are logged and the event is fired under that name, and rides of different users are never merged. The *fallback name* is used when the bike does not say who is riding.

## Heart rate
The bike's heart rate comes from contact sensors in the handgrips. They read 0 when hands are off, and sometimes report a constant ~185 bpm that the console itself
also shows. The integration ignores zeros, and ignores values above the rider's maximum heart rate plus 5 or jumping more than 30 bpm from the recent median. The
maximum comes from an optional file `<config>/weight/people.json`:
```json
{"people": {"alice": {"birth_year": 1980}, "bob": {"max_hr": 200}}}
```
(`max_hr`, else `220 - age` from `birth_year`; unknown names use 200). The paths `weight/people.json` and `weight/bike_log.csv` are constants in `const.py`.

## Troubleshooting
| Symptom | Likely cause |
| --- | --- |
| The bike is never discovered | It is asleep: press Quick Start. It only advertises while awake. Check that a proxy or adapter can hear it (*Settings > Devices > Bluetooth > Advertisement monitor*). |
| Sensors say `unavailable` | Nothing is connected: the bike is asleep, or something else (a phone app) holds the single connection. |
| It connects and drops | Weak signal. A link at -84 dBm failed during service discovery; -60 dBm was solid. Move the proxy closer. |
| Heart rate is empty or jumps to 185 | Grip sensors: hands off reads 0, poor contact reads ~185. Both are filtered. |
| A short ride is missing from the log | Rides under 5 minutes of pedalling are discarded by design (`MIN_RIDE_S`). |
| Rider shows the wrong name | Check *Configure > Console users*: the number is the user's position in the console's list. |

## Limitations
* One bike, one connection; the bike must be awake for Home Assistant to find it.
* Distance and calories carry a small start offset in the bike's counters, so only their change during a ride is used. Calories are within about 2 of the console.
* Tested on one bike (firmware `R04.12.004P`, hardware `H44_R15`), resistance levels 1-23, rides up to about 10 minutes, Home Assistant 2026.10.
* **Other models or firmware?** `tools/capture_local.py` logs the raw packets from a computer's Bluetooth (see [docs/PROTOCOL.md](docs/PROTOCOL.md)); an issue with a capture
  and a photo of the console is the best way to extend support.

## Development
```bash
python3 -m unittest discover -s tests          # pure-Python tests: protocol, ride tracker, ride log
```
The tests that drive the real coordinator and config flow need Home Assistant importable (skipped otherwise). In a virtualenv:
```bash
pip install homeassistant bluetooth-adapters bleak-retry-connector habluetooth aiousbwatcher aiohasupervisor   # plus whatever the import errors ask for
python -m unittest discover -s tests
```
`tests/fixtures/ride_sample.jsonl` is a real, anonymized ride (raw packets with relative times only: about 9 minutes, levels 1-23, a sprint, a stop, an End) that the tests
replay through the tracker and the coordinator. Your own raw captures go in `captures/`, which is git-ignored because they hold ride and heart-rate data.

## Credits and licences
MIT licensed (see [LICENSE](LICENSE)). The protocol knowledge builds on the [TrackMyIndoorWorkout](https://github.com/TrackMyIndoorWorkout/TrackMyIndoorWorkout)
project (Apache-2.0, issue #240 and `schwinn_x70.dart`) and ursoft's `ANT_Libraries` ESP32 sketch; the motion channel, the console-user number and the pause/end
behaviour come from the captures described in [docs/PROTOCOL.md](docs/PROTOCOL.md). Not affiliated with Schwinn or Nautilus.
