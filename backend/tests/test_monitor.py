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




def test_fases_em_milissegundos_viram_segundos():
    from app.em63 import translate

    # JM208MK6 (software V1.58): fases em ms, ciclo em s
    ms = translate({"pEm63a.ActTimCyc": 2.8, "pEm63a.myActTimMldCls": 1220, "pEm63a.myActTimMldOpn": 1600,
                    "pEm63a.myActTimCool": 0, "pEm63a.myActTimEject": 0})
    assert ms["ClampLastTime"] == 1.22 and ms["OpenLastTime"] == 1.6 and ms["ColdLastTime"] == 0
    # painel que já manda em segundos fica como está
    s = translate({"pEm63a.ActTimCyc": 31.4, "pEm63a.myActTimMldCls": 2.1, "pEm63a.myActTimCool": 12.0})
    assert s["ClampLastTime"] == 2.1 and s["ColdLastTime"] == 12.0
    # sem ciclo conhecido: valores enormes só podem ser ms
    assert translate({"pEm63a.myActTimMldOpn": 1620})["OpenLastTime"] == 1.62
