"""Fontes de dados: o gateway OPC-UA real (só leitura) e um simulador para testes.

IMPORTANTE: este módulo só LÊ dados. Não existe nenhuma chamada de escrita
OPC-UA e não deve existir (o teste test_codigo_nao_escreve_na_maquina garante).
"""
from __future__ import annotations

import json
import math
import random
import time
from pathlib import Path
from typing import Protocol

from asyncua import Client, ua

from .tags import ALARM_PREFIX, BARREL_ZONES


class Source(Protocol):
    async def connect(self) -> None: ...
    async def close(self) -> None: ...
    async def list_tags(self) -> list[str]: ...
    async def read(self, tags: list[str]) -> dict[str, object]: ...


class OpcUaSource:
    """Lê variáveis ns=<namespace>;s=<Nome> do servidor OPC-UA do gateway."""

    CHUNK = 200

    def __init__(self, url: str, namespace: int = 1, cache_file: str | None = None):
        self.url = url
        self.namespace = namespace
        self.cache_file = Path(cache_file) if cache_file else None
        self._client: Client | None = None
        self._tags: list[str] = []
        self._nodes: dict[str, object] = {}

    async def connect(self) -> None:
        # Watchdog e timeout folgados: o gateway costuma estar em Wi-Fi/4G com latência alta.
        client = Client(self.url, timeout=30, watchdog_intervall=30)
        await client.connect()
        self._client = client
        self._nodes = {}

    async def close(self) -> None:
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception:
                pass
        self._client = None

    async def list_tags(self) -> list[str]:
        if self._tags:
            return self._tags
        if self.cache_file and self.cache_file.exists():
            self._tags = json.loads(self.cache_file.read_text(encoding="utf-8"))
            if self._tags:
                return self._tags
        assert self._client is not None
        found: list[str] = []

        async def browse(node, depth: int) -> None:
            if depth > 4:
                return
            for child in await node.get_children():
                cls = await child.read_node_class()
                nid = child.nodeid
                if cls == ua.NodeClass.Variable and nid.NamespaceIndex == self.namespace:
                    if nid.NodeIdType == ua.NodeIdType.String:
                        found.append(nid.Identifier)
                elif cls == ua.NodeClass.Object:
                    await browse(child, depth + 1)

        await browse(self._client.nodes.objects, 0)
        self._tags = sorted(set(found))
        if self.cache_file:
            self.cache_file.parent.mkdir(parents=True, exist_ok=True)
            self.cache_file.write_text(json.dumps(self._tags), encoding="utf-8")
        return self._tags

    def _node(self, tag: str):
        node = self._nodes.get(tag)
        if node is None:
            assert self._client is not None
            node = self._client.get_node(ua.NodeId(tag, self.namespace))
            self._nodes[tag] = node
        return node

    async def read(self, tags: list[str]) -> dict[str, object]:
        assert self._client is not None
        out: dict[str, object] = {}
        for i in range(0, len(tags), self.CHUNK):
            chunk = tags[i : i + self.CHUNK]
            values = await self._client.read_values([self._node(t) for t in chunk])
            out.update(zip(chunk, values))
        return out


class SimulatedSource:
    """Imita uma injetora produzindo: aquece o canhão, faz ciclos e gera alarmes de vez em quando."""

    def __init__(self, cycle_seconds: float = 24.0, seed: int | None = None):
        self.cycle = cycle_seconds
        self.rng = random.Random(seed)
        self.t0 = time.time() - 3600  # começa com o canhão já aquecido
        self.last_cycle_end = time.time()
        self.count = 1200
        self.bad = 14
        self.power = 3810.0
        self.alarm_until = 0.0
        self.alarm_tag = ""
        self.setpoints = {"TempNozzle": 230.0, "TempZ1": 270.0, "TempZ2": 260.0,
                          "TempZ3": 250.0, "TempZ4": 200.0, "TempZ5": 200.0, "TempOil": 45.0}
        self._tags: list[str] = []

    async def connect(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def list_tags(self) -> list[str]:
        if not self._tags:
            from .tags import ALARM_LABELS, HISTORY_TAGS

            extra = ["Inject1Pres", "Inject1Speed", "Plast1Pos", "Open4Pos", "Clamp1Pres",
                     "MaxTempOil", "WiFiSsid"]
            self._tags = sorted(set(HISTORY_TAGS) | set(ALARM_LABELS) | set(extra))
        return self._tags

    def _temp(self, sp: float, phase: float) -> float:
        warm = 1 - math.exp(-(time.time() - self.t0) / 90)
        base = 25 + (sp - 25) * warm
        return round(base + 1.6 * math.sin(time.time() / 37 + phase) + self.rng.uniform(-0.4, 0.4), 1)

    async def read(self, tags: list[str]) -> dict[str, object]:
        now = time.time()
        running = now >= self.alarm_until
        if running and now - self.last_cycle_end >= self.cycle:
            self.last_cycle_end = now
            self.count += 1
            if self.rng.random() < 0.03:
                self.bad += 1
            self.power += 0.06
            self.cycle = 24.0 + self.rng.uniform(-1.2, 1.8)
        if now >= self.alarm_until and self.rng.random() < 0.004:
            self.alarm_tag = self.rng.choice(
                ["WarnFrontSafetyDoorOpen", "WarnCycleTooLong", "WarnProductDropNot", "WarnRobotAlarm"]
            )
            self.alarm_until = now + self.rng.uniform(40, 120)

        vals: dict[str, object] = {}
        for i, (_, now_tag, sp_tag, min_tag, max_tag) in enumerate(BARREL_ZONES):
            sp = self.setpoints[sp_tag]
            vals[sp_tag] = sp
            vals[now_tag] = self._temp(sp, i)
            vals[min_tag] = 10.0
            vals[max_tag] = 10.0
        vals.update({
            "TempOil": self.setpoints["TempOil"], "MaxTempOil": 60.0,
            "TempOilNow": self._temp(self.setpoints["TempOil"], 9),
            "DeviceOnOff": 1.0, "OperatorMode": 3.0 if running else 0.0,
            "ProductionStatus": 1.0 if running else 0.0,
            "ProductionCount": float(self.count), "BadProduction": float(self.bad),
            "ProductionGoodCount": float(self.count - self.bad), "DefectCount": float(self.bad),
            "TargetProductionCount": 2000.0,
            "CycleMonitor": round(self.cycle, 2), "ProductionCycle": round(self.cycle, 2),
            "InjectLastTime": round(1.1 + self.rng.uniform(-0.05, 0.05), 2),
            "HoldLastTime": round(2.5 + self.rng.uniform(-0.05, 0.05), 2),
            "PlastLastTime": round(4.8 + self.rng.uniform(-0.2, 0.2), 2),
            "ColdLastTime": 9.0, "ClampLastTime": round(1.9 + self.rng.uniform(-0.05, 0.05), 2),
            "OpenLastTime": round(1.6 + self.rng.uniform(-0.05, 0.05), 2),
            "EjectLastTime": 0.8,
            "InjectMaxPress": round(96 + self.rng.uniform(-3, 3), 1),
            "InjectEndLastPos": round(6.2 + self.rng.uniform(-0.3, 0.3), 2),
            "PlastEndLastPos": round(180 + self.rng.uniform(-0.5, 0.5), 1),
            "InjectMaxSpeed": 100.0, "PowerConsume": round(self.power, 2),
            "Inject1Pres": 110.0, "Inject1Speed": 100.0, "Plast1Pos": 180.0, "Open4Pos": 360.0,
            "Clamp1Pres": 40.0, "WiFiSsid": "simulador",
        })
        for tag in tags:
            if tag.startswith(ALARM_PREFIX):
                vals[tag] = 1.0 if (tag == self.alarm_tag and now < self.alarm_until) else 0.0
        return {t: vals.get(t, 0.0) for t in tags}
