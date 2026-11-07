"""Connection manager + data coordinator for the Schwinn 170/270 bike.

Push only: connect when an advertisement shows the bike awake, subscribe to the three notify characteristics, feed a RideTracker, and push a
snapshot to the entities once a second. Drops the link after IDLE_DISCONNECT_S without any packet so a Bluetooth proxy slot is not held.
The bike accepts ONE connection: close nRF Connect / the phone app while this runs.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import time
from datetime import timedelta
from typing import Any

from bleak.backends.device import BLEDevice
from bleak.exc import BleakError
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection

from homeassistant.components import bluetooth
from homeassistant.components.bluetooth.match import ADDRESS, CONNECTABLE, BluetoothCallbackMatcher
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS, EVENT_HOMEASSISTANT_STOP
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from . import protocol as pr
from .const import (
    BIKE_LOG,
    CONF_RIDER,
    CONF_USER_MAP,
    DEFAULT_RIDER,
    DEFAULT_USER_MAP,
    DOMAIN,
    EVENT_RIDE_FINISHED,
    FAILED_CONNECT_RETRY_S,
    IDLE_DISCONNECT_S,
    MERGE_WAIT_S,
    MIN_RIDE_S,
    PEOPLE_FILE,
    QUICK_RETRY_S,
    RECONNECT_COOLDOWN_S,
)
from .ridelog import append_row, last_logged, row_from_summary
from .tracker import RideTracker

_LOGGER = logging.getLogger(__name__)

type SchwinnConfigEntry = ConfigEntry[SchwinnCoordinator]


class SchwinnCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Owns the Bluetooth link and the ride tracker for one bike."""

    config_entry: SchwinnConfigEntry

    def __init__(self, hass: HomeAssistant, entry: SchwinnConfigEntry) -> None:
        super().__init__(hass, _LOGGER, config_entry=entry, name=entry.title, update_interval=None)
        self.address: str = entry.data[CONF_ADDRESS].upper()
        self.rider: str = entry.options.get(CONF_RIDER, DEFAULT_RIDER)
        self.user_map: dict[int, str] = pr.parse_user_map(entry.options.get(CONF_USER_MAP, DEFAULT_USER_MAP))
        self.tracker = RideTracker(min_active_s=MIN_RIDE_S, merge_wait_s=MERGE_WAIT_S)
        self.connected = False
        self._client: BleakClientWithServiceCache | None = None
        self._connecting = False
        self._intentional_disconnect = False
        self._next_attempt = 0.0
        self._last_packet = 0.0
        self._unsub: list = []
        self.data = self._snapshot()

    # ---- lifecycle ---------------------------------------------------------------------------
    async def async_start(self) -> None:
        # the "Last ride" sensor must not read Unknown after a restart: reload the newest logged ride
        self.tracker.last_summary = await self.hass.async_add_executor_job(last_logged, self.hass.config.path(BIKE_LOG))
        default, by_user = await self.hass.async_add_executor_job(self._load_max_hrs)
        self.tracker.configure_max_hr(default, by_user)
        self._unsub.append(
            bluetooth.async_register_callback(
                self.hass,
                self._on_advertisement,
                BluetoothCallbackMatcher({ADDRESS: self.address, CONNECTABLE: True}),
                bluetooth.BluetoothScanningMode.ACTIVE,
            )
        )
        self._unsub.append(async_track_time_interval(self.hass, self._on_tick, timedelta(seconds=1)))
        self._unsub.append(self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, self._async_on_ha_stop))

    async def async_stop(self) -> None:
        for unsub in self._unsub:
            unsub()
        self._unsub.clear()
        self._intentional_disconnect = True
        await self._async_flush()
        client, self._client = self._client, None
        self.connected = False
        if client is not None:
            try:
                await client.disconnect()
            except (BleakError, TimeoutError, OSError):
                pass

    async def _async_flush(self) -> None:
        """Unloading / shutting down: close the ride and release the pending one now (no merge wait) so nothing is lost."""
        self.tracker.flush()
        for summary in self.tracker.pop_finished():
            await self._async_log_ride(summary)

    async def _async_on_ha_stop(self, _event: Event) -> None:
        await self._async_flush()

    def _rider_for(self, summary_or_state: dict) -> str:
        """person id for a ride / the current console user: the console user number mapped through user_map, else the configured fallback rider"""
        return self.user_map.get(summary_or_state.get("user"), self.rider)

    def _load_max_hrs(self) -> tuple[int, dict[int, int]]:
        """Max HR (heart-rate filter limit) per console user from people.json (`max_hr`, else 220 - age from `birth_year`). Unknown person: 200 (permissive:
        the filter's jump rule still removes the bogus bursts). The fallback for no user info is the fallback rider's value, else 177."""
        try:
            with open(self.hass.config.path(PEOPLE_FILE)) as f:
                people = json.load(f).get("people", {})
        except (OSError, ValueError, TypeError):
            people = {}

        def hr_for(pid: str, unknown: int) -> int:
            person = people.get(pid, {})
            try:
                if person.get("max_hr"):
                    return int(person["max_hr"])
                if person.get("birth_year"):
                    return 220 - (dt.date.today().year - int(person["birth_year"]))
            except (ValueError, TypeError):
                pass
            return unknown

        by_user = {u: hr_for(pid, 200) for u, pid in self.user_map.items()}
        return hr_for(self.rider, 177), by_user

    # ---- connection --------------------------------------------------------------------------
    @callback
    def _on_advertisement(self, service_info: bluetooth.BluetoothServiceInfoBleak, change: bluetooth.BluetoothChange) -> None:
        if self._client is not None or self._connecting or time.monotonic() < self._next_attempt:
            return
        self._connecting = True
        self.config_entry.async_create_background_task(self.hass, self._async_connect(service_info.device), f"{DOMAIN}_connect_{self.address}")

    def _ble_device(self) -> BLEDevice | None:
        return bluetooth.async_ble_device_from_address(self.hass, self.address, connectable=True)

    async def _async_connect(self, device: BLEDevice) -> None:
        client: BleakClientWithServiceCache | None = None
        try:
            client = await establish_connection(
                BleakClientWithServiceCache,
                device,
                self.name,
                disconnected_callback=self._on_disconnected,
                max_attempts=3,
                ble_device_callback=self._ble_device,
            )
            await client.write_gatt_char(pr.CONTROL_UUID, pr.START_HR_STREAM, response=True)
            await client.start_notify(pr.WORKOUT_UUID, self._on_workout)
            await client.start_notify(pr.HR_UUID, self._on_hr)
            await client.start_notify(pr.MOTION_UUID, self._on_motion)
        except (BleakError, TimeoutError, OSError) as err:
            _LOGGER.debug("Could not connect to the bike %s: %s", self.address, err)
            self._next_attempt = time.monotonic() + (QUICK_RETRY_S if self.tracker.ride_open else FAILED_CONNECT_RETRY_S)
            if client is not None:
                try:
                    await client.disconnect()
                except (BleakError, TimeoutError, OSError):
                    pass
        else:
            self._client = client
            self.connected = True
            self._last_packet = time.monotonic()
            _LOGGER.debug("Connected to the bike %s", self.address)
            self._push(force=True)
        finally:
            self._connecting = False

    @callback
    def _on_disconnected(self, client: Any) -> None:
        self._client = None
        self.connected = False
        if self._intentional_disconnect:
            self._intentional_disconnect = False
        else:
            # link dropped (range, interference): retry soon; an open ride stays open because the bike's counters carry on
            self._next_attempt = time.monotonic() + QUICK_RETRY_S
        self._push(force=True)

    # ---- notifications (run in the event loop) -----------------------------------------------
    @callback
    def _on_workout(self, _: Any, data: bytearray) -> None:
        self._last_packet = time.monotonic()
        raw = bytes(data)
        workout = pr.parse_workout(raw)
        if workout is not None:
            self.tracker.feed_workout(workout)
        elif (state := pr.parse_state(raw)) is not None:
            self.tracker.feed_state(state[1], user=state[0])  # <user> <state>: 1 = workout started, 3 = ended
        self._drain()

    @callback
    def _on_hr(self, _: Any, data: bytearray) -> None:
        self._last_packet = time.monotonic()
        raw = bytes(data)
        self.tracker.feed_user(pr.parse_user(raw))
        self.tracker.feed_hr(pr.parse_hr(raw))

    @callback
    def _on_motion(self, _: Any, data: bytearray) -> None:
        self._last_packet = time.monotonic()
        motion = pr.parse_motion(bytes(data))
        if motion is not None:
            self.tracker.feed_motion(motion)
            self._drain()

    # ---- ticking, finished rides, entity data ------------------------------------------------
    @callback
    def _on_tick(self, _now: dt.datetime) -> None:
        self.tracker.tick()
        self._drain()
        # silent link: free the proxy slot, but NEVER while a ride is open (the bike goes quiet when you stop pedalling; a pause must not split the ride
        # and we want to be there for the resume and the End packet). An open ride ends by itself after the tracker's idle_end_s (3 min).
        if self._client is not None and not self.tracker.ride_open and time.monotonic() - self._last_packet > IDLE_DISCONNECT_S:
            self.hass.async_create_task(self._async_disconnect_idle())
        self._push()

    async def _async_disconnect_idle(self) -> None:
        client, self._client = self._client, None
        if client is None:
            return
        self._intentional_disconnect = True
        self._next_attempt = time.monotonic() + RECONNECT_COOLDOWN_S
        self.connected = False
        try:
            await client.disconnect()
        except (BleakError, TimeoutError, OSError):
            pass
        self._push(force=True)

    @callback
    def _drain(self) -> None:
        for summary in self.tracker.pop_finished():
            self.config_entry.async_create_background_task(self.hass, self._async_log_ride(summary), f"{DOMAIN}_log_ride")

    async def _async_log_ride(self, summary: dict) -> None:
        path = self.hass.config.path(BIKE_LOG)
        rider = self._rider_for(summary)
        self.tracker.last_summary = {**summary, "person": rider}
        row = row_from_summary(rider, summary)
        try:
            await self.hass.async_add_executor_job(append_row, path, row)
        except OSError as err:
            _LOGGER.error("Could not write the ride to %s: %s", path, err)
        _LOGGER.info("Ride finished (%s): %s min, %s kcal, %s mi", summary["reason"], summary["active_min"], summary["kcal"], summary["miles"])
        self.hass.bus.async_fire(EVENT_RIDE_FINISHED, {**summary, "person": rider})

    def _snapshot(self) -> dict[str, Any]:
        snap = self.tracker.snapshot()
        # display: only a rider the bike has actually reported (before a workout starts the bike sends no user: show Unknown, not the logging fallback)
        rider = self.user_map.get(snap["user"]) if snap["user"] else None
        return {**snap, "rider": rider, "connected": self.connected}

    @callback
    def _push(self, force: bool = False) -> None:
        snap = self._snapshot()
        if force or snap != self.data:
            self.async_set_updated_data(snap)
