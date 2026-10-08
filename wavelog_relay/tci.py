"""TCI (Transceiver Control Interface, ExpertSDR / SunSDR / Hermes-Lite via Thetis etc.) client.

TCI is a text protocol over WebSocket (default ws://127.0.0.1:40001). Commands look like
    vfo:0,0,14074000;   modulation:0,usb;   split_enable:0,true;   drive:0,50;   trx:0,true;
On connect the server sends its full state, then pushes every change.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Callable

log = logging.getLogger("tci")

# TCI modulation -> Wavelog/CAT mode string
MODE_MAP = {
    "am": "AM", "sam": "AM", "dsb": "AM",
    "lsb": "LSB", "usb": "USB",
    "cw": "CW",
    "nfm": "FM", "wfm": "FM",
    "digl": "PKTLSB", "digu": "PKTUSB",
    "drm": "DRM",
}


@dataclass
class TrxState:
    vfo: dict[int, int] = field(default_factory=dict)  # channel -> Hz
    modulation: str | None = None
    split: bool = False
    drive: int | None = None  # percent
    tx: bool = False

    @property
    def rx_frequency(self) -> int | None:
        return self.vfo.get(0)

    @property
    def tx_frequency(self) -> int | None:
        if self.split and self.vfo.get(1):
            return self.vfo[1]
        return self.vfo.get(0)

    @property
    def mode(self) -> str | None:
        if not self.modulation:
            return None
        return MODE_MAP.get(self.modulation.lower(), self.modulation.upper()[:10])


def parse_commands(text: str) -> list[tuple[str, list[str]]]:
    """Split a TCI text frame into (command, args) tuples."""
    out = []
    for chunk in text.split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        name, _, args = chunk.partition(":")
        out.append((name.strip().lower(), [a.strip() for a in args.split(",")] if args else []))
    return out


class TciState:
    def __init__(self):
        self.trx: dict[int, TrxState] = {}
        self.device: str | None = None
        self.protocol: str | None = None
        self.ready = False

    def _t(self, idx: str) -> TrxState:
        i = int(idx)
        return self.trx.setdefault(i, TrxState())

    def apply(self, cmd: str, args: list[str]) -> set[int]:
        """Apply one command; return the set of trx indexes whose state changed."""
        try:
            if cmd == "vfo" and len(args) >= 3:
                t = self._t(args[0])
                hz = int(float(args[2]))
                if t.vfo.get(int(args[1])) != hz:
                    t.vfo[int(args[1])] = hz
                    return {int(args[0])}
            elif cmd == "modulation" and len(args) >= 2:
                t = self._t(args[0])
                if t.modulation != args[1]:
                    t.modulation = args[1]
                    return {int(args[0])}
            elif cmd == "split_enable" and len(args) >= 2:
                t = self._t(args[0])
                v = args[1].lower() == "true"
                if t.split != v:
                    t.split = v
                    return {int(args[0])}
            elif cmd == "drive":
                # TCI 1.x: drive:value  /  TCI 1.5+: drive:trx,value
                trx, value = ("0", args[0]) if len(args) == 1 else (args[0], args[1])
                t = self._t(trx)
                v = int(float(value))
                if t.drive != v:
                    t.drive = v
                    return {int(trx)}
            elif cmd == "trx" and len(args) >= 2:
                self._t(args[0]).tx = args[1].lower() == "true"
            elif cmd == "device" and args:
                self.device = args[0]
            elif cmd == "protocol" and args:
                self.protocol = ",".join(args)
            elif cmd == "ready":
                self.ready = True
        except ValueError:
            log.debug("Ignoring malformed TCI command %s:%s", cmd, args)
        return set()


class TciClient:
    def __init__(self, url: str, on_change: Callable[[int, TrxState], None],
                 reconnect_delay: float = 5.0):
        self.url = url
        self.on_change = on_change
        self.reconnect_delay = reconnect_delay
        self.state = TciState()

    def handle_text(self, text: str) -> None:
        changed: set[int] = set()
        for cmd, args in parse_commands(text):
            changed |= self.state.apply(cmd, args)
        for idx in changed:
            self.on_change(idx, self.state.trx[idx])

    async def run(self) -> None:
        import websockets  # imported lazily so the rest of the relay works without it

        while True:
            try:
                async with websockets.connect(self.url, ping_interval=20, ping_timeout=20,
                                              max_size=None) as ws:
                    log.info("Connected to TCI server %s", self.url)
                    self.state = TciState()
                    async for message in ws:
                        if isinstance(message, str):
                            self.handle_text(message)
                        # binary frames carry IQ/audio streams; ignore them
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.warning("TCI connection to %s lost (%s); retrying in %.0fs",
                            self.url, e, self.reconnect_delay)
            await asyncio.sleep(self.reconnect_delay)
