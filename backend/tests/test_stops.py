"""Paradas: detecção automática, tempo mínimo, sugestão de motivo e motivos padrão."""
from types import SimpleNamespace

import pytest

from app.machines import Machine, MachineStore
from app.stops import DEFAULT_REASONS, StopTracker
from app.storage import Storage


@pytest.fixture
def storage(tmp_path):
    s = Storage(str(tmp_path / "h.db"))
    yield s
    s.close()


def test_micro_parada_nao_conta_e_parada_comeca_no_ultimo_ciclo(storage):
    t = StopTracker(storage, lambda: 120)
    t.on_read(0, True, False, [])
    t.on_read(60, False, False, [])
    t.on_read(100, True, False, [])  # parou 100 s: micro-parada, não registra
    assert storage.stops(0) == []
    t.on_read(200, False, False, [])
    t.on_read(230, False, False, [])  # 130 s sem ciclo: abre a parada desde o último ciclo
    [s] = storage.stops(0)
    assert s["started"] == 100 and s["ended"] is None and s["reason"] is None
    t.on_read(500, True, False, [])  # voltou a produzir: fecha
    [s] = storage.stops(0)
    assert (s["started"], s["ended"]) == (100, 500)


def test_sugere_motivo_por_alarme_ou_maquina_desligada(storage):
    t = StopTracker(storage, lambda: 60)
    t.on_read(0, True, False, [])
    t.on_read(70, False, False, ["Emergência acionada"])
    s = storage.open_stop()
    assert s["reason"] == "falha" and s["auto"] and "Emergência" in s["detail"]
    t.on_read(80, True, False, [])
    t.on_read(200, False, True, [])
    assert storage.open_stop()["reason"] == "desligada"


def test_sugestao_tardia_nao_apaga_motivo_escolhido(storage):
    t = StopTracker(storage, lambda: 60)
    t.on_read(0, True, False, [])
    t.on_read(70, False, False, [])
    sid = storage.open_stop()["id"]
    storage.classify_stop(sid, "troca_molde", "trocando o molde do copinho")
    t.on_read(80, False, False, ["Alarme qualquer"])
    s = storage.open_stop()
    assert s["reason"] == "troca_molde" and not s["auto"] and s["note"].startswith("trocando")


def test_sem_comunicacao_vira_parada_propria(storage):
    t = StopTracker(storage, lambda: 60)
    t.on_read(0, True, False, [])
    t.on_read(100, False, False, [])  # parada aberta desde 0
    t.on_disconnect(last_ok=100)  # caiu: a parada fecha na última leitura boa
    t.on_reconnect(lost_at=100, now=1000)
    rows = sorted(storage.stops(0), key=lambda r: r["started"])
    assert [(r["started"], r["ended"], r["reason"]) for r in rows] == [(0, 100, None), (100, 1000, "sem_comunicacao")]
    t.on_reconnect(lost_at=1000, now=1030)  # queda curta: não registra
    assert len(storage.stops(0)) == 2


def test_parada_aberta_quando_o_monitor_reinicia_fecha_na_ultima_leitura(storage):
    storage.add_samples(50, {"ProductionCount": 1.0})
    sid = storage.start_stop(10)
    StopTracker(storage, lambda: 60)
    s = storage.stops(0)[0]
    assert s["id"] == sid and s["ended"] == 50


def test_motivos_padrao_e_cadastro(tmp_path):
    store = MachineStore(str(tmp_path / "m.db"))
    codes = [r["code"] for r in store.list_reasons()]
    assert codes == [c for c, _ in DEFAULT_REASONS]
    store.seed_reasons()  # rodar de novo não duplica nem desfaz mudanças
    store.update_reason("intervalo", label="Almoço")
    store.seed_reasons()
    assert store.reason_labels()["intervalo"] == "Almoço"
    r = store.add_reason("Falta de energia")
    assert r["code"] == "falta_de_energia" and store.list_reasons()[-1]["code"] == "outro"
    with pytest.raises(ValueError):
        store.add_reason("falta de energia")
    with pytest.raises(ValueError):
        store.update_reason("outro", active=False)
    store.update_reason("qualidade", active=False)
    assert "qualidade" not in [r["code"] for r in store.list_reasons(only_active=True)]


def test_tempo_minimo_no_cadastro_e_banco_antigo(tmp_path):
    import sqlite3
    path = str(tmp_path / "old.db")
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE machines (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, kind TEXT NOT NULL,"
                " host TEXT NOT NULL, client TEXT NOT NULL DEFAULT 'Chen Hsong', opcua_port INTEGER NOT NULL DEFAULT 4840,"
                " namespace INTEGER, ams_net_id TEXT NOT NULL DEFAULT '', ads_port INTEGER NOT NULL DEFAULT 801,"
                " vnc_enabled INTEGER, vnc_port INTEGER NOT NULL DEFAULT 5900, vnc_view_password TEXT NOT NULL DEFAULT '',"
                " vnc_control_password TEXT NOT NULL DEFAULT '', notes TEXT NOT NULL DEFAULT '')")
    con.execute("INSERT INTO machines(name, kind, host) VALUES ('Velha', 'demo', 'x')")
    con.commit(); con.close()
    store = MachineStore(path)  # banco de antes das paradas: ganha a coluna com 2 min
    assert store.get(1).stop_min_seconds == 120
    assert store.update(1, {"stop_min_seconds": 300}).stop_min_seconds == 300
    with pytest.raises(ValueError):
        store.update(1, {"stop_min_seconds": 5})


def test_api_de_paradas(tmp_path):
    from fastapi.testclient import TestClient

    from app import main
    from app.manager import MachineManager

    store = MachineStore(str(tmp_path / "m.db"))
    mid = store.add(Machine(None, "Demo", "demo", "127.0.0.1")).id
    store.set_active(mid)
    cfg = SimpleNamespace(
        machines_dir=str(tmp_path / "machines"), poll_seconds=0.05, full_poll_seconds=0.2,
        retention_days=7, vnc_tcp_port=0, vnc_tcp_bind="127.0.0.1", vnc_control_timeout=300,
        vnc_audit_path=None, ads_local_net_id="",
    )
    main.app.state.manager = MachineManager(store, cfg)
    try:
        with TestClient(main.app) as client:
            st = main.app.state.manager.collector.storage
            import time
            now = time.time()
            a = st.start_stop(now - 600, ended=now - 300)
            b = st.start_stop(now - 200, "falha", "Emergência acionada")
            r = client.get("/api/stops?hours=1").json()
            assert r["unclassified"] == 1 and len(r["stops"]) == 2
            # só escreveu o que aconteceu: vira "Outro"
            r = client.put(f"/api/stops/{a}", json={"note": "  esperando o eletricista "})
            assert r.json()["reason"] == "outro" and r.json()["note"] == "esperando o eletricista"
            assert client.put(f"/api/stops/{b}", json={"reason": "nao_existe"}).status_code == 422
            assert client.put("/api/stops/9999", json={"reason": "falha"}).status_code == 404
            s = client.get("/api/stops?hours=1").json()
            assert s["unclassified"] == 0 and {x["label"] for x in s["summary"]} == {"Outro", "Falha / alarme"}
            assert client.post("/api/stop-reasons", json={"label": "Falta de energia"}).status_code == 201
            assert client.put("/api/stop-reasons/outro", json={"active": False}).status_code == 422
            assert len(client.get("/api/stop-reasons").json()["reasons"]) == len(DEFAULT_REASONS) + 1
    finally:
        del main.app.state.manager
