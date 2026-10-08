"""Pushes live rig state (frequency/mode/power) to Wavelog's /api/v2/radio endpoint.

Updates are coalesced: a burst of changes (spinning the VFO) results in at most one POST
per `min_interval`, and an unchanged state is re-sent every `heartbeat` seconds so Wavelog
keeps showing the radio as live.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from .wavelog import WavelogClient, WavelogError

log = logging.getLogger("radio")


class RadioReporter:
    def __init__(self, client: WavelogClient, name: str, *, min_interval: float = 1.0,
                 heartbeat: float = 30.0):
        self.client = client
        self.name = name
        self.min_interval = min_interval
        self.heartbeat = heartbeat
        self._state: dict[str, Any] = {}
        self._sent: dict[str, Any] | None = None
        self._last_post = 0.0
        self._changed = asyncio.Event()

    def update(self, **fields: Any) -> None:
        new = {k: v for k, v in fields.items() if v is not None}
        if not new.get("frequency"):
            return
        if new != self._state:
            self._state = new
            self._changed.set()

    async def run(self) -> None:
        while True:
            try:
                await asyncio.wait_for(self._changed.wait(), timeout=self.heartbeat)
            except asyncio.TimeoutError:
                pass
            self._changed.clear()
            if not self._state:
                continue
            wait = self.min_interval - (time.monotonic() - self._last_post)
            if wait > 0:
                await asyncio.sleep(wait)
            state = dict(self._state)
            try:
                await self.client.post_radio(self.name, **state)
                if state != self._sent:
                    log.debug("Radio %s -> %s", self.name, state)
                self._sent = state
            except WavelogError as e:
                log.warning("Radio update for %s failed: %s", self.name, e)
            self._last_post = time.monotonic()
