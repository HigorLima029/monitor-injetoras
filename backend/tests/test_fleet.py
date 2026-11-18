"""Fleet: todas as máquinas coletando ao mesmo tempo; leitura rápida só com alguém olhando."""
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.collector import Collector
from app.machines import Machine, MachineStore
from app.manager import Fleet
from app.sources import SimulatedSource
from app.storage import Storage


def _settings(tmp_path, **kw):
    base = dict(
        machines_dir=str(tmp_path / "machines"), poll_seconds=2, idle_poll_seconds=10,
        viewer_linger_seconds=60, full_poll_seconds=60, retention_days=7, vnc_control_timeout=300,
        vnc_audit_path=None, ads_local_net_id="",
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _demo(name):
    return Machine(None, name, "demo", "127.0.0.1")


def _setup(tmp_path, machines, settings=None, **kw):
    store = MachineStore(str(tmp_path / "m.db"))
    ids = [store.add(m).id for m in machines]
    return store, Fleet(store, settings or _settings(tmp_path), None, **kw), ids


async def _until(cond, timeout=3.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not cond():
        if loop.time() > end:
            raise AssertionError("condição não aconteceu a tempo")
        await asyncio.sleep(0.02)


class _Hang:
    """Fonte cujo read() nunca volta (4G travado)."""

    def __init__(self):
        self._s = SimulatedSource()

    async def connect(self):
        await self._s.connect()

    async def list_tags(self):
        return await self._s.list_tags()

    async def read(self, tags):
        await asyncio.Event().wait()

    async def close(self):
        await self._s.close()


# ---- coletor -------------------------------------------------------------
def test_set_poll_seconds_acorda_o_laco():
    async def run():
        c = Collector(SimulatedSource(), Storage(":memory:"), poll_seconds=30, full_poll_seconds=999)
        c.start()
        try:
            await _until(lambda: c.reads == 1)
            await asyncio.sleep(0.1)
            assert c.reads == 1  # dormindo 30 s
            c.set_poll_seconds(0.05)
            await _until(lambda: c.reads >= 3, timeout=1)
            assert c.poll_seconds == 0.05
        finally:
            await c.stop()
            c.storage.close()

    asyncio.run(run())


# ---- frota ------------------------------------------------------------------
def test_todas_as_maquinas_coletam(tmp_path):
    async def run():
        store, fleet, ids = _setup(tmp_path, [_demo("A"), _demo("B"), _demo("C")],
                                   _settings(tmp_path, idle_poll_seconds=0.05))
        try:
            await fleet.start()
            rts = [fleet.runtime(i) for i in ids]
            assert all(rt is not None for rt in rts)
            assert len({id(rt.collector) for rt in rts}) == 3
            cols = [rt.collector for rt in rts]
            await _until(lambda: all(c.reads >= 2 for c in cols))
            assert all(rt.machine.id == i for rt, i in zip(rts, ids))
            assert fleet.runtime(999) is None
            for i in ids:
                st = fleet.status_of(i)
                assert set(st) >= {"online", "state", "latency_ms", "checked", "active_alarms"}
                assert st["online"] is True and isinstance(st["active_alarms"], int)
        finally:
            await fleet.stop()
            store.close()
        assert all(c._task.done() for c in cols) and all(rt.closed for rt in rts)

    asyncio.run(run())


def test_sem_ninguem_olhando_le_a_cada_10s(tmp_path):
    async def run():
        store, fleet, (a,) = _setup(tmp_path, [_demo("A")])
        try:
            await fleet.start()
            assert fleet.runtime(a).collector.poll_seconds == 10
        finally:
            await fleet.stop()
            store.close()

    asyncio.run(run())


def test_intervalo_rapido_com_espectador_e_volta_a_10s(tmp_path):
    async def run():
        now = [1000.0]
        store, fleet, (a,) = _setup(tmp_path, [_demo("A")], clock=lambda: now[0])
        try:
            await fleet.start()
            rt = fleet.runtime(a)
            rt.enter()
            assert rt.collector.poll_seconds == 2
            rt.enter()
            rt.leave()  # ainda tem um olhando
            now[0] += 120
            rt.tick()
            assert rt.collector.poll_seconds == 2
            rt.leave()
            now[0] += 59
            rt.tick()
            assert rt.collector.poll_seconds == 2
            now[0] += 2
            rt.tick()
            assert rt.collector.poll_seconds == 10
            rt.enter()  # voltou alguém: rápido na hora
            assert rt.collector.poll_seconds == 2
            rt.leave()
            rt.leave()  # sobra de leave não deixa o contador negativo
            assert rt.viewers == 0
        finally:
            await fleet.stop()
            store.close()

    asyncio.run(run())


def test_laco_da_frota_aplica_o_intervalo_ocioso(tmp_path):
    async def run():
        store, fleet, (a,) = _setup(tmp_path, [_demo("A")],
                                    _settings(tmp_path, viewer_linger_seconds=0.1))
        fleet.tick_interval = 0.05
        try:
            await fleet.start()
            rt = fleet.runtime(a)
            rt.enter()
            rt.leave()
            assert rt.collector.poll_seconds == 2
            await _until(lambda: rt.collector.poll_seconds == 10)
        finally:
            await fleet.stop()
            store.close()

    asyncio.run(run())


def test_maquina_travada_nao_atrasa_as_outras(tmp_path):
    async def run():
        store = MachineStore(str(tmp_path / "m.db"))
        a = store.add(_demo("A")).id
        b = store.add(_demo("B")).id
        fleet = Fleet(store, _settings(tmp_path, idle_poll_seconds=0.05), None,
                      source_factory=lambda m: _Hang() if m.id == a else SimulatedSource())
        try:
            await fleet.start()
            rb = fleet.runtime(b)
            await _until(lambda: rb.collector.reads >= 5)
            assert fleet.runtime(a).collector.reads == 0
            assert rb.collector.storage.history(["CycleMonitor"], 0)["CycleMonitor"]
        finally:
            await fleet.stop()
            store.close()

    asyncio.run(run())


def test_inalcancavel_nao_atrasa_a_subida(tmp_path):
    async def run():
        store, fleet, (a,) = _setup(tmp_path, [Machine(None, "X", "em63", "10.255.255.1")])
        try:
            t0 = asyncio.get_running_loop().time()
            await fleet.start()
            assert asyncio.get_running_loop().time() - t0 < 1
            assert fleet.runtime(a).collector.connected is False
            assert fleet.status_of(a)["online"] is False
        finally:
            await fleet.stop()
            store.close()

    asyncio.run(run())


def test_start_sobrevive_a_falha_numa_maquina(tmp_path):
    async def run():
        store = MachineStore(str(tmp_path / "m.db"))
        a = store.add(_demo("A")).id
        b = store.add(_demo("B")).id

        def factory(m):
            if m.id == a:
                raise RuntimeError("quebrou")
            return SimulatedSource()

        fleet = Fleet(store, _settings(tmp_path), None, source_factory=factory)
        try:
            await fleet.start()
            assert fleet.runtime(a) is None and fleet.runtime(b) is not None
            assert fleet.status_of(a)["online"] is None
        finally:
            await fleet.stop()
            store.close()

    asyncio.run(run())


def test_add_abre_a_maquina_nova(tmp_path):
    async def run():
        store, fleet, _ = _setup(tmp_path, [_demo("A")])
        try:
            await fleet.start()
            b = store.add(_demo("B")).id
            assert fleet.runtime(b) is None
            await fleet.add(b)
            assert fleet.runtime(b) is not None and not fleet.runtime(b).collector._task.done()
            assert (tmp_path / "machines" / f"{b}.db").exists()
        finally:
            await fleet.stop()
            store.close()

    asyncio.run(run())


def test_reload_so_reabre_a_maquina_mudada(tmp_path):
    async def run():
        store, fleet, (a, b) = _setup(tmp_path, [_demo("A"), _demo("B")])
        try:
            await fleet.start()
            ra, rb = fleet.runtime(a), fleet.runtime(b)
            ca, cb = ra.collector, rb.collector
            ra.enter()
            store.update(a, {"host": "127.0.0.9"})
            await fleet.reload(a, connection_changed=True)
            assert fleet.runtime(a) is ra  # mesmo runtime: quem está olhando continua contado
            assert ra.collector is not ca and ca._task.done()
            assert ra.machine.host == "127.0.0.9" and ra.viewers == 1
            assert ra.collector.poll_seconds == 2  # rápido: ainda tem gente olhando
            assert rb.collector is cb and not cb._task.done()
            v = ra.version
            store.update(b, {"name": "B2", "notes": "x", "client_id": 7})
            await fleet.reload(b, connection_changed=False)
            assert rb.collector is cb and rb.machine.name == "B2" and rb.machine.client_id == 7
            assert ra.version == v
        finally:
            await fleet.stop()
            store.close()

    asyncio.run(run())


def test_reload_avisa_quem_assina(tmp_path):
    async def run():
        store, fleet, (a,) = _setup(tmp_path, [_demo("A")])
        try:
            await fleet.start()
            rt = fleet.runtime(a)
            q = rt.collector.subscribe()
            v = rt.version
            store.update(a, {"client_id": 3})
            await fleet.reload(a, connection_changed=False)
            assert rt.version == v + 1
            msgs = []
            while not q.empty():
                msgs.append(q.get_nowait())
            assert any(m.get("_fleet") for m in msgs)
        finally:
            await fleet.stop()
            store.close()

    asyncio.run(run())


def test_remove_fecha_tudo_da_maquina(tmp_path):
    async def run():
        ms = [Machine(None, "BR", "em63", "127.0.0.1", vnc_port=59999, vnc_view_password="v"), _demo("D")]
        store, fleet, (a, d) = _setup(tmp_path, ms, source_factory=lambda m: SimulatedSource())
        try:
            await fleet.start()
            rt = fleet.runtime(a)
            hub, col = await fleet.open_screen(a), rt.collector
            assert hub is not None and rt.hub is hub
            q = col.subscribe()
            await fleet.remove(a)
            assert fleet.runtime(a) is None and rt.closed
            assert col._task.done() and hub._closed
            msgs = []
            while not q.empty():
                msgs.append(q.get_nowait())
            assert any(m.get("_fleet") for m in msgs)
            assert not fleet.runtime(d).collector._task.done()
            await fleet.remove(a)  # de novo: nada acontece
        finally:
            await fleet.stop()
            store.close()

    asyncio.run(run())


class _SlowClose:
    """Fonte cujo close() demora muito (máquina meio aberta)."""

    def __init__(self):
        self._s = SimulatedSource()

    def __getattr__(self, name):
        return getattr(self._s, name)

    async def close(self):
        await asyncio.sleep(30)


def test_fechamento_lento_tem_limite(tmp_path):
    async def run():
        store = MachineStore(str(tmp_path / "m.db"))
        a = store.add(_demo("A")).id
        fleet = Fleet(store, _settings(tmp_path), None, source_factory=lambda m: _SlowClose())
        fleet.close_timeout = 0.3
        try:
            await fleet.start()
            t0 = asyncio.get_running_loop().time()
            await fleet.reload(a, connection_changed=True)
            await fleet.remove(a)
            assert asyncio.get_running_loop().time() - t0 < 1.5
        finally:
            await fleet.stop()
            store.close()

    asyncio.run(run())


def test_falha_ao_parar_coletor_ainda_fecha_hub(tmp_path):
    async def run():
        store, fleet, (a,) = _setup(tmp_path, [Machine(None, "A", "em63", "127.0.0.1", vnc_view_password="v")])
        try:
            await fleet.start()
            rt = fleet.runtime(a)
            hub = await fleet.open_screen(a)

            async def bad():
                raise RuntimeError("x")

            rt.collector.stop = bad
            await fleet.remove(a)
            assert hub._closed
        finally:
            await fleet.stop()
            store.close()

    asyncio.run(run())


def test_probe_e_check(tmp_path):
    async def run():
        server = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        store, fleet, (a,) = _setup(tmp_path, [Machine(None, "B", "chy", "127.0.0.1", opcua_port=port)],
                                    source_factory=lambda m: SimulatedSource())
        try:
            assert isinstance(await fleet.probe("127.0.0.1", port), float)
            await fleet.start()
            assert fleet.status_of(a)["latency_ms"] is None
            lat = await fleet.check(store.get(a))
            st = fleet.status_of(a)
            assert st["latency_ms"] == lat and st["checked"]
        finally:
            await fleet.stop()
            store.close()
            server.close()
            await server.wait_closed()
        assert await fleet.probe("127.0.0.1", port) is None

    asyncio.run(run())


def test_sem_porta_vnc_comum(tmp_path):
    async def run():
        store, fleet, (a,) = _setup(tmp_path, [Machine(None, "BR", "em63", "127.0.0.1",
                                                       vnc_view_password="v")])
        try:
            await fleet.start()
            assert (await fleet.open_screen(a))._tcp_server is None
        finally:
            await fleet.stop()
            store.close()

    asyncio.run(run())


def test_config_padrao():
    from app.config import Settings
    s = Settings()
    assert s.idle_poll_seconds == 10 and s.viewer_linger_seconds == 60
    assert s.cerhost_max_screens == 2 and s.screen_idle_seconds == 120
    assert not hasattr(s, "vnc_tcp_port") and not hasattr(s, "vnc_tcp_bind")


# ---- tela da Beckhoff (ponte CERHost) -------------------------------------
class FakeBridge:
    """Ponte sem Wine/Xvfb: registra start/stop e expõe estado e porta como a real."""

    def __init__(self, m, port=45901, state="connected", error=None, start_delay=0.0, stop_delay=0.0):
        self.machine, self.port, self.final_state, self.final_error = m, port, state, error
        self.view_password, self.control_password = m.vnc_view_password, m.vnc_control_password
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


def _bk_fleet(tmp_path, machines, **bridge_kw):
    bridges = []

    def factory(m):
        b = FakeBridge(m, **bridge_kw)
        bridges.append(b)
        return b

    store, fleet, ids = _setup(tmp_path, machines, source_factory=lambda m: SimulatedSource(),
                               bridge_factory=factory)
    return store, fleet, ids, bridges


def test_ads_com_tela_sobe_ponte_e_hub(tmp_path):
    async def run():
        store, fleet, (a,), bridges = _bk_fleet(tmp_path, [_ads()])
        try:
            await fleet.start()
            rt = fleet.runtime(a)
            assert rt.bridge is None and rt.hub is None and bridges == []  # só ao abrir a tela
            await fleet.open_screen(a)
            await asyncio.sleep(0.05)
            b = rt.bridge
            assert b is bridges[0] and b.started == 1 and b.state == "connected"
            assert b.machine.id == a
            assert rt.hub is not None and rt.hub.host == "127.0.0.1"
            assert rt.hub.current_port() == 45901
            assert rt.hub.view_password == "vista" and rt.hub.control_password == "op"
            assert rt.hub.name == "Bk" and rt.hub.audit_label == "10.0.0.7"
            b.port = 45999
            assert rt.hub.current_port() == 45999
        finally:
            await fleet.stop()
            store.close()
        assert bridges[0].stopped == 1

    asyncio.run(run())


def test_ponte_lenta_nao_trava_a_subida(tmp_path):
    async def run():
        store, fleet, (a,), bridges = _bk_fleet(tmp_path, [_ads()], start_delay=30)
        try:
            await fleet.start()
            t0 = asyncio.get_running_loop().time()
            await fleet.open_screen(a)
            assert asyncio.get_running_loop().time() - t0 < 1
            await asyncio.sleep(0)
            rt = fleet.runtime(a)
            assert rt.bridge.state == "starting" and rt.hub is not None and rt.collector is not None
        finally:
            await fleet.stop()  # cancela a subida em andamento
            store.close()
        assert bridges[0].stopped == 1

    asyncio.run(run())


def test_parada_lenta_da_ponte_tem_limite(tmp_path):
    async def run():
        store, fleet, (a,), bridges = _bk_fleet(tmp_path, [_ads()], stop_delay=30)
        fleet.close_timeout = 0.2
        try:
            await fleet.start()
            await fleet.open_screen(a)
            t0 = asyncio.get_running_loop().time()
            await fleet.remove(a)
            assert asyncio.get_running_loop().time() - t0 < 1.5
        finally:
            await fleet.stop()
            store.close()

    asyncio.run(run())


def test_ads_sem_tela_nao_sobe_ponte(tmp_path):
    async def run():
        ms = [Machine(None, "Bk", "ads", "10.0.0.7"),  # padrão: tela da Beckhoff desligada
              _ads("Desligada", vnc_enabled=False)]
        store, fleet, ids, bridges = _bk_fleet(tmp_path, ms)
        try:
            await fleet.start()
            for i in ids:
                assert await fleet.open_screen(i) is None
                rt = fleet.runtime(i)
                assert rt.bridge is None and rt.hub is None and rt.collector is not None
            assert bridges == []
        finally:
            await fleet.stop()
            store.close()

    asyncio.run(run())


def test_fabrica_padrao_da_ponte(tmp_path):
    from app.cerhost import CerhostBridge
    store, fleet, _ = _setup(tmp_path, [])
    fleet.settings.cerhost_exe = "/opt/cerhost/CERHOST.exe"
    fleet.settings.cerhost_display = 77
    fleet.settings.cerhost_wineprefix = "/srv/wine-cerhost"
    b = fleet.build_bridge(_ads())
    assert isinstance(b, CerhostBridge)
    assert (b.host, b.display, b.cerhost_exe, b.view_password, b.control_password, b.wineprefix) == \
        ("10.0.0.7", 77, "/opt/cerhost/CERHOST.exe", "vista", "op", "/srv/wine-cerhost")
    store.close()


def test_hub_da_br_nao_mudou(tmp_path):
    store, fleet, _ = _setup(tmp_path, [])
    hub = fleet._new_hub(Machine(2, "BR", "em63", "10.0.0.8", vnc_view_password="v"), None)
    assert hub is not None and hub.host == "10.0.0.8" and hub.current_port() == 5900
    assert fleet._new_hub(Machine(3, "D", "demo", "10.0.0.9", vnc_enabled=True, vnc_view_password="v"),
                          None) is None
    store.close()


def test_touch_mantem_rapido_pelo_linger(tmp_path):
    async def run():
        now = [1000.0]
        store, fleet, (a,) = _setup(tmp_path, [_demo("A")], clock=lambda: now[0])
        try:
            await fleet.start()
            rt = fleet.runtime(a)
            rt.touch()
            assert rt.collector.poll_seconds == 2
            now[0] += 59
            rt.tick()
            assert rt.collector.poll_seconds == 2
            now[0] += 2
            rt.tick()
            assert rt.collector.poll_seconds == 10
        finally:
            await fleet.stop()
            store.close()

    asyncio.run(run())


def test_reload_abre_maquina_que_nao_abriu(tmp_path):
    async def run():
        store = MachineStore(str(tmp_path / "m.db"))
        a = store.add(Machine(None, "A", "demo", "quebrado")).id

        def factory(m):
            if m.host == "quebrado":
                raise RuntimeError("não abre")
            return SimulatedSource()

        fleet = Fleet(store, _settings(tmp_path), None, source_factory=factory)
        try:
            await fleet.start()
            assert fleet.runtime(a) is None
            store.update(a, {"host": "127.0.0.1"})
            await fleet.reload(a, connection_changed=True)
            assert fleet.runtime(a) is not None
            # reload que falha tira o runtime; corrigir de novo reabre
            store.update(a, {"host": "quebrado"})
            await fleet.reload(a, connection_changed=True)
            assert fleet.runtime(a) is None
            store.update(a, {"host": "127.0.0.2"})
            await fleet.reload(a, connection_changed=False)
            assert fleet.runtime(a) is not None
        finally:
            await fleet.stop()
            store.close()

    asyncio.run(run())


def test_stop_fecha_maquinas_travadas_em_paralelo(tmp_path):
    async def run():
        store = MachineStore(str(tmp_path / "m.db"))
        for n in "ABCD":
            store.add(_demo(n))
        fleet = Fleet(store, _settings(tmp_path), None, source_factory=lambda m: _SlowClose())
        fleet.close_timeout = 0.3
        await fleet.start()
        t0 = asyncio.get_running_loop().time()
        await fleet.stop()
        assert asyncio.get_running_loop().time() - t0 < 0.9  # em série seriam 4 x 0,3 s
        assert fleet.runtimes() == []
        store.close()

    asyncio.run(run())


# ---- telas sob demanda (uma ponte por máquina, com limite) -----------------
def _bk_real(tmp_path, n, clock=None, **kw):
    """Frota com N Beckhoffs e a ponte de verdade sem CERHOST.exe (fica "missing": nada sobe)."""
    s = _settings(tmp_path, cerhost_exe="", cerhost_display=99, cerhost_wineprefix="",
                  cerhost_pidfile=str(tmp_path / "data" / "cerhost.pids"), **kw)
    machines = [_ads(f"Bk{i}", vnc_view_password="", vnc_control_password="") for i in range(n)]
    extra = {"clock": clock} if clock is not None else {}
    return _setup(tmp_path, machines, s, source_factory=lambda m: SimulatedSource(), **extra)


def test_duas_pontes_usam_displays_diferentes(tmp_path):
    from app.cerhost import CerhostBridge

    async def run():
        store, fleet, (a, b) = _bk_real(tmp_path, 2)
        try:
            await fleet.start()
            assert fleet.runtime(a).hub is None and fleet.runtime(a).bridge is None  # sob demanda
            ha = await fleet.open_screen(a)
            hb = await fleet.open_screen(b)
            assert ha is not hb and await fleet.open_screen(a) is ha  # a mesma, se já aberta
            ba, bb = fleet.runtime(a).bridge, fleet.runtime(b).bridge
            assert isinstance(ba, CerhostBridge) and isinstance(bb, CerhostBridge)
            assert (ba.display, bb.display) == (99, 100)
            assert Path(ba.pidfile) == tmp_path / "data" / "cerhost_99.pids"
            assert Path(bb.pidfile) == tmp_path / "data" / "cerhost_100.pids"
            # o hub usa as senhas da ponte (geradas por ela), não as do cadastro (vazias)
            assert ha.view_password == ba.view_password and len(ha.view_password) == 8
            assert ha.control_password == ba.control_password
            await fleet.close_screen(a)
            assert fleet.runtime(a).bridge is None and ha._closed and ba._stopped
            await fleet.open_screen(a)
            assert fleet.runtime(a).bridge.display == 99  # o display liberado volta a ser usado
        finally:
            await fleet.stop()
            store.close()

    asyncio.run(run())


def test_terceira_tela_beckhoff_recusada_com_mensagem(tmp_path):
    from app.manager import ScreenLimit

    async def run():
        store, fleet, ids = _bk_real(tmp_path, 3)
        br = store.add(Machine(None, "BR", "em63", "127.0.0.1", vnc_port=59999)).id
        try:
            await fleet.start()
            await fleet.open_screen(ids[0])
            await fleet.open_screen(ids[1])
            with pytest.raises(ScreenLimit) as exc:
                await fleet.open_screen(ids[2])
            assert str(exc.value) == ("Limite de telas da Beckhoff abertas ao mesmo tempo. "
                                      "Tente de novo em alguns minutos.")
            assert fleet.runtime(ids[2]).bridge is None and fleet.runtime(ids[2]).hub is None
            assert await fleet.open_screen(br) is not None  # B&R não conta no limite
            await fleet.close_screen(ids[0])
            assert await fleet.open_screen(ids[2]) is not None
            assert fleet.runtime(ids[2]).bridge.display == 99
        finally:
            await fleet.stop()
            store.close()

    asyncio.run(run())


def test_tela_ociosa_fecha_e_libera_display(tmp_path):
    async def run():
        now = [1000.0]
        store, fleet, (a, b) = _bk_real(tmp_path, 2, screen_idle_seconds=120, clock=lambda: now[0])
        try:
            await fleet.start()
            hub = await fleet.open_screen(a)
            bridge = fleet.runtime(a).bridge
            now[0] += 119
            await fleet.close_idle_screens()
            assert fleet.runtime(a).hub is hub  # ainda dentro do prazo
            hub.sessions["olho"] = object()  # alguém assistindo: não fecha, e o prazo recomeça
            now[0] += 500
            await fleet.close_idle_screens()
            assert fleet.runtime(a).hub is hub
            del hub.sessions["olho"]
            await fleet.close_idle_screens()  # saiu agora: conta daqui
            now[0] += 119
            await fleet.close_idle_screens()
            assert fleet.runtime(a).hub is hub
            now[0] += 2
            await fleet.close_idle_screens()
            rt = fleet.runtime(a)
            assert rt.hub is None and rt.bridge is None and hub._closed and bridge._stopped
            assert rt.collector is not None and not rt.collector._task.done()  # dados seguem
            await fleet.open_screen(b)
            assert fleet.runtime(b).bridge.display == 99  # display liberado
        finally:
            await fleet.stop()
            store.close()

    asyncio.run(run())


def test_br_sem_senha_vnc_abre_tela(tmp_path):
    async def run():
        store, fleet, (a,) = _setup(tmp_path, [Machine(None, "BR", "em63", "10.0.0.8")],
                                    source_factory=lambda m: SimulatedSource())
        try:
            await fleet.start()
            assert fleet.runtime(a).hub is None  # sob demanda
            hub = await fleet.open_screen(a)
            assert hub is not None and hub.host == "10.0.0.8" and hub.current_port() == 5900
            assert hub.view_password == "" and fleet.runtime(a).bridge is None
        finally:
            await fleet.stop()
            store.close()

    asyncio.run(run())
