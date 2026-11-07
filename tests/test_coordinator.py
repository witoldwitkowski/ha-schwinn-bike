"""Drives the REAL SchwinnCoordinator with a fake Bluetooth client and replays the captured ride through its notification handlers.
Needs Home Assistant importable (run with a venv that has it); skipped otherwise."""
import asyncio
import csv
import glob
import json
import os
import sys
import tempfile
import types
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.normpath(os.path.join(HERE, "..")))
sys.path.insert(0, HERE)

try:
    from custom_components.schwinn_bike import coordinator as co
    from custom_components.schwinn_bike import protocol as pr
    from custom_components.schwinn_bike.tracker import RideTracker
    HAVE_HA = True
except ImportError:  # pragma: no cover
    HAVE_HA = False

CAPTURE = os.path.join(HERE, "fixtures", "ride_sample.jsonl")  # a real, anonymized ride (relative times only): joined mid-ride, ends with the console End packet


class FakeClient:
    def __init__(self):
        self.handlers, self.writes, self.disconnected = {}, [], False

    async def write_gatt_char(self, uuid, data, response=False):
        self.writes.append((uuid, bytes(data), response))

    async def start_notify(self, uuid, cb):
        self.handlers[uuid] = cb

    async def disconnect(self):
        self.disconnected = True


@unittest.skipUnless(HAVE_HA, "homeassistant not installed")
class CoordinatorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        loop = asyncio.get_running_loop()
        self.hass = mock.MagicMock()
        self.hass.loop = loop
        self.hass.config.path = lambda rel: os.path.join(self.tmp.name, rel)
        self.hass.async_add_executor_job = lambda fn, *a: loop.run_in_executor(None, fn, *a)
        self.hass.async_create_task = lambda coro: loop.create_task(coro)
        self.fired = []
        self.hass.bus.async_fire = lambda name, data: self.fired.append((name, data))
        self.bg = []
        self.entry = mock.MagicMock()
        self.entry.data = {"address": "aa:bb:cc:dd:ee:ff"}
        self.entry.options = {"rider": "alice", "user_map": "1=alice, 2=bob, 3=carol"}
        self.entry.title = "SCHWINN 170/270"
        self.entry.async_create_background_task = lambda hass, coro, name: self.bg.append(loop.create_task(coro))
        os.makedirs(os.path.join(self.tmp.name, "weight"))
        with open(os.path.join(self.tmp.name, "weight", "people.json"), "w") as f:
            json.dump({"people": {"alice": {"birth_year": 1980}, "bob": {"birth_year": 2010}, "carol": {"birth_year": 1975}}}, f)
        self.now = 1000.0
        self.clock = lambda: self.now

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def start(self):
        self.client = FakeClient()
        self.adv_cb = None

        def reg(hass, cb, matcher, mode):
            self.adv_cb = cb
            self.matcher, self.mode = matcher, mode
            return lambda: None

        patches = [
            mock.patch.object(co.bluetooth, "async_register_callback", reg),
            mock.patch.object(co, "async_track_time_interval", lambda hass, cb, iv: (lambda: None)),
            mock.patch.object(co, "establish_connection", mock.AsyncMock(return_value=self.client)),
            mock.patch.object(co, "time", types.SimpleNamespace(monotonic=self.clock)),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        c = co.SchwinnCoordinator(self.hass, self.entry)
        self.assertEqual(c.tracker.min_active_s, 300.0, "rides under 5 minutes of pedalling must be thrown away")
        c.tracker = RideTracker(clock=self.clock, min_active_s=co.MIN_RIDE_S)
        c.async_set_updated_data = lambda data: setattr(c, "data", data)
        await c.async_start()
        return c

    async def connect(self, c):
        info = mock.MagicMock()
        info.device = object()
        self.adv_cb(info, None)
        await asyncio.gather(*self.bg)
        self.bg.clear()

    async def test_connect_subscribes_and_starts_hr_stream(self):
        c = await self.start()
        self.assertEqual(self.mode, co.bluetooth.BluetoothScanningMode.ACTIVE)
        self.assertEqual(c.tracker.max_hr, 220 - (__import__("datetime").date.today().year - 1980))  # from people.json birth_year
        await self.connect(c)
        self.assertTrue(c.connected)
        self.assertEqual(self.client.writes, [(pr.CONTROL_UUID, pr.START_HR_STREAM, True)])
        self.assertEqual(set(self.client.handlers), {pr.WORKOUT_UUID, pr.HR_UUID, pr.MOTION_UUID})
        # a second advertisement while connected must not start another connection
        self.adv_cb(mock.MagicMock(), None)
        self.assertEqual(self.bg, [])

    async def test_replay_real_ride_logs_one_row_and_fires_event(self):
        if not os.path.exists(CAPTURE):
            self.skipTest("capture not present")
        c = await self.start()
        await self.connect(c)
        h = self.client.handlers
        speeds, riding_seen, paused_speed = [], [], []
        t0 = None
        with open(CAPTURE) as fh:
            lines = [json.loads(x) for x in fh]
        last_tick = 0
        for r in lines:
            self.now = 1000.0 + r["t"]
            while last_tick + 1 <= r["t"]:
                last_tick += 1
                self.now = 1000.0 + last_tick
                c._on_tick(None)
                speeds.append(c.data["speed_mph"])
                riding_seen.append(c.data["riding"])
            self.now = 1000.0 + r["t"]
            uuid = {"5c7d82a0": pr.WORKOUT_UUID, "6be8f580": pr.HR_UUID, "a46a4a80": pr.MOTION_UUID}.get(r["uuid"][:8])
            if uuid is None:  # e.g. 35ddd0a0: the bike's 4-byte ack of the HR-start command, which we do not subscribe to
                continue
            h[uuid](None, bytearray.fromhex(r["hex"]))
            await asyncio.sleep(0)
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "weight", "bike_log.csv")), "nothing is logged during the merge wait")
        self.now += 200  # nobody starts another ride: the wait ends, the ride becomes final
        c._on_tick(None)
        await asyncio.gather(*self.bg)
        self.assertTrue(any(s > 20 for s in speeds), "sprint should show >20 mph")
        self.assertTrue(any(riding_seen) and not all(riding_seen), "riding must go False during the stop/pause")
        # exactly one logged ride (the console 'ended' packet), with the right numbers
        with open(os.path.join(self.tmp.name, "weight", "bike_log.csv")) as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual((row["person"], row["reason"]), ("alice", "ended"))
        self.assertTrue(47 <= int(row["kcal"]) <= 53, row)
        self.assertTrue(1.9 <= float(row["miles"]) <= 2.3, row)
        self.assertTrue(110 <= int(row["avg_hr"]) <= 135 and int(row["max_hr"]) <= 177, row)
        self.assertEqual([n for n, _ in self.fired], ["schwinn_bike_ride_finished"])
        self.assertEqual(self.fired[0][1]["person"], "alice")
        # the ride totals stay visible after the ride, live values read 0
        self.assertEqual(c.data["session_kcal"], int(row["kcal"]))
        self.assertEqual(c.data["speed_mph"], 0.0)
        print("\nCOORDINATOR REPLAY ROW:", {k: row[k] for k in ("active_min", "kcal", "miles", "avg_mph", "avg_hr", "max_hr", "max_level")})

    async def test_ride_by_console_user_2_is_logged_to_that_user(self):
        """the sample ride with its packets rewritten to console user 2: the heart-rate packets carry user 2 in byte 7 and the End packet is 05 09 02 03 00
        (an earlier parser required user 1, so user 2's End was ignored)"""
        c = await self.start()
        year = __import__("datetime").date.today().year
        self.assertEqual((c.tracker.max_hr_by_user[1], c.tracker.max_hr_by_user[2], c.tracker.max_hr_by_user[3]), (220 - (year - 1980), 220 - (year - 2010), 220 - (year - 1975)))
        await self.connect(c)
        h = self.client.handlers
        with open(CAPTURE) as fh:
            lines = [json.loads(x) for x in fh if x.strip()]
        for r in lines:
            uuid = {"5c7d82a0": pr.WORKOUT_UUID, "6be8f580": pr.HR_UUID, "a46a4a80": pr.MOTION_UUID}.get(r["uuid"][:8])
            if uuid is None:
                continue
            raw = bytearray.fromhex(r["hex"])
            if uuid == pr.HR_UUID and len(raw) == 20:
                raw[7] = 2  # console user 2
            elif uuid == pr.WORKOUT_UUID and len(raw) == 5:
                raw[2] = 2
            self.now = 1000.0 + r["t"]
            h[uuid](None, raw)
        self.assertEqual(c.tracker.user, 2)
        c._on_tick(None)
        self.assertEqual(c.data["rider"], "bob")  # the live "Rider" sensor
        self.assertFalse(c.tracker.ride_open, "user 2's End packet must close the ride")
        self.now += 200
        c._on_tick(None)
        await asyncio.gather(*self.bg)
        with open(os.path.join(self.tmp.name, "weight", "bike_log.csv")) as f:
            rows = list(csv.DictReader(f))
        self.assertEqual([(r["person"], r["reason"]) for r in rows], [("bob", "ended")])
        self.assertEqual(self.fired[0][1]["person"], "bob")
        self.assertTrue(47 <= int(rows[0]["kcal"]) <= 53, rows[0])

    async def test_idle_disconnect_releases_link_and_cools_down(self):
        c = await self.start()
        await self.connect(c)
        h = self.client.handlers
        h[pr.HR_UUID](None, bytearray(20))
        self.now += 61  # silent for >60 s
        c._on_tick(None)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        self.assertTrue(self.client.disconnected)
        self.assertFalse(c.connected)
        # advertisement during the cooldown does nothing; after it, we reconnect
        self.adv_cb(mock.MagicMock(), None)
        self.assertEqual(self.bg, [])
        self.now += 130
        self.client.disconnected = False
        self.adv_cb(mock.MagicMock(), None)
        self.assertEqual(len(self.bg), 1)
        await asyncio.gather(*self.bg)

    async def test_silent_bike_mid_ride_keeps_the_ride_and_the_link(self):
        """live finding 2026-11-06: the bike goes silent when you stop pedalling. A pause must not split the ride or drop the link."""
        from test_tracker import Rig

        c = await self.start()
        await self.connect(c)
        c.tracker.min_active_s = 60  # this test uses a short ride; the 5-minute rule has its own test
        rig = Rig(tracker=c.tracker)
        rig.t = self.now
        rig.pedal(70)  # a ride is open
        self.now = rig.t
        c._last_packet = self.now
        self.assertTrue(c.tracker.ride_open)
        self.now += 100  # 100 s of total silence (hands off, not pedalling)
        c._on_tick(None)
        await asyncio.sleep(0)
        self.assertFalse(self.client.disconnected, "must stay connected while a ride is open")
        self.assertTrue(c.tracker.ride_open)
        self.now += 100  # 200 s without pedalling: the tracker's own idle rule closes the ride; it waits for the merge window
        c._on_tick(None)
        self.assertFalse(c.tracker.ride_open)
        self.assertIsNone(c.tracker.last_summary, "not final yet: a second ride might still start")
        # no ride is open any more and the link has been silent: the watchdog frees the proxy slot
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        self.assertTrue(self.client.disconnected)
        self.now += 200  # the merge wait ends: final
        c._on_tick(None)
        await asyncio.gather(*self.bg)
        self.assertEqual(c.tracker.last_summary["reason"], "idle")

    async def test_unexpected_drop_retries_soon_and_keeps_ride_open(self):
        c = await self.start()
        await self.connect(c)
        c.tracker._open = True  # a ride is in progress
        c._on_disconnected(object())
        self.assertFalse(c.connected)
        self.assertTrue(c.tracker.ride_open)  # NOT closed by a radio drop
        self.adv_cb(mock.MagicMock(), None)
        self.assertEqual(self.bg, [])  # too soon (5 s)
        self.now += 6
        self.bg.clear()
        self.adv_cb(mock.MagicMock(), None)
        self.assertEqual(len(self.bg), 1)
        await asyncio.gather(*self.bg)


@unittest.skipUnless(HAVE_HA, "homeassistant not installed")
class SetupEntryTests(unittest.IsolatedAsyncioTestCase):
    async def test_setup_registers_no_update_listener(self):
        """Home Assistant rejects update listeners together with OptionsFlowWithReload (saving the options raised a 500 in a live test)"""
        import custom_components.schwinn_bike as integration

        hass, entry = mock.MagicMock(), mock.MagicMock()
        hass.config_entries.async_forward_entry_setups = mock.AsyncMock()
        coordinator = mock.MagicMock()
        coordinator.async_start = mock.AsyncMock()
        with mock.patch.object(integration, "SchwinnCoordinator", return_value=coordinator):
            self.assertTrue(await integration.async_setup_entry(hass, entry))
        entry.add_update_listener.assert_not_called()
        self.assertIs(entry.runtime_data, coordinator)


@unittest.skipUnless(HAVE_HA, "homeassistant not installed")
class ConfigFlowTests(unittest.IsolatedAsyncioTestCase):
    def _flow(self):
        from custom_components.schwinn_bike import config_flow as cf

        flow = cf.SchwinnConfigFlow()
        flow.hass = mock.MagicMock()
        flow.context = {}
        flow.async_set_unique_id = mock.AsyncMock()
        flow._abort_if_unique_id_configured = lambda **k: None
        flow._async_current_ids = lambda include_ignore=False: set()
        return cf, flow

    def _info(self):
        info = mock.MagicMock()
        info.name, info.address, info.service_uuids = "SCHWINN 170/270", "AA:BB:CC:DD:EE:FF", [pr.SERVICE_UUID]
        return info

    async def test_user_step_lists_awake_bike_and_creates_entry(self):
        cf, flow = self._flow()
        with mock.patch.object(cf, "async_discovered_service_info", lambda hass, connectable=True: [self._info()]):
            form = await flow.async_step_user()
            self.assertEqual((form["type"].value, form["step_id"]), ("form", "user"))
            done = await flow.async_step_user({"address": "AA:BB:CC:DD:EE:FF", "rider": "alice"})
        self.assertEqual(done["type"].value, "create_entry")
        self.assertEqual(done["data"], {"address": "AA:BB:CC:DD:EE:FF"})
        self.assertEqual(done["options"], {"rider": "alice", "user_map": co.DEFAULT_USER_MAP})

    async def test_user_step_aborts_when_no_bike_awake(self):
        cf, flow = self._flow()
        with mock.patch.object(cf, "async_discovered_service_info", lambda hass, connectable=True: []):
            res = await flow.async_step_user()
        self.assertEqual((res["type"].value, res["reason"]), ("abort", "no_devices_found"))

    async def test_bluetooth_discovery_confirm(self):
        cf, flow = self._flow()
        res = await flow.async_step_bluetooth(self._info())
        self.assertEqual((res["type"].value, res["step_id"]), ("form", "bluetooth_confirm"))
        done = await flow.async_step_bluetooth_confirm({"rider": "alice"})
        self.assertEqual((done["type"].value, done["title"]), ("create_entry", "SCHWINN 170/270"))


if __name__ == "__main__":
    unittest.main()
