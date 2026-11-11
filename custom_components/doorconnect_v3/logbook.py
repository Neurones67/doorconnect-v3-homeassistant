"""Show cloud history events in the Home Assistant logbook."""
from __future__ import annotations

from collections.abc import Callable

from homeassistant.components.logbook.const import LOGBOOK_ENTRY_MESSAGE, LOGBOOK_ENTRY_NAME
from homeassistant.core import Event, HomeAssistant, callback

from .const import DOMAIN, EVENT_DOORCONNECT


@callback
def async_describe_events(
    hass: HomeAssistant,
    async_describe_event: Callable[[str, str, Callable[[Event], dict[str, str]]], None],
) -> None:
    @callback
    def describe(event: Event) -> dict[str, str]:
        data = event.data
        msg = data.get("description", "event")
        if data.get("time"):
            msg += f" (at {str(data['time'])[11:16]})"
        return {LOGBOOK_ENTRY_NAME: "Door station", LOGBOOK_ENTRY_MESSAGE: msg}

    async_describe_event(DOMAIN, EVENT_DOORCONNECT, describe)
