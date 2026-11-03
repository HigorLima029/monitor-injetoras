import asyncio
import re
import time
from pathlib import Path

import pytest

from app.collector import Collector
from app.sources import OpcUaSource, SimulatedSource
from app.storage import Storage
from tests import fake_gateway

APP_DIR = Path(__file__).resolve().parents[1] / "app"


def test_codigo_nao_escreve_na_maquina():
    """Garantia de segurança: nenhuma chamada de escrita OPC-UA no código."""
    pattern = re.compile(r"write_value|set_value|write_attribute|write_params|set_attribute")
    for py in APP_DIR.glob("*.py"):
        assert not pattern.search(py.read_text(encoding="utf-8")), py.name


def test_le_gateway_falso(tmp_path):
    async def run():
        server, nodes = await fake_gateway.start(4851)
        try:
            src = OpcUaSource("opc.tcp://127.0.0.1:4851", 1, cache_file=str(tmp_path / "c.json"))
            await src.connect()
            tags = await src.list_tags()
            assert "ProductionCount" in tags and "WarnCycleTooLong" in tags
            vals = await src.read(["ProductionCount", "TempZ1Now", "WiFiSsid"])
            assert vals == {"ProductionCount": 100.0, "TempZ1Now": 268.4, "WiFiSsid": "Higorp"}

            storage = Storage(":memory:")
            col = Collector(src, storage, poll_seconds=0.1, full_poll_seconds=999)
            col.tags = tags
            await col.poll_once()
            assert col.machine_state() == "parada"

            await nodes["WarnCycleTooLong"].write_value(1.0)  # só o servidor de teste é alterado
            await nodes["ProductionCount"].write_value(101.0)
            await col.poll_once()
            assert col.active_alarms == {"WarnCycleTooLong"}
            assert col.machine_state() == "alarme"

            await nodes["WarnCycleTooLong"].write_value(0.0)
            await col.poll_once()
            assert col.machine_state() == "produzindo"
            events = storage.alarm_events(0)
            assert len(events) == 1 and events[0]["ended"] is not None
            await src.close()
            assert (tmp_path / "c.json").exists()
        finally:
            await server.stop()

    asyncio.run(run())


def test_incrementos_por_hora():
    s = Storage(":memory:")
    base = time.time() - 7200
    for i, count in enumerate([10, 12, 15, 15, 20, 26]):
        s.add_samples(base + i * 1200, {"ProductionCount": float(count)})
    per = s.increments("ProductionCount", base - 1, 3600)
    assert sum(v for _, v in per) == 16


def test_api_com_simulador(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app import main

    col = Collector(SimulatedSource(cycle_seconds=0.05, seed=1), Storage(":memory:"),
                    poll_seconds=0.05, full_poll_seconds=0.2)
    main.app.state.collector = col
    with TestClient(main.app) as client:
        time.sleep(0.6)
        live = client.get("/api/live").json()
        assert live["status"]["connected"] is True
        assert "TempZ1Now" in live["values"]
        hist = client.get("/api/history", params={"tags": "ProductionCount", "minutes": 5}).json()
        assert hist["series"]["ProductionCount"]
        prod = client.get("/api/production", params={"hours": 1}).json()
        assert prod["produced"] > 0
        assert client.get("/api/alarms").json()["monitored"] > 50
        tags = client.get("/api/tags", params={"q": "temp"}).json()
        assert all("temp" in r["tag"].lower() for r in tags["rows"])
        vals = client.get("/api/values", params={"tags": "Clamp1Pres,Open4Pos,Hold2Time,Nada"}).json()
        assert vals["values"]["Clamp1Pres"] == 40.0 and vals["values"]["Open4Pos"] == 360.0
        assert vals["values"]["Nada"] is None
        assert "Hold2Time" in col.fast_tags
        with client.websocket_connect("/ws") as ws:
            assert "status" in ws.receive_json()


def test_le_painel_br_euromap63():
    from app.em63 import Em63Source
    from tests import fake_br

    async def run():
        server, nodes = await fake_br.start(4852)
        try:
            src = Em63Source("opc.tcp://127.0.0.1:4852")
            await src.connect()
            tags = await src.list_tags()
            assert "ProductionCount" in tags and "EM63.pEm63a.myActIPAddr" in tags
            vals = await src.read(["ProductionCount", "BadProduction", "ProductionGoodCount",
                                   "CycleMonitor", "TempZ1", "TempZ1Now", "MaxTempZ1", "TempZ5",
                                   "TempOilNow", "WarnMachineError", "EM63.pEm63a.myActIPAddr"])
            assert vals["ProductionCount"] == 9283 and vals["BadProduction"] == 3
            assert vals["ProductionGoodCount"] == 9280 and vals["CycleMonitor"] == 31.4
            assert vals["TempZ1"] == 180.0 and vals["TempZ1Now"] == 176.5 and vals["MaxTempZ1"] == 20.0
            assert vals["TempZ5"] is None  # zona desligada no painel
            assert vals["TempOilNow"] == 41.0 and vals["WarnMachineError"] == 0.0
            assert vals["EM63.pEm63a.myActIPAddr"] == "10.86.20.253"

            col = Collector(src, Storage(":memory:"), poll_seconds=0.1, full_poll_seconds=999)
            col.tags = tags
            await col.poll_once()
            assert col.machine_state() == "parada"
            await nodes["pEm63m.ActStsMach"].write_value("Error")  # só o servidor de teste muda
            await col.poll_once()
            assert col.active_alarms == {"WarnMachineError"} and col.machine_state() == "alarme"
            await nodes["pEm63m.ActStsMach"].write_value("Production")
            await nodes["pEm63m.ActCntCyc"].write_value(9284)
            await col.poll_once()
            assert col.machine_state() == "produzindo"
            await src.close()
        finally:
            await server.stop()

    asyncio.run(run())


def _rfb_client_bytes():
    import struct as st
    return (b"RFB 003.008\n" + b"\x02" + b"A" * 16 + b"\x01"                  # versao, senha VNC, ClientInit
            + b"\x02\x00" + st.pack(">H", 2) + st.pack(">ii", 0, -223)        # SetEncodings
            + b"\x03\x00" + st.pack(">HHHH", 0, 0, 800, 600)                  # FramebufferUpdateRequest
            + b"\x04\x01\x00\x00" + st.pack(">I", 0x61)                       # KeyEvent  (bloquear)
            + b"\x05\x01" + st.pack(">HH", 100, 200)                          # PointerEvent (bloquear)
            + b"\x06\x00\x00\x00" + st.pack(">I", 3) + b"abc"                 # ClientCutText (bloquear)
            + b"\x03\x01" + st.pack(">HHHH", 0, 0, 800, 600))                 # outro pedido de imagem


def test_vnc_filtro_bloqueia_teclas_e_cliques():
    from app.vnc_proxy import ReadOnlyFilter

    data = _rfb_client_bytes()
    f = ReadOnlyFilter()
    out = b"".join(f.feed(data[i:i + 3]) for i in range(0, len(data), 3))  # chega picado, como na rede
    assert f.dropped == 3
    assert b"\x04\x01" not in out[30:] and b"\x05\x01" not in out[30:] and b"abc" not in out
    assert out.count(b"\x03") >= 2 and out.startswith(b"RFB 003.008\n\x02")
    import pytest as _p
    with _p.raises(ValueError):
        ReadOnlyFilter().feed(b"RFB 003.008\n\x01\x01" + b"\x09" + b"x" * 20)  # tipo desconhecido


def test_ws_vnc_repassa_so_visualizacao(monkeypatch):
    import socket
    import threading

    from fastapi.testclient import TestClient

    from app import main

    recebido = bytearray()
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    def painel_falso():
        conn, _ = srv.accept()
        conn.sendall(b"RFB 003.008\n")
        conn.settimeout(2)
        try:
            while True:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                recebido.extend(chunk)
        except OSError:
            pass
        conn.close()

    threading.Thread(target=painel_falso, daemon=True).start()
    monkeypatch.setattr(main, "settings", main.settings.__class__(
        **{**main.settings.__dict__, "vnc_enabled": True, "vnc_host": "127.0.0.1", "vnc_port": port}))
    main.app.state.collector = Collector(SimulatedSource(seed=2), Storage(":memory:"), poll_seconds=1)
    with TestClient(main.app) as client:
        with client.websocket_connect("/ws/vnc", subprotocols=["binary"]) as ws:
            assert ws.receive_bytes() == b"RFB 003.008\n"
            ws.send_bytes(_rfb_client_bytes())
            time.sleep(0.5)
    time.sleep(0.3)
    data = bytes(recebido)
    assert data.startswith(b"RFB 003.008\n") and b"abc" not in data
    assert b"\x05\x01\x00\x64\x00\xc8" not in data and b"\x04\x01\x00\x00\x00\x00\x00\x61" not in data
    srv.close()
