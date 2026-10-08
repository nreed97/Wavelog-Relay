"""N1MM Logger+ UDP broadcasts (XML), default port 12060.

Handled packet types:
    <contactinfo>    a QSO was logged          -> uploaded to Wavelog
    <lookupinfo>     a call was typed/looked up -> rotate antenna
    <RadioInfo>      rig state                  -> Wavelog radio (optional)
    <contactreplace>/<contactdelete> are reported but not synchronised.

N1MM frequencies are in units of 10 Hz (e.g. 1407400 = 14.074 MHz).
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from datetime import datetime

from . import adif

log = logging.getLogger("n1mm")

# N1MM mode -> (ADIF MODE, ADIF SUBMODE)
MODE_MAP: dict[str, tuple[str, str | None]] = {
    "USB": ("SSB", "USB"),
    "LSB": ("SSB", "LSB"),
    "SSB": ("SSB", None),
    "CW": ("CW", None),
    "AM": ("AM", None),
    "FM": ("FM", None),
    "RTTY": ("RTTY", None),
    "PSK31": ("PSK", "PSK31"),
    "PSK63": ("PSK", "PSK63"),
    "PSK125": ("PSK", "PSK125"),
    "FT8": ("FT8", None),
    "FT4": ("MFSK", "FT4"),
    "JT65": ("JT65", None),
    "JT9": ("JT9", None),
    "MFSK": ("MFSK", None),
    "OLIVIA": ("OLIVIA", None),
}


def parse(data: bytes) -> tuple[str, dict[str, str]] | None:
    """Return (lowercase root tag, {lowercase child tag: text}) or None if not N1MM XML."""
    try:
        root = ET.fromstring(data.decode("utf-8", errors="replace").strip().lstrip("﻿"))
    except ET.ParseError:
        return None
    fields = {child.tag.lower(): (child.text or "").strip() for child in root}
    return root.tag.lower(), fields


def _freq_mhz(value: str | None) -> float | None:
    """N1MM 10 Hz units -> MHz."""
    try:
        v = float(value or "")
    except ValueError:
        return None
    return v / 100000.0 if v > 0 else None


def freq_hz(value: str | None) -> int | None:
    mhz = _freq_mhz(value)
    return int(round(mhz * 1_000_000)) if mhz else None


def is_original(fields: dict[str, str]) -> bool:
    """In a multi-computer N1MM network every node rebroadcasts; only log the originating copy."""
    return fields.get("isoriginal", "True").strip().lower() != "false"


def _is_number(value: str | None) -> bool:
    try:
        return float(value or "") > 0
    except ValueError:
        return False


def contact_to_adif(f: dict[str, str]) -> dict[str, object]:
    """Map an N1MM <contactinfo> packet to ADIF fields."""
    ts = f.get("timestamp", "")
    dt = None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y/%m/%d %H:%M:%S"):
        try:
            dt = datetime.strptime(ts, fmt)
            break
        except ValueError:
            continue
    if dt is None:
        raise ValueError(f"unparseable N1MM timestamp {ts!r}")

    tx = _freq_mhz(f.get("txfreq"))
    rx = _freq_mhz(f.get("rxfreq"))
    freq = tx or rx
    band = adif.band_for_mhz(freq)
    if band is None:
        # N1MM's <band> is the band edge in MHz (e.g. "14", "3.5").
        try:
            band = adif.band_for_mhz(float(f.get("band", "")))
        except ValueError:
            band = None

    raw_mode = (f.get("mode") or "").upper()
    mode, submode = MODE_MAP.get(raw_mode, (raw_mode, None))

    rec: dict[str, object] = {
        "CALL": f.get("call", "").upper(),
        "QSO_DATE": dt.strftime("%Y%m%d"),
        "TIME_ON": dt.strftime("%H%M%S"),
        "BAND": band,
        "MODE": mode,
        "SUBMODE": submode,
        "FREQ": adif.format_mhz(freq) if freq else None,
        "FREQ_RX": adif.format_mhz(rx) if rx and tx and abs(rx - tx) > 1e-6 else None,
        "RST_SENT": f.get("snt"),
        "RST_RCVD": f.get("rcv"),
        "STX": f.get("sntnr") if f.get("sntnr") not in (None, "", "0") else None,
        "SRX": f.get("rcvnr") if f.get("rcvnr") not in (None, "", "0") else None,
        "SRX_STRING": f.get("exchange1") or None,
        "GRIDSQUARE": f.get("gridsquare"),
        "NAME": f.get("name"),
        "QTH": f.get("qth"),
        "ARRL_SECT": f.get("section"),
        "PRECEDENCE": f.get("prec"),
        "CHECK": f.get("ck") if f.get("ck") not in (None, "", "0") else None,
        "TX_PWR": f.get("power") if _is_number(f.get("power")) else None,
        "COMMENT": f.get("comment"),
        "OPERATOR": (f.get("operator") or "").upper() or None,
        "STATION_CALLSIGN": (f.get("mycall") or "").upper() or None,
        "CONTEST_ID": f.get("contestname") if f.get("contestname") not in (None, "", "DXLOG") else None,
    }
    return rec
