"""Cloud history coordinator: polls the event log and fires events for new entries."""
from __future__ import annotations

import datetime as dt
import logging
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .cloud import CloudAuthError, CloudError, QvCloud
from .const import DEFAULT_HISTORY_INTERVAL, DOMAIN, EVENT_DOORCONNECT, OPT_HISTORY_INTERVAL

_LOGGER = logging.getLogger(__name__)

_TIME_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y/%m/%d %H:%M:%S")


def parse_time(value: str) -> dt.datetime | None:
    """Cloud timestamps are local time, without a time zone."""
    for fmt in _TIME_FORMATS:
        try:
            naive = dt.datetime.strptime(value[:19], fmt)
        except (ValueError, TypeError):
            continue
        return naive.replace(tzinfo=dt_util.get_default_time_zone())
    return None


class HistoryCoordinator(DataUpdateCoordinator[list[dict[str, Any]]]):
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, cloud: QvCloud) -> None:
        minutes = entry.options.get(OPT_HISTORY_INTERVAL, DEFAULT_HISTORY_INTERVAL)
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} history",
            update_interval=timedelta(minutes=minutes),
        )
        self.cloud = cloud
        self._seen: set[str] | None = None  # None = first poll, fire nothing

    async def _async_update_data(self) -> list[dict[str, Any]]:
        try:
            events = await self.cloud.async_get_history()
        except CloudAuthError as err:
            # Do not retry: the Qvii account gets locked after a few failures.
            raise ConfigEntryAuthFailed(str(err)) from err
        except CloudError as err:
            raise UpdateFailed(str(err)) from err

        for ev in events:
            ev["datetime"] = parse_time(ev["time"])

        if self._seen is not None:
            fresh = [ev for ev in events if ev["id"] not in self._seen]
            for ev in sorted(fresh, key=lambda e: e["time"]):
                self.hass.bus.async_fire(EVENT_DOORCONNECT, {
                    k: (v.isoformat() if isinstance(v, dt.datetime) else v) for k, v in ev.items()
                })
        self._seen = (self._seen or set()) | {ev["id"] for ev in events}
        return events
