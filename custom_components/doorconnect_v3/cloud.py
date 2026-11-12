"""Event history from the Qvii cloud (the backend of the DoorConnect app).

No Home Assistant dependency: can be used and tested on its own.

The door station does not expose its log locally: the app reads it from the OEM cloud
(Qualvision/Qvii). Sequence reverse-engineered from the APK, then validated on a real account:

  0. Servers GET  https://sh2.qvcloud.net/mst/query, XML command query-hlrv2 (no credentials)
             -> URL of each service for the caller's region: oauth2, (sec-)userapp, (sec-)alarmapp.
             Same request as the app's native P2P library (ServerResource::BuildHlr in
             libqv-p2p-v2.so); the app's client certificate is not needed.
  1. OAuth   GET  <oauth2>/qvoauthv2/token  (grant_type=password,
             pwd = sha256_hex(account password)) -> JWT access_token (claim qv-uid = ACCOUNT_ID)
  2. Login   "duplex" channel on <sec-userapp>:
             GET  /auth/user;jus_duplex=down   -> jsessionid cookie
             POST /auth/user;jus_duplex=up     -> XML envelope, command=login
             GET  /auth/user;jus_duplex=down   -> answer: SESSION_ID + account-id
  3. Alarm   POST <sec-alarmapp>/UserAlarm command=client-login
             (Content-Type: application/xml WITHOUT charset)
  4. Read    POST <sec-alarmapp>/UserAlarm command=client-query-recordlist
             (empty filter, pageno starts at 0)

The servers present a private QUALVISION certificate without host name: TLS verification is
disabled.
⚠ The account gets locked after a few wrong passwords: never retry blindly.
"""
from __future__ import annotations

import asyncio
import base64
import datetime as dt
import hashlib
import json
import logging
import re
import secrets
import xml.etree.ElementTree as ET
from typing import Any

import aiohttp

_LOGGER = logging.getLogger(__name__)

MST_URL = "https://sh2.qvcloud.net/mst/query"  # bootstrap server hard-coded in the app
MST_VERSION = "4456"  # protocol version sent by libqv-p2p-v2.so
# Fallback servers if the bootstrap server cannot be reached (its answer for Europe, 2026-11).
DEFAULT_SERVERS = {
    "oauth2": "https://oauth2shi.qvcloud.net:443",
    "userapp": "https://shi-18-sec.qvcloud.net:443/auth/user",
    "alarmapp": "https://shi-25-sec.qvcloud.net:4443/UserAlarm",
}
OEM_ID = "G0123,A0058,G0058"
APP_ID = "4000"
CLIENT_TYPE = "0"
REGION_ID = "0"
FLAG = "tdkcloud"
XML_DECL = '<?xml version="1.0" encoding="UTF-8"?>'

# Alarm server result codes meaning "session invalid" (SDKStatus)
SESSION_ERRORS = {"1009", "1012"}

EVENT_TYPES: dict[int | None, str] = {19: "ring", 7: "ring", 63: "unlock", 2: "motion"}
UNLOCK_METHODS: dict[int | None, str] = {
    0: "default", 1: "qrcode", 2: "password", 3: "indoor_monitor", 4: "app",
    5: "rf_badge", 6: "exit_button", 7: "dtmf", 8: "google", 9: "alexa", 10: "face",
}
UNLOCK_METHOD_LABELS = {
    "default": "unknown", "qrcode": "QR code", "password": "code", "indoor_monitor": "indoor monitor",
    "app": "app", "rf_badge": "RFID badge", "exit_button": "exit button", "dtmf": "DTMF",
    "google": "Google", "alexa": "Alexa", "face": "face",
}
OUTPUTS: dict[int | None, str] = {1: "strike", 2: "gate"}
OUTPUT_LABELS = {"strike": "door strike", "gate": "gate"}


class CloudError(Exception):
    """Error while talking to the cloud."""


class CloudHTTPError(CloudError):
    """Non-2xx HTTP answer (nginx answers 404 when the server-side session is gone)."""

    def __init__(self, status: int, path: str) -> None:
        super().__init__(f"HTTP {status} from {path}")
        self.status = status


class CloudAuthError(CloudError):
    """Credentials rejected (do not retry in a loop: the account would get locked)."""


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


_BARE_AMP = re.compile(r"&(?!(?:[A-Za-z]+|#\d+|#x[0-9A-Fa-f]+);)")
_BAD_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _parse_xml(txt: str) -> ET.Element:
    """Parse a server answer, tolerating what the Qvii servers are known to send.

    The answers are not always well-formed XML: a UTF-8 BOM or whitespace before the XML
    declaration, bare "&" inside snapshot URLs, control characters in names.
    """
    try:
        return ET.fromstring(txt)
    except ET.ParseError:
        pass
    cleaned = _BAD_CHARS.sub("", _BARE_AMP.sub("&amp;", txt.lstrip("\ufeff \t\r\n")))
    return ET.fromstring(cleaned)


def _find(txt: str, path: str) -> str | None:
    try:
        return (_parse_xml(txt).findtext(path) or "").strip() or None
    except ET.ParseError:
        return None


def _int(v: str | None) -> int | None:
    try:
        return int(v) if v is not None else None
    except ValueError:
        return None


def _jwt_uid(token: str) -> str | None:
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        return str(json.loads(base64.urlsafe_b64decode(part)).get("qv-uid") or "") or None
    except (IndexError, ValueError):
        return None


def _tz_gmt() -> str:
    offset = dt.datetime.now().astimezone().utcoffset() or dt.timedelta()
    off = int(round(offset.total_seconds() / 60))
    sign = "+" if off >= 0 else "-"
    off = abs(off)
    return f"GMT{sign}{off // 60:02d}:{off % 60:02d}"


def build_server_query(client_id: str, region: str = REGION_ID) -> str:
    """query-hlrv2 request, field for field as built by ServerResource::BuildHlr."""
    types = "userapp,sec-userapp,alarmapp,sec-alarmapp,oauth2"
    return (f"{XML_DECL}<envelope><header><flag>{FLAG}</flag><command>query-hlrv2</command>"
            f"<seq>1</seq></header><content><server-type>{types}</server-type><oem>{OEM_ID}</oem>"
            f"<devid></devid><public-ip></public-ip><client-id>{client_id}</client-id>"
            f"<regionid>{region}</regionid><version>{MST_VERSION}</version></content></envelope>")


def parse_servers(txt: str) -> dict[str, str]:
    """query-hlrv2 answer -> {"oauth2": base URL, "userapp": URL, "alarmapp": URL}.

    The "sec-" variant of a service is preferred (the one the app uses for its secure channel).
    """
    try:
        root = _parse_xml(txt)
    except ET.ParseError as err:
        raise CloudError("unreadable server list") from err
    if (root.findtext(".//header/result") or "").strip() != "0":
        raise CloudError(f"server list: result={root.findtext('.//header/result')}")
    found: dict[str, str] = {}
    for srv in root.iterfind(".//content/server"):
        kind = (srv.findtext("server-type") or "").strip()
        url = (srv.findtext("url") or "").strip().rstrip("/")
        uri = (srv.findtext("uri") or "").strip()
        if (srv.findtext("query-result") or "").strip() != "1" or not url.startswith("https://"):
            continue
        found[kind] = url if uri in ("", "/") else url + uri
    out = {}
    for kind in ("oauth2", "userapp", "alarmapp"):
        url = found.get(f"sec-{kind}") or found.get(kind)
        if url:
            out[kind] = url
    if len(out) < 3:
        raise CloudError(f"server list incomplete: {sorted(found)}")
    return out


def describe(ev: dict[str, Any]) -> str:
    """Human-readable label of a normalized event."""
    if ev["type"] == "unlock":
        txt = f"{OUTPUT_LABELS.get(ev['output'], 'output').capitalize()} opened"
        if ev.get("method"):
            txt += f" ({UNLOCK_METHOD_LABELS.get(ev['method'], ev['method'])})"
        return txt
    if ev["type"] == "ring":
        return "Call from the outdoor station"
    if ev["type"] == "motion":
        return "Motion detected"
    return f"Event {ev['raw_event']}"


def parse_records(txt: str) -> list[dict[str, Any]]:
    """Turn a client-query-recordlist answer into normalized events."""
    try:
        root = _parse_xml(txt)
    except ET.ParseError as err:
        _LOGGER.debug("Unreadable history answer (%d chars): %r", len(txt), txt[:600])
        if not txt.strip():
            raise CloudError("empty history answer") from err
        raise CloudError(f"unreadable history answer ({err}; starts with {txt[:40]!r})") from err
    out = []
    for item in root.iterfind(".//record/data"):
        g = lambda t: (item.findtext(t) or "").strip()  # noqa: E731
        raw_event = _int(g("event"))
        state = _int(g("alarmstate"))
        info = _int(g("alarminfo"))
        etype = EVENT_TYPES.get(raw_event, "other")
        ev = {
            "id": g("id") or g("alarmid") or f"{g('time')}-{raw_event}-{info}",
            "time": g("time"),
            "type": etype,
            "output": OUTPUTS.get(info) if etype == "unlock" else None,
            "method": UNLOCK_METHODS.get(state, str(state)) if etype == "unlock" else None,
            "badge": g("entrancekeyname") or None,
            "source": g("alarmsourcename") or g("alarmsource") or None,
            "channel": g("chname") or None,
            "device_id": g("devid") or None,
            "snapshot_url": g("recordsubresurl") or g("recordresurl") or None,
            "raw_event": raw_event,
            "raw_state": state,
            "raw_info": info,
        }
        ev["description"] = describe(ev)
        out.append(ev)
    return out


class QvCloud:
    """Qvii cloud session, transparently re-authenticated when it expires."""

    def __init__(self, session: aiohttp.ClientSession, user: str, password: str, client_id: str) -> None:
        self._http = session
        self._user = user
        self._pwd = sha256_hex(password)
        self._client_id = client_id
        self._alarm_session: str | None = None
        self._user_session: str | None = None
        self.account_id: str | None = None
        self.servers: dict[str, str] = dict(DEFAULT_SERVERS)
        self.full_logins = 0  # statistics, for diagnostics

    async def async_resolve_servers(self) -> None:
        """Ask the bootstrap server which servers serve this region (like the app does)."""
        try:
            async with self._http.get(
                MST_URL, data=build_server_query(self._client_id or secrets.token_hex(8)).encode(), ssl=False,
                headers={"Content-Type": "application/xml;charset=utf-8"},
                timeout=aiohttp.ClientTimeout(total=20),
            ) as r:
                txt = (await r.read()).decode("utf-8-sig", errors="replace")
            self.servers = parse_servers(txt)
        except (aiohttp.ClientError, asyncio.TimeoutError, CloudError) as err:
            _LOGGER.warning("Could not get the Qvii server list (%s), using %s", err, self.servers)
            return
        _LOGGER.debug("Qvii servers: %s", self.servers)

    async def _xml(self, url: str, body: str) -> str:
        # Exactly like the app: Content-Type without charset + a separate "charset" header.
        async with self._http.post(
            url, data=(XML_DECL + body).encode(), ssl=False,
            headers={"Content-Type": "application/xml", "charset": "utf-8"},
            timeout=aiohttp.ClientTimeout(total=30),
        ) as r:
            raw = await r.read()
            path = url.split("/", 3)[-1]
            _LOGGER.debug("POST %s -> HTTP %s, %d bytes, cookies set: %s",
                          path, r.status, len(raw), sorted(r.cookies) or "none")
            if r.status >= 400:
                raise CloudHTTPError(r.status, path)
            # The servers do not send a charset; "utf-8-sig" also drops a leading BOM.
            return raw.decode("utf-8-sig", errors="replace")

    async def async_oauth(self) -> str:
        params = {
            "grant_type": "password", "client_id": "00", "client_type": CLIENT_TYPE,
            "oemid": OEM_ID, "appid": APP_ID, "usr": self._user, "pwd": self._pwd,
            "region_id": REGION_ID, "client_flag": "1",
        }
        try:
            async with self._http.get(
                f"{self.servers['oauth2']}/qvoauthv2/token", params=params, ssl=False,
                timeout=aiohttp.ClientTimeout(total=20),
            ) as r:
                data = await r.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as err:
            raise CloudError(f"OAuth: {err}") from err
        token = data.get("access_token") if isinstance(data, dict) else None
        if not token:
            msg = data.get("err-msg") if isinstance(data, dict) else data
            raise CloudAuthError(f"OAuth rejected: {msg}")
        self.account_id = _jwt_uid(token)
        return token

    async def _user_login(self) -> str:
        header = (f"<header><client><app>{APP_ID}</app><id>00</id><oem>{OEM_ID}</oem>"
                  f"<type>{CLIENT_TYPE}</type></client><command>login</command><flag>{FLAG}</flag>"
                  "<seq>0</seq><session></session><version></version></header>")
        content = (f"<content><account>{self._user}</account><password>{self._pwd}</password>"
                   f"<auth-type>0</auth-type><auth-code></auth-code>"
                   f"<ip-region-id>{REGION_ID}</ip-region-id></content>")
        down = f"{self.servers['userapp']};jus_duplex=down"
        up = f"{self.servers['userapp']};jus_duplex=up"
        timeout = aiohttp.ClientTimeout(total=45)

        async with self._http.get(down, ssl=False, timeout=timeout) as r:
            await r.read()  # sets the jsessionid cookie
        replies = [await self._xml(up, f"<envelope>{header}{content}</envelope>")]
        for _ in range(6):
            async with self._http.get(down, ssl=False, timeout=timeout) as r:
                txt = await r.text(errors="replace")
            if txt.strip():
                replies.append(txt)
                if "<result>" in txt:
                    break
            await asyncio.sleep(0.5)

        sid = res = None
        for t in replies:
            sid = sid or _find(t, ".//session/id") or _find(t, ".//header/session")
            self.account_id = _find(t, ".//account-id") or self.account_id
            res = res or _find(t, ".//result")
        if res not in (None, "0"):
            raise CloudAuthError(f"login rejected (result={res})")
        if not sid:
            raise CloudError("login: no SESSION_ID received")
        return sid

    async def _alarm_login(self, session_id: str) -> str:
        auth_host = self.servers["userapp"].split("/")[2].split(":")[0]
        content = (f"<content><account><authserver>{auth_host}</authserver><id>{self.account_id}</id>"
                   f"</account><client><appid>{APP_ID}</appid><id>{self._client_id}</id>"
                   f"<notifylang>en</notifylang><oemid>{OEM_ID}</oemid><timezone>{_tz_gmt()}</timezone>"
                   f"<token></token><tokentype>0</tokentype><type>{CLIENT_TYPE}</type></client></content>")
        txt = await self._xml(
            self.servers["alarmapp"],
            f"<envelope><header><command>client-login</command><flag>{FLAG}</flag><seq>0</seq>"
            f"<session>{session_id}</session></header>{content}</envelope>",
        )
        res = _find(txt, ".//result")
        if res != "0":
            raise CloudError(f"alarm client-login: result={res}")
        return _find(txt, ".//session/id") or _find(txt, ".//header/session") or session_id

    async def async_login(self) -> None:
        """Full login: server lookup, OAuth (password), duplex user login, alarm client-login."""
        self._http.cookie_jar.clear()
        self._alarm_session = self._user_session = None
        await self.async_resolve_servers()
        await self.async_oauth()
        try:
            self._user_session = await self._user_login()
            self._alarm_session = await self._alarm_login(self._user_session)
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise CloudError(f"login: {err}") from err
        self.full_logins += 1
        _LOGGER.debug("Connected to the Qvii cloud (account %s)", self.account_id)

    async def _query_page(self, maxid: str, page: int, lines: int) -> tuple[str | None, str | None, str]:
        content = ("<content><filter><devid><id></id></devid><event><type></type></event>"
                   f"<peroid></peroid></filter><maxid>{maxid}</maxid>"
                   f"<pagelinenum>{lines}</pagelinenum><pageno>{page}</pageno></content>")
        txt = await self._xml(
            self.servers["alarmapp"],
            f"<envelope><header><command>client-query-recordlist</command><flag>{FLAG}</flag>"
            f"<seq>0</seq><session>{self._alarm_session}</session></header>{content}</envelope>",
        )
        return _find(txt, ".//header/result"), _find(txt, ".//content/maxid"), txt

    async def _read_history(self, pages: int, lines: int) -> list[dict[str, Any]] | None:
        """Read the history with the current alarm session; None if that session has expired."""
        events: list[dict[str, Any]] = []
        maxid = "0"
        for page in range(pages):
            try:
                res, new_maxid, txt = await self._query_page(maxid, page, lines)
            except CloudHTTPError as err:
                if err.status != 404:
                    raise
                _LOGGER.debug("History query answered HTTP 404: treating the session as expired")
                return None
            except (aiohttp.ClientError, asyncio.TimeoutError) as err:
                raise CloudError(f"history: {err}") from err
            # An empty body has been seen when the alarm session is no longer valid.
            if res in SESSION_ERRORS or not txt.strip():
                return None
            if res not in (None, "0"):
                raise CloudError(f"history: result={res}")
            got = parse_records(txt)
            events += got
            if len(got) < lines:
                break
            maxid = new_maxid or maxid
        return events

    async def async_get_history(self, pages: int = 2, lines: int = 15) -> list[dict[str, Any]]:
        """Most recent events first (same order as the app).

        The user session only lives while the duplex down channel is polled (max-age 120 s,
        DownChannelManager in the app), and the alarm session depends on it: with a poll every
        few minutes both have expired, so an expired session means a full login.
        """
        if not self._alarm_session:
            await self.async_login()
        if (events := await self._read_history(pages, lines)) is not None:
            return events
        _LOGGER.debug("Qvii cloud session expired, logging in again")
        await self.async_login()
        if (events := await self._read_history(pages, lines)) is not None:
            return events
        raise CloudError("cloud session still invalid after re-login")
