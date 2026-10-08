"""Run the whole relay against a fake Wavelog server, fake TCI server and fake PstRotatorAz."""

import asyncio
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import websockets

from wavelog_relay import wsjtx
from wavelog_relay.app import Relay
from wavelog_relay.config import from_dict
from tests.test_units import N1MM_CONTACT


class FakeWavelog:
    def __init__(self):
        self.requests = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _reply(self, status, data):
                body = json.dumps({"data": data}).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                outer.requests.append(("GET", self.path, self.headers.get("Authorization"), None))
                if "/station/1" in self.path:
                    self._reply(200, {"id": 1, "gridsquare": "FN31pr"})
                elif "/lookup" in self.path:
                    self._reply(200, {"callsign": "DL1ABC", "gridsquare": "", "dxcc": "Germany",
                                      "dxcc_lat": "51", "dxcc_long": "10"})
                else:
                    self._reply(404, None)

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append(("POST", self.path, self.headers.get("Authorization"), body))
                if self.path.endswith("/qso"):
                    self._reply(201, {"parsed": 1, "imported": 1, "skipped": 0, "messages": []})
                else:
                    self._reply(200, {"id": 1})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def posts(self, suffix):
        return [r[3] for r in self.requests if r[0] == "POST" and r[1].endswith(suffix)]


def free_udp_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


async def wait_for(cond, timeout=5.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not cond():
        if loop.time() > end:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.05)


def test_relay_end_to_end(tmp_path):
    asyncio.run(_scenario(tmp_path))


async def _scenario(tmp_path):
    wl = FakeWavelog()

    # Fake PstRotatorAz
    rot = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rot.bind(("127.0.0.1", 0))
    rot.setblocking(False)
    rot_port = rot.getsockname()[1]

    # Fake TCI server
    async def tci_handler(ws):
        await ws.send("protocol:ExpertSDR3,1.9;device:SunSDR2PRO;trx_count:1;")
        await ws.send("vfo:0,0,14074000;modulation:0,digu;drive:0,50;ready;")
        await asyncio.sleep(10)

    tci_server = await websockets.serve(tci_handler, "127.0.0.1", 0)
    tci_port = tci_server.sockets[0].getsockname()[1]

    wsjtx_port, n1mm_port = free_udp_port(), free_udp_port()
    cfg = from_dict({
        "wavelog": {"url": wl.url, "token": "wl2_test", "station_profile_id": 1},
        "relay": {"spool_file": str(tmp_path / "spool.jsonl"), "failed_file": str(tmp_path / "f.adi")},
        "rotator": {"enabled": True, "port": rot_port, "debounce": 0.1},
        "wsjtx": [{"name": "W", "port": wsjtx_port,
                   "instances": {"WSJT-X - 6m": {"station_profile_id": 7}}}],
        "n1mm": [{"name": "N", "bind": "127.0.0.1", "port": n1mm_port}],
        "tci": [{"name": "T", "url": f"ws://127.0.0.1:{tci_port}", "radio": "SunSDR", "max_power": 100}],
    })
    relay = Relay(cfg)
    task = asyncio.create_task(relay.run_forever())
    await wait_for(lambda: relay.rotation is not None and len(relay.transports) == 2)

    tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    # WSJT-X: select a DX station with a grid -> antenna turns (FN31 -> PM95 Japan ~ 330 deg)
    tx.sendto(wsjtx.encode_status("WSJT-X - 6m", 50313000, "FT8", "JA1XYZ", "PM95"), ("127.0.0.1", wsjtx_port))
    loop = asyncio.get_running_loop()
    data = await asyncio.wait_for(loop.sock_recv(rot, 1024), 5)
    az = int(data.decode().split("<AZIMUTH>")[1].split("<")[0])
    assert 320 <= az <= 345, az

    # WSJT-X logs a QSO from the 6m instance -> station 7
    adif_text = ("<adif_ver:5>3.1.0<eoh><call:6>JA1XYZ<gridsquare:4>PM95<mode:3>FT8<qso_date:8>20261008"
                 "<time_on:6>120000<band:2>6m<freq:9>50.314500<eor>")
    for _ in range(2):  # second copy is deduplicated
        tx.sendto(wsjtx.encode_logged_adif("WSJT-X - 6m", adif_text), ("127.0.0.1", wsjtx_port))
    await wait_for(lambda: len(wl.posts("/qso")) >= 1)

    # N1MM: lookup (no grid) -> rotate via Wavelog DXCC lookup; contact -> logged to default station
    tx.sendto(b"<lookupinfo><call>DL1ABC</call></lookupinfo>", ("127.0.0.1", n1mm_port))
    data = await asyncio.wait_for(loop.sock_recv(rot, 1024), 5)
    az = int(data.decode().split("<AZIMUTH>")[1].split("<")[0])
    assert 40 <= az <= 60, az
    tx.sendto(N1MM_CONTACT, ("127.0.0.1", n1mm_port))
    await wait_for(lambda: len(wl.posts("/qso")) >= 2)

    # TCI -> radio status
    await wait_for(lambda: any(p.get("radio") == "SunSDR" for p in wl.posts("/radio")))

    await asyncio.sleep(0.3)
    task.cancel()
    tci_server.close()
    tx.close()
    rot.close()
    wl.server.shutdown()

    qsos = wl.posts("/qso")
    assert len(qsos) == 2
    assert qsos[0]["station_profile_id"] == 7 and qsos[0]["import_type"] == "adif"
    assert "<CALL:6>JA1XYZ" in qsos[0]["adif"]
    assert qsos[1]["station_profile_id"] == 1 and "<CALL:6>DL1ABC" in qsos[1]["adif"]
    radio = [p for p in wl.posts("/radio") if p["radio"] == "SunSDR"][0]
    assert radio == {"radio": "SunSDR", "frequency": 14074000, "mode": "PKTUSB", "power": 50.0}
    assert all(r[2] == "Bearer wl2_test" for r in wl.requests)
    assert all(r[1].startswith("/index.php/api/v2/") for r in wl.requests)
    assert (tmp_path / "spool.jsonl").read_text() == ""
