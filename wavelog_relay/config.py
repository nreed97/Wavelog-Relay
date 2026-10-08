"""TOML configuration loading and validation."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .geo import is_valid_grid


class ConfigError(Exception):
    pass


@dataclass
class WavelogConfig:
    url: str
    token: str
    station_profile_id: int
    index_php: bool = True
    verify_tls: bool = True
    timeout: float = 15.0


@dataclass
class RotatorConfig:
    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 12000
    my_grid: str | None = None
    my_lat: float | None = None
    my_lon: float | None = None
    offset: float = 0.0
    long_path: bool = False
    min_change: float = 3.0
    debounce: float = 1.0


@dataclass
class InstanceConfig:
    station_profile_id: int | None = None
    radio: str | None = None
    rotate: bool | None = None


@dataclass
class WsjtxConfig:
    name: str
    port: int = 2237
    bind: str = "127.0.0.1"
    multicast_group: str | None = None
    multicast_interface: str = "0.0.0.0"
    station_profile_id: int | None = None
    log_qsos: bool = True
    rotate: bool = True
    radio: str | None = None
    forward: list[str] = field(default_factory=list)
    instances: dict[str, InstanceConfig] = field(default_factory=dict)


@dataclass
class N1mmConfig:
    name: str = "N1MM"
    port: int = 12060
    bind: str = "0.0.0.0"
    station_profile_id: int | None = None
    log_qsos: bool = True
    rotate_on_lookup: bool = True
    rotate_on_log: bool = False
    radio: str | None = None
    radio_nr: int = 1
    forward: list[str] = field(default_factory=list)


@dataclass
class TciConfig:
    name: str
    url: str = "ws://127.0.0.1:40001"
    trx: int = 0
    radio: str | None = None
    max_power: float | None = None
    heartbeat: float = 30.0
    min_interval: float = 1.0


@dataclass
class Config:
    wavelog: WavelogConfig
    rotator: RotatorConfig
    wsjtx: list[WsjtxConfig]
    n1mm: list[N1mmConfig]
    tci: list[TciConfig]
    spool_file: str | None = "wavelog-relay-spool.jsonl"
    failed_file: str | None = "wavelog-relay-failed.adi"
    log_level: str = "INFO"


def _build(cls, data: dict, where: str):
    known = set(cls.__dataclass_fields__)
    unknown = set(data) - known
    if unknown:
        raise ConfigError(f"[{where}] unknown key(s): {', '.join(sorted(unknown))}")
    try:
        return cls(**data)
    except TypeError as e:
        raise ConfigError(f"[{where}] {e}") from e


def parse_hostport(value: str, default_host: str = "127.0.0.1") -> tuple[str, int]:
    host, sep, port = value.rpartition(":")
    if not sep:
        return default_host, int(value)
    return host or default_host, int(port)


def load(path: str | os.PathLike) -> Config:
    p = Path(path)
    try:
        raw = tomllib.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise ConfigError(f"config file not found: {p}") from e
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{p}: {e}") from e
    return from_dict(raw)


def from_dict(raw: dict) -> Config:
    wl = dict(raw.get("wavelog") or {})
    token_env = wl.pop("token_env", None)
    if token_env:
        wl.setdefault("token", os.environ.get(token_env, ""))
    if not wl.get("token"):
        wl["token"] = os.environ.get("WAVELOG_TOKEN", "")
    for key in ("url", "token", "station_profile_id"):
        if not wl.get(key):
            raise ConfigError(f"[wavelog] {key} is required")
    if not str(wl["token"]).startswith("wl2_"):
        raise ConfigError("[wavelog] token must be an API v2 token (starts with wl2_)")
    wavelog = _build(WavelogConfig, wl, "wavelog")

    rotator = _build(RotatorConfig, dict(raw.get("rotator") or {}), "rotator")
    if rotator.my_grid and not is_valid_grid(rotator.my_grid):
        raise ConfigError(f"[rotator] my_grid {rotator.my_grid!r} is not a valid Maidenhead locator")

    wsjtx = []
    for i, item in enumerate(raw.get("wsjtx") or []):
        item = dict(item)
        item.setdefault("name", f"WSJT-X #{i + 1}")
        item["multicast_group"] = item.get("multicast_group") or None
        item["instances"] = {k: _build(InstanceConfig, dict(v), f"wsjtx.instances.{k}")
                             for k, v in (item.get("instances") or {}).items()}
        wsjtx.append(_build(WsjtxConfig, item, f"wsjtx #{i + 1}"))

    n1mm = [_build(N1mmConfig, dict(item), f"n1mm #{i + 1}")
            for i, item in enumerate(raw.get("n1mm") or [])]

    tci = []
    for i, item in enumerate(raw.get("tci") or []):
        item = dict(item)
        item.setdefault("name", f"TCI #{i + 1}")
        tci.append(_build(TciConfig, item, f"tci #{i + 1}"))

    relay = dict(raw.get("relay") or {})
    unknown = set(relay) - {"spool_file", "failed_file", "log_level"}
    if unknown:
        raise ConfigError(f"[relay] unknown key(s): {', '.join(sorted(unknown))}")

    ports = [(c.bind, c.port) for c in wsjtx] + [(c.bind, c.port) for c in n1mm]
    if len(set(ports)) != len(ports):
        raise ConfigError("two UDP listeners are configured on the same bind address and port")

    return Config(wavelog=wavelog, rotator=rotator, wsjtx=wsjtx, n1mm=n1mm, tci=tci,
                  spool_file=relay.get("spool_file", "wavelog-relay-spool.jsonl") or None,
                  failed_file=relay.get("failed_file", "wavelog-relay-failed.adi") or None,
                  log_level=relay.get("log_level", "INFO"))
