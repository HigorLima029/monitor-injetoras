"""Coletor: lê o gateway periodicamente, guarda histórico e avisa os clientes WebSocket."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from .sources import Source
from .storage import Storage
from .tags import ALARM_PREFIX, HISTORY_TAGS

log = logging.getLogger("coletor")


def to_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    return None


class Collector:
    def __init__(self, source: Source, storage: Storage, poll_seconds: float = 5,
                 full_poll_seconds: float = 60, retention_days: int = 7):
        self.source = source
        self.storage = storage
        self.poll_seconds = poll_seconds
        self.full_poll_seconds = full_poll_seconds
        self.retention_days = retention_days

        self.connected = False
        self.last_ok: float | None = None
        self.last_error: str | None = None
        self.tags: list[str] = []
        self.values: dict[str, Any] = {}
        self.active_alarms: set[str] = storage.open_alarm_tags()
        self.last_count_change: float | None = None
        self.reads = 0
        self._last_full = 0.0
        self._last_prune = 0.0
        self._subscribers: set[asyncio.Queue] = set()
        self._watched: dict[str, float] = {}
        self._task: asyncio.Task | None = None

    # ---- ciclo de vida --------------------------------------------------
    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="coletor")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        await self.source.close()

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=5)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    # ---- leitura --------------------------------------------------------
    @property
    def alarm_tags(self) -> list[str]:
        return [t for t in self.tags if t.startswith(ALARM_PREFIX)]

    @property
    def fast_tags(self) -> list[str]:
        known = set(self.tags)
        now = time.time()
        self._watched = {t: exp for t, exp in self._watched.items() if exp > now}
        base = [t for t in HISTORY_TAGS if t in known] + self.alarm_tags
        extra = [t for t in self._watched if t in known and t not in base]
        return base + extra

    def watch(self, tags: list[str], seconds: float = 30) -> None:
        """Inclui tags na leitura rápida enquanto alguém está olhando a tela que as usa."""
        until = time.time() + seconds
        for t in tags:
            self._watched[t] = until

    def current(self, tags: list[str]) -> dict[str, Any]:
        return {t: self.values.get(t) for t in tags}

    async def _run(self) -> None:
        while True:
            try:
                await self.source.connect()
                self.tags = await self.source.list_tags()
                self.connected = True
                self.last_error = None
                log.info("conectado: %d variáveis", len(self.tags))
                while True:
                    await self.poll_once()
                    await asyncio.sleep(self.poll_seconds)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # rede instável: registra e tenta de novo
                self.connected = False
                self.last_error = f"{type(exc).__name__}: {exc}"[:300]
                log.warning("conexão caiu: %s", self.last_error)
                await self.source.close()
                self._broadcast()
                await asyncio.sleep(5)

    async def poll_once(self) -> None:
        now = time.time()
        full = now - self._last_full >= self.full_poll_seconds
        tags = self.tags if full else self.fast_tags
        values = await self.source.read(tags)
        if full:
            self._last_full = now
        self._apply(now, values)

    def _apply(self, now: float, values: dict[str, Any]) -> None:
        prev_count = to_number(self.values.get("ProductionCount"))
        self.values.update(values)
        self.connected = True
        self.last_ok = now
        self.reads += 1

        count = to_number(self.values.get("ProductionCount"))
        if prev_count is not None and count is not None and count != prev_count:
            self.last_count_change = now

        history = {t: to_number(values[t]) for t in HISTORY_TAGS if t in values}
        if history:
            self.storage.add_samples(now, history)

        active = {t for t in values if t.startswith(ALARM_PREFIX) and (to_number(values[t]) or 0) != 0}
        checked = {t for t in values if t.startswith(ALARM_PREFIX)}
        for tag in active - self.active_alarms:
            self.storage.open_alarm(tag, now)
        for tag in (self.active_alarms & checked) - active:
            self.storage.close_alarm(tag, now)
        self.active_alarms = (self.active_alarms - checked) | active

        if now - self._last_prune > 3600:
            self.storage.prune(now - self.retention_days * 86400)
            self._last_prune = now
        self._broadcast()

    # ---- estado derivado ------------------------------------------------
    def machine_state(self) -> str:
        if not self.connected:
            return "offline"
        if self.active_alarms:
            return "alarme"
        cycle = to_number(self.values.get("CycleMonitor")) or 0
        window = max(3 * cycle, 120)
        if self.last_count_change and time.time() - self.last_count_change <= window:
            return "produzindo"
        return "parada"

    def status(self) -> dict:
        return {
            "connected": self.connected,
            "state": self.machine_state(),
            "last_ok": self.last_ok,
            "last_error": self.last_error,
            "tag_count": len(self.tags),
            "reads": self.reads,
            "active_alarms": sorted(self.active_alarms),
            "server_time": time.time(),
        }

    def live(self) -> dict:
        known = set(self.values)
        return {
            "status": self.status(),
            "values": {t: self.values[t] for t in HISTORY_TAGS if t in known},
        }

    def _broadcast(self) -> None:
        payload = self.live()
        for q in list(self._subscribers):
            if q.full():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            q.put_nowait(payload)
