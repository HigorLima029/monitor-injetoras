import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.machines import Machine, MachineStore
from app.manager import MachineManager


def _settings(tmp_path):
    return SimpleNamespace(
        machines_dir=str(tmp_path / "machines"), poll_seconds=0.05, full_poll_seconds=0.2,
        retention_days=7, vnc_tcp_port=0, vnc_tcp_bind="127.0.0.1", vnc_control_timeout=300,
        vnc_audit_path=None, ads_local_net_id="",
    )


def _setup(tmp_path, machines, **kw):
    store = MachineStore(str(tmp_path / "m.db"))
    ids = [store.add(m).id for m in machines]
    return store, MachineManager(store, _settings(tmp_path), **kw), ids


def _demo(name):
    return Machine(None, name, "demo", "127.0.0.1")


def test_troca_fecha_o_anterior(tmp_path):
    async def run():
        store, mgr, (a, b) = _setup(tmp_path, [_demo("A"), _demo("B")])
        try:
            await mgr.activate(a)
            await asyncio.sleep(0.3)
            assert mgr.collector.values
            old = mgr.collector
            await mgr.activate(b)
            assert old._task.done()
            assert mgr.machine.id == b and store.active_id() == b
            assert (tmp_path / "machines" / f"{a}.db").exists()
            assert (tmp_path / "machines" / f"{b}.db").exists()
        finally:
            await mgr.stop()
            store.close()

    asyncio.run(run())


def test_aviso_machine_changed(tmp_path):
    async def run():
        store, mgr, (a, b) = _setup(tmp_path, [_demo("A"), _demo("B")])
        try:
            await mgr.activate(a)
            q = mgr.collector.subscribe()
            await mgr.activate(b)
            msgs = []
            while not q.empty():
                msgs.append(q.get_nowait())
            assert any(m.get("type") == "machine_changed" and m.get("machine_id") == b for m in msgs)
        finally:
            await mgr.stop()
            store.close()

    asyncio.run(run())


def test_ativar_inalcancavel_e_rapido(tmp_path):
    async def run():
        m = Machine(None, "X", "em63", "10.255.255.1")
        store, mgr, (a,) = _setup(tmp_path, [m])
        try:
            t0 = asyncio.get_running_loop().time()
            await mgr.activate(a)
            assert asyncio.get_running_loop().time() - t0 < 1
            assert mgr.collector.connected is False
        finally:
            await mgr.stop()
            store.close()

    asyncio.run(run())


def test_cliques_rapidos(tmp_path):
    async def run():
        from app.sources import SimulatedSource
        created = []
        store = MachineStore(str(tmp_path / "m.db"))
        a = store.add(_demo("A")).id
        b = store.add(_demo("B")).id
        mgr = MachineManager(store, _settings(tmp_path), source_factory=lambda m: SimulatedSource())
        orig = mgr._new_collector

        def spy(m):
            c = orig(m)
            created.append(c)
            return c

        mgr._new_collector = spy
        try:
            await asyncio.gather(mgr.activate(a), mgr.activate(b))
            assert mgr.machine.id == b
            assert len(created) == 2
            assert sum(1 for c in created if c._task and not c._task.done()) == 1
        finally:
            await mgr.stop()
            store.close()

    asyncio.run(run())


def test_activate_desconhecido_nao_derruba_a_ativa(tmp_path):
    async def run():
        store, mgr, (a,) = _setup(tmp_path, [_demo("A")])
        try:
            await mgr.activate(a)
            with pytest.raises(KeyError):
                await mgr.activate(999)
            assert mgr.machine.id == a and not mgr.collector._task.done()
        finally:
            await mgr.stop()
            store.close()

    asyncio.run(run())


def test_probe(tmp_path):
    async def run():
        store, mgr, _ = _setup(tmp_path, [_demo("A")])
        server = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            assert isinstance(await mgr.probe("127.0.0.1", port), float)
        finally:
            server.close()
            await server.wait_closed()
        assert await mgr.probe("127.0.0.1", port) is None
        store.close()

    asyncio.run(run())


def test_status_de_nao_ativa_e_ativa(tmp_path):
    async def run():
        server = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        store, mgr, (a, b) = _setup(
            tmp_path, [_demo("A"), Machine(None, "B", "chy", "127.0.0.1", opcua_port=port)])
        try:
            await mgr.activate(a)
            assert mgr.status_of(b) == {"online": None, "latency_ms": None, "checked": None}
            await mgr.probe_all()
            st = mgr.status_of(b)
            assert st["online"] is True and st["latency_ms"] is not None and st["checked"]
            sa = mgr.status_of(a)
            assert set(sa) == {"online", "latency_ms", "checked", "state"}
        finally:
            await mgr.stop()
            store.close()
            server.close()
            await server.wait_closed()

    asyncio.run(run())


class _SlowClose:
    """Fonte cujo close() demora muito (máquina meio aberta)."""

    def __init__(self):
        from app.sources import SimulatedSource
        self._s = SimulatedSource()

    def __getattr__(self, name):
        return getattr(self._s, name)

    async def close(self):
        await asyncio.sleep(30)


def test_fechamento_lento_tem_limite(tmp_path):
    async def run():
        from app.sources import SimulatedSource
        store = MachineStore(str(tmp_path / "m.db"))
        a = store.add(_demo("A")).id
        b = store.add(_demo("B")).id
        mgr = MachineManager(store, _settings(tmp_path),
                             source_factory=lambda m: _SlowClose() if m.id == a else SimulatedSource())
        mgr.close_timeout = 0.3
        try:
            await mgr.activate(a)
            t0 = asyncio.get_running_loop().time()
            await mgr.activate(b)
            assert asyncio.get_running_loop().time() - t0 < 1.5
            assert mgr.machine.id == b
        finally:
            await mgr.stop()
            store.close()

    asyncio.run(run())


def test_hub_vnc_troca_e_mantem_a_porta(tmp_path):
    async def run():
        import socket
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        ms = [Machine(None, n, "em63", "127.0.0.1", vnc_port=59999, vnc_view_password="vista")
              for n in "AB"]
        store, mgr, (a, b) = _setup(tmp_path, ms)
        mgr.settings.vnc_tcp_port = port
        try:
            await mgr.activate(a)
            old = mgr.hub
            assert old is not None
            _, w = await asyncio.open_connection("127.0.0.1", port)
            w.close()
            await mgr.activate(b)
            assert mgr.hub is not None and mgr.hub is not old and old._closed
            _, w = await asyncio.open_connection("127.0.0.1", port)
            w.close()
            assert mgr.hub.tcp_port == port
        finally:
            await mgr.stop()
            store.close()

    asyncio.run(run())


def test_start_sobrevive_a_falha_na_ativa(tmp_path):
    async def run():
        def boom(m):
            raise RuntimeError("quebrou")

        store = MachineStore(str(tmp_path / "m.db"))
        a = store.add(_demo("A")).id
        store.set_active(a)
        mgr = MachineManager(store, _settings(tmp_path), source_factory=boom)
        try:
            await mgr.start()
            assert mgr._status_task is not None and not mgr._status_task.done()
            assert mgr.collector is None
        finally:
            await mgr.stop()
            store.close()

    asyncio.run(run())


def test_falha_ao_parar_coletor_ainda_fecha_hub(tmp_path):
    async def run():
        ms = [Machine(None, n, "em63", "127.0.0.1", vnc_view_password="v") for n in "AB"]
        store, mgr, (a, b) = _setup(tmp_path, ms)
        try:
            await mgr.activate(a)
            old_h = mgr.hub

            async def bad():
                raise RuntimeError("x")

            mgr.collector.stop = bad
            await mgr.activate(b)
            assert old_h._closed
        finally:
            await mgr.stop()
            store.close()

    asyncio.run(run())


def test_probe_all_pula_demo_e_a_ativa(tmp_path):
    async def run():
        from app.sources import SimulatedSource
        ms = [Machine(None, "A", "chy", "10.0.0.1"), _demo("B"), Machine(None, "C", "chy", "10.0.0.3")]
        store, mgr, (a, b, c) = _setup(tmp_path, ms, source_factory=lambda m: SimulatedSource())
        sondados = []

        async def probe(host, port, timeout=3.0):
            sondados.append(host)
            return 1.0

        mgr.probe = probe
        try:
            await mgr.activate(a)
            await mgr.probe_all()
            assert sondados == ["10.0.0.3"]
            assert mgr.status_of(c)["online"] is True
        finally:
            await mgr.stop()
            store.close()

    asyncio.run(run())


def _free_port():
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_porta_vnc_so_assume_no_hub_da_subida(tmp_path):
    async def run():
        ms = [Machine(None, n, "em63", "127.0.0.1", vnc_port=59999, vnc_view_password="vista",
                      vnc_control_password="op") for n in "AB"]
        store, mgr, (a, b) = _setup(tmp_path, ms)
        store.set_active(a)
        mgr.settings.vnc_tcp_port = _free_port()
        try:
            await mgr.start()
            assert mgr.hub.tcp_auto_take is True  # hub da subida: como antes
            await mgr.activate(b)
            assert mgr.hub.tcp_auto_take is False  # depois de uma troca: 5901 só visualização
            await mgr.activate(a)
            assert mgr.hub.tcp_auto_take is False
        finally:
            await mgr.stop()
            store.close()

    asyncio.run(run())


def test_refresh_atualiza_sem_reabrir(tmp_path):
    async def run():
        store, mgr, (a,) = _setup(tmp_path, [_demo("A")])
        try:
            await mgr.activate(a)
            c = mgr.collector
            store.update(a, {"name": "A2"})
            await mgr.refresh(a)
            assert mgr.machine.name == "A2" and mgr.collector is c
        finally:
            await mgr.stop()
            store.close()

    asyncio.run(run())


# ---- tela da Beckhoff (ponte CERHost) -------------------------------------
class FakeBridge:
    """Ponte sem Wine/Xvfb: registra start/stop e expõe estado e porta como a real."""

    def __init__(self, m, port=45901, state="connected", error=None, start_delay=0.0, stop_delay=0.0):
        self.machine, self.port, self.final_state, self.final_error = m, port, state, error
        self.state, self.error = "stopped", None
        self.start_delay, self.stop_delay = start_delay, stop_delay
        self.started = self.stopped = 0

    async def start(self):
        self.started += 1
        self.state = "starting"
        await asyncio.sleep(self.start_delay)
        self.state, self.error = self.final_state, self.final_error

    async def stop(self):
        self.stopped += 1
        await asyncio.sleep(self.stop_delay)
        self.state, self.error = "stopped", None


def _ads(name="Bk", **kw):
    kw.setdefault("vnc_enabled", True)
    kw.setdefault("vnc_view_password", "vista")
    kw.setdefault("vnc_control_password", "op")
    return Machine(None, name, "ads", "10.0.0.7", ams_net_id="5.79.97.240.1.1", **kw)


def _bk_manager(tmp_path, machines, **bridge_kw):
    from app.sources import SimulatedSource
    bridges = []

    def factory(m):
        b = FakeBridge(m, **bridge_kw)
        bridges.append(b)
        return b

    store, mgr, ids = _setup(tmp_path, machines, source_factory=lambda m: SimulatedSource(),
                             bridge_factory=factory)
    return store, mgr, ids, bridges


def test_ads_com_tela_sobe_ponte_e_hub(tmp_path):
    async def run():
        store, mgr, (a,), bridges = _bk_manager(tmp_path, [_ads()])
        try:
            await mgr.activate(a)
            await asyncio.sleep(0.05)
            b = mgr.bridge
            assert b is bridges[0] and b.started == 1 and b.state == "connected"
            assert b.machine.id == a
            assert mgr.hub is not None and mgr.hub.host == "127.0.0.1"
            assert mgr.hub.current_port() == 45901
            assert mgr.hub.view_password == "vista" and mgr.hub.control_password == "op"
            assert mgr.hub.name == "Bk"
            b.port = 45999  # x11vnc reiniciado em outra porta: o hub acompanha
            assert mgr.hub.current_port() == 45999
            b.port = None  # x11vnc desligado enquanto o robô reconecta
            assert mgr.hub.current_port() is None
        finally:
            await mgr.stop()
            store.close()
        assert bridges[0].stopped == 1 and mgr.bridge is None

    asyncio.run(run())


def test_ponte_lenta_nao_trava_o_activate(tmp_path):
    async def run():
        store, mgr, (a,), bridges = _bk_manager(tmp_path, [_ads()], start_delay=30)
        try:
            t0 = asyncio.get_running_loop().time()
            await mgr.activate(a)
            assert asyncio.get_running_loop().time() - t0 < 1
            await asyncio.sleep(0)
            assert mgr.bridge.state == "starting" and mgr.hub is not None
            assert mgr.collector is not None
        finally:
            await mgr.stop()  # cancela a subida em andamento
            store.close()
        assert bridges[0].stopped == 1

    asyncio.run(run())


def test_troca_para_br_para_ponte(tmp_path):
    async def run():
        ms = [_ads(), Machine(None, "BR", "em63", "127.0.0.1", vnc_port=59999, vnc_view_password="v")]
        store, mgr, (a, b), bridges = _bk_manager(tmp_path, ms)
        try:
            await mgr.activate(a)
            old_hub = mgr.hub
            await mgr.activate(b)
            assert bridges[0].stopped == 1 and old_hub._closed
            assert mgr.bridge is None
            assert mgr.hub is not None and mgr.hub.host == "127.0.0.1" and mgr.hub.port == 59999
            assert len(bridges) == 1
        finally:
            await mgr.stop()
            store.close()

    asyncio.run(run())


def test_parada_lenta_da_ponte_tem_limite(tmp_path):
    async def run():
        store, mgr, (a, b), bridges = _bk_manager(tmp_path, [_ads(), _demo("D")], stop_delay=30)
        mgr.close_timeout = 0.2
        try:
            await mgr.activate(a)
            t0 = asyncio.get_running_loop().time()
            await mgr.activate(b)
            assert asyncio.get_running_loop().time() - t0 < 1.5
            assert mgr.machine.id == b and mgr.bridge is None
        finally:
            await mgr.stop()
            store.close()

    asyncio.run(run())


def test_ads_sem_tela_nao_sobe_ponte(tmp_path):
    async def run():
        ms = [Machine(None, "Bk", "ads", "10.0.0.7"),  # padrão: tela da Beckhoff desligada
              _ads("SemSenha", vnc_view_password=""),
              _ads("Desligada", vnc_enabled=False)]
        store, mgr, ids, bridges = _bk_manager(tmp_path, ms)
        try:
            assert ms[0].effective_vnc_enabled is False
            for i in ids:
                await mgr.activate(i)
                assert mgr.bridge is None and mgr.hub is None and mgr.collector is not None
            assert bridges == []
        finally:
            await mgr.stop()
            store.close()

    asyncio.run(run())


def test_ads_porta_5901_so_assume_na_subida(tmp_path):
    async def run():
        store, mgr, (a, b), bridges = _bk_manager(tmp_path, [_ads("A"), _ads("B")])
        store.set_active(a)
        mgr.settings.vnc_tcp_port = _free_port()
        try:
            await mgr.start()
            assert mgr.hub.tcp_auto_take is True and mgr.bridge is bridges[0]
            await mgr.activate(b)
            assert mgr.hub.tcp_auto_take is False
            assert bridges[0].stopped == 1 and mgr.bridge is bridges[1]
        finally:
            await mgr.stop()
            store.close()

    asyncio.run(run())


def test_fabrica_padrao_da_ponte(tmp_path):
    from app.cerhost import CerhostBridge
    store, mgr, _ = _setup(tmp_path, [])
    mgr.settings.cerhost_exe = "/opt/cerhost/CERHOST.exe"
    mgr.settings.cerhost_display = 77
    mgr.settings.cerhost_wineprefix = "/srv/wine-cerhost"
    b = mgr.build_bridge(_ads())
    assert isinstance(b, CerhostBridge)
    assert (b.host, b.display, b.cerhost_exe, b.view_password, b.control_password, b.wineprefix) == \
        ("10.0.0.7", 77, "/opt/cerhost/CERHOST.exe", "vista", "op", "/srv/wine-cerhost")
    store.close()


def test_hub_da_br_nao_mudou(tmp_path):
    store, mgr, _ = _setup(tmp_path, [])
    hub = mgr._new_hub(Machine(2, "BR", "em63", "10.0.0.8", vnc_view_password="v"), None)
    assert hub is not None and hub.host == "10.0.0.8" and hub.current_port() == 5900
    assert mgr._new_hub(Machine(3, "D", "demo", "10.0.0.9", vnc_enabled=True, vnc_view_password="v"),
                        None) is None
    store.close()
