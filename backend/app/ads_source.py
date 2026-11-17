"""Fonte ADS para painéis Beckhoff (TwinCAT 2): só leitura, mesma interface das outras fontes.

IMPORTANTE: este módulo só LÊ variáveis do PLC. Não existe escrita e não deve existir
(o teste test_codigo_nao_escreve_na_maquina garante).
"""
from __future__ import annotations

import asyncio
import ctypes
import logging
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
MSG_TIME_LEN = 23

# tag -> (símbolo no PLC, tipo pyads)
# Os quatro primeiros vieram do kit de campo; os demais do mapa da própria Chen Hsong
# (tabela Xmold2 do banco /work/user do gateway CHL3200, versão TwinCAT 2).
_SCALARS = {
    "CycleMonitor": (".CYCLETIME_MQTT", "LREAL"),
    "ProductionCount": (".NPARTSCNT_MQTT", "DINT"),
    "GoodProduction": (".NGOODPARTS", "DINT"),
    "BadProduction": (".NBADPARTS", "DINT"),
    "PlastLastTime": (".STAXIS_HMIITF[5].STLATCHEDVALUES.DOSINGTIME", "LREAL"),
    "ColdLastTime": (".STAXIS_HMIITF[5].LATCHEDTIMER[1]", "LREAL"),
}
# Temperatura real de cada zona (TempZ<n>Now).
_ACT_TEMP = ".AAAPFWTEMPTOHMI[{}].ACTUALTEMP"
# Modo de operação: LEDs do teclado do painel. OperatorMode = valor da primeira lâmpada acesa.
OPERATOR_MODES = {1.0: "Manual", 2.0: "Semiautomático", 3.0: "Automático", 4.0: "Ajuste"}
_MODE_SYMBOLS = [
    (".STUSBLED.FULLAUTOMATIC", 3.0),
    (".STUSBLED.SEMIAUTOMATIC", 2.0),
    (".STUSBLED.MANUALON", 1.0),
    (".STUSBLED.CALIBRATIONON", 4.0),
]
_TEMP_SYMBOL = ".AAAPFWTEMPPPARAMFROMHMI"
_MSG_SYMBOL = ".STHMIMESSAGE"

FIXED_TAGS = (
    list(_SCALARS)
    + ["OperatorMode"]
    + [f"TempZ{z}Now" for z in range(1, ZONES + 1)]
    + [f"TempZ{z}" for z in range(1, ZONES + 1)]
    + [f"StandbyTempZ{z}" for z in range(1, ZONES + 1)]
)


log = logging.getLogger(__name__)

# open_port/set_local_address sao globais do processo: so uma vez.
_porta_aberta = False


def _plctype(kind: str):
    return getattr(pyads, f"PLCTYPE_{kind}") if pyads is not None else kind


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
        self._logged: set[str] = set()
        self._sum_ok: bool | None = None   # o PLC aceita leitura em lote (sum read)? None = ainda não sei
        self._missing: set[str] = set()    # símbolos que este PLC não tem (até reconectar)

    async def connect(self) -> None:
        global _porta_aberta
        await self.close()  # nao vaza uma conexao anterior
        factory = self._factory
        if factory is None:
            if pyads is None:
                raise RuntimeError("pyads nao instalado: necessario para conectar ao PLC Beckhoff")
            if self.local_net_id and not _porta_aberta:
                pyads.open_port()
                pyads.set_local_address(self.local_net_id)
                _porta_aberta = True
            factory = _default_factory
        plc = factory(self.ams_net_id, self.port, self.host)
        await asyncio.to_thread(plc.open)
        self._plc = plc
        self._sum_ok = None
        self._missing = set()

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

    def _read_block(self, plc, symbol: str, size: int) -> bytes:
        sym = plc.get_symbol(symbol)
        tipo = ctypes.c_ubyte * size  # mesmo que PLCTYPE_BYTE * size; tamanho calculado antes
        return bytes(plc.read(sym.index_group, sym.index_offset, tipo))

    def _falha(self, chave: str, exc: Exception) -> None:
        if chave not in self._logged:  # avisa uma vez, nao a cada leitura
            self._logged.add(chave)
            log.warning("ADS %s: leitura de %s falhou: %s", self.host, chave, exc)

    def _read_named(self, plc, wanted: dict[str, str]) -> tuple[dict[str, object], list[Exception], int]:
        """Lê símbolos simples por nome. Tenta um lote só (uma ida e volta no 4G);
        se o PLC não aceitar, lê um por um. Devolve (valores, erros, tentativas)."""
        syms = [s for s in wanted if s not in self._missing]
        vals: dict[str, object] = {}
        erros: list[Exception] = []
        if not syms:
            return vals, erros, 0
        if self._sum_ok is not False and len(syms) > 1:
            try:
                res = plc.read_list_by_name(syms)
            except Exception:
                res = None
            if res is not None:
                self._sum_ok = True
                for s in syms:
                    v = res.get(s)
                    if v is None or isinstance(v, str):  # pyads devolve o texto do erro no lugar do valor
                        exc = RuntimeError(f"leitura em lote falhou: {v}")
                        erros.append(exc)
                        self._falha(s, exc)
                    else:
                        vals[s] = v
                return vals, erros, len(syms)
        falhou: list[str] = []
        for s in syms:
            try:
                vals[s] = plc.read_by_name(s, _plctype(wanted[s]))
            except Exception as exc:
                erros.append(exc)
                falhou.append(s)
                self._falha(s, exc)
        if vals:  # a conexão está boa: o que falhou é símbolo que este painel não tem
            self._missing.update(falhou)
            if not falhou and len(syms) > 1:
                self._sum_ok = False  # tudo existe e mesmo assim o lote falhou: PLC sem leitura em lote
        return vals, erros, len(syms)

    def _read_all(self, tags: list[str]) -> dict[str, object]:
        plc = self._plc
        if plc is None:
            raise RuntimeError("AdsSource nao conectado")
        out: dict[str, object] = {}
        extra: dict[str, object] = {}
        tentativas = 0
        erros: list[Exception] = []

        wanted: dict[str, str] = {}
        for tag in tags:
            if tag in _SCALARS:
                name, kind = _SCALARS[tag]
                wanted[name] = kind
            elif tag.startswith("TempZ") and tag.endswith("Now"):
                try:
                    z = int(tag[5:-3])
                except ValueError:
                    continue
                if 1 <= z <= ZONES:
                    wanted[_ACT_TEMP.format(z)] = "LREAL"
            elif tag == "OperatorMode":
                for name, _ in _MODE_SYMBOLS:
                    wanted[name] = "BOOL"
        if wanted:
            vals, errs, n = self._read_named(plc, wanted)
            erros += errs
            tentativas += n
            for tag in tags:
                if tag in _SCALARS and _SCALARS[tag][0] in vals:
                    out[tag] = vals[_SCALARS[tag][0]]
                elif tag.startswith("TempZ") and tag.endswith("Now"):
                    try:
                        sym = _ACT_TEMP.format(int(tag[5:-3]))
                    except ValueError:
                        continue
                    if sym in vals:
                        out[tag] = vals[sym]
            if "OperatorMode" in tags:
                lidos = [(code, vals[name]) for name, code in _MODE_SYMBOLS if name in vals]
                if lidos:
                    out["OperatorMode"] = next((code for code, on in lidos if on), 0.0)

        if any(t.startswith(("TempZ", "StandbyTempZ")) and not t.endswith("Now") for t in tags):
            tentativas += 1
            try:
                raw = self._read_block(plc, _TEMP_SYMBOL, ZONES * ZONE_STRIDE)
                for z in range(1, ZONES + 1):
                    base = (z - 1) * ZONE_STRIDE
                    out[f"TempZ{z}"] = struct.unpack_from("<d", raw, base)[0]
                    out[f"StandbyTempZ{z}"] = struct.unpack_from("<d", raw, base + 8)[0]
            except Exception as exc:
                erros.append(exc)
                self._falha(_TEMP_SYMBOL, exc)

        if any(t.startswith(ALARM_PREFIX_ADS) for t in tags):
            tentativas += 1
            try:
                raw = self._read_block(plc, _MSG_SYMBOL, MSG_ROWS * MSG_ROW_SIZE)
                active: set[int] = set()
                for row in range(MSG_ROWS):
                    base = row * MSG_ROW_SIZE
                    hora = raw[base + MSG_TIME_OFFSET: base + MSG_TIME_OFFSET + MSG_TIME_LEN]
                    if not hora.split(b"\x00")[0].decode("latin1", "ignore").strip():
                        continue
                    active.add(struct.unpack_from("<i", raw, base)[0])
                self._seen_alarms |= active
                # Todos os IDs conhecidos ou já vistos, pedidos ou não: um ID novo no painel
                # (fora da lista de tags lida na conexão) também chega ao coletor e vira alarme.
                for alarm_id in set(ADS_ALARMS) | self._seen_alarms:
                    extra[f"{ALARM_PREFIX_ADS}{alarm_id}"] = 1.0 if alarm_id in active else 0.0
                for tag in tags:
                    if tag.startswith(ALARM_PREFIX_ADS):
                        try:
                            alarm_id = int(tag[len(ALARM_PREFIX_ADS):])
                        except ValueError:
                            continue  # tag desconhecida: fica sem valor
                        out[tag] = 1.0 if alarm_id in active else 0.0
            except Exception as exc:
                erros.append(exc)
                self._falha(_MSG_SYMBOL, exc)

        if tentativas and len(erros) == tentativas:
            # Nada foi lido: falha de conexao, o coletor reconecta.
            raise erros[-1]
        return {**extra, **{t: out.get(t) for t in tags}}

    async def read(self, tags: list[str]) -> dict[str, object]:
        return await asyncio.to_thread(self._read_all, tags)
