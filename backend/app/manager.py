"""Dono da máquina ativa: troca coletor, hub VNC e ponte CERHost em tempo de execução e sonda o
status de todas."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from pathlib import Path

from .cerhost import CerhostBridge
from .collector import Collector
from .em63 import Em63Source
from .machines import Machine, MachineStore
from .sources import OpcUaSource, SimulatedSource
from .storage import Storage
from .vnc_hub import VncHub

log = logging.getLogger(__name__)

PROBE_INTERVAL = 30.0
PROBE_TIMEOUT = 3.0
CLOSE_TIMEOUT = 5.0  # maior que o limite do disconnect() das fontes OPC-UA (4 s)
SWITCH_REASON = "máquina trocada"
BRIDGE_HOST = "127.0.0.1"  # o x11vnc da ponte só escuta aqui


class MachineManager:
    def __init__(self, store: MachineStore, settings, source_factory=None, bridge_factory=None):
        self.store = store
        self.settings = settings
        self.source_factory = source_factory  # testes: fábrica(machine) -> fonte
        self.bridge_factory = bridge_factory  # testes: fábrica(machine) -> ponte (sem Wine/Xvfb)
        self.machine: Machine | None = None
        self.collector: Collector | None = None
        self.hub: VncHub | None = None
        self.bridge: CerhostBridge | None = None  # tela da Beckhoff (só com ads e tela ligada)
        self._bridge_task: asyncio.Task | None = None
        self.close_timeout = CLOSE_TIMEOUT  # limite do desligamento da máquina anterior
        self._lock = asyncio.Lock()
        self._probes: dict[int, tuple[float | None, float]] = {}  # id -> (latência ms, ts)
        self._status_task: asyncio.Task | None = None

    # ---- ciclo de vida --------------------------------------------------
    async def start(self) -> None:
        active = self.store.active_id()
        if active is not None and self.store.get(active) is not None:
            try:
                await self.activate(active, at_startup=True)
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

    def _wants_screen(self, m: Machine) -> bool:
        """Tela ao vivo: só com VNC ligado, fora do modo demo e com a senha de visualização."""
        if m.kind == "demo" or not m.effective_vnc_enabled:
            return False
        if not m.vnc_view_password:
            log.warning("Tela ao vivo desligada para %s: sem senha de visualização", m.name)
            return False
        return True

    def build_bridge(self, m: Machine):
        """Ponte CERHost da Beckhoff (CERHOST.exe no Wine, servido por x11vnc em 127.0.0.1)."""
        if self.bridge_factory is not None:
            return self.bridge_factory(m)
        s = self.settings
        return CerhostBridge(m.host, getattr(s, "cerhost_display", 99), getattr(s, "cerhost_exe", ""),
                             m.vnc_view_password, m.vnc_control_password,
                             wineprefix=getattr(s, "cerhost_wineprefix", ""))

    def _new_hub(self, m: Machine, bridge) -> VncHub | None:
        """Hub da tela ao vivo. Beckhoff (ads): o painel é o x11vnc da ponte, em 127.0.0.1.

        A porta da ponte só existe depois que o CERHost conecta e pode mudar (o x11vnc é desligado
        enquanto o robô reconecta): o hub pergunta a porta à ponte a cada tentativa e, enquanto não
        houver, tenta de novo a cada 3 s, como faz com um painel VNC fora do ar.
        """
        s = self.settings
        common = dict(control_timeout=s.vnc_control_timeout, name=m.name, audit_path=s.vnc_audit_path)
        if m.kind == "ads":
            if bridge is None:
                return None
            return VncHub(BRIDGE_HOST, bridge.port or 0, m.vnc_view_password, m.vnc_control_password,
                          port_provider=lambda: bridge.port, **common)
        if not self._wants_screen(m):
            return None
        return VncHub(m.host, m.vnc_port, m.vnc_view_password, m.vnc_control_password, **common)

    def _start_bridge(self, bridge) -> asyncio.Task:
        """A subida da ponte pode levar dezenas de segundos: roda em segundo plano."""
        async def run():
            try:
                await bridge.start()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("falha ao subir a tela da Beckhoff")
        return asyncio.create_task(run(), name="ponte-cerhost")

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
        old_b, old_bt = self.bridge, self._bridge_task
        self.machine = self.collector = self.hub = self.bridge = self._bridge_task = None
        try:
            if old_c is not None:
                await self._bounded("coletor", old_c.stop())
        finally:
            try:
                if old_c is not None:
                    old_c.storage.close()
            finally:
                try:
                    if old_h is not None:
                        await self._bounded("hub VNC", old_h.close(SWITCH_REASON))
                finally:
                    await self._close_bridge(old_b, old_bt)

    async def _close_bridge(self, bridge, task: asyncio.Task | None) -> None:
        """Encerra a ponte (CERHost, x11vnc, Openbox, Xvfb), mesmo no meio da subida.

        Primeiro cancela a tarefa de subida (se ela nem começou, não começa mais: nada sobe depois
        do stop); depois o stop da ponte, que cancela o vigia e derruba os processos.
        """
        try:
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await asyncio.wait_for(task, self.close_timeout)
        finally:
            if bridge is not None:
                await self._bounded("tela da Beckhoff", bridge.stop())

    async def activate(self, machine_id: int, at_startup: bool = False) -> Machine:
        """Abre a máquina (fechando a anterior).

        Só o hub aberto na subida do servidor deixa a porta VNC comum assumir o controle no primeiro
        toque. Depois de uma troca, um Espelho IHM que reconecta sozinho cairia na outra máquina:
        ali a porta fica só visualização (operar continua possível pela Tela ao vivo).
        """
        async with self._lock:
            m = self.store.get(machine_id)
            if m is None:
                raise KeyError(machine_id)  # antes de derrubar a ativa
            old_c = self.collector
            if old_c is not None:
                old_c.notify({"type": "machine_changed", "machine_id": machine_id})
            await self._close_current()
            collector = self._new_collector(m)
            hub = bridge = bridge_task = None
            try:
                if m.kind == "ads" and self._wants_screen(m):
                    bridge = self.build_bridge(m)
                hub = self._new_hub(m, bridge)
                collector.start()  # não espera a conexão: o coletor continua tentando
                if hub is not None and self.settings.vnc_tcp_port:
                    try:
                        await hub.start_tcp(self.settings.vnc_tcp_bind, self.settings.vnc_tcp_port,
                                            auto_take=at_startup)
                    except OSError as exc:
                        log.warning("porta VNC %s indisponível (%s)", self.settings.vnc_tcp_port, exc)
                self.store.set_active(machine_id)
                if bridge is not None:
                    bridge_task = self._start_bridge(bridge)  # não espera: o hub reconecta sozinho
            except BaseException:
                await collector.stop()
                collector.storage.close()
                if hub is not None:
                    await hub.close()
                await self._close_bridge(bridge, bridge_task)
                raise
            self.machine, self.collector, self.hub = m, collector, hub
            self.bridge, self._bridge_task = bridge, bridge_task
            log.info("máquina ativa: %s (id %s)", m.name, m.id)
            return m

    async def refresh(self, machine_id: int) -> None:
        """Cadastro mudou sem mexer na conexão (nome, cliente, notas): atualiza sem reabrir."""
        async with self._lock:
            if self.machine is not None and self.machine.id == machine_id:
                m = self.store.get(machine_id)
                if m is not None:
                    self.machine = m
                    if self.hub is not None:
                        self.hub.name = m.name

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

    async def check(self, m: Machine) -> float | None:
        """Sonda uma máquina agora e guarda o resultado, para a lista mostrar o mesmo que o teste manual."""
        latency = await self.probe(m.host, m.status_port, PROBE_TIMEOUT)
        if self.store.get(m.id) is not None:  # pode ter sido apagada durante o teste
            self._probes[m.id] = (latency, time.time())
        return latency

    async def probe_all(self) -> None:
        """Sonda as outras máquinas: a aberta já tem o coletor, e a simulada não tem rede."""
        machines = self.store.list()
        active = self.machine.id if self.machine is not None else None
        targets = [m for m in machines if m.kind != "demo" and m.id != active]
        results = await asyncio.gather(
            *(self.probe(m.host, m.status_port) for m in targets), return_exceptions=True)
        now = time.time()
        for m, r in zip(targets, results):
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
