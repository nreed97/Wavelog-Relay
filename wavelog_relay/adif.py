"""Minimal ADIF helpers: build and parse records, map frequencies to bands."""

from __future__ import annotations

import re

# (lower MHz, upper MHz, ADIF band)
BANDS: list[tuple[float, float, str]] = [
    (0.1357, 0.1378, "2190m"),
    (0.472, 0.479, "630m"),
    (1.8, 2.0, "160m"),
    (3.5, 4.0, "80m"),
    (5.06, 5.45, "60m"),
    (7.0, 7.3, "40m"),
    (10.1, 10.15, "30m"),
    (14.0, 14.35, "20m"),
    (18.068, 18.168, "17m"),
    (21.0, 21.45, "15m"),
    (24.89, 24.99, "12m"),
    (28.0, 29.7, "10m"),
    (40.0, 45.0, "8m"),
    (50.0, 54.0, "6m"),
    (54.000001, 69.9, "5m"),
    (70.0, 71.0, "4m"),
    (144.0, 148.0, "2m"),
    (222.0, 225.0, "1.25m"),
    (420.0, 450.0, "70cm"),
    (902.0, 928.0, "33cm"),
    (1240.0, 1300.0, "23cm"),
    (2300.0, 2450.0, "13cm"),
    (3300.0, 3500.0, "9cm"),
    (5650.0, 5925.0, "6cm"),
    (10000.0, 10500.0, "3cm"),
    (24000.0, 24250.0, "1.25cm"),
    (47000.0, 47200.0, "6mm"),
    (75500.0, 81000.0, "4mm"),
]


def band_for_mhz(mhz: float | None) -> str | None:
    if mhz is None:
        return None
    for low, high, band in BANDS:
        if low <= mhz <= high:
            return band
    return None


def format_mhz(mhz: float) -> str:
    """Format MHz with up to 1 Hz resolution and no trailing zeros."""
    return f"{mhz:.6f}".rstrip("0").rstrip(".")


def field(name: str, value: object) -> str:
    text = str(value)
    # ADIF lengths count characters of the encoded data; Wavelog's parser is UTF-8 aware.
    return f"<{name.upper()}:{len(text)}>{text}"


def make_record(fields: dict[str, object]) -> str:
    """Build a single ADIF record terminated by <EOR>. Empty/None values are skipped."""
    parts = [field(k, v) for k, v in fields.items() if v is not None and str(v) != ""]
    return " ".join(parts) + " <EOR>\n"


def make_document(records: list[str], program: str = "WavelogRelay") -> str:
    header = field("ADIF_VER", "3.1.4") + " " + field("PROGRAMID", program) + " <EOH>\n"
    return header + "".join(records)


_TAG_RE = re.compile(r"<([A-Za-z0-9_]+)(?::(\d+)(?::[A-Za-z])?)?>")


def parse(text: str) -> list[dict[str, str]]:
    """Parse ADIF text into a list of records with lowercase field names."""
    records: list[dict[str, str]] = []
    eoh = re.search(r"<eoh>", text, re.IGNORECASE)
    pos = eoh.end() if eoh else 0
    current: dict[str, str] = {}
    while True:
        m = _TAG_RE.search(text, pos)
        if not m:
            break
        name = m.group(1).lower()
        if name == "eor":
            if current:
                records.append(current)
            current = {}
            pos = m.end()
            continue
        if m.group(2) is None:
            pos = m.end()
            continue
        length = int(m.group(2))
        value = text[m.end(): m.end() + length]
        current[name] = value
        pos = m.end() + length
    if current:
        records.append(current)
    return records
