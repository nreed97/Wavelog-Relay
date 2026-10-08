# Wavelog Relay

A small, always-on bridge between your shack software and [Wavelog](https://www.wavelog.org/)
using the **Wavelog REST API v2** (`/api/v2`, `wl2_` Bearer tokens).

```
 WSJT-X / JTDX (any number) ─┐  UDP 2237…        ┌─► Wavelog  POST /api/v2/qso    (logged QSOs, ADIF)
 N1MM Logger+ ───────────────┼─ UDP 12060 ─► relay ─► Wavelog  POST /api/v2/radio  (live freq/mode/power)
 TCI radio (ExpertSDR, …) ───┘  WebSocket 40001   └─► PstRotatorAz  UDP 12000     (turn antenna to the DX)
```

## What it does

| Source | Logs QSOs to Wavelog | Turns the antenna | Reports the radio |
|---|---|---|---|
| **WSJT-X / JTDX / MSHV** (multiple listeners and/or multiple instances per port) | yes, from the *Logged ADIF* message | when you select a DX call (uses the DX grid from WSJT-X) | optional, dial freq + mode |
| **N1MM Logger+** | yes, from `<contactinfo>` (multi-PC safe: only the originating copy) | when a call is entered (`<lookupinfo>`) and/or logged | optional, from `<RadioInfo>` |
| **TCI** | – | – | yes: VFO A/B (split), mode, power (drive % × `max_power`) |

**Antenna heading.** The relay works out where the station is in this order: grid from WSJT-X/N1MM
→ grid from an earlier QSO in your Wavelog log (`GET /api/v2/lookup`) → DXCC entity centre from the
same lookup. It then computes the short-path (or long-path) great-circle bearing from your QTH and
sends `<PST><AZIMUTH>nnn</AZIMUTH></PST>` to PstRotatorAz. Fast changes are debounced and small
changes (< `min_change` degrees) are ignored, so the rotator is not hammered.

**No lost QSOs.** Every QSO goes into an on-disk spool first. If Wavelog or the internet is down the
relay keeps retrying with backoff, also across restarts. QSOs that Wavelog rejects (validation
errors) are appended to `wavelog-relay-failed.adi` for manual import. Duplicate datagrams are dropped.

**Plays well with others.** UDP ports are exclusive, so each listener can `forward` packets to
GridTracker, JTAlert, etc. For WSJT-X, replies from those apps (click-to-call, halt TX) are passed back
to the right WSJT-X instance. Alternatively use a WSJT-X multicast group.

## Install

Python 3.11+.

```bash
pip install .            # or: pip install websockets && python -m wavelog_relay ...
cp config.example.toml config.toml
```

## Wavelog setup

1. In Wavelog create an **API v2 token** (starts with `wl2_`) with these scopes:
   - `qso:write` — upload QSOs
   - `radio:write` — live radio status (TCI / WSJT-X / N1MM)
   - `lookup:read` — locate calls without a grid for the rotator
   - `station:read` — read your home grid from the station profile (or set `my_grid`)
2. Note the **station location id(s)** you want to log into (`station_profile_id`).

## Program setup

- **WSJT-X**: *Settings → Reporting → UDP Server* `127.0.0.1`, port `2237` (match `[[wsjtx]]`).
  Running several instances (`wsjtx --rig-name=6m`) on one port is fine; give each its own station
  location / radio name under `[wsjtx.instances."WSJT-X - 6m"]`, or point each at its own port with
  its own `[[wsjtx]]` block.
- **N1MM Logger+**: *Config → Configure Ports… → Broadcast Data*: enable **Contacts**, **Lookup**
  (for rotation) and optionally **Radio**; set the address to `127.0.0.1:12060`.
- **PstRotatorAz**: enable *UDP Control* (default port 12000).
- **TCI**: enable the TCI server in ExpertSDR/Thetis (default `ws://127.0.0.1:40001`).

## Run

```bash
wavelog-relay -c config.toml --check          # verify URL + token
wavelog-relay -c config.toml --rotate JA1XYZ  # test the rotator
wavelog-relay -c config.toml                  # run (add -v for debug logging)
```

Run it as a service (Windows Task Scheduler / NSSM, or systemd) so it is always up while you operate.

## Notes and limits

- Wavelog's API needs `/index.php/api/v2/...` unless your web server rewrites URLs; set
  `index_php = false` if yours does.
- N1MM edits (`contactreplace`) and deletes are not mirrored into Wavelog; a warning is logged.
- WSJT-X's *QSO Logged* (type 5) message is ignored in favour of *Logged ADIF* (type 12), which carries
  every field.

## Development

```bash
pip install -e '.[test]'
pytest
```

The end-to-end test runs the relay against a fake Wavelog server, a fake TCI server and a fake
PstRotatorAz socket.
