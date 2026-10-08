"""WSJT-X (and JTDX / MSHV) UDP protocol: decoder plus an asyncio listener.

Messages are Qt QDataStream encoded (big endian):
    quint32 magic 0xadbccbda, quint32 schema, quint32 type, utf8 id, payload...
Only the messages the relay needs are decoded:
    0  Heartbeat
    1  Status      dial frequency, mode, DX call, DX grid, ...
    6  Close
    12 Logged ADIF the complete ADIF record of a freshly logged QSO
"""

from __future__ import annotations

import asyncio
import logging
import socket
import struct
from dataclasses import dataclass
from typing import Callable

log = logging.getLogger("wsjtx")

MAGIC = 0xADBCCBDA

HEARTBEAT = 0
STATUS = 1
CLOSE = 6
LOGGED_ADIF = 12


class _Reader:
    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0

    def _take(self, fmt: str):
        size = struct.calcsize(fmt)
        if self.pos + size > len(self.data):
            raise EOFError
        (value,) = struct.unpack_from(fmt, self.data, self.pos)
        self.pos += size
        return value

    def u8(self) -> int:
        return self._take(">B")

    def bool(self) -> bool:
        return self._take(">B") != 0

    def u32(self) -> int:
        return self._take(">I")

    def u64(self) -> int:
        return self._take(">Q")

    def utf8(self) -> str | None:
        length = self.u32()
        if length == 0xFFFFFFFF:
            return None
        if self.pos + length > len(self.data):
            raise EOFError
        raw = self.data[self.pos:self.pos + length]
        self.pos += length
        return raw.decode("utf-8", errors="replace")


@dataclass
class Heartbeat:
    id: str
    max_schema: int | None = None
    version: str | None = None
    revision: str | None = None


@dataclass
class Status:
    id: str
    dial_frequency: int | None = None   # Hz
    mode: str | None = None
    dx_call: str | None = None
    report: str | None = None
    tx_mode: str | None = None
    tx_enabled: bool | None = None
    transmitting: bool | None = None
    decoding: bool | None = None
    rx_df: int | None = None
    tx_df: int | None = None
    de_call: str | None = None
    de_grid: str | None = None
    dx_grid: str | None = None
    tx_watchdog: bool | None = None
    sub_mode: str | None = None
    fast_mode: bool | None = None
    special_op_mode: int | None = None
    frequency_tolerance: int | None = None
    tr_period: int | None = None
    configuration_name: str | None = None
    tx_message: str | None = None


@dataclass
class Close:
    id: str


@dataclass
class LoggedAdif:
    id: str
    adif: str


Message = Heartbeat | Status | Close | LoggedAdif


def decode(data: bytes) -> Message | None:
    """Decode a WSJT-X datagram. Returns None for messages the relay does not use."""
    r = _Reader(data)
    try:
        if r.u32() != MAGIC:
            return None
        r.u32()  # schema
        msg_type = r.u32()
        ident = r.utf8() or ""
    except EOFError:
        return None

    if msg_type == HEARTBEAT:
        hb = Heartbeat(ident)
        try:
            hb.max_schema = r.u32()
            hb.version = r.utf8()
            hb.revision = r.utf8()
        except EOFError:
            pass
        return hb

    if msg_type == STATUS:
        st = Status(ident)
        # Fields were appended over WSJT-X releases; read as many as are present.
        readers = [
            ("dial_frequency", r.u64), ("mode", r.utf8), ("dx_call", r.utf8), ("report", r.utf8),
            ("tx_mode", r.utf8), ("tx_enabled", r.bool), ("transmitting", r.bool),
            ("decoding", r.bool), ("rx_df", r.u32), ("tx_df", r.u32), ("de_call", r.utf8),
            ("de_grid", r.utf8), ("dx_grid", r.utf8), ("tx_watchdog", r.bool),
            ("sub_mode", r.utf8), ("fast_mode", r.bool), ("special_op_mode", r.u8),
            ("frequency_tolerance", r.u32), ("tr_period", r.u32),
            ("configuration_name", r.utf8), ("tx_message", r.utf8),
        ]
        for name, fn in readers:
            try:
                setattr(st, name, fn())
            except EOFError:
                break
        return st

    if msg_type == CLOSE:
        return Close(ident)

    if msg_type == LOGGED_ADIF:
        try:
            return LoggedAdif(ident, r.utf8() or "")
        except EOFError:
            return None

    return None


# -- encoding helpers (used by tests and handy for simulating WSJT-X) ---------

def _utf8(s: str | None) -> bytes:
    if s is None:
        return struct.pack(">I", 0xFFFFFFFF)
    b = s.encode("utf-8")
    return struct.pack(">I", len(b)) + b


def encode_header(msg_type: int, ident: str, schema: int = 2) -> bytes:
    return struct.pack(">III", MAGIC, schema, msg_type) + _utf8(ident)


def encode_logged_adif(ident: str, adif: str) -> bytes:
    return encode_header(LOGGED_ADIF, ident) + _utf8(adif)


def encode_status(ident: str, dial_frequency: int, mode: str, dx_call: str, dx_grid: str = "",
                  de_call: str = "", de_grid: str = "", transmitting: bool = False) -> bytes:
    return (encode_header(STATUS, ident) + struct.pack(">Q", dial_frequency) + _utf8(mode)
            + _utf8(dx_call) + _utf8("") + _utf8(mode) + struct.pack(">BBB", 0, int(transmitting), 0)
            + struct.pack(">II", 1500, 1500) + _utf8(de_call) + _utf8(de_grid) + _utf8(dx_grid))


class _Protocol(asyncio.DatagramProtocol):
    def __init__(self, on_datagram: Callable[[bytes, tuple], None]):
        self.on_datagram = on_datagram

    def datagram_received(self, data: bytes, addr: tuple) -> None:
        self.on_datagram(data, addr)


async def open_udp_listener(host: str, port: int, on_datagram: Callable[[bytes, tuple], None],
                            multicast_group: str | None = None,
                            multicast_interface: str = "0.0.0.0") -> asyncio.DatagramTransport:
    """Bind a UDP socket (optionally joining a multicast group) and feed datagrams to a callback."""
    loop = asyncio.get_running_loop()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if multicast_group:
        if hasattr(socket, "SO_REUSEPORT"):
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except OSError:
                pass
        sock.bind(("" if host in ("0.0.0.0", "") else host, port))
        mreq = socket.inet_aton(multicast_group) + socket.inet_aton(multicast_interface)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    else:
        sock.bind((host, port))
    sock.setblocking(False)
    transport, _ = await loop.create_datagram_endpoint(lambda: _Protocol(on_datagram), sock=sock)
    return transport
