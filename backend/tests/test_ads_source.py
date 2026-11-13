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
