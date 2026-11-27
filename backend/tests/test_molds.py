"""Moldes: cadastro, troca de molde com histórico e peças = ciclos x cavidades."""
import time

import pytest

from app.collector import Collector
from app.machines import Machine, MachineStore
from app.molds import Mold
from app.storage import Storage


@pytest.fixture
def store(tmp_path):
    s = MachineStore(str(tmp_path / "m.db"))
    s.add(Machine(None, "Inovance", "inovance", "192.168.10.118"))
    s.add(Machine(None, "JM208", "em63", "10.86.20.253"))
    yield s
    s.close()


def test_cadastro_de_molde_valida(store):
    m = store.add_mold(Mold(None, " 9000001 ", "Copinho", 16))
    assert (m.code, m.cavities, m.active_cavities) == ("9000001", 16, 16)  # ativas = todas
    with pytest.raises(ValueError):
        store.add_mold(Mold(None, "9000001", "repetido", 2))
    with pytest.raises(ValueError):
        store.add_mold(Mold(None, "X", "", 4, active_cavities=5))
    with pytest.raises(ValueError):
        store.add_mold(Mold(None, "Y", "", 0))
    with pytest.raises(ValueError):
        store.add_mold(Mold(None, "  ", "", 2))


def test_mudar_cavidades_mantem_todas_ativas(store):
    m = store.add_mold(Mold(None, "A", "", 8))
    assert store.update_mold(m.id, {"cavities": 16}).active_cavities == 16
    m2 = store.add_mold(Mold(None, "B", "", 8, active_cavities=7))
    assert store.update_mold(m2.id, {"cavities": 16}).active_cavities == 7  # tampada continua


def test_troca_de_molde_registra_historico(store):
    a = store.add_mold(Mold(None, "A", "Molde A", 2))
    b = store.add_mold(Mold(None, "B", "Molde B", 16))
    assert store.active_cavities(1) == 1  # sem molde: 1 peça por ciclo
    store.mount_mold(1, a.id, now=100)
    store.mount_mold(1, a.id, now=150)  # mesmo molde: não cria registro
    store.mount_mold(1, b.id, now=200)
    assert store.active_cavities(1) == 16
    hist = store.mold_history(1)
    assert [(h["mold_code"], h["mounted_at"], h["removed_at"]) for h in hist] == [
        ("B", 200, None), ("A", 100, 200)]
    store.mount_mold(1, None, now=300)  # retira o molde
    assert store.mounted_mold(1) is None and store.active_cavities(1) == 1
    assert store.mold_history(1)[0]["removed_at"] == 300


def test_molde_nao_pode_estar_em_duas_maquinas_nem_ser_apagado_montado(store):
    a = store.add_mold(Mold(None, "A", "", 2))
    store.mount_mold(1, a.id)
    with pytest.raises(ValueError):
        store.mount_mold(2, a.id)
    with pytest.raises(ValueError):
        store.delete_mold(a.id)
    store.mount_mold(1, None)
    store.delete_mold(a.id)
    assert store.get_mold(a.id) is None


def test_tampar_cavidade_do_molde_montado_abre_novo_registro(store):
    a = store.add_mold(Mold(None, "A", "", 16))
    store.mount_mold(1, a.id, now=100)
    store.update_mold(a.id, {"active_cavities": 15}, now=500)
    assert store.active_cavities(1) == 15
    hist = store.mold_history(1)
    assert [(h["active_cavities"], h["mounted_at"], h["removed_at"]) for h in hist] == [
        (15, 500, None), (16, 100, 500)]


def test_historico_continua_depois_de_editar_o_molde(store):
    a = store.add_mold(Mold(None, "A", "Antigo", 4))
    store.mount_mold(1, a.id, now=100)
    store.mount_mold(1, None, now=200)
    store.update_mold(a.id, {"name": "Novo"})
    assert store.mold_history(1)[0]["mold_name"] == "Antigo"  # foto de quando montou


class _Src:
    pass


def _collector(tmp_path, cav):
    storage = Storage(str(tmp_path / "h.db"))
    return Collector(_Src(), storage, cavities=lambda: cav["n"]), storage


def test_pecas_somam_ciclos_vezes_cavidades_do_momento(tmp_path):
    cav = {"n": 2}
    c, storage = _collector(tmp_path, cav)
    t = time.time()
    c._apply(t, {"ProductionCount": 1000.0})  # primeira leitura: não soma nada
    c._apply(t + 1, {"ProductionCount": 1010.0})  # +10 ciclos x 2
    cav["n"] = 16  # troca de molde
    c._apply(t + 2, {"ProductionCount": 1015.0})  # +5 ciclos x 16
    assert c.piece_count == 10 * 2 + 5 * 16
    assert c.values["PieceCount"] == 100
    c._apply(t + 3, {"ProductionCount": 0.0})  # contador zerado: não soma
    c._apply(t + 4, {"ProductionCount": 50000.0})  # salto falso vindo de 0: não soma
    c._apply(t + 5, {"ProductionCount": 50001.0})
    assert c.piece_count == 100 + 16
    per = storage.increments("PieceCount", t - 10, 3600, raw=True)
    assert sum(v for _, v in per) == 116


def test_total_de_pecas_continua_depois_de_reiniciar(tmp_path):
    cav = {"n": 4}
    c, storage = _collector(tmp_path, cav)
    t = time.time()
    c._apply(t, {"ProductionCount": 10.0})
    c._apply(t + 1, {"ProductionCount": 12.0})
    assert c.piece_count == 8
    c2 = Collector(_Src(), storage, cavities=lambda: 4)
    assert c2.piece_count == 8  # não volta a zero (nem dá salto) ao reconectar


def test_api_de_moldes(tmp_path):
    from types import SimpleNamespace

    from fastapi.testclient import TestClient

    from app import main
    from app.manager import MachineManager

    store = MachineStore(str(tmp_path / "m.db"))
    store.add(Machine(None, "Demo", "demo", "127.0.0.1"))
    cfg = SimpleNamespace(
        machines_dir=str(tmp_path / "machines"), poll_seconds=0.05, full_poll_seconds=0.2,
        retention_days=7, vnc_tcp_port=0, vnc_tcp_bind="127.0.0.1", vnc_control_timeout=300,
        vnc_audit_path=None, ads_local_net_id="",
    )
    main.app.state.manager = MachineManager(store, cfg)
    try:
        with TestClient(main.app) as client:
            r = client.post("/api/molds", json={"code": "9000001", "name": "Copinho", "cavities": 16})
            assert r.status_code == 201, r.text
            mold_id = r.json()["id"]
            assert client.post("/api/molds", json={"code": "9000001"}).status_code == 422
            r = client.put("/api/machines/1/mold", json={"mold_id": mold_id})
            assert r.status_code == 200 and r.json()["current"]["active_cavities"] == 16
            assert client.delete(f"/api/molds/{mold_id}").status_code == 409
            lst = client.get("/api/molds").json()["molds"]
            assert lst[0]["mounted_on"]["machine_id"] == 1
            assert client.get("/api/machines").json()["machines"][0]["mold"]["mold_code"] == "9000001"
            r = client.put("/api/machines/1/mold", json={"mold_id": None})
            assert r.json()["current"] is None and len(r.json()["history"]) == 1
            assert client.delete(f"/api/molds/{mold_id}").status_code == 204
            assert client.put("/api/machines/99/mold", json={"mold_id": None}).status_code == 404
    finally:
        del main.app.state.manager


def test_producao_por_hora_usa_pecas_e_cai_nos_ciclos_no_historico_antigo(tmp_path):
    from app.main import _pieces_per_bucket

    storage = Storage(str(tmp_path / "p.db"))
    # hora 1: só o contador da máquina (antes dos moldes); hora 2: com PieceCount (x16)
    storage.add_samples(3600 * 1 + 1, {"ProductionCount": 100.0})
    storage.add_samples(3600 * 1 + 9, {"ProductionCount": 110.0})
    storage.add_samples(3600 * 2 + 1, {"ProductionCount": 110.0, "PieceCount": 0.0})
    storage.add_samples(3600 * 2 + 9, {"ProductionCount": 115.0, "PieceCount": 80.0})
    per = dict((b, v) for b, v in _pieces_per_bucket(storage, 0, 3600))
    assert per[3600] == 10 and per[7200] == 80
