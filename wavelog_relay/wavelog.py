"""Wavelog REST API v2 client plus a persistent, retrying QSO upload queue.

API reference: https://docs.wavelog.org/developer/api-v2/
  - Auth:   Authorization: Bearer wl2_...  (v2 tokens only)
  - QSOs:   POST /api/v2/qso      {station_profile_id, import_type: "adif", adif}
  - Radios: POST /api/v2/radio    {radio, frequency, frequency_rx, mode, mode_rx, power}
  - Lookup: GET  /api/v2/lookup?callsign=...
  - Station GET  /api/v2/station/{id}
Responses use the envelope {"data": ...} / {"error": {"code", "message"}}.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import __version__

log = logging.getLogger("wavelog")


class WavelogError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(f"HTTP {status} {code}: {message}")
        self.status = status
        self.code = code
        self.message = message

    @property
    def retryable(self) -> bool:
        # Network errors (status 0), rate limiting and server-side failures are worth retrying;
        # validation/auth errors will fail the same way next time.
        return self.status == 0 or self.status == 429 or self.status >= 500


class WavelogClient:
    def __init__(self, url: str, token: str, *, index_php: bool = True, timeout: float = 15.0,
                 verify_tls: bool = True):
        base = url.rstrip("/")
        self.api_base = f"{base}/index.php/api/v2" if index_php else f"{base}/api/v2"
        self.token = token
        self.timeout = timeout
        self._ssl_context = None
        if not verify_tls:
            import ssl
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            self._ssl_context = ctx

    # -- low level ---------------------------------------------------------

    def _request_sync(self, method: str, path: str, body: dict | None = None,
                      query: dict | None = None) -> tuple[int, Any]:
        url = f"{self.api_base}/{path.lstrip('/')}"
        if query:
            url += "?" + urllib.parse.urlencode({k: v for k, v in query.items() if v is not None})
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self.token}")
        req.add_header("Accept", "application/json")
        req.add_header("User-Agent", f"WavelogRelay/{__version__}")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self._ssl_context) as resp:
                raw = resp.read()
                status = resp.status
        except urllib.error.HTTPError as e:
            raw = e.read()
            status = e.code
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise WavelogError(0, "network_error", str(getattr(e, "reason", e))) from e

        payload: Any = None
        if raw:
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                payload = None
        if status >= 400:
            err = (payload or {}).get("error", {}) if isinstance(payload, dict) else {}
            raise WavelogError(status, err.get("code", "http_error"),
                               err.get("message", raw[:200].decode(errors="replace") if raw else ""))
        return status, payload

    async def request(self, method: str, path: str, body: dict | None = None,
                      query: dict | None = None) -> Any:
        _, payload = await asyncio.to_thread(self._request_sync, method, path, body, query)
        if isinstance(payload, dict) and "data" in payload:
            return payload["data"]
        return payload

    # -- resources ---------------------------------------------------------

    async def status(self) -> Any:
        return await self.request("GET", "status")

    async def post_adif(self, adif: str, station_profile_id: int) -> dict:
        return await self.request("POST", "qso", {
            "station_profile_id": int(station_profile_id),
            "import_type": "adif",
            "adif": adif,
        })

    async def post_radio(self, radio: str, **fields: Any) -> dict:
        body = {"radio": radio}
        body.update({k: v for k, v in fields.items() if v is not None})
        return await self.request("POST", "radio", body)

    async def lookup(self, callsign: str) -> dict:
        return await self.request("GET", "lookup", query={"callsign": callsign})

    async def station(self, station_id: int) -> dict:
        return await self.request("GET", f"station/{int(station_id)}")


@dataclass
class PendingQso:
    adif: str
    station_profile_id: int
    source: str
    summary: str
    created: float
    attempts: int = 0

    def to_json(self) -> dict:
        return self.__dict__.copy()


class QsoUploader:
    """Uploads QSOs in order, retrying transient failures and persisting the backlog to disk
    so nothing is lost if Wavelog or the network is down, or the relay restarts."""

    def __init__(self, client: WavelogClient, spool_path: str | os.PathLike | None,
                 failed_path: str | os.PathLike | None = None, max_backoff: float = 300.0):
        self.client = client
        self.spool_path = Path(spool_path) if spool_path else None
        self.failed_path = Path(failed_path) if failed_path else None
        self.max_backoff = max_backoff
        self._pending: list[PendingQso] = []
        self._wake = asyncio.Event()
        self._recent: dict[str, float] = {}
        self._load_spool()

    def _load_spool(self) -> None:
        if not self.spool_path or not self.spool_path.exists():
            return
        try:
            for line in self.spool_path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    self._pending.append(PendingQso(**json.loads(line)))
            if self._pending:
                log.info("Loaded %d unsent QSO(s) from %s", len(self._pending), self.spool_path)
        except (OSError, ValueError, TypeError) as e:
            log.error("Could not read spool file %s: %s", self.spool_path, e)

    def _save_spool(self) -> None:
        if not self.spool_path:
            return
        try:
            self.spool_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.spool_path.with_suffix(self.spool_path.suffix + ".tmp")
            tmp.write_text("".join(json.dumps(q.to_json()) + "\n" for q in self._pending), encoding="utf-8")
            os.replace(tmp, self.spool_path)
        except OSError as e:
            log.error("Could not write spool file %s: %s", self.spool_path, e)

    def _record_failure(self, qso: PendingQso, error: Exception) -> None:
        log.error("QSO %s from %s rejected by Wavelog: %s", qso.summary, qso.source, error)
        if not self.failed_path:
            return
        try:
            self.failed_path.parent.mkdir(parents=True, exist_ok=True)
            with self.failed_path.open("a", encoding="utf-8") as f:
                f.write(f"# {time.strftime('%Y-%m-%d %H:%M:%SZ', time.gmtime())} {qso.source} "
                        f"station_profile_id={qso.station_profile_id}: {error}\n{qso.adif.strip()}\n")
        except OSError as e:
            log.error("Could not write failed-QSO file %s: %s", self.failed_path, e)

    def submit(self, adif: str, station_profile_id: int, source: str, summary: str,
               dedupe_key: str | None = None, dedupe_window: float = 600.0) -> bool:
        """Queue a QSO for upload. Returns False if it was dropped as a duplicate."""
        now = time.time()
        self._recent = {k: t for k, t in self._recent.items() if now - t < dedupe_window}
        if dedupe_key:
            if dedupe_key in self._recent:
                log.info("Ignoring duplicate QSO %s from %s", summary, source)
                return False
            self._recent[dedupe_key] = now
        self._pending.append(PendingQso(adif, int(station_profile_id), source, summary, now))
        self._save_spool()
        self._wake.set()
        log.info("Queued QSO %s from %s -> station %s", summary, source, station_profile_id)
        return True

    @property
    def backlog(self) -> int:
        return len(self._pending)

    async def run(self) -> None:
        backoff = 5.0
        while True:
            if not self._pending:
                self._wake.clear()
                await self._wake.wait()
                continue
            qso = self._pending[0]
            qso.attempts += 1
            try:
                result = await self.client.post_adif(qso.adif, qso.station_profile_id)
            except WavelogError as e:
                if e.retryable:
                    log.warning("Upload of %s failed (%s); retrying in %.0fs (%d queued)",
                                qso.summary, e, backoff, len(self._pending))
                    self._save_spool()
                    try:
                        await asyncio.wait_for(self._wake.wait(), timeout=backoff)
                    except asyncio.TimeoutError:
                        pass
                    self._wake.clear()
                    backoff = min(backoff * 2, self.max_backoff)
                    continue
                self._record_failure(qso, e)
            else:
                imported = (result or {}).get("imported", 0) if isinstance(result, dict) else 0
                skipped = (result or {}).get("skipped", 0) if isinstance(result, dict) else 0
                if imported:
                    log.info("Logged %s to Wavelog (station %s)", qso.summary, qso.station_profile_id)
                elif skipped:
                    log.info("Wavelog skipped %s as a duplicate", qso.summary)
                else:
                    log.warning("Wavelog accepted %s but imported nothing: %s", qso.summary, result)
            backoff = 5.0
            self._pending.pop(0)
            self._save_spool()
