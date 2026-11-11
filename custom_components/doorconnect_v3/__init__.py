"""Home Assistant integration for the Philips WelcomeEye Connect 3 door station (DoorConnect app)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_create_clientsession, async_get_clientsession
from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC, DeviceInfo

from .cloud import QvCloud
from .const import (
    CONF_CLOUD_CLIENT_ID,
    CONF_CLOUD_PASSWORD,
    CONF_CLOUD_USER,
    CONF_DEVICE_PASSWORD,
    CONF_MEDIA_CLIENT_ID,
    CONF_STREAM_TOKEN,
    DEFAULT_STREAM_BIND,
    DEFAULT_STREAM_PORT,
    DOMAIN,
    OPT_STREAM_BIND,
    OPT_STREAM_PORT,
)
from .coordinator import HistoryCoordinator
from .local import DeviceError, LocalDevice
from .media import MediaHub, MediaServer

PLATFORMS = [Platform.BUTTON, Platform.CAMERA, Platform.SENSOR]


@dataclass
class DoorConnectData:
    device: LocalDevice
    hub: MediaHub
    server: MediaServer
    history: HistoryCoordinator | None
    device_info: DeviceInfo
    camera: Any = None  # DoorCamera, set by the camera platform


type DoorConnectConfigEntry = ConfigEntry[DoorConnectData]


async def async_setup_entry(hass: HomeAssistant, entry: DoorConnectConfigEntry) -> bool:
    host = entry.data[CONF_HOST]
    password = entry.data[CONF_DEVICE_PASSWORD]
    device = LocalDevice(async_get_clientsession(hass, verify_ssl=False), host, password)

    try:
        info = await device.product_info()
    except DeviceError as err:
        raise ConfigEntryNotReady(f"Door station unreachable: {err}") from err

    device_info = DeviceInfo(
        identifiers={(DOMAIN, entry.unique_id or host)},
        connections={(CONNECTION_NETWORK_MAC, info["mac"])} if info["mac"] else set(),
        manufacturer="Philips / Extel (OEM Qualvision)",
        model=info["model"],
        name="Door station",
        sw_version=info["version"],
    )

    hub = MediaHub(host, password, entry.data[CONF_MEDIA_CLIENT_ID], device.streamkey)
    server = MediaServer(
        hub,
        entry.data[CONF_STREAM_TOKEN],
        entry.options.get(OPT_STREAM_BIND, DEFAULT_STREAM_BIND),
        entry.options.get(OPT_STREAM_PORT, DEFAULT_STREAM_PORT),
    )
    await server.async_start()

    history = None
    if entry.data.get(CONF_CLOUD_USER):
        cloud = QvCloud(
            async_create_clientsession(hass, verify_ssl=False),
            entry.data[CONF_CLOUD_USER],
            entry.data[CONF_CLOUD_PASSWORD],
            entry.data[CONF_CLOUD_CLIENT_ID],
        )
        history = HistoryCoordinator(hass, entry, cloud)

    entry.runtime_data = DoorConnectData(device, hub, server, history, device_info)

    async def _cleanup() -> None:
        await hub.async_close()
        await server.async_stop()

    entry.async_on_unload(_cleanup)
    entry.async_on_unload(entry.add_update_listener(_async_reload))

    if history:
        # Non-blocking history: if the cloud is down, everything else still works.
        await history.async_refresh()

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: DoorConnectConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_reload(hass: HomeAssistant, entry: DoorConnectConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
