import pytest

from wavelog_relay import adif, geo, n1mm, tci, wsjtx
from wavelog_relay.config import ConfigError, from_dict


def test_grid_to_latlon():
    lat, lon = geo.grid_to_latlon("FN31pr")
    assert lat == pytest.approx(41.729, abs=0.01)
    assert lon == pytest.approx(-72.708, abs=0.01)
    assert geo.grid_to_latlon("JO") == (55.0, 10.0)
    with pytest.raises(ValueError):
        geo.grid_to_latlon("ZZ99")


def test_bearing_known_paths():
    # Connecticut -> London is roughly NE (~50 degrees)
    ct = geo.grid_to_latlon("FN31")
    london = geo.grid_to_latlon("IO91")
    assert geo.bearing(*ct, *london) == pytest.approx(51, abs=3)
    # Due north / due east sanity
    assert geo.bearing(0, 0, 10, 0) == pytest.approx(0)
    assert geo.bearing(0, 0, 0, 10) == pytest.approx(90)


def test_adif_roundtrip_and_bands():
    rec = adif.make_record({"CALL": "K1ABC", "BAND": "20m", "MODE": "FT8", "NAME": None})
    parsed = adif.parse(adif.make_document([rec]))
    assert parsed == [{"call": "K1ABC", "band": "20m", "mode": "FT8"}]
    assert adif.band_for_mhz(14.074) == "20m"
    assert adif.band_for_mhz(50.313) == "6m"
    assert adif.band_for_mhz(13.0) is None
    assert adif.format_mhz(14.0740) == "14.074"


def test_wsjtx_status_and_adif_decode():
    st = wsjtx.decode(wsjtx.encode_status("WSJT-X - IC7300", 14074000, "FT8", "JA1XYZ", "PM95"))
    assert isinstance(st, wsjtx.Status)
    assert (st.id, st.dial_frequency, st.mode, st.dx_call, st.dx_grid) == \
        ("WSJT-X - IC7300", 14074000, "FT8", "JA1XYZ", "PM95")

    text = "<adif_ver:5>3.1.0<eoh><call:6>JA1XYZ<band:3>20m<eor>"
    msg = wsjtx.decode(wsjtx.encode_logged_adif("WSJT-X", text))
    assert isinstance(msg, wsjtx.LoggedAdif) and msg.adif == text
    assert wsjtx.decode(b"garbage") is None


N1MM_CONTACT = b"""<?xml version="1.0" encoding="utf-8"?>
<contactinfo>
  <contestname>CQ-WW-SSB</contestname><timestamp>2026-10-08 14:23:05</timestamp>
  <mycall>W1AW</mycall><band>14</band><rxfreq>1420050</rxfreq><txfreq>1420050</txfreq>
  <operator>K1OP</operator><mode>USB</mode><call>dl1abc</call><snt>59</snt><sntnr>5</sntnr>
  <rcv>59</rcv><rcvnr>14</rcvnr><gridsquare>JO62</gridsquare><power>100</power>
  <IsOriginal>True</IsOriginal><ID>abc123</ID>
</contactinfo>"""


def test_n1mm_contact_to_adif():
    kind, f = n1mm.parse(N1MM_CONTACT)
    assert kind == "contactinfo" and n1mm.is_original(f)
    rec = n1mm.contact_to_adif(f)
    assert rec["CALL"] == "DL1ABC"
    assert rec["QSO_DATE"] == "20261008" and rec["TIME_ON"] == "142305"
    assert rec["BAND"] == "20m" and rec["FREQ"] == "14.2005"
    assert (rec["MODE"], rec["SUBMODE"]) == ("SSB", "USB")
    assert rec["STX"] == "5" and rec["SRX"] == "14"
    assert rec["CONTEST_ID"] == "CQ-WW-SSB" and rec["TX_PWR"] == "100"
    assert rec["FREQ_RX"] is None


def test_tci_state():
    st = tci.TciState()
    changed = set()
    for cmd, args in tci.parse_commands("vfo:0,0,7074000;modulation:0,digu;drive:0,40;vfo:0,1,7076000;"):
        changed |= st.apply(cmd, args)
    assert changed == {0}
    t = st.trx[0]
    assert (t.rx_frequency, t.tx_frequency, t.mode, t.drive) == (7074000, 7074000, "PKTUSB", 40)
    st.apply("split_enable", ["0", "true"])
    assert t.tx_frequency == 7076000


def test_config_validation():
    base = {"wavelog": {"url": "https://x", "token": "wl2_abc", "station_profile_id": 1}}
    cfg = from_dict({**base, "wsjtx": [{"port": 2237}, {"port": 2239, "instances": {"A": {"radio": "R"}}}]})
    assert cfg.wsjtx[1].instances["A"].radio == "R"
    with pytest.raises(ConfigError):
        from_dict({"wavelog": {**base["wavelog"], "token": "legacy"}})
    with pytest.raises(ConfigError):
        from_dict({**base, "wsjtx": [{"port": 2237}, {"port": 2237}]})
    with pytest.raises(ConfigError):
        from_dict({**base, "rotator": {"bogus": 1}})
