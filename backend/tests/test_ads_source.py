import asyncio
import struct

import pytest

from app.ads_source import ADS_ALARMS, AdsSource
from app.tags import alarm_label


class FakePlc:
    """PLC falso: devolve bytes montados como o TwinCAT 2 real."""

    def __init__(self, net_id=None, port=None, host=None):
        self.opened = 0
        self.closed = 0
        self.escalares = {".CYCLETIME_MQTT": 24.5, ".NPARTSCNT_MQTT": 120,
                          ".NGOODPARTS": 118, ".NBADPARTS": 2, ".BALARMACTIVE": True}
        temp = bytearray(112 * 8)
        struct.pack_into("<d", temp, 4 * 112, 195.0)       # zona 5, setpoint
        struct.pack_into("<d", temp, 4 * 112 + 8, 150.0)   # zona 5, standby
        msg = bytearray(96 * 100)
        for linha, aid in enumerate((318, 999)):
            struct.pack_into("<i", msg, linha * 96, aid)
            msg[linha * 96 + 56: linha * 96 + 64] = b"10:20:30"
        struct.pack_into("<i", msg, 2 * 96, 15)  # hora vazia: sem alarme
        self.blocos = {".AAAPFWTEMPPPARAMFROMHMI": bytes(temp), ".STHMIMESSAGE": bytes(msg)}

    def open(self):
        self.opened += 1

    def close(self):
        self.closed += 1

    def read_by_name(self, name, plctype):
        return self.escalares[name]

    def get_symbol(self, name):
        class Sym:
            index_group = 1
            index_offset = name
        return Sym()

    def read(self, index_group, index_offset, plctype):
        return list(self.blocos[index_offset])


def test_le_kpis_setpoints_e_alarmes():
    async def run():
        plc = FakePlc()
        src = AdsSource("1.2.3.4.1.1", "10.0.0.5", plc_factory=lambda *a: plc)
        await src.connect()
        vals = await src.read(["CycleMonitor", "ProductionCount", "TempZ5", "StandbyTempZ5",
                               "WarnAds318", "WarnAds15"])
        assert vals["CycleMonitor"] == 24.5
        assert vals["ProductionCount"] == 120
        assert vals["TempZ5"] == 195.0
        assert vals["StandbyTempZ5"] == 150.0
        assert vals["WarnAds318"] == 1.0
        assert vals["WarnAds15"] == 0.0
        assert "WarnAds999" in await src.list_tags()
        assert set(f"WarnAds{i}" for i in ADS_ALARMS) <= set(await src.list_tags())
        await src.close()
    asyncio.run(run())


def test_rotulo_alarme_ads():
    assert alarm_label("WarnAds318") == "Porta do Bico esta aberta"
    assert alarm_label("WarnAds999") == "Alarme Beckhoff 999"


def test_fecha_conexao():
    async def run():
        plc = FakePlc()
        src = AdsSource("1.2.3.4.1.1", "10.0.0.5", plc_factory=lambda *a: plc)
        await src.close()  # sem connect: não explode
        await src.connect()
        await src.close()
        assert plc.opened == 1 and plc.closed == 1
    asyncio.run(run())


def _src(plc):
    return AdsSource("1.2.3.4.1.1", "10.0.0.5", plc_factory=lambda *a: plc)


def test_tag_desconhecida_e_none_e_alarme_inativo_e_zero():
    async def run():
        src = _src(FakePlc())
        await src.connect()
        v = await src.read(["TagInventada", "WarnAdsXYZ", "TempZ9", "WarnAds419", "GoodProduction", "BadProduction"])
        assert v["TagInventada"] is None and v["WarnAdsXYZ"] is None and v["TempZ9"] is None
        assert v["WarnAds419"] == 0.0
        assert v["GoodProduction"] == 118 and v["BadProduction"] == 2
    asyncio.run(run())


def test_simbolo_escalar_ausente_so_afeta_aquela_tag():
    async def run():
        plc = FakePlc()
        del plc.escalares[".NGOODPARTS"]
        src = _src(plc)
        await src.connect()
        v = await src.read(["GoodProduction", "BadProduction", "TempZ5"])
        assert v["GoodProduction"] is None
        assert v["BadProduction"] == 2 and v["TempZ5"] == 195.0
    asyncio.run(run())


def test_bloco_ausente_da_none_sem_derrubar_o_resto():
    async def run():
        plc = FakePlc()
        del plc.blocos[".STHMIMESSAGE"]
        src = _src(plc)
        await src.connect()
        v = await src.read(["CycleMonitor", "WarnAds318"])
        assert v["CycleMonitor"] == 24.5 and v["WarnAds318"] is None
    asyncio.run(run())


def test_falha_total_levanta():
    async def run():
        class Morto(FakePlc):
            def read_by_name(self, *a):
                raise ConnectionError("sem rota")

            def get_symbol(self, *a):
                raise ConnectionError("sem rota")
        src = _src(Morto())
        await src.connect()
        with pytest.raises(ConnectionError):
            await src.read(["CycleMonitor", "TempZ1", "WarnAds318"])
    asyncio.run(run())


def test_read_sem_connect_levanta_erro_claro():
    async def run():
        with pytest.raises(RuntimeError, match="nao conectado"):
            await _src(FakePlc()).read(["CycleMonitor"])
    asyncio.run(run())


def test_connect_duas_vezes_fecha_a_anterior():
    async def run():
        plcs = [FakePlc(), FakePlc()]
        it = iter(plcs)
        src = AdsSource("1.2.3.4.1.1", "10.0.0.5", plc_factory=lambda *a: next(it))
        await src.connect()
        await src.connect()
        assert plcs[0].closed == 1 and plcs[1].closed == 0
    asyncio.run(run())


def test_alarme_desconhecido_chega_ao_coletor(tmp_path):
    from app.collector import Collector
    from app.storage import Storage

    async def run():
        plc = FakePlc()
        src = _src(plc)
        storage = Storage(str(tmp_path / "h.db"))
        c = Collector(src, storage, poll_seconds=0.05, full_poll_seconds=0)
        try:
            await src.connect()
            c.tags = await src.list_tags()  # antes da 1ª leitura: 999 ainda não é conhecido
            assert "WarnAds999" not in c.tags
            await c.poll_once()
            assert "WarnAds999" in c.active_alarms and "WarnAds318" in c.active_alarms
            assert alarm_label("WarnAds999") == "Alarme Beckhoff 999"
            # o 999 some do painel: o coletor fecha o alarme mesmo sem ele estar em c.tags
            msg = bytearray(plc.blocos[".STHMIMESSAGE"])
            msg[96 + 56: 96 + 64] = bytes(8)
            plc.blocos[".STHMIMESSAGE"] = bytes(msg)
            c.full_poll_seconds = 3600
            await c.poll_once()
            assert "WarnAds999" not in c.active_alarms and "WarnAds318" in c.active_alarms
            assert any(e["tag"] == "WarnAds999" and e["ended"] for e in storage.alarm_events(0))
        finally:
            await src.close()
            storage.close()
    asyncio.run(run())


# --- mapa da Chen Hsong (tabela Xmold2 do gateway CHL3200) ---

class PlcMapa(FakePlc):
    """PLC com os símbolos do mapa Xmold2 e leitura em lote (sum read)."""

    def __init__(self, *a, lote=True):
        super().__init__(*a)
        self.lote = lote
        self.lotes = 0
        self.unitarias = 0
        for z in range(1, 9):
            self.escalares[f".AAAPFWTEMPTOHMI[{z}].ACTUALTEMP"] = 180.0 + z
        self.escalares.update({
            ".STAXIS_HMIITF[5].STLATCHEDVALUES.DOSINGTIME": 4.2,
            ".STAXIS_HMIITF[5].LATCHEDTIMER[1]": 12.0,
            ".STUSBLED.FULLAUTOMATIC": False,
            ".STUSBLED.SEMIAUTOMATIC": True,
            ".STUSBLED.MANUALON": False,
            ".STUSBLED.CALIBRATIONON": False,
        })

    def read_by_name(self, name, plctype):
        self.unitarias += 1
        return self.escalares[name]

    def read_list_by_name(self, names):
        if not self.lote:
            raise RuntimeError("ADSERR 1793: servico nao suportado")
        self.lotes += 1
        faltando = [n for n in names if n not in self.escalares]
        if faltando:  # pyads: símbolo inexistente derruba a consulta de informações
            raise RuntimeError(f"symbol not found: {faltando[0]}")
        return {n: self.escalares[n] for n in names}


def test_temperatura_real_modo_e_fases_em_um_lote():
    async def run():
        plc = PlcMapa()
        src = _src(plc)
        await src.connect()
        v = await src.read(["TempZ1Now", "TempZ8Now", "TempZ5", "OperatorMode",
                            "PlastLastTime", "ColdLastTime", "CycleMonitor"])
        assert v["TempZ1Now"] == 181.0 and v["TempZ8Now"] == 188.0
        assert v["TempZ5"] == 195.0          # setpoint continua vindo do bloco antigo
        assert v["OperatorMode"] == 2.0      # semiautomático
        assert v["PlastLastTime"] == 4.2 and v["ColdLastTime"] == 12.0
        assert v["CycleMonitor"] == 24.5
        assert plc.lotes == 1 and plc.unitarias == 0  # uma ida e volta só
        assert {"TempZ1Now", "OperatorMode", "PlastLastTime"} <= set(await src.list_tags())
    asyncio.run(run())


def test_modo_sem_lampada_acesa_e_zero():
    async def run():
        plc = PlcMapa()
        plc.escalares[".STUSBLED.SEMIAUTOMATIC"] = False
        src = _src(plc)
        await src.connect()
        assert (await src.read(["OperatorMode"]))["OperatorMode"] == 0.0
    asyncio.run(run())


def test_plc_sem_leitura_em_lote_le_um_por_um_e_para_de_tentar_o_lote():
    async def run():
        plc = PlcMapa(lote=False)
        src = _src(plc)
        await src.connect()
        tags = ["TempZ1Now", "TempZ2Now", "CycleMonitor"]
        v = await src.read(tags)
        assert v["TempZ1Now"] == 181.0 and v["CycleMonitor"] == 24.5
        await src.read(tags)
        assert src._sum_ok is False
        assert plc.unitarias == 6  # 3 + 3, sem insistir no lote na segunda leitura
    asyncio.run(run())


def test_simbolo_que_o_painel_nao_tem_vira_none_e_o_lote_volta_sem_ele():
    async def run():
        plc = PlcMapa()
        del plc.escalares[".AAAPFWTEMPTOHMI[7].ACTUALTEMP"]
        src = _src(plc)
        await src.connect()
        tags = ["TempZ6Now", "TempZ7Now", "CycleMonitor"]
        v = await src.read(tags)
        assert v["TempZ7Now"] is None and v["TempZ6Now"] == 186.0 and v["CycleMonitor"] == 24.5
        plc.unitarias = 0
        v = await src.read(tags)
        assert v["TempZ7Now"] is None and v["TempZ6Now"] == 186.0
        assert plc.unitarias == 0 and src._sum_ok is True  # segunda leitura: lote sem o símbolo ausente
    asyncio.run(run())


def test_erro_por_item_no_lote_vira_none():
    async def run():
        class Parcial(PlcMapa):
            def read_list_by_name(self, names):
                r = super().read_list_by_name(names)
                r[".STAXIS_HMIITF[5].LATCHEDTIMER[1]"] = "ADSERR_DEVICE_SYMBOLNOTFOUND"
                return r
        src = _src(Parcial())
        await src.connect()
        v = await src.read(["ColdLastTime", "PlastLastTime"])
        assert v["ColdLastTime"] is None and v["PlastLastTime"] == 4.2
    asyncio.run(run())
