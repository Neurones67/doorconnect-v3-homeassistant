"""Local CGI client for the door station (HTTPS :443 /tdkcgi).

No Home Assistant dependency: can be used and tested on its own.

Recipe (validated on IDS94E6W, firmware V401.R001.A331):
  POST https://<ip>/tdkcgi, self-signed server certificate (not verified),
  no client certificate required, GoAhead Cookie header mandatory (any value),
  XML envelope with security=username, username=adminapp2, password=sha256_hex(device code).
"""
from __future__ import annotations

import asyncio
import hashlib
import re

import aiohttp

USERNAME = "adminapp2"


class DeviceError(Exception):
    """Generic error while talking to the door station."""


class DeviceAuthError(DeviceError):
    """Device code rejected (error 401 / -4)."""


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _tag(xml: str, tag: str) -> str | None:
    m = re.search(rf"<{tag}>([^<]*)</{tag}>", xml)
    return m.group(1).strip() if m else None


class LocalDevice:
    """Local CGI commands: product info, stream key, door opening."""

    def __init__(self, session: aiohttp.ClientSession, host: str, password: str) -> None:
        self._session = session
        self.host = host
        self._pwd = sha256_hex(password)

    async def call(self, command: str, content: str = "", timeout: float = 12) -> str:
        payload = (
            "<?xml version='1.0' encoding='utf-8'?>\n"
            "<envelope><header><security>username</security>"
            f"<username>{USERNAME}</username><password>{self._pwd}</password></header>"
            f"<body><command>{command}</command><content>{content}</content></body></envelope>"
        )
        headers = {
            "Content-Type": "application/xml;charset=UTF-8",
            "Cookie": "_goaheadwebSessionId=0",
            "Connection": "close",
        }
        try:
            async with self._session.post(
                f"https://{self.host}/tdkcgi",
                data=payload.encode(),
                headers=headers,
                ssl=False,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
                text = await resp.text(errors="replace")
        except asyncio.TimeoutError as err:
            # NB: with a wrong non-empty code the device deliberately stalls its answer
            # (anti brute-force), so a timeout may also mean "wrong code".
            raise DeviceError(f"timeout on {command}") from err
        except aiohttp.ClientError as err:
            raise DeviceError(f"{command}: {err}") from err

        err_code = _tag(text, "error")
        if err_code in ("401", "-4"):
            raise DeviceAuthError(f"device code rejected ({err_code})")
        if err_code != "0":
            raise DeviceError(f"{command} -> error={err_code}")
        return text

    async def product_info(self) -> dict[str, str | None]:
        xml = await self.call("get.product.info")
        return {
            "mac": (_tag(xml, "mac") or "").lower() or None,
            "version": _tag(xml, "version"),
            "model": _tag(xml, "model"),
        }

    async def streamkey(self) -> bytes:
        xml = await self.call("get.device.streamkey")
        key = _tag(xml, "key")
        if not key or len(key) != 32:
            raise DeviceError("missing or invalid stream key")
        return key.encode()

    async def open_door(self, lock: int = 1, door: int = 1) -> None:
        """lock 1 = door strike, 2 = gate; door = channel (1 = main outdoor station)."""
        await self.call(
            "set.device.opendoor",
            f"<door>{door}</door><locknumber>{lock}</locknumber><password>{self._pwd}</password>",
        )
