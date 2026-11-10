"""Fonte OPC-UA para paineis B&R da Chen Hsong com o mapeamento Euromap 63 (ex.: JM228).

O painel publica as variaveis em ns=<urn:B&R/pv/>;s=::EM63Mapp:pEm63a.<Campo> (maquina)
e ::EM63Mapp:pEm63m.<Campo> (producao). Aqui elas sao traduzidas para os mesmos nomes
internos usados com o gateway da CHY (ProductionCount, TempZ1Now, ...), para que os
dashboards funcionem sem mudanca.

IMPORTANTE: somente leitura. Nenhuma escrita OPC-UA (o teste de seguranca cobre este arquivo).
"""
from __future__ import annotations

from asyncua import Client, ua

NS_URI = "urn:B&R/pv/"
PREFIX = "::EM63Mapp:"

# Alarme sintetico: o Euromap 63 nao traz a lista de alarmes, so o estado geral da maquina.
ALARM_ERROR = "WarnMachineError"

# campo Euromap 63 -> tag interna (valores simples)
SIMPLE = {
    "pEm63m.ActCntCyc": "ProductionCount",
    "pEm63m.ActCntPrtRej": "BadProduction",
    "pEm63a.ActTimCyc": "CycleMonitor",
    "pEm63a.ActTimFill": "InjectLastTime",
    "pEm63a.ActTimPlst": "PlastLastTime",
    "pEm63a.myActTimCool": "ColdLastTime",
    "pEm63a.myActTimMldCls": "ClampLastTime",
    "pEm63a.myActTimMldOpn": "OpenLastTime",
    "pEm63a.myActTimEject": "EjectLastTime",
    "pEm63a.ActPrsMachHydMax": "InjectMaxPress",
    "pEm63a.ActStkCsh": "InjectEndLastPos",
    "pEm63a.ActStkPlst": "PlastEndLastPos",
    "pEm63a.SetTmpOil": "TempOil",
    "pEm63a.ActTmpOil": "TempOilNow",
}
# listas por zona do canhao (Barrel Zone 01..08)
ZONE_ARRAYS = {
    "pEm63a.ActSwitStaBrlZn": "switch",
    "pEm63a.SetTmpBrlZn": "set",
    "pEm63a.ActTmpBrlZn": "act",
    "pEm63a.SetTmpBrlZnLdev": "low",
    "pEm63a.SetTmpBrlZnHdev": "high",
}
# Tempos de fase "my*" (fechamento, abertura, resfriamento, extracao): em alguns paineis vem em
# milissegundos (JM208MK6, software V1.58: fechamento 1220 = 1,22 s), em outros pode vir em segundos.
PHASE_FIELDS = ["pEm63a.myActTimCool", "pEm63a.myActTimMldCls", "pEm63a.myActTimMldOpn", "pEm63a.myActTimEject"]
STATUS_FIELDS = ["pEm63m.ActStsMach", "pEm63m.ActStsCyc", "pEm63m.SetCntCyc", "pEm63m.ActDescJob"]
MAX_ZONES = 5  # o painel do Monitor tem Zona 1..5


def _num(v):
    if isinstance(v, bool):
        return float(v)
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return None


def _phases_to_seconds(raw: dict[str, object], out: dict[str, object]) -> None:
    """Converte os tempos de fase para segundos quando o painel manda em milissegundos.

    Compara com o tempo total do ciclo (sempre em segundos no Euromap 63): se a soma das fases
    passa de 5x o ciclo, so pode estar em ms. Sem ciclo conhecido, uma fase acima de 300 "s" tambem.
    """
    phases = {f: SIMPLE[f] for f in PHASE_FIELDS if f in raw and out.get(SIMPLE[f]) is not None}
    if not phases:
        return
    values = [out[t] for t in phases.values()]
    cycle = out.get("CycleMonitor") or 0
    in_ms = sum(values) > 5 * cycle if cycle > 0 else max(values) > 300
    if in_ms:
        for tag in phases.values():
            out[tag] = round(out[tag] / 1000.0, 3)


def translate(raw: dict[str, object]) -> dict[str, object]:
    """Converte os campos Euromap 63 lidos para as tags internas do Monitor."""
    out: dict[str, object] = {}
    for field, tag in SIMPLE.items():
        if field in raw:
            out[tag] = _num(raw[field])
    _phases_to_seconds(raw, out)
    count, bad = out.get("ProductionCount"), out.get("BadProduction")
    if count is not None:
        out["DefectCount"] = bad
        out["ProductionGoodCount"] = max(0.0, count - (bad or 0.0))
    out["ProductionCycle"] = out.get("CycleMonitor")
    target = _num(raw.get("pEm63m.SetCntCyc"))
    out["TargetProductionCount"] = target if target else None

    lists = {kind: raw.get(field) for field, kind in ZONE_ARRAYS.items()}
    switches = lists.get("switch") or []
    for i in range(MAX_ZONES):
        n = i + 1
        on = i < len(switches) and str(switches[i]).strip() not in ("0", "", "False", "false")

        def pick(kind):
            arr = lists.get(kind) or []
            return _num(arr[i]) if on and i < len(arr) else None

        out[f"TempZ{n}"] = pick("set")
        out[f"TempZ{n}Now"] = pick("act")
        out[f"MinTempZ{n}"] = pick("low")
        out[f"MaxTempZ{n}"] = pick("high")

    status = str(raw.get("pEm63m.ActStsMach") or "")
    out["MachineStatusText"] = status or None
    out["DeviceOnOff"] = 1.0
    out["ProductionStatus"] = 1.0 if status.lower().startswith(("prod", "auto")) else 0.0
    out[ALARM_ERROR] = 1.0 if status.lower() == "error" else 0.0
    return out


INTERNAL_TAGS = sorted(set(translate({}).keys()) | set(SIMPLE.values()) | {"DefectCount", "ProductionGoodCount"})


class Em63Source:
    """Le o Euromap 63 do painel B&R e entrega nos nomes internos do Monitor."""

    CHUNK = 200

    def __init__(self, url: str, fallback_namespace: int = 6):
        self.url = url
        self.fallback_ns = fallback_namespace
        self._client: Client | None = None
        self._ns = fallback_namespace
        self._fields: list[str] = []  # todos os campos pEm63a.* / pEm63m.* encontrados
        self._nodes: dict[str, object] = {}

    async def connect(self) -> None:
        client = Client(self.url, timeout=30, watchdog_intervall=30)
        await client.connect()
        self._client = client
        try:
            self._ns = await client.get_namespace_index(NS_URI)
        except Exception:
            self._ns = self.fallback_ns
        self._nodes = {}

    async def close(self) -> None:
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception:
                pass
        self._client = None

    async def _browse_fields(self) -> list[str]:
        assert self._client is not None
        found: list[str] = []
        for struct in ("pEm63a", "pEm63m"):
            node = self._client.get_node(ua.NodeId(f"{PREFIX}{struct}", self._ns))
            try:
                children = await node.get_children()
            except Exception:
                continue
            for child in children:
                ident = child.nodeid.Identifier
                if isinstance(ident, str) and ident.startswith(PREFIX):
                    found.append(ident[len(PREFIX):])
        return sorted(set(found))

    async def list_tags(self) -> list[str]:
        if not self._fields:
            self._fields = await self._browse_fields()
            if not self._fields:
                raise RuntimeError("Nao achei ::EM63Mapp no servidor OPC-UA (nao e um painel B&R Euromap 63?)")
        # tags internas + os campos brutos (para a tela Variaveis)
        return INTERNAL_TAGS + [f"EM63.{f}" for f in self._fields]

    def _node(self, field: str):
        node = self._nodes.get(field)
        if node is None:
            assert self._client is not None
            node = self._client.get_node(ua.NodeId(f"{PREFIX}{field}", self._ns))
            self._nodes[field] = node
        return node

    async def _read_fields(self, fields: list[str]) -> dict[str, object]:
        assert self._client is not None
        out: dict[str, object] = {}
        for i in range(0, len(fields), self.CHUNK):
            chunk = fields[i:i + self.CHUNK]
            try:
                values = await self._client.read_values([self._node(f) for f in chunk])
            except Exception:
                values = []
                for f in chunk:
                    try:
                        values.append(await self._node(f).read_value())
                    except Exception:
                        values.append(None)
            out.update(zip(chunk, values))
        return out

    async def read(self, tags: list[str]) -> dict[str, object]:
        raw_wanted = [t[5:] for t in tags if t.startswith("EM63.")]
        needed = set(SIMPLE) | set(ZONE_ARRAYS) | set(STATUS_FIELDS)
        known = set(self._fields) if self._fields else needed
        fields = sorted((needed & known) | set(raw_wanted))
        raw = await self._read_fields(fields)
        internal = translate(raw)
        out: dict[str, object] = {}
        for t in tags:
            if t.startswith("EM63."):
                v = raw.get(t[5:])
                out[t] = list(v) if isinstance(v, (list, tuple)) else v
            elif t in internal:
                out[t] = internal[t]
        return out
