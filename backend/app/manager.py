"""Frota: todas as máquinas cadastradas conectadas ao mesmo tempo, cada uma com o seu coletor.

Sem ninguém olhando, cada coletor lê a cada `idle_poll_seconds` (10 s, economiza o 4G); com alguém
com a máquina aberta (dados ou tela), lê a cada `poll_seconds`, e volta ao intervalo ocioso
`viewer_linger_seconds` depois que o último sai.

A tela ao vivo é sob demanda (`open_screen`): o hub VNC (e, na Beckhoff, a ponte CERHost) só
sobe quando alguém abre a tela, e fecha depois de `screen_idle_seconds` sem ninguém. Cada ponte
viva tem a sua tela virtual (Xvfb :N, de `cerhost_display` em diante) e o seu arquivo de pids;
no máximo `cerhost_max_screens` pontes ao mesmo tempo (cada uma é um Wine + Xvfb + x11vnc).
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
SCREEN_REASON = "tela fechada por falta de uso"
SCREEN_LIMIT_MESSAGE = ("Limite de telas da Beckhoff abertas ao mesmo tempo. "
                        "Tente de novo em alguns minutos.")


class ScreenLimit(Exception):
    """Já há `cerhost_max_screens` telas da Beckhoff abertas."""

    def __init__(self, message: str = SCREEN_LIMIT_MESSAGE) -> None:
        super().__init__(message)


class ScreenUnavailable(Exception):
    """A tela não tem como funcionar (ex.: faltam ferramentas no servidor); a mensagem diz o quê."""


class MachineRuntime:
    """Uma máquina aberta: coletor sempre ligado; hub VNC e ponte CERHost só com a tela aberta.

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
        self.display: int | None = None  # tela virtual da ponte (Beckhoff), enquanto viva
        self.screen_idle_since: float | None = None  # hub aberto e sem ninguém desde
        self.last_bridge: dict | None = None  # estado da última ponte que não pôde funcionar
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
        self._idle_task: asyncio.Task | None = None
        # telas virtuais reservadas: da abertura até a ponte terminar de fechar (é o que conta
        # no limite, para uma tela ainda morrendo não deixar outra subir junto)
        self._displays: set[int] = set()
        self._live_bridges: list = []  # pontes da frota, até terminarem de fechar

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
        if self._idle_task:
            # sem cancelar: um fechamento no meio deixaria a ponte meio encerrada (já tem limite)
            with contextlib.suppress(Exception):
                await self._idle_task
            self._idle_task = None
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
            # fechar uma ponte pode levar segundos: em segundo plano, sem atrasar os intervalos
            if self._idle_task is None or self._idle_task.done():
                self._idle_task = asyncio.create_task(self.close_idle_screens(),
                                                      name="frota-telas-ociosas")

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
        """Tela ao vivo: só com VNC ligado e fora do modo demo. Senha não é exigida: painel B&R
        sem senha funciona (o hub aceita segurança "None") e a ponte da Beckhoff gera as suas."""
        return m.kind != "demo" and m.effective_vnc_enabled

    @property
    def max_screens(self) -> int:
        return int(getattr(self.settings, "cerhost_max_screens", 2))

    @property
    def screen_idle_seconds(self) -> float:
        return float(getattr(self.settings, "screen_idle_seconds", 120))

    def _pidfile_for(self, display: int) -> str:
        """Um arquivo de pids por tela virtual, na pasta do `cerhost_pidfile` (data/cerhost_99.pids)."""
        base = getattr(self.settings, "cerhost_pidfile", "")
        if not base:
            return ""
        return str(Path(base).parent / f"cerhost_{display}.pids")

    def build_bridge(self, m: Machine, display: int | None = None):
        """Ponte CERHost da Beckhoff (CERHOST.exe no Wine, servido por x11vnc em 127.0.0.1)."""
        if self.bridge_factory is not None:
            return self.bridge_factory(m)
        s = self.settings
        if display is None:
            display = getattr(s, "cerhost_display", 99)
        bridge = CerhostBridge(m.host, display, getattr(s, "cerhost_exe", ""),
                               m.vnc_view_password, m.vnc_control_password,
                               wineprefix=getattr(s, "cerhost_wineprefix", ""),
                               pidfile=self._pidfile_for(display))
        # prefixo Wine compartilhado: `wineserver -k` só sem outra ponte viva
        bridge.may_kill_wineserver = lambda: not any(b is not bridge for b in self._live_bridges)
        return bridge

    def _alloc_display(self) -> int:
        """Menor tela virtual livre de `cerhost_display` em diante."""
        d = int(getattr(self.settings, "cerhost_display", 99))
        while d in self._displays:
            d += 1
        self._displays.add(d)
        return d

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
            # o registro mostra o IP do painel, nao o 127.0.0.1:porta interno da ponte; as senhas
            # sao as do x11vnc da ponte (geradas por ela quando o cadastro nao tem)
            return VncHub(BRIDGE_HOST, bridge.port or 0, bridge.view_password, bridge.control_password,
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
        """Liga o coletor da máquina do runtime (não espera a conexão). A tela é sob demanda."""
        collector = self._new_collector(rt.machine, rt.wanted_poll())
        try:
            collector.start()  # o coletor continua tentando sozinho
        except BaseException:
            collector.storage.close()
            raise
        rt.collector = collector

    # ---- tela ao vivo (sob demanda) -----------------------------------------
    async def open_screen(self, machine_id: int) -> VncHub | None:
        """Hub da tela ao vivo da máquina, aberto agora se preciso (Beckhoff: com a sua ponte).

        None = máquina fora da frota ou sem tela. `ScreenLimit` = já há `cerhost_max_screens`
        telas da Beckhoff reservadas (B&R não conta: não sobe nada no servidor).
        `ScreenUnavailable` = a ponte não tem como funcionar (faltam ferramentas): a tela é
        fechada na hora, sem segurar a vaga, e `rt.last_bridge` guarda o motivo para o status."""
        async with self._lock(machine_id):
            rt = self._runtimes.get(machine_id)
            if rt is None or rt.closed:
                return None
            if rt.hub is not None and not rt.hub.closed:
                rt.screen_idle_since = self.clock()  # vai ser usada: o prazo recomeça
                return rt.hub
            m = rt.machine
            if not self._wants_screen(m):
                return None
            bridge = display = None
            if m.kind == "ads":
                # contar e reservar sem nenhum await no meio: outra máquina não passa junto
                if len(self._displays) >= self.max_screens:
                    raise ScreenLimit()
                display = self._alloc_display()
            try:
                if display is not None:
                    bridge = self.build_bridge(m, display)
                hub = self._new_hub(m, bridge)
            except BaseException:
                if display is not None:
                    self._displays.discard(display)
                raise
            rt.hub, rt.bridge, rt.display = hub, bridge, display
            rt.screen_idle_since = self.clock()
            rt.last_bridge = None
            if bridge is not None:
                self._live_bridges.append(bridge)
                rt.bridge_task = self._start_bridge(bridge)  # não espera: o hub reconecta sozinho
                # deixa a subida correr até a primeira espera: falta de ferramenta já aparece
                await asyncio.sleep(0)
                if bridge.state == "missing":
                    message = bridge.error or "Tela da Beckhoff indisponível"
                    log.info("tela ao vivo de %s não tem como subir; vaga liberada", m.name)
                    await self._close_screen(rt, "tela indisponível")
                    rt.last_bridge = {"state": "missing", "error": message}
                    raise ScreenUnavailable(message)
            log.info("tela ao vivo aberta: %s%s", m.name,
                     f" (tela virtual :{display})" if display is not None else "")
            return hub

    async def close_screen(self, machine_id: int, reason: str = SCREEN_REASON) -> None:
        """Fecha hub e ponte da máquina (quem estava assistindo sai) e libera a tela virtual."""
        async with self._lock(machine_id):
            rt = self._runtimes.get(machine_id)
            if rt is not None:
                await self._close_screen(rt, reason)

    async def screen_failed(self, machine_id: int, hub: VncHub) -> None:
        """A sessão de `hub` caiu com erro: se ninguém mais estiver nele, fecha já (sem esperar
        `screen_idle_seconds` segurando a vaga e a tela virtual)."""
        async with self._lock(machine_id):
            rt = self._runtimes.get(machine_id)
            if rt is None or rt.hub is not hub or hub.sessions:
                return
            log.info("tela ao vivo de %s falhou sem ninguém assistindo: fechando", rt.machine.name)
            await self._close_screen(rt, "tela indisponível")

    async def close_idle_screens(self) -> None:
        """Fecha as telas sem ninguém há `screen_idle_seconds` (o laço da frota chama)."""
        now = self.clock()
        idle = []
        for mid, rt in list(self._runtimes.items()):
            hub = rt.hub
            if hub is None:
                continue
            if hub.sessions:
                rt.screen_idle_since = None
            elif rt.screen_idle_since is None:
                rt.screen_idle_since = now
            elif now - rt.screen_idle_since >= self.screen_idle_seconds:
                idle.append(mid)
        await asyncio.gather(*(self._close_if_idle(mid) for mid in idle), return_exceptions=True)

    async def _close_if_idle(self, machine_id: int) -> None:
        async with self._lock(machine_id):
            rt = self._runtimes.get(machine_id)
            if rt is None or rt.hub is None or rt.hub.sessions or rt.screen_idle_since is None:
                return  # alguém entrou enquanto esperava a vez
            if self.clock() - rt.screen_idle_since < self.screen_idle_seconds:
                return
            log.info("tela ao vivo de %s sem ninguém há %ss: fechando", rt.machine.name,
                     int(self.screen_idle_seconds))
            await self._close_screen(rt, SCREEN_REASON)

    async def _close_screen(self, rt: MachineRuntime, reason: str) -> None:
        h, b, bt, d = rt.hub, rt.bridge, rt.bridge_task, rt.display
        rt.hub = rt.bridge = rt.bridge_task = rt.display = rt.screen_idle_since = None
        try:
            if h is not None:
                await self._bounded("hub VNC", h.close(reason))
        finally:
            try:
                await self._close_bridge(b, bt)
            finally:
                # só agora (ponte já encerrada): libera a vaga, a tela virtual e o wineserver
                if b is not None:
                    self._live_bridges = [x for x in self._live_bridges if x is not b]
                if d is not None:
                    self._displays.discard(d)

    async def _bounded(self, what: str, coro) -> None:
        """Desligamento com limite de tempo: máquina "meio aberta" não trava as outras ações."""
        try:
            await asyncio.wait_for(coro, self.close_timeout)
        except asyncio.TimeoutError:
            log.warning("%s demorou mais de %ss para fechar; seguindo", what, self.close_timeout)
        except Exception:
            log.exception("falha ao fechar %s", what)

    async def _close_parts(self, rt: MachineRuntime, reason: str) -> None:
        c = rt.collector
        rt.collector = None
        try:
            if c is not None:
                await self._bounded("coletor", c.stop())
        finally:
            try:
                if c is not None:
                    c.storage.close()
            finally:
                await self._close_screen(rt, reason)

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
