"""Wires the sources (WSJT-X, N1MM, TCI) to the sinks (Wavelog, PstRotatorAz)."""

from __future__ import annotations

import asyncio
import logging

from . import adif, n1mm, wsjtx
from .config import Config, N1mmConfig, TciConfig, WsjtxConfig, parse_hostport
from .geo import grid_to_latlon, is_valid_grid
from .radio import RadioReporter
from .rotator import PstRotatorAz, RotationController
from .tci import TciClient, TrxState
from .wavelog import QsoUploader, WavelogClient, WavelogError

log = logging.getLogger("relay")


class Relay:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        wl = cfg.wavelog
        self.client = WavelogClient(wl.url, wl.token, index_php=wl.index_php,
                                    timeout=wl.timeout, verify_tls=wl.verify_tls)
        self.uploader = QsoUploader(self.client, cfg.spool_file, cfg.failed_file)
        self.reporters: dict[str, RadioReporter] = {}
        self.rotation: RotationController | None = None
        self.tasks: list[asyncio.Task] = []
        self.transports: list[asyncio.DatagramTransport] = []

    # -- shared helpers ----------------------------------------------------

    def reporter(self, name: str | None, *, heartbeat: float = 30.0,
                 min_interval: float = 1.0) -> RadioReporter | None:
        if not name:
            return None
        if name not in self.reporters:
            self.reporters[name] = RadioReporter(self.client, name, heartbeat=heartbeat,
                                                 min_interval=min_interval)
        return self.reporters[name]

    def rotate(self, callsign: str | None, grid: str | None, source: str) -> None:
        if self.rotation and callsign:
            self.rotation.request(callsign, grid, source)

    async def _resolve_home(self) -> tuple[float, float] | None:
        r = self.cfg.rotator
        if r.my_lat is not None and r.my_lon is not None:
            return r.my_lat, r.my_lon
        if r.my_grid:
            return grid_to_latlon(r.my_grid)
        try:
            station = await self.client.station(self.cfg.wavelog.station_profile_id)
        except WavelogError as e:
            log.error("Could not read station %s from Wavelog to find your grid (%s). "
                      "Set [rotator] my_grid instead.", self.cfg.wavelog.station_profile_id, e)
            return None
        grid = (station or {}).get("gridsquare") or ""
        if not is_valid_grid(grid):
            log.error("Station %s has no usable gridsquare; set [rotator] my_grid",
                      self.cfg.wavelog.station_profile_id)
            return None
        log.info("Home location from Wavelog station %s: %s", self.cfg.wavelog.station_profile_id, grid)
        return grid_to_latlon(grid)

    # -- startup -----------------------------------------------------------

    async def setup_rotation(self) -> None:
        r = self.cfg.rotator
        if not r.enabled or self.rotation:
            return
        home = await self._resolve_home()
        self.rotation = RotationController(
            PstRotatorAz(r.host, r.port), self.client, home, offset=r.offset,
            long_path=r.long_path, min_change=r.min_change, debounce=r.debounce)
        log.info("PstRotatorAz control on udp://%s:%d", r.host, r.port)

    async def start(self) -> None:
        log.info("Wavelog API: %s", self.client.api_base)

        await self.setup_rotation()

        for c in self.cfg.wsjtx:
            src = WsjtxSource(self, c)
            self.transports.append(await src.open())
        for c in self.cfg.n1mm:
            src = N1mmSource(self, c)
            self.transports.append(await src.open())
        for c in self.cfg.tci:
            self.tasks.append(asyncio.create_task(TciSource(self, c).client.run(), name=f"tci:{c.name}"))

        self.tasks.append(asyncio.create_task(self.uploader.run(), name="uploader"))
        for rep in self.reporters.values():
            self.tasks.append(asyncio.create_task(rep.run(), name=f"radio:{rep.name}"))

    async def run_forever(self) -> None:
        await self.start()
        try:
            await asyncio.gather(*self.tasks)
        finally:
            for t in self.transports:
                t.close()
            for t in self.tasks:
                t.cancel()


class _UdpSource:
    def __init__(self, relay: Relay, name: str, forward: list[str]):
        self.relay = relay
        self.name = name
        self.forward = [parse_hostport(f) for f in forward]
        self.transport: asyncio.DatagramTransport | None = None

    def _forward(self, data: bytes) -> None:
        for target in self.forward:
            self.transport.sendto(data, target)

    def _is_forward_target(self, addr: tuple) -> bool:
        return (addr[0], addr[1]) in self.forward


class WsjtxSource(_UdpSource):
    def __init__(self, relay: Relay, cfg: WsjtxConfig):
        super().__init__(relay, cfg.name, cfg.forward)
        self.cfg = cfg
        self.peers: dict[str, tuple] = {}            # WSJT-X instance id -> its address
        self.last_dx: dict[str, tuple[str, str]] = {}
        # Create radio reporters up front so Relay.start() schedules them.
        relay.reporter(cfg.radio)
        for inst in cfg.instances.values():
            relay.reporter(inst.radio)

    async def open(self) -> asyncio.DatagramTransport:
        self.transport = await wsjtx.open_udp_listener(
            self.cfg.bind, self.cfg.port, self.on_datagram,
            self.cfg.multicast_group, self.cfg.multicast_interface)
        where = self.cfg.multicast_group or self.cfg.bind
        log.info("[%s] listening for WSJT-X on udp://%s:%d", self.name, where, self.cfg.port)
        return self.transport

    def _instance(self, ident: str):
        inst = self.cfg.instances.get(ident)
        spid = (inst and inst.station_profile_id) or self.cfg.station_profile_id \
            or self.relay.cfg.wavelog.station_profile_id
        radio = (inst and inst.radio) or self.cfg.radio
        rotate = self.cfg.rotate if inst is None or inst.rotate is None else inst.rotate
        return spid, radio, rotate

    def on_datagram(self, data: bytes, addr: tuple) -> None:
        if self._is_forward_target(addr):
            # A downstream app (GridTracker, JTAlert, ...) replying to WSJT-X: pass it back.
            msg_id = None
            try:
                r = wsjtx._Reader(data)
                if r.u32() == wsjtx.MAGIC:
                    r.u32(), r.u32()
                    msg_id = r.utf8()
            except EOFError:
                pass
            peer = self.peers.get(msg_id or "") or next(iter(self.peers.values()), None)
            if peer:
                self.transport.sendto(data, peer)
            return

        self._forward(data)
        msg = wsjtx.decode(data)
        if msg is None:
            return
        self.peers[msg.id] = addr

        if isinstance(msg, wsjtx.Heartbeat):
            if msg.id not in self.last_dx:
                log.info("[%s] WSJT-X instance %r (%s) at %s:%d", self.name, msg.id,
                         msg.version or "?", addr[0], addr[1])
                self.last_dx[msg.id] = ("", "")
        elif isinstance(msg, wsjtx.Status):
            self._on_status(msg)
        elif isinstance(msg, wsjtx.LoggedAdif):
            self._on_logged(msg)
        elif isinstance(msg, wsjtx.Close):
            log.info("[%s] WSJT-X instance %r closed", self.name, msg.id)
            self.last_dx.pop(msg.id, None)

    def _on_status(self, st: wsjtx.Status) -> None:
        _, radio, rotate = self._instance(st.id)
        rep = self.relay.reporter(radio)
        if rep and st.dial_frequency:
            rep.update(frequency=st.dial_frequency, mode=(st.mode or None))

        call = (st.dx_call or "").strip().upper()
        grid = (st.dx_grid or "").strip()
        if rotate and call and (call, grid) != self.last_dx.get(st.id):
            self.relay.rotate(call, grid, f"{self.name}/{st.id}")
        self.last_dx[st.id] = (call, grid)

    def _on_logged(self, msg: wsjtx.LoggedAdif) -> None:
        if not self.cfg.log_qsos:
            return
        spid, _, _ = self._instance(msg.id)
        for rec in adif.parse(msg.adif):
            call = rec.get("call", "").upper()
            if not call:
                continue
            summary = f"{call} {rec.get('band', '')} {rec.get('mode', '')}".strip()
            key = "|".join([call, rec.get("qso_date", ""), rec.get("time_on", "")[:4],
                            rec.get("band", "").lower(), rec.get("mode", "").upper()])
            self.relay.uploader.submit(adif.make_document([adif.make_record(rec)]), spid,
                                       f"{self.name}/{msg.id}", summary, dedupe_key=key)


class N1mmSource(_UdpSource):
    def __init__(self, relay: Relay, cfg: N1mmConfig):
        super().__init__(relay, cfg.name, cfg.forward)
        self.cfg = cfg
        relay.reporter(cfg.radio)

    async def open(self) -> asyncio.DatagramTransport:
        self.transport = await wsjtx.open_udp_listener(self.cfg.bind, self.cfg.port, self.on_datagram)
        log.info("[%s] listening for N1MM+ on udp://%s:%d", self.name, self.cfg.bind, self.cfg.port)
        return self.transport

    def on_datagram(self, data: bytes, addr: tuple) -> None:
        self._forward(data)
        parsed = n1mm.parse(data)
        if parsed is None:
            return
        kind, f = parsed
        if kind == "contactinfo":
            self._on_contact(f)
        elif kind == "lookupinfo":
            if self.cfg.rotate_on_lookup:
                self.relay.rotate(f.get("call"), f.get("gridsquare"), self.name)
        elif kind == "radioinfo":
            self._on_radio(f)
        elif kind in ("contactreplace", "contactdelete"):
            log.warning("[%s] N1MM %s for %s is not synchronised to Wavelog; edit it there by hand",
                        self.name, kind, f.get("call"))

    def _on_contact(self, f: dict[str, str]) -> None:
        if not n1mm.is_original(f):
            return
        if self.cfg.rotate_on_log:
            self.relay.rotate(f.get("call"), f.get("gridsquare"), self.name)
        if not self.cfg.log_qsos:
            return
        try:
            rec = n1mm.contact_to_adif(f)
        except ValueError as e:
            log.error("[%s] cannot convert N1MM contact %s: %s", self.name, f.get("call"), e)
            return
        if not rec["CALL"]:
            return
        spid = self.cfg.station_profile_id or self.relay.cfg.wavelog.station_profile_id
        summary = f"{rec['CALL']} {rec.get('BAND') or ''} {rec.get('MODE') or ''}".strip()
        key = f.get("id") or "|".join(str(rec.get(k) or "") for k in ("CALL", "QSO_DATE", "TIME_ON", "BAND", "MODE"))
        self.relay.uploader.submit(adif.make_document([adif.make_record(rec)]), spid,
                                   self.name, summary, dedupe_key=f"n1mm|{key}")

    def _on_radio(self, f: dict[str, str]) -> None:
        rep = self.relay.reporter(self.cfg.radio)
        if not rep:
            return
        try:
            if int(f.get("radionr") or 1) != self.cfg.radio_nr:
                return
        except ValueError:
            return
        rx = n1mm.freq_hz(f.get("freq"))
        tx = n1mm.freq_hz(f.get("txfreq")) or rx
        split = (f.get("issplit") or "").lower() == "true"
        rep.update(frequency=tx, frequency_rx=rx if split and rx != tx else None,
                   mode=(f.get("mode") or "").upper()[:10] or None)


class TciSource:
    def __init__(self, relay: Relay, cfg: TciConfig):
        self.relay = relay
        self.cfg = cfg
        self.reporter = relay.reporter(cfg.radio or cfg.name, heartbeat=cfg.heartbeat,
                                       min_interval=cfg.min_interval)
        self.client = TciClient(cfg.url, self.on_change)

    def on_change(self, idx: int, st: TrxState) -> None:
        if idx != self.cfg.trx or not self.reporter:
            return
        power = None
        if self.cfg.max_power is not None and st.drive is not None:
            power = round(self.cfg.max_power * st.drive / 100.0, 1)
        rx, tx = st.rx_frequency, st.tx_frequency
        self.reporter.update(frequency=tx, frequency_rx=rx if rx != tx else None,
                             mode=st.mode, power=power)
