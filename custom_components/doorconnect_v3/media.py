"""On-demand live video from the door station (TCP 34567).

No Home Assistant dependency: can be used and tested on its own.

Protocol (reverse-engineered from liblive_player.so + MITM captures of the app on the LAN):
  1. Negotiation, 32 bytes IN CLEAR: [0]=0xA9, rest zero. The device answers 32 bytes.
  2. Every later message = AES-256-CBC(streamkey, IV=b"0"*16), applied separately to the
     32-byte header and to body || SHA-256(header || body) || zero padding.
     Header: [0]=type [1:9]=unix timestamp u64 LE [9]=ext [11:13]=body length [13:]=fields.
  3. Login, type 0x01 (ext 0xA0):
       "adminapp2&&<sha256_hex(code)>\\0G0123A0058G0058\\0clientid=<16 hex>\\0"
  4. Stream trigger: three 0xFE messages (ext 0x30), sub-types 0x05, 0x06, 0x02,
     1-byte body 0x00 (exactly what the app does).
  5. Keepalive, type 0x00 (ext 0x20), every 2 s.
  6. The device pushes 0xA0..0xA3 frames: encrypted header, [11:15]=length u32 LE,
     NO SHA. Only the first 32 bytes of the payload are encrypted.
     Payload = 16-byte descriptor (00 00 01 E1=I / E0=P / E3=audio, [4:8]=length,
     [8:12]=unix seconds, [12:14]=milliseconds) + 4-byte marker (00 08 80 xx)
     + H.264 Annex-B (2048x1152, ~15-18 fps).

⚠ Only these message types (0xA9, 0x01, 0xFE 05/06/02, 0x00) are ever sent. Others
(0x0B/0x0C/0x0D "talk", 0x0E) triggered the indoor monitor's emergency mode during tests.

The device accepts a single media session at a time: the connection is only opened
while at least one viewer (or snapshot request) needs it, then closed.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import io
import logging
import secrets
import struct
import time
from collections.abc import Awaitable, Callable

from aiohttp import web
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

_LOGGER = logging.getLogger(__name__)

MEDIA_PORT = 34567
IV = b"0" * 16
USER = b"adminapp2"
FW_CODES = b"G0123A0058G0058"
ENC_PREFIX = 32          # encrypted bytes at the start of each media payload
KEEPALIVE_S = 2.0
IDLE_GRACE_S = 5.0       # close the session N s after the last viewer left
GOP_MAX_FRAMES = 400     # current GOP buffer (lets new viewers start immediately)
QUEUE_MAX = 300
RECONNECT_TRIES = 3


class MediaError(Exception):
    """The media session could not be opened or was lost."""


# ───────────────────────── protocol ─────────────────────────
def _aes_dec(key: bytes, data: bytes) -> bytes:
    d = Cipher(algorithms.AES(key), modes.CBC(IV)).decryptor()
    return d.update(data) + d.finalize()


def _aes_enc(key: bytes, data: bytes) -> bytes:
    e = Cipher(algorithms.AES(key), modes.CBC(IV)).encryptor()
    return e.update(data) + e.finalize()


def build_message(key: bytes, typ: int, ext: int, fields: bytes, body: bytes = b"") -> bytes:
    hdr = (bytes([typ]) + struct.pack("<Q", int(time.time())) + bytes([ext, 0]) + fields).ljust(32, b"\0")
    payload = body + hashlib.sha256(hdr + body).digest()
    payload += b"\0" * (-len(payload) % 16)
    return _aes_enc(key, hdr) + _aes_enc(key, payload)


def login_message(key: bytes, password: str, client_id: str) -> bytes:
    pw = hashlib.sha256(password.encode()).hexdigest().encode()
    body = USER + b"&&" + pw + b"\0" + FW_CODES + b"\0clientid=" + client_id.encode() + b"\0"
    fields = struct.pack("<H", len(body)) + bytes([0x01, 0x00, 0x01, 0x00, 0x00])
    return build_message(key, 0x01, 0xA0, fields, body)


def trigger_messages(key: bytes) -> bytes:
    return b"".join(
        build_message(key, 0xFE, 0x30, struct.pack("<H", 1) + bytes([sub]), b"\0")
        for sub in (0x05, 0x06, 0x02)
    )


def keepalive_message(key: bytes) -> bytes:
    return build_message(key, 0x00, 0x20, b"\0" * 5)


def parse_frames(key: bytes, buf: bytearray) -> tuple[list[tuple[int, bool, bytes]], int]:
    """Split the buffer. Returns ([(90 kHz timestamp, is_keyframe, h264)], bytes consumed)."""
    out: list[tuple[int, bool, bytes]] = []
    i = 0
    n = len(buf)
    while n - i >= 32:
        h = _aes_dec(key, bytes(buf[i:i + 32]))
        typ = h[0]
        if 0xA0 <= typ <= 0xA3:
            length = struct.unpack("<I", h[11:15])[0]
            if n - i - 32 < length:
                break
            raw = bytes(buf[i + 32:i + 32 + length])
            i += 32 + length
            k = min(ENC_PREFIX, len(raw) // 16 * 16)
            payload = (_aes_dec(key, raw[:k]) + raw[k:]) if k else raw
            if len(payload) > 20 and payload[:3] == b"\x00\x00\x01" and payload[3] in (0xE0, 0xE1):
                sec, msec = struct.unpack("<IH", payload[8:14])
                out.append((sec * 90000 + msec * 90, payload[3] == 0xE1, payload[20:]))
        else:
            ext = h[9]
            blen = struct.unpack("<H", h[11:13])[0] if (ext & 0xD0) else 0
            size = (blen + 32 + 15) // 16 * 16
            if n - i - 32 < size:
                break
            i += 32 + size  # control messages: ignored
    return out, i


# ───────────────────────── on-demand session ─────────────────────────
class _Subscriber:
    __slots__ = ("queue", "synced")

    def __init__(self) -> None:
        # items: (90 kHz timestamp, is_keyframe, H.264 Annex-B); None = end of stream
        self.queue: asyncio.Queue[tuple[int, bool, bytes] | None] = asyncio.Queue(QUEUE_MAX)
        self.synced = False


class MediaHub:
    """Keeps the media session open while there are consumers, and fans it out to all of them."""

    def __init__(
        self,
        host: str,
        password: str,
        client_id: str,
        get_streamkey: Callable[[], Awaitable[bytes]],
    ) -> None:
        self.host = host
        self._password = password
        self._client_id = client_id
        self._get_streamkey = get_streamkey
        self._subs: set[_Subscriber] = set()
        self._kf_waiters: list[asyncio.Future[bytes]] = []
        self._task: asyncio.Task | None = None
        self._gop: list[tuple[int, bool, bytes]] = []  # current keyframe + following P-frames
        self._last_ts = 0
        self.last_keyframe: bytes | None = None
        self.last_keyframe_ts: float = 0.0
        self.last_error: str | None = None
        self._closing = False

    @property
    def streaming(self) -> bool:
        return self._task is not None and not self._task.done()

    def _wanted(self) -> bool:
        return bool(self._subs or self._kf_waiters) and not self._closing

    def _ensure_running(self) -> None:
        if not self.streaming and not self._closing:
            self._task = asyncio.get_running_loop().create_task(self._run(), name="doorconnect_v3-media")

    # --- consumer API ---
    def subscribe(self) -> _Subscriber:
        sub = _Subscriber()
        if self._gop:  # start immediately from the current GOP
            for f in self._gop:
                sub.queue.put_nowait(f)
            sub.synced = True
        self._subs.add(sub)
        self._ensure_running()
        return sub

    def unsubscribe(self, sub: _Subscriber) -> None:
        self._subs.discard(sub)

    async def async_get_keyframe(self, timeout: float = 20) -> bytes:
        """Return a recent H.264 keyframe (SPS+PPS+IDR), opening the session if needed."""
        if self.last_keyframe and time.monotonic() - self.last_keyframe_ts < 3:
            return self.last_keyframe
        fut: asyncio.Future[bytes] = asyncio.get_running_loop().create_future()
        self._kf_waiters.append(fut)
        self._ensure_running()
        try:
            return await asyncio.wait_for(fut, timeout)
        finally:
            with contextlib.suppress(ValueError):
                self._kf_waiters.remove(fut)

    async def async_close(self) -> None:
        self._closing = True
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
        self._end_subscribers()

    # --- internals ---
    def _end_subscribers(self) -> None:
        for sub in self._subs:
            with contextlib.suppress(asyncio.QueueFull):
                sub.queue.put_nowait(None)
        self._subs.clear()

    def _dispatch(self, ts: int, is_key: bool, h264: bytes) -> None:
        # Device clock (ms resolution), forced strictly increasing (e.g. after an NTP jump).
        ts = ts if ts > self._last_ts else self._last_ts + 90
        self._last_ts = ts
        item = (ts, is_key, h264)
        if is_key:
            self._gop = [item]
            self.last_keyframe = h264
            self.last_keyframe_ts = time.monotonic()
            for fut in self._kf_waiters:
                if not fut.done():
                    fut.set_result(h264)
        elif self._gop and len(self._gop) < GOP_MAX_FRAMES:
            self._gop.append(item)
        for sub in self._subs:
            if not sub.synced:
                if not is_key:
                    continue
                sub.synced = True
            try:
                sub.queue.put_nowait(item)
            except asyncio.QueueFull:
                # slow viewer: flush and resync on the next keyframe
                while not sub.queue.empty():
                    sub.queue.get_nowait()
                sub.synced = False

    async def _run(self) -> None:
        failures = 0
        try:
            while self._wanted():
                try:
                    await self._session()
                    failures = 0
                except (OSError, asyncio.TimeoutError, MediaError, asyncio.IncompleteReadError) as err:
                    failures += 1
                    self.last_error = str(err) or type(err).__name__
                    _LOGGER.warning("Video session with %s interrupted (%s), attempt %s/%s",
                                    self.host, self.last_error, failures, RECONNECT_TRIES)
                    if failures >= RECONNECT_TRIES:
                        break
                    await asyncio.sleep(2)
                except Exception as err:  # noqa: BLE001 — keep the hub alive
                    self.last_error = repr(err)
                    _LOGGER.exception("Unexpected error in the video session")
                    break
        finally:
            self._gop = []
            for fut in self._kf_waiters:
                if not fut.done():
                    fut.set_exception(MediaError(self.last_error or "video session unavailable"))
            self._end_subscribers()

    async def _session(self) -> None:
        key = await self._get_streamkey()
        reader, writer = await asyncio.wait_for(asyncio.open_connection(self.host, MEDIA_PORT), 6)
        _LOGGER.debug("Video session opened to %s:%s", self.host, MEDIA_PORT)
        try:
            writer.write(bytes([0xA9]) + b"\0" * 31)
            await writer.drain()
            await asyncio.wait_for(reader.readexactly(32), 6)
            writer.write(login_message(key, self._password, self._client_id))
            await writer.drain()
            await asyncio.sleep(0.4)
            writer.write(trigger_messages(key))
            await writer.drain()

            buf = bytearray()
            last_ka = time.monotonic()
            last_data = time.monotonic()
            idle_since: float | None = None
            while True:
                try:
                    chunk = await asyncio.wait_for(reader.read(65536), 1.0)
                except asyncio.TimeoutError:
                    chunk = None
                now = time.monotonic()
                if chunk == b"":
                    raise MediaError("connection closed by the device")
                if chunk:
                    last_data = now
                    buf += chunk
                    frames, used = parse_frames(key, buf)
                    del buf[:used]
                    for frame in frames:
                        self._dispatch(*frame)
                elif now - last_data > 15:
                    raise MediaError("no data for 15 s")
                if now - last_ka > KEEPALIVE_S:
                    last_ka = now
                    writer.write(keepalive_message(key))
                    await writer.drain()
                if self._wanted():
                    idle_since = None
                elif idle_since is None:
                    idle_since = now
                elif now - idle_since > IDLE_GRACE_S:
                    _LOGGER.debug("No more viewers, closing the video session")
                    return
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()


# ───────────────────────── MPEG-TS (timestamps for the players) ─────────────────────────
def _crc32_mpeg(data: bytes) -> int:
    crc = 0xFFFFFFFF
    for b in data:
        crc ^= b << 24
        for _ in range(8):
            crc = ((crc << 1) ^ 0x04C11DB7) & 0xFFFFFFFF if crc & 0x80000000 else (crc << 1) & 0xFFFFFFFF
    return crc


def _psi(table: bytes) -> bytes:
    """PSI section (pointer_field + section + CRC) padded to 184 bytes."""
    section = table + struct.pack(">I", _crc32_mpeg(table))
    return (b"\0" + section).ljust(184, b"\xff")


_PMT_PID = 0x1000
_VIDEO_PID = 0x100
_PAT = _psi(bytes([0x00, 0xB0, 0x0D, 0x00, 0x01, 0xC1, 0x00, 0x00, 0x00, 0x01,
                   0xE0 | (_PMT_PID >> 8), _PMT_PID & 0xFF]))
_PMT = _psi(bytes([0x02, 0xB0, 0x12, 0x00, 0x01, 0xC1, 0x00, 0x00,
                   0xE0 | (_VIDEO_PID >> 8), _VIDEO_PID & 0xFF, 0xF0, 0x00,
                   0x1B, 0xE0 | (_VIDEO_PID >> 8), _VIDEO_PID & 0xFF, 0xF0, 0x00]))
_AUD = b"\x00\x00\x00\x01\x09\xf0"


class TsMuxer:
    """Minimal MPEG-TS muxer for a single H.264 stream (PAT/PMT, PES with PTS/DTS, PCR)."""

    def __init__(self) -> None:
        self._cc: dict[int, int] = {}
        self._t0: int | None = None

    def _packet(self, pid: int, payload: bytes, pusi: bool, adapt: bytes | None = None) -> bytes:
        cc = self._cc.get(pid, 0)
        self._cc[pid] = (cc + 1) & 0x0F
        head = bytes([0x47, (0x40 if pusi else 0) | (pid >> 8), pid & 0xFF])
        if adapt is None:
            return head + bytes([0x10 | cc]) + payload
        return head + bytes([0x30 | cc, len(adapt)]) + adapt + payload

    @staticmethod
    def _ts_field(prefix: int, ts: int) -> bytes:
        return bytes([
            (prefix << 4) | (((ts >> 30) & 0x07) << 1) | 1,
            (ts >> 22) & 0xFF, (((ts >> 15) & 0x7F) << 1) | 1,
            (ts >> 7) & 0xFF, ((ts & 0x7F) << 1) | 1,
        ])

    def mux(self, ts90k: int, is_key: bool, h264: bytes) -> bytes:
        if self._t0 is None:
            self._t0 = ts90k
        pts = (ts90k - self._t0 + 90000) & ((1 << 33) - 1)  # 1 s offset, PTS = DTS (no B-frames)
        out = bytearray()
        if is_key:
            out += self._packet(0, _PAT, True)
            out += self._packet(_PMT_PID, _PMT, True)
        pes = (b"\x00\x00\x01\xe0\x00\x00\x80\xc0\x0a"
               + self._ts_field(3, pts) + self._ts_field(1, pts) + _AUD + h264)
        # first packet: adaptation field carrying the PCR (+ random access flag on keyframes)
        pcr = pts
        flags = 0x10 | (0x40 if is_key else 0)
        adapt = bytes([flags]) + struct.pack(">IH", pcr >> 1, ((pcr & 1) << 15) | 0x7E00)
        pos = 0
        first = True
        while pos < len(pes):
            room = 184 - (len(adapt) + 1 if adapt is not None else 0)
            chunk = pes[pos:pos + room]
            pos += len(chunk)
            if len(chunk) < room:  # last packet: stuffing in the adaptation field
                pad = room - len(chunk)
                if adapt is None:
                    adapt = b"" if pad == 1 else bytes([0x00]) + b"\xff" * (pad - 2)
                else:
                    adapt += b"\xff" * pad
            out += self._packet(_VIDEO_PID, chunk, first, adapt)
            first = False
            adapt = None
        return bytes(out)


# ───────────────────────── local HTTP server (source for go2rtc / stream) ─────────────────────────
class MediaServer:
    """Serves the stream at http://<bind>:<port>/<token>/live.ts (timestamped MPEG-TS, used by HA)
    and /<token>/live.h264 (raw H.264 Annex-B, handy for mpv/ffplay).

    The MPEG-TS carries PTS/DTS taken from the device clock: required by HA's `stream`
    worker (PyAV packets without DTS are rejected) and natively understood by go2rtc.
    """

    def __init__(self, hub: MediaHub, token: str, bind: str = "127.0.0.1", port: int = 0) -> None:
        self.hub = hub
        self.token = token
        self.bind = bind
        self.port = port
        self._runner: web.AppRunner | None = None

    @property
    def url(self) -> str:
        host = "127.0.0.1" if self.bind in ("0.0.0.0", "") else self.bind
        return f"http://{host}:{self.port}/{self.token}/live.ts"

    async def async_start(self) -> None:
        app = web.Application()
        app.router.add_get("/{token}/live.ts", self._handle_ts)
        app.router.add_get("/{token}/live.h264", self._handle_raw)
        self._runner = web.AppRunner(app, access_log=None, handle_signals=False)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.bind, self.port)
        await site.start()
        if self.port == 0:
            self.port = site._server.sockets[0].getsockname()[1]  # noqa: SLF001
        _LOGGER.debug("Local video server listening on %s", self.url)

    async def async_stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()
            self._runner = None

    async def _handle_ts(self, request: web.Request) -> web.StreamResponse:
        muxer = TsMuxer()
        return await self._serve(request, "video/mp2t", lambda t, k, d: muxer.mux(t, k, d))

    async def _handle_raw(self, request: web.Request) -> web.StreamResponse:
        return await self._serve(request, "application/octet-stream", lambda t, k, d: d)

    async def _serve(self, request: web.Request, ctype: str,
                     encode: Callable[[int, bool, bytes], bytes]) -> web.StreamResponse:
        if not secrets.compare_digest(request.match_info["token"], self.token):
            raise web.HTTPNotFound()
        resp = web.StreamResponse(headers={"Content-Type": ctype, "Cache-Control": "no-cache"})
        await resp.prepare(request)
        sub = self.hub.subscribe()
        try:
            while True:
                item = await sub.queue.get()
                if item is None:
                    break
                await resp.write(encode(*item))
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            self.hub.unsubscribe(sub)
        return resp


# ───────────────────────── still image ─────────────────────────
def keyframe_to_jpeg(h264: bytes, quality: int = 85) -> bytes | None:
    """Decode an H.264 keyframe to JPEG (PyAV, shipped with Home Assistant). Blocking."""
    import av  # late import: heavy, and not available outside HA

    codec = av.CodecContext.create("h264", "r")
    frames = []
    # parse(None) flushes the parser: otherwise the single picture (last NAL) stays pending.
    for pkt in codec.parse(h264) + codec.parse(None):
        frames.extend(codec.decode(pkt))
    with contextlib.suppress(Exception):
        frames.extend(codec.decode(None))
    if not frames:
        return None
    frame = frames[-1]
    try:
        img = frame.to_image()
        out = io.BytesIO()
        img.save(out, "JPEG", quality=quality)
        return out.getvalue()
    except ImportError:  # no Pillow: use libav's MJPEG encoder
        from fractions import Fraction

        enc = av.CodecContext.create("mjpeg", "w")
        enc.width, enc.height = frame.width, frame.height
        enc.pix_fmt = "yuvj420p"
        enc.time_base = Fraction(1, 25)
        pkts = enc.encode(frame.reformat(format="yuvj420p"))
        return bytes(pkts[0]) if pkts else None
