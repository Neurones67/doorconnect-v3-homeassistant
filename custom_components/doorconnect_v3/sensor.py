"""History sensors (cloud): last event, last unlock, last call."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorEntityDescription
from homeassistant.core import HomeAssistant, callback
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


class HistorySensor(  # pyright: ignore[reportIncompatibleVariableOverride]  ("available": HA typing quirk)
    CoordinatorEntity[HistoryCoordinator], SensorEntity
):
    _attr_has_entity_name = True
    _unrecorded_attributes = frozenset({"history"})

    def __init__(self, entry: DoorConnectConfigEntry, coordinator: HistoryCoordinator,
                 description: HistorySensorDescription) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._desc = description  # typed access to select/as_timestamp
        self._attr_unique_id = f"{entry.unique_id}_{description.key}"
        self._attr_device_info = entry.runtime_data.device_info
        self._update_attrs()

    @callback
    def _handle_coordinator_update(self) -> None:
        self._update_attrs()
        super()._handle_coordinator_update()

    def _update_attrs(self) -> None:
        events = self.coordinator.data or []
        ev = next((e for e in events if self._desc.select(e)), None)
        if ev is None:
            self._attr_native_value = None
            self._attr_extra_state_attributes = {}
            return
        self._attr_native_value = (
            ev["datetime"] if self._desc.as_timestamp else ev["description"]
        )
        attrs: dict[str, Any] = {k: ev[k] for k in _PUBLIC if ev.get(k) is not None}
        attrs["time"] = ev["datetime"].isoformat() if ev["datetime"] else ev["time"]
        if self._desc.key == "last_event":
            attrs["history"] = [
                {"time": e["datetime"].isoformat() if e["datetime"] else e["time"],
                 "description": e["description"], "type": e["type"],
                 **({"method": e["method"]} if e["method"] else {}),
                 **({"output": e["output"]} if e["output"] else {})}
                for e in events[:HISTORY_ATTR_LEN]
            ]
        self._attr_extra_state_attributes = attrs
