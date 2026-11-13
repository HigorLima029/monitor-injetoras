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
