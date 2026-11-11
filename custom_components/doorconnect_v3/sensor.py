"""History sensors (cloud): last event, last unlock, last call."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import DoorConnectConfigEntry
from .coordinator import HistoryCoordinator

HISTORY_ATTR_LEN = 25
_PUBLIC = ("description", "type", "output", "method", "badge", "source", "channel", "snapshot_url")


@dataclass(frozen=True, kw_only=True)
class HistorySensorDescription(SensorEntityDescription):
    select: Callable[[dict[str, Any]], bool]
    as_timestamp: bool = True


SENSORS = (
    HistorySensorDescription(key="last_event", translation_key="last_event", icon="mdi:history",
                             select=lambda e: True, as_timestamp=False),
    HistorySensorDescription(key="last_unlock", translation_key="last_unlock", icon="mdi:door-open",
                             device_class=SensorDeviceClass.TIMESTAMP, select=lambda e: e["type"] == "unlock"),
    HistorySensorDescription(key="last_ring", translation_key="last_ring", icon="mdi:bell-ring",
                             device_class=SensorDeviceClass.TIMESTAMP, select=lambda e: e["type"] == "ring"),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: DoorConnectConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    coordinator = entry.runtime_data.history
    if coordinator is None:
        return
    async_add_entities(HistorySensor(entry, coordinator, d) for d in SENSORS)


class HistorySensor(CoordinatorEntity[HistoryCoordinator], SensorEntity):
    _attr_has_entity_name = True
    _unrecorded_attributes = frozenset({"history"})

    def __init__(self, entry: DoorConnectConfigEntry, coordinator: HistoryCoordinator,
                 description: HistorySensorDescription) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry.unique_id}_{description.key}"
        self._attr_device_info = entry.runtime_data.device_info

    def _event(self) -> dict[str, Any] | None:
        return next((e for e in self.coordinator.data or [] if self.entity_description.select(e)), None)

    @property
    def native_value(self):
        ev = self._event()
        if ev is None:
            return None
        return ev["datetime"] if self.entity_description.as_timestamp else ev["description"]

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        ev = self._event()
        if ev is None:
            return None
        attrs = {k: ev[k] for k in _PUBLIC if ev.get(k) is not None}
        attrs["time"] = ev["datetime"].isoformat() if ev["datetime"] else ev["time"]
        if self.entity_description.key == "last_event":
            attrs["history"] = [
                {"time": e["datetime"].isoformat() if e["datetime"] else e["time"],
                 "description": e["description"], "type": e["type"],
                 **({"method": e["method"]} if e["method"] else {}),
                 **({"output": e["output"]} if e["output"] else {})}
                for e in (self.coordinator.data or [])[:HISTORY_ATTR_LEN]
            ]
        return attrs
