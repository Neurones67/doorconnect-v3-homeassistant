"""Buttons: open the door strike / gate, refresh the camera still image."""
from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import DoorConnectConfigEntry
from .const import LOCKS
from .local import DeviceError


async def async_setup_entry(
    hass: HomeAssistant, entry: DoorConnectConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    entities: list[ButtonEntity] = [OpenButton(entry, lock, key) for lock, key in LOCKS.items()]
    entities.append(SnapshotButton(entry))
    async_add_entities(entities)


class _Base(ButtonEntity):
    _attr_has_entity_name = True

    def __init__(self, entry: DoorConnectConfigEntry, key: str) -> None:
        self._data = entry.runtime_data
        self._attr_translation_key = key
        self._attr_unique_id = f"{entry.unique_id}_{key}"
        self._attr_device_info = self._data.device_info


class OpenButton(_Base):
    _attr_icon = "mdi:door-open"

    def __init__(self, entry: DoorConnectConfigEntry, lock: int, key: str) -> None:
        super().__init__(entry, f"open_{key}")
        self._lock = lock

    async def async_press(self) -> None:
        try:
            await self._data.device.open_door(lock=self._lock)
        except DeviceError as err:
            raise HomeAssistantError(f"The door station refused to open: {err}") from err


class SnapshotButton(_Base):
    _attr_icon = "mdi:camera-retake"

    def __init__(self, entry: DoorConnectConfigEntry) -> None:
        super().__init__(entry, "refresh_snapshot")

    async def async_press(self) -> None:
        camera = self._data.camera
        if camera is None:
            raise HomeAssistantError("Camera not initialized yet")
        await camera.async_refresh_snapshot()
