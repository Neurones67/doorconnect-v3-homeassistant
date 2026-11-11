"""Integration tests with a simulated door station and cloud (no network access)."""
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant import config_entries
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.doorconnect_v3.cloud import parse_records
from custom_components.doorconnect_v3.const import DOMAIN, EVENT_DOORCONNECT

INFO = {"mac": "34:17:36:00:00:01", "version": "V401.R001.A331", "model": "IDS94E6W"}

HISTORY_XML = """<?xml version="1.0" encoding="UTF-8"?><envelope><header><result>0</result></header>
<content><maxid>99</maxid><record>
<data><id>3</id><time>2026-11-10 13:44:41</time><event>19</event><alarmsourcename>Door 1</alarmsourcename>
<recordsubresurl>https://example.invalid/snap.jpg</recordsubresurl></data>
<data><id>2</id><time>2026-11-10 12:00:00</time><event>63</event><alarmstate>5</alarmstate><alarminfo>1</alarminfo>
<alarmsourcename>Door 1</alarmsourcename></data>
<data><id>1</id><time>2026-11-09 20:59:00</time><event>63</event><alarmstate>4</alarmstate><alarminfo>2</alarminfo></data>
</record></content></envelope>"""


def test_parse_records():
    ev = parse_records(HISTORY_XML)
    assert [e["type"] for e in ev] == ["ring", "unlock", "unlock"]
    assert ev[1]["description"] == "Door strike opened (RFID badge)"
    assert ev[2]["description"] == "Gate opened (app)"
    assert ev[0]["snapshot_url"].endswith("snap.jpg")


@pytest.fixture
def mocks(socket_enabled):
    with (
        patch("custom_components.doorconnect_v3.local.LocalDevice.product_info", AsyncMock(return_value=INFO)),
        patch("custom_components.doorconnect_v3.local.LocalDevice.open_door", AsyncMock()) as open_door,
        patch("custom_components.doorconnect_v3.cloud.QvCloud.async_oauth", AsyncMock(return_value="tok")),
        patch("custom_components.doorconnect_v3.cloud.QvCloud.async_get_history",
              AsyncMock(return_value=parse_records(HISTORY_XML))) as history,
    ):
        yield open_door, history


async def _setup(hass: HomeAssistant):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: "192.0.2.10", "device_password": "123456"})
    assert result["step_id"] == "cloud"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"cloud_user": "a@b.c", "cloud_password": "x"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    return result["result"]


async def test_setup_entities_and_open(hass: HomeAssistant, mocks):
    open_door, _ = mocks
    entry = await _setup(hass)
    assert entry.state is config_entries.ConfigEntryState.LOADED

    states = {s.entity_id: s for s in hass.states.async_all()}
    print(sorted(states))
    assert "button.door_station_open_door_strike" in states
    btn = "button.door_station_open_door_strike"
    await hass.services.async_call("button", "press", {"entity_id": btn}, blocking=True)
    open_door.assert_awaited_once_with(lock=1)

    last_unlock = next(s for e, s in states.items() if e.startswith("sensor.") and "unlock" in e)
    assert last_unlock.state.startswith("2026-11-10T")
    assert last_unlock.attributes["method"] == "rf_badge"

    cam = next(e for e in states if e.startswith("camera."))
    assert entry.runtime_data.server.url.startswith("http://127.0.0.1:")
    from homeassistant.components.camera import async_get_stream_source
    assert await async_get_stream_source(hass, cam) == entry.runtime_data.server.url

    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_new_events_fired(hass: HomeAssistant, mocks):
    _, history = mocks
    entry = await _setup(hass)
    fired = []
    hass.bus.async_listen(EVENT_DOORCONNECT, lambda ev: fired.append(ev.data))
    new = parse_records(HISTORY_XML.replace("<id>3</id>", "<id>4</id>"))
    history.return_value = new
    await entry.runtime_data.history.async_refresh()
    await hass.async_block_till_done()
    assert len(fired) == 1 and fired[0]["type"] == "ring"
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_bad_device_code(hass: HomeAssistant):
    from custom_components.doorconnect_v3.local import DeviceAuthError
    with patch("custom_components.doorconnect_v3.local.LocalDevice.product_info",
               AsyncMock(side_effect=DeviceAuthError("x"))):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: "192.0.2.10", "device_password": "000000"})
    assert result["errors"] == {"base": "invalid_auth"}


def test_parse_records_tolerates_sloppy_xml() -> None:
    """BOM, bare '&' in snapshot URLs and control characters must not break the history."""
    from custom_components.doorconnect_v3.cloud import parse_records

    txt = (
        "﻿\n<?xml version=\"1.0\" encoding=\"UTF-8\"?><envelope><header><result>0</result>"
        "</header><content><record><data><id>42</id><time>2026-11-11 13:00:00</time>"
        "<event>19</event><alarmsourcename>Door\x01 1</alarmsourcename>"
        "<recordresurl>https://x.example/a.jpg?k=1&t=2</recordresurl></data></record></content>"
        "</envelope>"
    )
    events = parse_records(txt)
    assert len(events) == 1
    assert events[0]["type"] == "ring"
    assert events[0]["snapshot_url"] == "https://x.example/a.jpg?k=1&t=2"
