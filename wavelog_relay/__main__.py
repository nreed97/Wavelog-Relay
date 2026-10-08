"""Command line entry point: python -m wavelog_relay -c config.toml"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from . import __version__
from .app import Relay
from .config import ConfigError, load
from .wavelog import WavelogError


async def _check(relay: Relay) -> int:
    cfg = relay.cfg
    print(f"Wavelog API base: {relay.client.api_base}")
    try:
        st = await relay.client.station(cfg.wavelog.station_profile_id)
        print(f"Token OK. Station {cfg.wavelog.station_profile_id}: "
              f"{st.get('name')} / {st.get('callsign')} / {st.get('gridsquare')}")
    except WavelogError as e:
        print(f"Station check failed: {e}")
        if e.status == 403:
            print("  (the token lacks station:read; logging only needs qso:write)")
            return 0
        return 1
    return 0


async def _test_rotate(relay: Relay, callsign: str) -> int:
    await relay.setup_rotation()
    if not relay.rotation:
        print("Rotator is not enabled in the config")
        return 1
    target = await relay.rotation.resolve(callsign.upper())
    if target is None:
        print(f"No location found for {callsign}")
        return 1
    az = relay.rotation.azimuth_for(target)
    if az is None:
        print("Home location unknown")
        return 1
    print(f"{target.callsign}: {target.how} -> {az:.0f}°")
    relay.rotation.point_at(target, "manual test")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="wavelog-relay", description=__doc__)
    ap.add_argument("-c", "--config", default="config.toml", help="path to config.toml")
    ap.add_argument("--check", action="store_true", help="verify the Wavelog token and exit")
    ap.add_argument("--rotate", metavar="CALL", help="point the antenna at CALL and exit")
    ap.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = ap.parse_args(argv)

    try:
        cfg = load(args.config)
    except ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 2

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else getattr(logging, cfg.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)-8s %(message)s",
    )
    relay = Relay(cfg)
    try:
        if args.check:
            return asyncio.run(_check(relay))
        if args.rotate:
            return asyncio.run(_test_rotate(relay, args.rotate))
        asyncio.run(relay.run_forever())
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
