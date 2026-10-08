"""PstRotatorAz UDP control and the logic that turns a callsign into an antenna heading.

PstRotator / PstRotatorAz listen for UDP commands (default port 12000) such as
    <PST><AZIMUTH>123</AZIMUTH></PST>
and, when queried with <PST>AZ?</PST>, answer on port+1 with "AZ:123".
"""

from __future__ import annotations

import asyncio
import logging
import socket
import time
from dataclasses import dataclass

from . import geo
from .wavelog import WavelogClient, WavelogError

log = logging.getLogger("rotator")


class PstRotatorAz:
    def __init__(self, host: str = "127.0.0.1", port: int = 12000):
        self.host = host
        self.port = port
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def _send(self, text: str) -> None:
        self._sock.sendto(text.encode("ascii"), (self.host, self.port))

    def set_azimuth(self, azimuth: float) -> None:
        az = int(round(azimuth)) % 360
        self._send(f"<PST><AZIMUTH>{az}</AZIMUTH></PST>")

    def stop(self) -> None:
        self._send("<PST><STOP>1</STOP></PST>")

    def close(self) -> None:
        self._sock.close()


@dataclass
class Target:
    callsign: str
    lat: float
    lon: float
    how: str  # where the location came from (grid / logbook grid / DXCC)


def _angle_diff(a: float, b: float) -> float:
    d = abs(a - b) % 360.0
    return min(d, 360.0 - d)


class RotationController:
    """Resolves a callsign's location and points the antenna at it.

    Location priority: the grid the source program reported (WSJT-X DX grid, N1MM
    gridsquare) -> the grid from a previous QSO in your Wavelog logbook -> the DXCC
    entity centre from the Wavelog lookup.
    """

    def __init__(self, rotator: PstRotatorAz, client: WavelogClient | None,
                 home: tuple[float, float] | None, *, offset: float = 0.0,
                 long_path: bool = False, min_change: float = 3.0, debounce: float = 1.0,
                 lookup_cache_seconds: float = 3600.0):
        self.rotator = rotator
        self.client = client
        self.home = home
        self.offset = offset
        self.long_path = long_path
        self.min_change = min_change
        self.debounce = debounce
        self.lookup_cache_seconds = lookup_cache_seconds
        self._cache: dict[str, tuple[float, dict | None]] = {}
        self._pending: asyncio.Task | None = None
        self._last_az: float | None = None

    def request(self, callsign: str, grid: str | None = None, source: str = "", *,
                immediate: bool = False) -> None:
        """Ask to turn towards a callsign. Rapid repeated requests (typing in N1MM, clicking
        through WSJT-X decodes) are debounced so only the last one moves the antenna."""
        callsign = (callsign or "").strip().upper()
        if not callsign:
            return
        if self._pending and not self._pending.done():
            self._pending.cancel()
        delay = 0.0 if immediate else self.debounce
        self._pending = asyncio.create_task(self._run(callsign, grid, source, delay))

    async def _run(self, callsign: str, grid: str | None, source: str, delay: float) -> None:
        try:
            if delay:
                await asyncio.sleep(delay)
            target = await self.resolve(callsign, grid)
            if target is None:
                log.info("No location known for %s; not rotating", callsign)
                return
            self.point_at(target, source)
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001 - never let a rotation failure kill the relay
            log.exception("Rotation to %s failed", callsign)

    async def _lookup(self, callsign: str) -> dict | None:
        if self.client is None:
            return None
        now = time.time()
        hit = self._cache.get(callsign)
        if hit and now - hit[0] < self.lookup_cache_seconds:
            return hit[1]
        try:
            data = await self.client.lookup(callsign)
        except WavelogError as e:
            log.warning("Wavelog lookup of %s failed: %s", callsign, e)
            return None
        self._cache[callsign] = (now, data if isinstance(data, dict) else None)
        return self._cache[callsign][1]

    async def resolve(self, callsign: str, grid: str | None = None) -> Target | None:
        if grid and geo.is_valid_grid(grid):
            lat, lon = geo.grid_to_latlon(grid)
            return Target(callsign, lat, lon, f"grid {grid.upper()}")

        data = await self._lookup(callsign)
        if not data:
            return None

        logged_grid = (data.get("gridsquare") or "").split(",")[0].strip()
        if logged_grid and geo.is_valid_grid(logged_grid):
            lat, lon = geo.grid_to_latlon(logged_grid)
            return Target(callsign, lat, lon, f"logbook grid {logged_grid.upper()}")

        try:
            lat, lon = float(data.get("dxcc_lat")), float(data.get("dxcc_long"))
        except (TypeError, ValueError):
            return None
        return Target(callsign, lat, lon, f"DXCC {data.get('dxcc') or '?'}")

    def azimuth_for(self, target: Target) -> float | None:
        if self.home is None:
            return None
        az = geo.bearing(self.home[0], self.home[1], target.lat, target.lon)
        if self.long_path:
            az = (az + 180.0) % 360.0
        return (az + self.offset) % 360.0

    def point_at(self, target: Target, source: str = "") -> None:
        az = self.azimuth_for(target)
        if az is None:
            log.warning("Home location unknown; cannot compute bearing to %s", target.callsign)
            return
        if self._last_az is not None and _angle_diff(az, self._last_az) < self.min_change:
            log.info("%s is at %.0f°; antenna already within %.0f°", target.callsign, az, self.min_change)
            return
        dist = geo.distance_km(self.home[0], self.home[1], target.lat, target.lon)
        log.info("Rotating to %.0f° for %s (%s, %.0f km)%s", az, target.callsign, target.how, dist,
                 f" [{source}]" if source else "")
        self.rotator.set_azimuth(az)
        self._last_az = az
