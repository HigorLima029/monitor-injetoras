"""Hub VNC: varios espectadores, uma conexao com o painel, papeis pela senha, um operador por vez."""
from __future__ import annotations

import asyncio
import struct
import time

import pytest

from app.vnc_des import des_encrypt_block, vnc_response
from app.vnc_hub import VncHub
from tests.fake_vnc import FakePanel, pixel

VIEW, CONTROL = "olhar", "operar"
# formato que o noVNC pede: 32 bpp, little-endian, R no byte 0, G no 1, B no 2
NOVNC_PF = struct.pack(">BBBBHHHBBB3x", 32, 24, 0, 1, 255, 255, 255, 0, 8, 16)


def test_des_vetor_conhecido():
    key, block = bytes.fromhex("133457799BBCDFF1"), bytes.fromhex("0123456789ABCDEF")
    assert des_encrypt_block(key, block).hex().upper() == "85E813540F0AB405"
    assert vnc_response("abc", bytes(16)) != vnc_response("abd", bytes(16))


class Viewer:
    """Cliente VNC minimo (como o Espelho IHM ou o noVNC) para os testes."""

    def __init__(self, reader, writer):
        self.r, self.w = reader, writer
        self.width = self.height = 0

    @classmethod
    async def connect(cls, port: int, password: str):
        r, w = await asyncio.open_connection("127.0.0.1", port)
        v = cls(r, w)
        assert await r.readexactly(12) == b"RFB 003.008\n"
        w.write(b"RFB 003.008\n")
        n = (await r.readexactly(1))[0]
        assert list(await r.readexactly(n)) == [2]
        w.write(b"\x02")
        w.write(vnc_response(password, await r.readexactly(16)))
        if struct.unpack(">I", await r.readexactly(4))[0] != 0:
            size = struct.unpack(">I", await r.readexactly(4))[0]
            reason = (await r.readexactly(size)).decode()
            w.close()
            return reason
        w.write(b"\x01")
        v.width, v.height = struct.unpack(">HH", await r.readexactly(4))
        await r.readexactly(16)
        await r.readexactly(struct.unpack(">I", await r.readexactly(4))[0])
        w.write(b"\x00\x00\x00\x00" + NOVNC_PF)
        return v

    async def frame(self, incremental: bool = False) -> list[tuple[int, int, int, int, bytes]]:
        self.w.write(b"\x03" + bytes([int(incremental)]) + struct.pack(">HHHH", 0, 0, self.width, self.height))
        assert (await asyncio.wait_for(self.r.readexactly(1), 5))[0] == 0
        n = struct.unpack(">H", (await self.r.readexactly(3))[1:])[0]
        rects = []
        for _ in range(n):
            x, y, w, h, enc = struct.unpack(">HHHHi", await self.r.readexactly(12))
            assert enc == 0
            rects.append((x, y, w, h, await self.r.readexactly(w * h * 4)))
        return rects

    def key(self, code: int = 0x61) -> None:
        self.w.write(b"\x04\x01\x00\x00" + struct.pack(">I", code))

    def click(self, x: int = 3, y: int = 2) -> None:
        self.w.write(b"\x05\x01" + struct.pack(">HH", x, y))

    def close(self) -> None:
        self.w.close()


async def _hub(panel_port: int, **kw) -> VncHub:
    hub = VncHub("127.0.0.1", panel_port, VIEW, CONTROL, idle_grace=0.2, min_interval=0.02, **kw)
    await hub.start_tcp("127.0.0.1", 0)
    return hub


async def _until(cond, timeout: float = 5) -> None:
    end = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > end:
            raise AssertionError("tempo esgotado esperando a condicao")
        await asyncio.sleep(0.02)


@pytest.fixture
def panel():
    p = FakePanel(VIEW, CONTROL)
    p.start()
    yield p
    p.stop()


def test_varios_espectadores_uma_conexao_so(panel):
    async def run():
        hub = await _hub(panel.port)
        try:
            a = await Viewer.connect(hub.tcp_port, VIEW)
            b = await Viewer.connect(hub.tcp_port, VIEW)
            for v in (a, b):
                rects = await v.frame()
                x, y, w, h, data = rects[0]
                assert (w, h) == (8, 6)
                bgr = pixel(3, 2)  # o painel manda B,G,R; o noVNC pediu R,G,B
                assert data[(2 * 8 + 3) * 4:(2 * 8 + 3) * 4 + 3] == bytes([bgr[2], bgr[1], bgr[0]])
            assert panel.max_active == 1 and len(panel.connections) == 1 and panel.refused_busy == 0
            assert panel.roles() == ["view"]  # sem operador, o hub fica com a senha de visualizacao
            # o hub pediu ao painel so formatos basicos e 32 bpp
            assert panel.connections[0]["encodings"] == [1, 0, -223]
            a.close(); b.close()
        finally:
            await hub.close()
    asyncio.run(run())


def test_espectador_nunca_mexe_no_painel(panel):
    async def run():
        hub = await _hub(panel.port)
        try:
            v = await Viewer.connect(hub.tcp_port, VIEW)
            await v.frame()
            for _ in range(5):
                v.key(); v.click()
            await asyncio.sleep(0.3)
            assert panel.applied() == [] and all(not c["ignored"] for c in panel.connections)
            assert hub.controller is None and hub.mode == "view"
            assert (await hub.take(next(iter(hub.sessions)))) == (False, "Esta sessao entrou com a senha de visualizacao.")
            v.close()
        finally:
            await hub.close()
    asyncio.run(run())


def test_senha_errada_e_recusada(panel):
    async def run():
        hub = await _hub(panel.port)
        try:
            assert await Viewer.connect(hub.tcp_port, "chute") == "Senha incorreta."
        finally:
            await hub.close()
    asyncio.run(run())


def test_operador_assume_opera_e_devolve(panel):
    async def run():
        hub = await _hub(panel.port)
        try:
            espectador = await Viewer.connect(hub.tcp_port, VIEW)
            await espectador.frame()
            op = await Viewer.connect(hub.tcp_port, CONTROL)
            await op.frame()
            op.key(0x41)  # cliente VNC comum: o primeiro toque assume o controle (e e descartado)
            await _until(lambda: hub.mode == "control" and hub.ready)
            assert panel.roles()[-1] == "control" and panel.max_active == 1
            op.key(0x42)
            op.click(5, 4)
            espectador.key(0x43)  # quem so assiste continua sem mexer
            await _until(lambda: len(panel.applied()) == 2)
            await asyncio.sleep(0.2)
            assert panel.applied() == [b"\x04\x01\x00\x00" + struct.pack(">I", 0x42), b"\x05\x01" + struct.pack(">HH", 5, 4)]
            assert all(not c["ignored"] for c in panel.connections)
            # o espectador continua recebendo imagem depois da troca de senha
            assert await espectador.frame()
            op_sid = hub.controller.sid
            assert await hub.release(op_sid)
            await _until(lambda: hub.mode == "view" and hub.ready and panel.roles()[-1] == "view")
            op.key(0x44)
            await asyncio.sleep(0.2)
            assert len(panel.applied()) == 2  # depois de devolver, nada mais passa (ate assumir de novo)
            textos = [e["text"] for e in hub.events]
            assert any("assumiu o controle" in t for t in textos) and any("deixou o controle" in t for t in textos)
            espectador.close(); op.close()
        finally:
            await hub.close()
    asyncio.run(run())


def test_um_operador_por_vez_e_tempo_limite(panel):
    async def run():
        hub = await _hub(panel.port, control_timeout=0.6)
        try:
            a = await Viewer.connect(hub.tcp_port, CONTROL)
            b = await Viewer.connect(hub.tcp_port, CONTROL)
            await a.frame(); await b.frame()
            sid_a, sid_b = list(hub.sessions)
            hub.sessions[sid_a].auto_take = hub.sessions[sid_b].auto_take = False
            assert (await hub.take(sid_a))[0]
            ok, msg = await hub.take(sid_b)
            assert not ok and "outro operador" in msg
            st = hub.status(sid_b)
            assert st["role"] == "control" and st["controller"]["me"] is False
            assert hub.status(sid_a)["controller"]["me"] is True
            await _until(lambda: hub.controller is None, timeout=4)  # 0,6 s sem uso: volta sozinho
            assert any("sem uso" in e["text"] for e in hub.events)
            await _until(lambda: hub.mode == "view" and hub.ready)
            a.close(); b.close()
        finally:
            await hub.close()
    asyncio.run(run())


def test_painel_liberado_quando_ninguem_assiste(panel):
    async def run():
        hub = await _hub(panel.port)
        try:
            v = await Viewer.connect(hub.tcp_port, VIEW)
            await v.frame()
            v.close()
            await _until(lambda: panel.active == 0 and hub.state == "idle", timeout=4)
        finally:
            await hub.close()
    asyncio.run(run())


def test_ws_e_api_de_controle(tmp_path, monkeypatch, panel):
    from fastapi.testclient import TestClient

    from types import SimpleNamespace

    from app import main
    from app.machines import Machine, MachineStore
    from app.manager import MachineManager
    from app.sources import SimulatedSource

    cfg = SimpleNamespace(
        machines_dir=str(tmp_path / "machines"), poll_seconds=1, full_poll_seconds=60,
        retention_days=7, vnc_tcp_port=0, vnc_tcp_bind="127.0.0.1", vnc_control_timeout=300,
        vnc_audit_path=None, ads_local_net_id="")
    store = MachineStore(str(tmp_path / "m.db"))
    store.set_active(store.add(Machine(
        None, "Painel", "em63", "127.0.0.1", vnc_enabled=True, vnc_port=panel.port,
        vnc_view_password=VIEW, vnc_control_password=CONTROL)).id)
    monkeypatch.setattr(main.app.state, "manager", MachineManager(store, cfg, source_factory=lambda m: SimulatedSource(seed=2)), raising=False)

    def rfb(ws, password):
        assert ws.receive_bytes() == b"RFB 003.008\n"
        ws.send_bytes(b"RFB 003.008\n")
        assert ws.receive_bytes() == b"\x01\x02"
        ws.send_bytes(b"\x02")
        ws.send_bytes(vnc_response(password, ws.receive_bytes()))
        assert ws.receive_bytes() == b"\x00\x00\x00\x00"
        ws.send_bytes(b"\x01")
        assert ws.receive_bytes()[:4] == struct.pack(">HH", 8, 6)

    with TestClient(main.app) as client:
        assert client.get("/api/info").json()["vnc_control"] is True
        with client.websocket_connect("/ws/vnc?sid=olho1", subprotocols=["binary"]) as ws:
            rfb(ws, VIEW)
            st = client.get("/api/vnc/status", params={"sid": "olho1"}).json()
            assert st["role"] == "view" and st["controller"] is None and "events" not in st
            r = client.post("/api/vnc/control", json={"sid": "olho1", "action": "take"})
            assert r.status_code == 403
        with client.websocket_connect("/ws/vnc?sid=op1", subprotocols=["binary"]) as ws:
            rfb(ws, CONTROL)
            r = client.post("/api/vnc/control", json={"sid": "op1", "action": "take"})
            assert r.status_code == 200 and r.json()["ok"]
            st = client.get("/api/vnc/status", params={"sid": "op1"}).json()
            assert st["controller"]["me"] is True and st["role"] == "control"
            assert client.post("/api/vnc/control", json={"sid": "op1", "action": "release"}).json()["ok"]
            assert client.get("/api/vnc/status", params={"sid": "op1"}).json()["controller"] is None
