"""Binary sensor: is somebody pedalling right now."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import SchwinnConfigEntry
from .entity import SchwinnEntity


async def async_setup_entry(hass: HomeAssistant, entry: SchwinnConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    async_add_entities([SchwinnRiding(entry.runtime_data)])


class SchwinnRiding(SchwinnEntity, BinarySensorEntity):
    _attr_name = "Riding"
    _attr_device_class = BinarySensorDeviceClass.MOVING

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "riding")

    @property
    def is_on(self) -> bool:
        return bool(self.coordinator.data["riding"])
