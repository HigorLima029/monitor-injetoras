"""Dono da máquina ativa: troca coletor e hub VNC em tempo de execução e sonda o status de todas."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from pathlib import Path

from .collector import Collector
from .em63 import Em63Source
from .machines import Machine, MachineStore
from .sources import OpcUaSource, SimulatedSource
from .storage import Storage
from .vnc_hub import VncHub

log = logging.getLogger(__name__)

PROBE_INTERVAL = 30.0
PROBE_TIMEOUT = 3.0
CLOSE_TIMEOUT = 5.0


class MachineManager:
    def __init__(self, store: MachineStore, settings, source_factory=None):
        self.store = store
        self.settings = settings
        self.source_factory = source_factory  # testes: fábrica(machine) -> fonte
        self.machine: Machine | None = None
        self.collector: Collector | None = None
        self.hub: VncHub | None = None
        self.close_timeout = CLOSE_TIMEOUT  # limite do desligamento da máquina anterior
        self._lock = asyncio.Lock()
        self._probes: dict[int, tuple[float | None, float]] = {}  # id -> (latência ms, ts)
        self._status_task: asyncio.Task | None = None

    # ---- ciclo de vida --------------------------------------------------
    async def start(self) -> None:
        active = self.store.active_id()
        if active is not None and self.store.get(active) is not None:
            try:
                await self.activate(active)
            except Exception:  # o servidor precisa subir mesmo sem a máquina ativa
                log.exception("falha ao abrir a máquina ativa %s", active)
        self._status_task = asyncio.create_task(self._status_loop(), name="status-maquinas")

    async def stop(self) -> None:
        if self._status_task:
            self._status_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._status_task
            self._status_task = None
        async with self._lock:
            await self._close_current()

    # ---- troca ----------------------------------------------------------
    def build_source(self, m: Machine):
        if self.source_factory is not None:
            return self.source_factory(m)
        if m.kind == "demo":
            return SimulatedSource()
        if m.kind == "em63":
            return Em63Source(m.opcua_url, m.effective_namespace)
        if m.kind == "ads":
            from .ads_source import AdsSource
            return AdsSource(m.ams_net_id, m.host, m.ads_port, self.settings.ads_local_net_id)
        cache = str(Path(self.settings.machines_dir) / f"{m.id}_tags.json")
        return OpcUaSource(m.opcua_url, m.effective_namespace, cache_file=cache)

    def _new_collector(self, m: Machine) -> Collector:
        s = self.settings
        mdir = Path(s.machines_dir)
        mdir.mkdir(parents=True, exist_ok=True)
        storage = Storage(str(mdir / f"{m.id}.db"))
        try:
            source = self.build_source(m)
            return Collector(source, storage, s.poll_seconds, s.full_poll_seconds, s.retention_days)
        except Exception:
            storage.close()
            raise

    def _new_hub(self, m: Machine) -> VncHub | None:
        """Tela ao vivo: só com VNC ligado, fora do modo demo e com a senha de visualização."""
        if m.kind == "demo" or not m.effective_vnc_enabled:
            return None
        if not m.vnc_view_password:
            log.warning("Tela ao vivo desligada para %s: sem senha de visualização", m.name)
            return None
        s = self.settings
        return VncHub(m.host, m.vnc_port, m.vnc_view_password, m.vnc_control_password,
                      control_timeout=s.vnc_control_timeout, name=m.name, audit_path=s.vnc_audit_path)

    async def _bounded(self, what: str, coro) -> None:
        """Desligamento com limite de tempo: máquina "meio aberta" não trava a troca."""
        try:
            await asyncio.wait_for(coro, self.close_timeout)
        except asyncio.TimeoutError:
            log.warning("%s demorou mais de %ss para fechar; seguindo", what, self.close_timeout)
        except Exception:
            log.exception("falha ao fechar %s", what)

    async def _close_current(self) -> None:
        old_c, old_h = self.collector, self.hub
        self.machine = self.collector = self.hub = None
        try:
            if old_c is not None:
                await self._bounded("coletor", old_c.stop())
        finally:
            try:
                if old_c is not None:
                    old_c.storage.close()
            finally:
                if old_h is not None:
                    await self._bounded("hub VNC", old_h.close())

    async def activate(self, machine_id: int) -> Machine:
        async with self._lock:
            m = self.store.get(machine_id)
            if m is None:
                raise KeyError(machine_id)  # antes de derrubar a ativa
            old_c = self.collector
            if old_c is not None:
                old_c.notify({"type": "machine_changed", "machine_id": machine_id})
            await self._close_current()
            collector = self._new_collector(m)
            hub = None
            try:
                hub = self._new_hub(m)
                collector.start()  # não espera a conexão: o coletor continua tentando
                if hub is not None and self.settings.vnc_tcp_port:
                    try:
                        await hub.start_tcp(self.settings.vnc_tcp_bind, self.settings.vnc_tcp_port)
                    except OSError as exc:
                        log.warning("porta VNC %s indisponível (%s)", self.settings.vnc_tcp_port, exc)
                self.store.set_active(machine_id)
            except BaseException:
                await collector.stop()
                collector.storage.close()
                if hub is not None:
                    await hub.close()
                raise
            self.machine, self.collector, self.hub = m, collector, hub
            log.info("máquina ativa: %s (id %s)", m.name, m.id)
            return m

    # ---- status ---------------------------------------------------------
    def status_of(self, machine_id: int) -> dict:
        last = self._probes.get(machine_id)
        latency, checked = (last[0], last[1]) if last else (None, None)
        c = self.collector
        if self.machine is not None and self.machine.id == machine_id and c is not None:
            return {"online": c.connected and not c.stale, "latency_ms": latency,
                    "checked": checked, "state": c.machine_state()}
        return {"online": None if last is None else latency is not None,
                "latency_ms": latency, "checked": checked}

    async def probe(self, host: str, port: int, timeout: float = PROBE_TIMEOUT) -> float | None:
        t0 = time.perf_counter()
        try:
            _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
        except (OSError, asyncio.TimeoutError):
            return None
        latency = (time.perf_counter() - t0) * 1000
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
        return latency

    async def probe_all(self) -> None:
        machines = self.store.list()
        results = await asyncio.gather(
            *(self.probe(m.host, m.status_port) for m in machines), return_exceptions=True)
        now = time.time()
        for m, r in zip(machines, results):
            self._probes[m.id] = (r if isinstance(r, float) else None, now)
        keep = {m.id for m in machines}
        for gone in [i for i in self._probes if i not in keep]:
            del self._probes[gone]

    async def _status_loop(self) -> None:
        while True:
            try:
                await self.probe_all()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("falha no laço de status")
            await asyncio.sleep(PROBE_INTERVAL)
