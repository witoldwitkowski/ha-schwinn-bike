"""Schwinn 170/270 exercise bike over Bluetooth (local push, via any HA Bluetooth adapter or ESPHome proxy)."""

from __future__ import annotations

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .coordinator import SchwinnConfigEntry, SchwinnCoordinator

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BINARY_SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: SchwinnConfigEntry) -> bool:
    """Set up the bike from a config entry."""
    coordinator = SchwinnCoordinator(hass, entry)
    await coordinator.async_start()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    # NO entry.add_update_listener(): the options flow is an OptionsFlowWithReload, which reloads the entry itself, and Home Assistant raises
    # "Config entry update listeners should not be used with OptionsFlowWithReload" (found 2026-11-06 when saving the options)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: SchwinnConfigEntry) -> bool:
    """Unload: close any ride in progress (it gets logged) and release the bike's single connection."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_stop()
    return unloaded

