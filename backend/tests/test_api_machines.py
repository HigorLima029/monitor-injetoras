from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app import main
from app.machines import Machine, MachineStore
from app.manager import MachineManager


def _settings(tmp_path):
    return SimpleNamespace(
        machines_dir=str(tmp_path / "machines"), poll_seconds=0.05, full_poll_seconds=0.2,
        retention_days=7, vnc_tcp_port=0, vnc_tcp_bind="127.0.0.1", vnc_control_timeout=300,
        vnc_audit_path=None, ads_local_net_id="",
    )


@pytest.fixture
def api(tmp_path):
    store = MachineStore(str(tmp_path / "m.db"))
    a = store.add(Machine(None, "A", "demo", "127.0.0.1")).id
    b = store.add(Machine(None, "B", "demo", "127.0.0.2")).id
    store.set_active(a)
    main.app.state.manager = MachineManager(store, _settings(tmp_path))
    try:
        with TestClient(main.app) as client:
            yield client, store, a, b
    finally:
        del main.app.state.manager


def test_crud_pela_api(api):
    client, store, a, b = api
    body = {"name": "B&R", "kind": "em63", "host": "10.0.0.9",
            "vnc_view_password": "segredo-view", "vnc_control_password": "segredo-ctl"}
    r = client.post("/api/machines", json=body)
    assert r.status_code == 201
    new = r.json()
    assert new["has_view_password"] is True and new["has_control_password"] is True
    assert "vnc_view_password" not in new
    listing = client.get("/api/machines")
    assert "segredo" not in listing.text
    client.post(f"/api/machines/{new['id']}/activate")  # ativa a máquina COM senha
    info = client.get("/api/info")
    assert info.json()["machine_id"] == new["id"] and info.json()["vnc_control"] is True
    assert "segredo" not in info.text
    client.post(f"/api/machines/{a}/activate")
    data = listing.json()
    assert data["active_id"] == a and len(data["machines"]) == 3
    assert all("status" in m for m in data["machines"])
    r = client.put(f"/api/machines/{new['id']}", json={**body, "name": "B&R 2", "vnc_view_password": ""})
    assert r.status_code == 200 and r.json()["name"] == "B&R 2"
    assert store.get(new["id"]).vnc_view_password == "segredo-view"
    assert client.put("/api/machines/999", json=body).status_code == 404
    assert client.post("/api/machines", json={**body, "kind": "plc"}).status_code == 422
    assert client.post("/api/machines", json={"name": "x"}).status_code == 422


def test_put_na_ativa_reativa(api):
    client, store, a, b = api
    r = client.put(f"/api/machines/{a}", json={"name": "A novo", "kind": "demo", "host": "127.0.0.1"})
    assert r.status_code == 200
    assert client.get("/api/info").json()["machine_name"] == "A novo"


def test_nao_apaga_ativa(api):
    client, store, a, b = api
    r = client.delete(f"/api/machines/{a}")
    assert r.status_code == 409
    assert r.json()["detail"] == "Não dá para apagar a máquina aberta. Abra outra antes."
    assert client.delete(f"/api/machines/{b}").status_code == 204
    assert store.get(b) is None


def test_trocar_pela_api(api):
    client, store, a, b = api
    assert client.get("/api/info").json()["machine_id"] == a
    r = client.post(f"/api/machines/{b}/activate")
    assert r.status_code == 200 and r.json()["id"] == b
    info = client.get("/api/info").json()
    assert info["machine_id"] == b and info["kind"] == "demo" and info["client"] == "Chen Hsong"
    assert client.get("/api/machines").json()["active_id"] == b
    assert client.post("/api/machines/999/activate").status_code == 404


def test_test_endpoint(api):
    client, store, a, b = api
    r = client.post(f"/api/machines/{a}/test")
    assert r.status_code == 200 and set(r.json()) == {"online", "latency_ms"}
    assert client.post("/api/machines/999/test").status_code == 404


def test_test_guarda_a_sonda(api):
    client, store, a, b = api
    main.app.state.manager._probes.clear()
    antes = {m["id"]: m["status"] for m in client.get("/api/machines").json()["machines"]}
    assert antes[b]["checked"] is None and antes[b]["online"] is None
    r = client.post(f"/api/machines/{b}/test").json()
    depois = {m["id"]: m["status"] for m in client.get("/api/machines").json()["machines"]}
    assert depois[b]["checked"] is not None
    assert depois[b]["online"] is r["online"]
    assert depois[b]["latency_ms"] == r["latency_ms"]


def test_ws_fecha_na_troca(api):
    client, store, a, b = api
    with client.websocket_connect("/ws") as ws:
        assert "status" in ws.receive_json()
        client.post(f"/api/machines/{b}/activate")
        for _ in range(50):
            msg = ws.receive_json()
            if msg.get("type") == "machine_changed":
                break
        assert msg["machine_id"] == b
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()


def test_sem_maquina_ativa_503(tmp_path):
    store = MachineStore(str(tmp_path / "m.db"))
    main.app.state.manager = MachineManager(store, _settings(tmp_path))
    try:
        with TestClient(main.app) as client:
            r = client.get("/api/live")
            assert r.status_code == 503
            assert r.json()["detail"] == "Nenhuma máquina aberta. Cadastre uma em Máquinas."
            assert client.get("/api/machines").json() == {"active_id": None, "machines": []}
            assert client.get("/api/info").status_code == 503
    finally:
        del main.app.state.manager


def test_put_parcial_preserva_campos(api):
    client, store, a, b = api
    m = store.add(Machine(None, "C", "em63", "10.0.0.5", vnc_port=5901, namespace=6,
                          vnc_enabled=True, notes="linha 3"))
    r = client.put(f"/api/machines/{m.id}", json={"name": "Nova"})
    assert r.status_code == 200 and r.json()["name"] == "Nova"
    got = store.get(m.id)
    assert (got.vnc_port, got.namespace, got.vnc_enabled, got.notes, got.host, got.kind) == \
        (5901, 6, True, "linha 3", "10.0.0.5", "em63")
    r = client.put(f"/api/machines/{m.id}", json={"namespace": None})
    assert store.get(m.id).namespace is None


def test_422_nao_ecoa_senhas(api):
    client, *_ = api
    r = client.post("/api/machines", json={"name": "X", "host": "h", "vnc_view_password": "SEGREDO"})
    assert r.status_code == 422 and "SEGREDO" not in r.text
    assert r.json()["detail"][0]["loc"][-1] == "kind"
    r = client.post("/api/machines", json={"name": "X", "kind": "plc", "host": "h",
                                           "vnc_view_password": "SEGREDO", "opcua_port": "abc"})
    assert r.status_code == 422 and "SEGREDO" not in r.text


def test_put_troca_ip_apaga_senhas_e_remove_explicito(api):
    client, store, a, b = api
    m = store.add(Machine(None, "C", "em63", "10.0.0.5", vnc_view_password="sv", vnc_control_password="sc"))
    r = client.put(f"/api/machines/{m.id}", json={"clear_vnc_control_password": True})
    assert r.status_code == 200 and r.json()["has_control_password"] is False
    assert r.json()["has_view_password"] is True
    r = client.put(f"/api/machines/{m.id}", json={"host": "10.6.6.6"})
    assert r.status_code == 200 and r.json()["has_view_password"] is False
    assert store.get(m.id).vnc_view_password == ""
    r = client.put(f"/api/machines/{m.id}", json={"vnc_view_password": "SEGREDO9", "clear_vnc_view_password": True})
    assert r.status_code == 422 and "SEGREDO9" not in r.text


def test_put_none_em_obrigatorio_e_422(api):
    client, store, a, b = api
    for campo in ("name", "client", "notes", "ams_net_id"):
        assert client.put(f"/api/machines/{b}", json={campo: None}).status_code == 422


def test_put_na_ativa_sem_mudar_conexao_nao_reativa(api):
    client, store, a, b = api
    mgr = main.app.state.manager
    antes = mgr.collector
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        r = client.put(f"/api/machines/{a}", json={"name": "A renomeada", "notes": "x", "client": "Outro"})
        assert r.status_code == 200
        assert mgr.collector is antes
        info = client.get("/api/info").json()
        assert info["machine_name"] == "A renomeada" and info["client"] == "Outro"
        r = client.put(f"/api/machines/{a}", json={"host": "127.0.0.9"})
        assert mgr.collector is not antes
        for _ in range(50):
            msg = ws.receive_json()
            if msg.get("type") == "machine_changed":
                break
        assert msg["machine_id"] == a


def test_beckhoff_mostra_8_zonas_sem_bico(tmp_path):
    from app.sources import SimulatedSource
    store = MachineStore(str(tmp_path / "m.db"))
    m = store.add(Machine(None, "Beckhoff", "ads", "127.0.0.1", ams_net_id="5.79.97.240.1.1")).id
    store.set_active(m)
    # fonte simulada no lugar do pyads: o teste olha so o formato das zonas
    main.app.state.manager = MachineManager(store, _settings(tmp_path),
                                            source_factory=lambda _m: SimulatedSource())
    try:
        with TestClient(main.app) as client:
            zones = client.get("/api/info").json()["zones"]
    finally:
        del main.app.state.manager
    assert [z["label"] for z in zones] == [f"Zona {i}" for i in range(1, 9)]
    assert zones[7]["setpoint"] == "TempZ8"
