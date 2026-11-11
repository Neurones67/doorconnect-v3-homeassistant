"""Door station camera, opened on demand only.

- Live stream: `stream_source` points to the integration's local HTTP server, which only
  opens the TCP 34567 session while a player (go2rtc/WebRTC or HLS) is connected.
- Still image: the last known picture is returned; it is refreshed during live viewing,
  with the "Refresh image" button, or once if no picture exists yet.
  This prevents a dashboard card from opening a session every 10 s (the device accepts
  a single session at a time and the app would be locked out).
"""
from __future__ import annotations

import asyncio
import logging
import time

from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import DoorConnectConfigEntry
from .media import MediaError, keyframe_to_jpeg

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: DoorConnectConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    camera = DoorCamera(entry)
    entry.runtime_data.camera = camera
    async_add_entities([camera])


class DoorCamera(Camera):
    _attr_has_entity_name = True
    _attr_translation_key = "doorstation"
    _attr_supported_features = CameraEntityFeature.STREAM

    def __init__(self, entry: DoorConnectConfigEntry) -> None:
        super().__init__()
        self._data = entry.runtime_data
        self._attr_unique_id = f"{entry.unique_id}_camera"
        self._attr_device_info = self._data.device_info
        self._jpeg: bytes | None = None
        self._jpeg_ts = 0.0
        self._tried_initial = False
        self._lock = asyncio.Lock()

    @property
    def is_streaming(self) -> bool:
        return self._data.hub.streaming

    async def stream_source(self) -> str | None:
        return self._data.server.url

    async def _encode(self, h264: bytes) -> None:
        jpeg = await self.hass.async_add_executor_job(keyframe_to_jpeg, h264)
        if jpeg:
            self._jpeg = jpeg
            self._jpeg_ts = time.monotonic()

    async def async_refresh_snapshot(self) -> None:
        async with self._lock:
            try:
                await self._encode(await self._data.hub.async_get_keyframe())
            except (MediaError, asyncio.TimeoutError) as err:
                raise HomeAssistantError(f"Could not get a picture: {err}") from err
        self.async_write_ha_state()

    async def async_camera_image(self, width: int | None = None, height: int | None = None) -> bytes | None:
        hub = self._data.hub
        # While live, reuse the keyframes flowing through (at most every 5 s).
        if hub.last_keyframe and hub.last_keyframe_ts > self._jpeg_ts + 5:
            async with self._lock:
                await self._encode(hub.last_keyframe)
        elif self._jpeg is None and not self._tried_initial:
            self._tried_initial = True
            try:
                await self.async_refresh_snapshot()
            except HomeAssistantError as err:
                _LOGGER.warning("%s", err)
        return self._jpeg
