"""Frota: todas as máquinas cadastradas conectadas ao mesmo tempo, cada uma com o seu coletor.

Sem ninguém olhando, cada coletor lê a cada `idle_poll_seconds` (10 s, economiza o 4G); com alguém
com a máquina aberta (dados ou tela), lê a cada `poll_seconds`, e volta ao intervalo ocioso
`viewer_linger_seconds` depois que o último sai.
"""
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

PROBE_TIMEOUT = 3.0
CLOSE_TIMEOUT = 5.0  # maior que o limite do disconnect() das fontes OPC-UA (4 s)
TICK_INTERVAL = 1.0  # o laço da frota confere o intervalo de leitura de cada máquina
REOPEN_REASON = "máquina reconfigurada"
REMOVE_REASON = "máquina removida"
BRIDGE_HOST = "127.0.0.1"  # o x11vnc da ponte só escuta aqui


class MachineRuntime:
    """Uma máquina aberta: coletor sempre ligado, hub VNC e ponte CERHost (se tiver tela).

    O objeto vive enquanto a máquina estiver cadastrada; um `reload` com mudança de conexão troca
    o coletor/hub/ponte dentro dele, sem perder a contagem de quem está olhando. `version` sobe a
    cada mudança de cadastro (quem assina o coletor confere permissão e coletor de novo).
    """

    def __init__(self, machine: Machine, settings, clock=time.monotonic):
        self.machine = machine
        self.settings = settings
        self.clock = clock
        self.collector: Collector | None = None
        self.hub: VncHub | None = None
        self.bridge = None
        self.bridge_task: asyncio.Task | None = None
        self.viewers = 0
        self.version = 0
        self.closed = False
        self._idle_since: float | None = None  # None = ainda tem gente (ou nunca teve: ocioso)
        self._touched: float | None = None  # último pedido de valores (telas que perguntam por HTTP)

    @property
    def fast_seconds(self) -> float:
        return self.settings.poll_seconds

    @property
    def idle_seconds(self) -> float:
        return getattr(self.settings, "idle_poll_seconds", 10)

    @property
    def linger_seconds(self) -> float:
        return getattr(self.settings, "viewer_linger_seconds", 60)

    def wanted_poll(self) -> float:
        if self.viewers > 0:
            return self.fast_seconds
        now = self.clock()
        for since in (self._idle_since, self._touched):
            if since is not None and now - since < self.linger_seconds:
                return self.fast_seconds
        return self.idle_seconds

    def tick(self) -> None:
        """Aplica no coletor o intervalo que vale agora."""
        if self.collector is not None:
            self.collector.set_poll_seconds(self.wanted_poll())

    def enter(self) -> None:
        self.viewers += 1
        self._idle_since = None
        self.tick()

    def touch(self) -> None:
        """Alguém pediu dados por HTTP (ex.: `/values`): conta como olhando por `linger` segundos."""
        self._touched = self.clock()
        self.tick()

    def leave(self) -> None:
        if self.viewers <= 0:
            return
        self.viewers -= 1
        if self.viewers == 0:
            self._idle_since = self.clock()
        self.tick()

    def changed(self, old: Collector | None = None) -> None:
        """Cadastro mudou: quem assina o coletor (o atual e o `old`, já trocado) acorda e confere
        permissão e coletor de novo. Esse aviso nunca vai para o navegador."""
        self.version += 1
        for c in {id(x): x for x in (old, self.collector) if x is not None}.values():
            c.notify({"_fleet": self.version})


class Fleet:
    def __init__(self, store: MachineStore, settings, accounts=None, source_factory=None,
                 bridge_factory=None, clock=time.monotonic):
        self.store = store
        self.settings = settings
        self.accounts = accounts  # o app define na subida se vier None
        self.source_factory = source_factory  # testes: fábrica(machine) -> fonte
        self.bridge_factory = bridge_factory  # testes: fábrica(machine) -> ponte (sem Wine/Xvfb)
        self.clock = clock
        self.close_timeout = CLOSE_TIMEOUT
        self.tick_interval = TICK_INTERVAL
        self._runtimes: dict[int, MachineRuntime] = {}
        self._locks: dict[int, asyncio.Lock] = {}
        self._probes: dict[int, tuple[float | None, float]] = {}  # id -> (latência ms, ts) do teste
        self._tick_task: asyncio.Task | None = None

    # ---- ciclo de vida --------------------------------------------------
    async def start(self) -> None:
        for m in self.store.list():
            await self.add(m.id)
        self._tick_task = asyncio.create_task(self._tick_loop(), name="frota-intervalos")

    async def stop(self) -> None:
        if self._tick_task:
            self._tick_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._tick_task
            self._tick_task = None
        # juntas: N máquinas travadas fecham no tempo de uma, não N vezes o limite
        await asyncio.gather(*(self.remove(mid) for mid in list(self._runtimes)),
                             return_exceptions=True)

    def runtime(self, machine_id: int) -> MachineRuntime | None:
        return self._runtimes.get(machine_id)

    def runtimes(self) -> list[MachineRuntime]:
        return list(self._runtimes.values())

    def _lock(self, machine_id: int) -> asyncio.Lock:
        return self._locks.setdefault(machine_id, asyncio.Lock())

    async def _tick_loop(self) -> None:
        while True:
            await asyncio.sleep(self.tick_interval)
            for rt in list(self._runtimes.values()):
                try:
                    rt.tick()
                except Exception:
                    log.exception("falha ao ajustar o intervalo de %s", rt.machine.name)

    # ---- cadastro -------------------------------------------------------
    async def add(self, machine_id: int) -> MachineRuntime | None:
        """Abre a máquina recém-cadastrada (ou já aberta: devolve a atual). Falha não derruba nada."""
        async with self._lock(machine_id):
            return await self._add_locked(machine_id)

    async def _add_locked(self, machine_id: int) -> MachineRuntime | None:
        rt = self._runtimes.get(machine_id)
        if rt is not None:
            return rt
        m = self.store.get(machine_id)
        if m is None:
            return None
        rt = MachineRuntime(m, self.settings, self.clock)
        try:
            await self._open(rt)
        except Exception:  # o servidor (e as outras máquinas) seguem sem esta
            log.exception("falha ao abrir a máquina %s (id %s)", m.name, m.id)
            return None
        self._runtimes[machine_id] = rt
        log.info("máquina aberta: %s (id %s)", m.name, m.id)
        return rt

    async def reload(self, machine_id: int, connection_changed: bool) -> None:
        """Cadastro mudou. Só conexão (IP, portas, tipo, senhas) reabre; nome/cliente/notas só atualizam.

        Máquina que não abriu (na subida, no cadastro ou num reload anterior) tenta abrir de novo:
        é assim que corrigir o cadastro faz a coleta voltar."""
        async with self._lock(machine_id):
            rt = self._runtimes.get(machine_id)
            m = self.store.get(machine_id)
            if m is None:
                return
            if rt is None:
                await self._add_locked(machine_id)
                return
            rt.machine = m
            if not connection_changed:
                if rt.hub is not None:
                    rt.hub.name = m.name
                rt.changed()
                return
            old = rt.collector
            await self._close_parts(rt, REOPEN_REASON)
            try:
                await self._open(rt)
            except Exception:
                log.exception("falha ao reabrir a máquina %s (id %s)", m.name, m.id)
                rt.closed = True
                self._runtimes.pop(machine_id, None)
            rt.changed(old)  # só agora: quem assina o antigo passa direto para o novo

    async def remove(self, machine_id: int) -> None:
        """Fecha coletor, hub e ponte da máquina; quem estava olhando é desconectado."""
        async with self._lock(machine_id):
            rt = self._runtimes.pop(machine_id, None)
            self._probes.pop(machine_id, None)
            if rt is None:
                return
            rt.closed = True
            old = rt.collector
            await self._close_parts(rt, REMOVE_REASON)
            rt.changed(old)
        self._locks.pop(machine_id, None)

    # ---- abrir / fechar -------------------------------------------------
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

    def _new_collector(self, m: Machine, poll: float) -> Collector:
        s = self.settings
        mdir = Path(s.machines_dir)
        mdir.mkdir(parents=True, exist_ok=True)
        storage = Storage(str(mdir / f"{m.id}.db"))
        try:
            source = self.build_source(m)
            return Collector(source, storage, poll, s.full_poll_seconds, s.retention_days)
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
                             wineprefix=getattr(s, "cerhost_wineprefix", ""),
                             pidfile=getattr(s, "cerhost_pidfile", ""))

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
            # o registro mostra o IP do painel, nao o 127.0.0.1:porta interno da ponte
            return VncHub(BRIDGE_HOST, bridge.port or 0, m.vnc_view_password, m.vnc_control_password,
                          port_provider=lambda: bridge.port, audit_label=m.host, **common)
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

    async def _open(self, rt: MachineRuntime) -> None:
        """Liga coletor (não espera a conexão), hub e ponte da máquina do runtime."""
        m = rt.machine
        collector = self._new_collector(m, rt.wanted_poll())
        hub = bridge = None
        try:
            if m.kind == "ads" and self._wants_screen(m):
                bridge = self.build_bridge(m)
            hub = self._new_hub(m, bridge)
            collector.start()  # o coletor continua tentando sozinho
        except BaseException:
            collector.storage.close()
            if hub is not None:
                await hub.close()
            raise
        rt.collector, rt.hub, rt.bridge = collector, hub, bridge
        if bridge is not None:
            rt.bridge_task = self._start_bridge(bridge)  # não espera: o hub reconecta sozinho

    async def _bounded(self, what: str, coro) -> None:
        """Desligamento com limite de tempo: máquina "meio aberta" não trava as outras ações."""
        try:
            await asyncio.wait_for(coro, self.close_timeout)
        except asyncio.TimeoutError:
            log.warning("%s demorou mais de %ss para fechar; seguindo", what, self.close_timeout)
        except Exception:
            log.exception("falha ao fechar %s", what)

    async def _close_parts(self, rt: MachineRuntime, reason: str) -> None:
        c, h, b, bt = rt.collector, rt.hub, rt.bridge, rt.bridge_task
        rt.collector = rt.hub = rt.bridge = rt.bridge_task = None
        try:
            if c is not None:
                await self._bounded("coletor", c.stop())
        finally:
            try:
                if c is not None:
                    c.storage.close()
            finally:
                try:
                    if h is not None:
                        await self._bounded("hub VNC", h.close(reason))
                finally:
                    await self._close_bridge(b, bt)

    async def _close_bridge(self, bridge, task: asyncio.Task | None) -> None:
        """Encerra a ponte (CERHost, x11vnc, Openbox, Xvfb), mesmo no meio da subida."""
        try:
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await asyncio.wait_for(task, self.close_timeout)
        finally:
            if bridge is not None:
                await self._bounded("tela da Beckhoff", bridge.stop())

    # ---- status ---------------------------------------------------------
    def status_of(self, machine_id: int) -> dict:
        last = self._probes.get(machine_id)
        latency, checked = (last[0], last[1]) if last else (None, None)
        rt = self._runtimes.get(machine_id)
        c = rt.collector if rt is not None else None
        if c is None:
            return {"online": None, "state": None, "latency_ms": latency, "checked": checked,
                    "active_alarms": 0}
        return {"online": c.connected and not c.stale, "state": c.machine_state(),
                "latency_ms": latency, "checked": checked, "active_alarms": len(c.active_alarms)}

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
        """Teste manual (TCP) de uma máquina; a lista passa a mostrar a latência medida."""
        latency = await self.probe(m.host, m.status_port, PROBE_TIMEOUT)
        if self.store.get(m.id) is not None:  # pode ter sido apagada durante o teste
            self._probes[m.id] = (latency, time.time())
        return latency
