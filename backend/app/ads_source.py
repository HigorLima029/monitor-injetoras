"""Fonte ADS para painéis Beckhoff (TwinCAT 2): só leitura, mesma interface das outras fontes.

IMPORTANTE: este módulo só LÊ variáveis do PLC. Não existe escrita e não deve existir
(o teste test_codigo_nao_escreve_na_maquina garante).
"""
from __future__ import annotations

import asyncio
import struct

try:  # pyads só é necessário com PLC real; os testes usam um PLC falso
    import pyads
except ImportError:  # pragma: no cover
    pyads = None

ALARM_PREFIX_ADS = "WarnAds"

# Textos dos alarmes do painel (kit de campo beckhoff-kit/03_dados.py).
ADS_ALARMS: dict[int, str] = {
    10: "EtherCAT Contador de Trabalho Errado",
    11: "EtherCAT Estado aparelho Errado",
    12: "EtherCAT contador escravos Errado",
    15: "Lubrificacao 1 nivel baixo",
    161: "Cilindro 1 Bico Zona 1 falhou",
    162: "Cilindro 1 Zona 2 falhou",
    225: "Ajuste Molde fim de curso da frente",
    226: "Ajuste Molde fim de curso de tras",
    318: "Porta do Bico esta aberta",
    419: "Posicao Ajuste Altura invalida",
}

ZONES = 8
ZONE_STRIDE = 112          # bytes por zona em .AAAPFWTEMPPPARAMFROMHMI
MSG_ROWS = 100
MSG_ROW_SIZE = 96          # bytes por linha em .STHMIMESSAGE
MSG_TIME_OFFSET = 56
MSG_TIME_LEN = 24

# tag -> (símbolo no PLC, tipo pyads)
_SCALARS = {
    "CycleMonitor": (".CYCLETIME_MQTT", "LREAL"),
    "ProductionCount": (".NPARTSCNT_MQTT", "DINT"),
    "GoodProduction": (".NGOODPARTS", "DINT"),
    "BadProduction": (".NBADPARTS", "DINT"),
}
_TEMP_SYMBOL = ".AAAPFWTEMPPPARAMFROMHMI"
_MSG_SYMBOL = ".STHMIMESSAGE"

FIXED_TAGS = (
    list(_SCALARS)
    + [f"TempZ{z}" for z in range(1, ZONES + 1)]
    + [f"StandbyTempZ{z}" for z in range(1, ZONES + 1)]
)


def _default_factory(net_id: str, port: int, host: str):
    return pyads.Connection(net_id, port, host)


class AdsSource:
    def __init__(self, ams_net_id: str, host: str, port: int = 801,
                 local_net_id: str = "", plc_factory=None):
        self.ams_net_id = ams_net_id
        self.host = host
        self.port = port
        self.local_net_id = local_net_id
        self._factory = plc_factory
        self._plc = None
        self._seen_alarms: set[int] = set()

    async def connect(self) -> None:
        factory = self._factory
        if factory is None:
            if pyads is None:
                raise RuntimeError("pyads nao instalado")
            # Estado global do pyads: so ajusta o endereco local quando informado.
            if self.local_net_id:
                pyads.open_port()
                pyads.set_local_address(self.local_net_id)
            factory = _default_factory
        plc = factory(self.ams_net_id, self.port, self.host)
        await asyncio.to_thread(plc.open)
        self._plc = plc

    async def close(self) -> None:
        plc, self._plc = self._plc, None
        if plc is not None:
            try:
                await asyncio.to_thread(plc.close)
            except Exception:
                pass

    async def list_tags(self) -> list[str]:
        ids = sorted(set(ADS_ALARMS) | self._seen_alarms)
        return FIXED_TAGS + [f"{ALARM_PREFIX_ADS}{i}" for i in ids]

    # --- leitura (bloqueante, roda em thread) ---

    def _read_block(self, symbol: str, size: int) -> bytes:
        sym = self._plc.get_symbol(symbol)
        tipo = pyads.PLCTYPE_BYTE * size  # tamanho calculado antes, evita erro de precedência
        return bytes(self._plc.read(sym.index_group, sym.index_offset, tipo))

    def _read_all(self, tags: list[str]) -> dict[str, object]:
        assert self._plc is not None
        out: dict[str, object] = {}
        for tag in tags:
            if tag in _SCALARS:
                name, kind = _SCALARS[tag]
                out[tag] = self._plc.read_by_name(name, getattr(pyads, f"PLCTYPE_{kind}"))
        if any(t.startswith(("TempZ", "StandbyTempZ")) for t in tags):
            raw = self._read_block(_TEMP_SYMBOL, ZONES * ZONE_STRIDE)
            for z in range(1, ZONES + 1):
                base = (z - 1) * ZONE_STRIDE
                out[f"TempZ{z}"] = struct.unpack_from("<d", raw, base)[0]
                out[f"StandbyTempZ{z}"] = struct.unpack_from("<d", raw, base + 8)[0]
        if any(t.startswith(ALARM_PREFIX_ADS) for t in tags):
            raw = self._read_block(_MSG_SYMBOL, MSG_ROWS * MSG_ROW_SIZE)
            active: set[int] = set()
            for row in range(MSG_ROWS):
                base = row * MSG_ROW_SIZE
                hora = raw[base + MSG_TIME_OFFSET: base + MSG_TIME_OFFSET + MSG_TIME_LEN]
                if not hora.split(b"\x00")[0].decode("latin1", "ignore").strip():
                    continue
                active.add(struct.unpack_from("<i", raw, base)[0])
            self._seen_alarms |= active
            for tag in tags:
                if tag.startswith(ALARM_PREFIX_ADS):
                    try:
                        out[tag] = 1.0 if int(tag[len(ALARM_PREFIX_ADS):]) in active else 0.0
                    except ValueError:
                        out[tag] = 0.0
        return {t: out.get(t, 0.0) for t in tags}

    async def read(self, tags: list[str]) -> dict[str, object]:
        return await asyncio.to_thread(self._read_all, tags)
