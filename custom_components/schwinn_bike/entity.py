"""Shared base entity."""

from __future__ import annotations

from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import SchwinnCoordinator


class SchwinnEntity(CoordinatorEntity[SchwinnCoordinator]):
    _attr_has_entity_name = True

    def __init__(self, coordinator: SchwinnCoordinator, key: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.address}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.address)},
            connections={(CONNECTION_BLUETOOTH, coordinator.address)},
            manufacturer="Nautilus, Inc.",
            model="SCHWINN 170/270",
            name=coordinator.config_entry.title,
        )
