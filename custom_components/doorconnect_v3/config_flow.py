"""UI configuration: door station (local) first, then an optional cloud account (history)."""
from __future__ import annotations

import os
import secrets
import uuid
from collections.abc import Mapping
from typing import Any

import probatio as vol  # Home Assistant >= 2026.9 validates schemas with probatio

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_HOST
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_create_clientsession, async_get_clientsession

from .cloud import APP_ID, CLIENT_TYPE, CloudAuthError, CloudError, QvCloud
from .const import (
    CONF_CLOUD_CLIENT_ID,
    CONF_CLOUD_PASSWORD,
    CONF_CLOUD_USER,
    CONF_DEVICE_PASSWORD,
    CONF_MEDIA_CLIENT_ID,
    CONF_STREAM_TOKEN,
    DEFAULT_HISTORY_INTERVAL,
    DEFAULT_STREAM_BIND,
    DEFAULT_STREAM_PORT,
    DOMAIN,
    OPT_HISTORY_INTERVAL,
    OPT_STREAM_BIND,
    OPT_STREAM_PORT,
)
from .local import DeviceAuthError, DeviceError, LocalDevice

CLOUD_SCHEMA = vol.Schema({
    vol.Optional(CONF_CLOUD_USER, default=""): str,
    vol.Optional(CONF_CLOUD_PASSWORD, default=""): str,
})


class DoorConnectConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self._title = "Door station"

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            device = LocalDevice(
                async_get_clientsession(self.hass, verify_ssl=False),
                user_input[CONF_HOST],
                user_input[CONF_DEVICE_PASSWORD],
            )
            try:
                info = await device.product_info()
            except DeviceAuthError:
                errors["base"] = "invalid_auth"
            except DeviceError:
                errors["base"] = "cannot_connect"
            else:
                await self.async_set_unique_id(info["mac"] or user_input[CONF_HOST])
                self._abort_if_unique_id_configured(updates={CONF_HOST: user_input[CONF_HOST]})
                self._title = f"Door station {info['model'] or ''}".strip()
                self._data = {
                    **user_input,
                    CONF_MEDIA_CLIENT_ID: os.urandom(8).hex(),
                    CONF_CLOUD_CLIENT_ID: f"00{CLIENT_TYPE}-{APP_ID}-{uuid.uuid4()}",
                    CONF_STREAM_TOKEN: secrets.token_urlsafe(24),
                }
                return await self.async_step_cloud()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required(CONF_HOST): str,
                vol.Required(CONF_DEVICE_PASSWORD): str,
            }),
            errors=errors,
        )

    async def _check_cloud(self, user: str, pwd: str) -> str | None:
        cloud = QvCloud(async_create_clientsession(self.hass, verify_ssl=False), user, pwd, "")
        try:
            await cloud.async_resolve_servers()  # never raises: falls back to default servers
            await cloud.async_oauth()  # single attempt: the account gets locked after a few failures
        except CloudAuthError:
            return "invalid_cloud_auth"
        except CloudError:
            return "cloud_unreachable"
        return None

    async def async_step_cloud(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            user = user_input.get(CONF_CLOUD_USER, "").strip()
            pwd = user_input.get(CONF_CLOUD_PASSWORD, "")
            if user and pwd:
                if err := await self._check_cloud(user, pwd):
                    errors["base"] = err
                else:
                    self._data[CONF_CLOUD_USER] = user
                    self._data[CONF_CLOUD_PASSWORD] = pwd
            if not errors:
                return self.async_create_entry(title=self._title, data=self._data)

        return self.async_show_form(step_id="cloud", data_schema=CLOUD_SCHEMA, errors=errors)

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()
        if user_input is not None:
            user = user_input[CONF_CLOUD_USER].strip()
            if err := await self._check_cloud(user, user_input[CONF_CLOUD_PASSWORD]):
                errors["base"] = err
            else:
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_CLOUD_USER: user,
                                         CONF_CLOUD_PASSWORD: user_input[CONF_CLOUD_PASSWORD]})
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({
                vol.Required(CONF_CLOUD_USER, default=entry.data.get(CONF_CLOUD_USER, "")): str,
                vol.Required(CONF_CLOUD_PASSWORD): str,
            }),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return DoorConnectOptionsFlow()


class DoorConnectOptionsFlow(OptionsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        opts = self.config_entry.options
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({
                vol.Required(OPT_HISTORY_INTERVAL,
                             default=opts.get(OPT_HISTORY_INTERVAL, DEFAULT_HISTORY_INTERVAL)):
                    vol.All(vol.Coerce(int), vol.Range(min=1, max=120)),
                vol.Required(OPT_STREAM_BIND, default=opts.get(OPT_STREAM_BIND, DEFAULT_STREAM_BIND)): str,
                vol.Required(OPT_STREAM_PORT, default=opts.get(OPT_STREAM_PORT, DEFAULT_STREAM_PORT)):
                    vol.All(vol.Coerce(int), vol.Range(min=0, max=65535)),
            }),
        )
