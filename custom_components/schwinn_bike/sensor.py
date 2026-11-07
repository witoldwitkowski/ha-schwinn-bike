"""Sensors: live values (available while connected) and the current/last ride's totals."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import UnitOfLength, UnitOfPower, UnitOfSpeed, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.typing import StateType

from .coordinator import SchwinnConfigEntry
from .entity import SchwinnEntity


@dataclass(frozen=True, kw_only=True)
class SchwinnSensorDescription(SensorEntityDescription):
    value_fn: Callable[[dict[str, Any]], StateType]
    live: bool = False  # live values are unavailable while the bike is not connected; ride totals keep their last value


SENSORS: tuple[SchwinnSensorDescription, ...] = (
    SchwinnSensorDescription(
        key="speed", name="Speed", live=True, device_class=SensorDeviceClass.SPEED, native_unit_of_measurement=UnitOfSpeed.MILES_PER_HOUR,
        state_class=SensorStateClass.MEASUREMENT, suggested_display_precision=1, value_fn=lambda d: d["speed_mph"],
    ),
    SchwinnSensorDescription(
        key="cadence", name="Cadence", live=True, native_unit_of_measurement="rpm", state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0, icon="mdi:rotate-right", value_fn=lambda d: d["cadence_rpm"],
    ),
    SchwinnSensorDescription(
        key="level", name="Resistance level", live=True, state_class=SensorStateClass.MEASUREMENT, icon="mdi:stairs-up",
        value_fn=lambda d: d["level"] or None,
    ),
    SchwinnSensorDescription(
        key="power", name="Power (bike estimate)", live=True, device_class=SensorDeviceClass.POWER, native_unit_of_measurement=UnitOfPower.WATT,
        state_class=SensorStateClass.MEASUREMENT, suggested_display_precision=0, value_fn=lambda d: d["power_w"],
    ),
    SchwinnSensorDescription(
        key="heart_rate", name="Heart rate", live=True, native_unit_of_measurement="bpm", state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:heart-pulse", value_fn=lambda d: d["heart_rate"],
    ),
    SchwinnSensorDescription(
        key="session_calories", name="Ride calories", native_unit_of_measurement="kcal", state_class=SensorStateClass.TOTAL_INCREASING,
        icon="mdi:fire", value_fn=lambda d: d["session_kcal"],
    ),
    SchwinnSensorDescription(
        key="session_distance", name="Ride distance", device_class=SensorDeviceClass.DISTANCE, native_unit_of_measurement=UnitOfLength.MILES,
        state_class=SensorStateClass.TOTAL_INCREASING, suggested_display_precision=2, value_fn=lambda d: d["session_miles"],
    ),
    SchwinnSensorDescription(
        key="session_time", name="Ride time", device_class=SensorDeviceClass.DURATION, native_unit_of_measurement=UnitOfTime.MINUTES,
        state_class=SensorStateClass.TOTAL_INCREASING, suggested_display_precision=1, value_fn=lambda d: d["session_minutes"],
    ),
    SchwinnSensorDescription(
        key="rider", name="Rider", icon="mdi:account", value_fn=lambda d: (d["rider"] or "").title() or None,
    ),
    SchwinnSensorDescription(
        key="last_ride_calories", name="Last ride calories", native_unit_of_measurement="kcal", icon="mdi:history",
        value_fn=lambda d: (d["last_ride"] or {}).get("kcal"),
    ),
)


async def async_setup_entry(hass: HomeAssistant, entry: SchwinnConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    coordinator = entry.runtime_data
    async_add_entities(SchwinnSensor(coordinator, d) for d in SENSORS)


class SchwinnSensor(SchwinnEntity, SensorEntity):
    entity_description: SchwinnSensorDescription

    def __init__(self, coordinator, description: SchwinnSensorDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> StateType:
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def available(self) -> bool:
        return self.coordinator.connected or not self.entity_description.live

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.entity_description.key == "last_ride_calories":
            return self.coordinator.data["last_ride"]  # the whole summary (times, averages, heart-rate histogram ...)
        return None
