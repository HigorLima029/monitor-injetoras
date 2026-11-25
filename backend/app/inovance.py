"""Fonte OPC-UA para injetoras Chen Hsong com controlador Inovance (painel Chen Hsong s6410, app warm15sopcua).

O painel expoe um servidor OPC-UA em ns=1 com nos NUMERICOS e nomes em ingles, sob
a arvore PLC > Modules > <Default> > App. Aqui esses nos sao lidos por NOME (browse
name, pois os nodeids numericos podem mudar entre maquinas/firmwares) e traduzidos
para as tags internas do Monitor (CycleMonitor, TempZ1Now, ...), para os dashboards
funcionarem sem mudanca.

PRIMEIRA VERSAO (mapeamento a confirmar com a maquina PRODUZINDO, pois parada vem 0):
  - ordem das temperaturas: assumimos Temp1 = bico, Temp2..6 = zonas 1..5;
  - contador de pecas: OpenCnt Hi/Low combinados como Hi*10000 + Low;
  - modo: Operate State != 0 -> produzindo (enum real a confirmar).

IMPORTANTE: somente leitura. Nenhuma escrita OPC-UA (o teste de seguranca cobre este arquivo).
"""
from __future__ import annotations

from asyncua import Client, ua

from .inovance_alarms import INO_ALARMS, ino_alarm_tag
from .sources import close_client

# Pasta (Object) onde ficam as variaveis da aplicacao, dentro de PLC > Modules > <Default>.
APP_FOLDER = "App"

# tag interna do Monitor -> nome da variavel no painel Inovance (browse name)
SIMPLE = {
    # tempos de ciclo e de fase (unidade a confirmar; passamos o valor cru)
    "CycleMonitor": "CycTime Real",
    "InjectLastTime": "Inj Tm Real",
    "PlastLastTime": "Charge Tm Real",      # dosagem = charge/plastificacao
    "ClampLastTime": "Close Tm Real",       # fechamento
    "OpenLastTime": "Open Tm Real",         # abertura
    "EjectLastTime": "Eject Tm Real",       # extracao
    # qualidade
    "InjectMaxPress": "Max InjPrs Real",
    "InjectMaxSpeed": "Inject Max Speed",
    "InjectEndLastPos": "Inject Min Pos",   # colchao = posicao minima da rosca (confirmar)
    "PlastEndLastPos": "Charge Pos Real",   # fim de dosagem
    # temperaturas reais: Temp1 = bico, Temp2..6 = zonas 1..5 (CONFIRMAR a ordem na maquina quente)
    "TempNozzleNow": "Temp1 Real",
    "TempZ1Now": "Temp2 Real",
    "TempZ2Now": "Temp3 Real",
    "TempZ3Now": "Temp4 Real",
    "TempZ4Now": "Temp5 Real",
    "TempZ5Now": "Temp6 Real",
    # setpoints de temperatura (mesma ordem)
    "TempNozzle": "Temp1 Set",
    "TempZ1": "Temp2 Set",
    "TempZ2": "Temp3 Set",
    "TempZ3": "Temp4 Set",
    "TempZ4": "Temp5 Set",
    "TempZ5": "Temp6 Set",
}

# contador de pecas: dois registradores (parte alta e baixa)
COUNT_HI = "OpenCnt Hi Real"
COUNT_LOW = "OpenCnt Low Real"
COUNT_SCALE = 65536  # Hi*65536 + Low: "开模总数高/低 16 位" na planilha oficial de pontos OPC-UA

STATE_FIELD = "Operate State"
OPERATE_STATE = {0x00: 1.0, 0x03: 2.0, 0x05: 3.0, 0x09: 3.0}
ALARM_FIELDS = ["Alarm1", "Alarm2"]  # cada um traz o codigo de um alarme ativo (0 = nenhum)

# todos os nomes brutos que o translate() precisa ler do painel
_SOURCE_NAMES = sorted(set(SIMPLE.values()) | {COUNT_HI, COUNT_LOW, STATE_FIELD, *ALARM_FIELDS})


def _num(v):
    if isinstance(v, bool):
        return float(v)
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return None


def translate(raw: dict[str, object]) -> dict[str, object]:
    """Converte os campos do painel Inovance lidos para as tags internas do Monitor."""
    out: dict[str, object] = {}
    for tag, field in SIMPLE.items():
        if field in raw:
            out[tag] = _num(raw[field])

    hi, low = _num(raw.get(COUNT_HI)), _num(raw.get(COUNT_LOW))
    if hi is not None or low is not None:
        count = (hi or 0.0) * COUNT_SCALE + (low or 0.0)
        out["ProductionCount"] = count
        out["ProductionGoodCount"] = count

    out["ProductionCycle"] = out.get("CycleMonitor")

    state = _num(raw.get(STATE_FIELD))
    # Operate State (planilha oficial): 0x00 manual, 0x03 semiautomatico, 0x05 automatico por
    # fotocelula, 0x09 automatico por tempo. Convertido para os codigos do Monitor
    # (1 manual, 2 semiautomatico, 3 automatico); valor desconhecido passa cru.
    out["OperatorMode"] = None if state is None else OPERATE_STATE.get(int(state), state)
    out["DeviceOnOff"] = 1.0
    out["ProductionStatus"] = 1.0 if state is not None and int(state) in (3, 5, 9) else 0.0

    return out


def active_alarm_codes(raw: dict[str, object]) -> set[int]:
    """Codigos dos alarmes ativos publicados em Alarm1/Alarm2 (0 = nenhum)."""
    codes: set[int] = set()
    for field in ALARM_FIELDS:
        v = _num(raw.get(field))
        if v is not None and v > 0:
            codes.add(int(v))
    return codes


INTERNAL_TAGS = sorted(
    set(SIMPLE.keys())
    | {"ProductionCount", "ProductionGoodCount", "ProductionCycle",
       "OperatorMode", "DeviceOnOff", "ProductionStatus"}
)
# um alarme por codigo conhecido (WarnIno<HEX>), cada um com o nome em portugues
ALARM_TAGS = [ino_alarm_tag(c) for c in sorted(INO_ALARMS)]


class InovanceSource:
    """Le o OPC-UA do painel Chen Hsong (controlador Inovance) e entrega nos nomes internos do Monitor."""

    CHUNK = 200

    def __init__(self, url: str, namespace: int = 1):
        self.url = url
        self.namespace = namespace
        self._client: Client | None = None
        self._nodes: dict[str, object] = {}  # browse name -> node da pasta App
        self._seen_alarms: set[int] = set()     # codigos fora da tabela que ja apareceram

    async def connect(self) -> None:
        client = Client(self.url, timeout=30, watchdog_intervall=30)
        await client.connect()
        self._client = client
        self._nodes = await self._map_app()

    async def close(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            await close_client(client)

    async def _find_app(self, node, depth: int):
        """Procura a pasta 'App' descendo pela arvore de objetos (PLC > Modules > <Default> > App).

        Uma unica chamada de browse por pasta (nome e tipo ja vem na resposta) e sem entrar
        na arvore padrao 'Server': pelo 4G cada ida e volta custa dezenas de ms.
        """
        if depth > 6:
            return None
        descs = await node.get_children_descriptions()
        for d in descs:
            if d.BrowseName.Name == APP_FOLDER:
                return self._client.get_node(d.NodeId)
        for d in descs:
            if d.NodeClass != ua.NodeClass.Object or d.BrowseName.Name == "Server" \
                    or d.NodeId == ua.NodeId(ua.ObjectIds.Server):
                continue
            found = await self._find_app(self._client.get_node(d.NodeId), depth + 1)
            if found is not None:
                return found
        return None

    async def _map_app(self) -> dict[str, object]:
        assert self._client is not None
        app = await self._find_app(self._client.nodes.objects, 0)
        if app is None:
            raise RuntimeError(
                "Nao achei a pasta 'App' no servidor OPC-UA (nao e um painel Inovance?)"
            )
        nodes: dict[str, object] = {}
        for d in await app.get_children_descriptions():
            if d.NodeClass == ua.NodeClass.Variable:
                nodes[d.BrowseName.Name] = self._client.get_node(d.NodeId)
        return nodes

    async def list_tags(self) -> list[str]:
        if not self._nodes:
            self._nodes = await self._map_app()
        # tags internas + os nomes brutos (para a tela Variaveis)
        extra = [ino_alarm_tag(c) for c in sorted(self._seen_alarms)]
        return INTERNAL_TAGS + ALARM_TAGS + extra + [f"INO.{n}" for n in sorted(self._nodes)]

    async def _read_names(self, names: list[str]) -> dict[str, object]:
        assert self._client is not None
        present = [n for n in names if n in self._nodes]
        out: dict[str, object] = {}
        for i in range(0, len(present), self.CHUNK):
            chunk = present[i:i + self.CHUNK]
            nodes = [self._nodes[n] for n in chunk]
            # Uma unica leitura por bloco. No que o painel recusa vira None, sem cair
            # para leitura um a um (pelo 4G, centenas de idas e voltas estouram o tempo).
            results = await self._client.read_attributes(nodes, ua.AttributeIds.Value)
            values = [r.Value.Value if r.StatusCode.is_good() and r.Value is not None else None
                      for r in results]
            out.update(zip(chunk, values))
        return out

    async def read(self, tags: list[str]) -> dict[str, object]:
        raw_wanted = [t[4:] for t in tags if t.startswith("INO.")]
        need_internal = any(not t.startswith("INO.") for t in tags)
        names = set(raw_wanted)
        if need_internal:
            names |= set(_SOURCE_NAMES)
        raw = await self._read_names(sorted(names))
        internal = translate(raw) if need_internal else {}
        out: dict[str, object] = {}
        if need_internal and all(f in raw for f in ALARM_FIELDS):
            # Todos os alarmes conhecidos (e os codigos novos ja vistos) vao juntos, pedidos
            # ou nao: um codigo novo tambem chega ao coletor e vira alarme com o codigo no nome.
            active = active_alarm_codes(raw)
            self._seen_alarms |= active - set(INO_ALARMS)
            for code in set(INO_ALARMS) | self._seen_alarms:
                out[ino_alarm_tag(code)] = 1.0 if code in active else 0.0
        for t in tags:
            if t.startswith("INO."):
                out[t] = raw.get(t[4:])
            elif t in internal:
                out[t] = internal[t]
        return out
