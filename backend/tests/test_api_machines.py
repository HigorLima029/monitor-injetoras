import time
from types import SimpleNamespace

import pytest
from starlette.websockets import WebSocketDisconnect

from app import main
from app.machines import Machine, MachineStore
from app.manager import Fleet


def _settings(tmp_path):
    return SimpleNamespace(
        machines_dir=str(tmp_path / "machines"), poll_seconds=0.05, idle_poll_seconds=0.05,
        full_poll_seconds=0.2,
        retention_days=7, vnc_control_timeout=300,
        vnc_audit_path=None, ads_local_net_id="",
    )


@pytest.fixture
def api(tmp_path, client_as):
    store = MachineStore(str(tmp_path / "m.db"))
    a = store.add(Machine(None, "A", "demo", "127.0.0.1")).id
    b = store.add(Machine(None, "B", "demo", "127.0.0.2")).id
    main.app.state.fleet = Fleet(store, _settings(tmp_path))
    try:
        yield client_as("mae"), store, a, b
    finally:
        del main.app.state.fleet


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
    assert main.app.state.fleet.runtime(new["id"]) is not None  # já coletando
    info = client.get(f"/api/machines/{new['id']}/info")
    assert info.json()["machine_id"] == new["id"] and info.json()["vnc_control"] is True
    assert "segredo" not in info.text
    data = listing.json()
    assert set(data) == {"machines"} and len(data["machines"]) == 3
    assert all("status" in m and "client_name" in m for m in data["machines"])
    r = client.put(f"/api/machines/{new['id']}", json={**body, "name": "B&R 2", "vnc_view_password": ""})
    assert r.status_code == 200 and r.json()["name"] == "B&R 2"
    assert store.get(new["id"]).vnc_view_password == "segredo-view"
    assert client.put("/api/machines/999", json=body).status_code == 404
    assert client.post("/api/machines", json={**body, "kind": "plc"}).status_code == 422
    assert client.post("/api/machines", json={"name": "x"}).status_code == 422


def test_put_atualiza_info(api):
    client, store, a, b = api
    r = client.put(f"/api/machines/{a}", json={"name": "A novo", "kind": "demo", "host": "127.0.0.1"})
    assert r.status_code == 200
    assert client.get(f"/api/machines/{a}/info").json()["machine_name"] == "A novo"


def test_apagar_fecha_a_maquina(api):
    client, store, a, b = api
    fleet = main.app.state.fleet
    rt = fleet.runtime(b)
    assert client.delete(f"/api/machines/{b}").status_code == 204
    assert store.get(b) is None and fleet.runtime(b) is None and rt.closed
    assert client.get(f"/api/machines/{b}/live").status_code == 404
    assert client.delete(f"/api/machines/{b}").status_code == 404
    assert client.get(f"/api/machines/{a}/live").status_code == 200


def test_rotas_por_maquina(api):
    client, store, a, b = api
    for mid in (a, b):
        info = client.get(f"/api/machines/{mid}/info").json()
        assert info["machine_id"] == mid and info["kind"] == "demo" and "client_id" in info
        assert info["machine"]["id"] == mid
    r = client.get("/api/machines/999/info")
    assert r.status_code == 404 and r.json()["detail"] == "Máquina não encontrada"


def test_rotas_antigas_removidas(api):
    client, store, a, b = api
    for path in ("/api/info", "/api/live", "/api/history?tags=x", "/api/production", "/api/alarms",
                 "/api/values?tags=x", "/api/tags"):
        assert client.get(path).status_code == 404, path
    assert client.post(f"/api/machines/{a}/activate").status_code in (404, 405)


def test_test_endpoint(api):
    client, store, a, b = api
    r = client.post(f"/api/machines/{a}/test")
    assert r.status_code == 200 and set(r.json()) == {"online", "latency_ms"}
    assert client.post("/api/machines/999/test").status_code == 404


def test_test_guarda_a_sonda(api):
    client, store, a, b = api
    main.app.state.fleet._probes.clear()
    antes = {m["id"]: m["status"] for m in client.get("/api/machines").json()["machines"]}
    assert antes[b]["checked"] is None and antes[b]["latency_ms"] is None
    r = client.post(f"/api/machines/{b}/test").json()
    depois = {m["id"]: m["status"] for m in client.get("/api/machines").json()["machines"]}
    assert depois[b]["checked"] is not None
    assert depois[b]["latency_ms"] == r["latency_ms"]


def test_ws_segue_no_coletor_novo_e_fecha_ao_apagar(api):
    client, store, a, b = api
    fleet = main.app.state.fleet
    rt = fleet.runtime(a)
    with client.websocket_connect(f"/ws/machines/{a}") as ws:
        assert "status" in ws.receive_json()
        assert rt.viewers == 1
        old = rt.collector
        assert client.put(f"/api/machines/{a}", json={"host": "127.0.0.9"}).status_code == 200
        assert rt.collector is not old
        for _ in range(50):
            msg = ws.receive_json()
            assert "_fleet" not in msg and "status" in msg
            if rt.collector.reads and msg["status"]["last_ok"]:
                break
        assert client.delete(f"/api/machines/{a}").status_code == 204
        with pytest.raises(WebSocketDisconnect) as exc:
            for _ in range(50):
                ws.receive_json()
        assert exc.value.code == 4404 and exc.value.reason == "Máquina não encontrada"
    assert rt.viewers == 0


def test_ws_de_maquina_inexistente_fecha(api):
    client, *_ = api
    with client.websocket_connect("/ws/machines/999") as ws:
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
    assert exc.value.code == 4404


def test_ws_acelera_a_leitura(api):
    client, store, a, b = api
    fleet = main.app.state.fleet
    rt = fleet.runtime(a)
    fleet.settings.idle_poll_seconds = 10
    rt.tick()
    assert rt.collector.poll_seconds == 10
    with client.websocket_connect(f"/ws/machines/{a}") as ws:
        ws.receive_json()
        assert rt.collector.poll_seconds == 0.05
    for _ in range(100):
        if rt.viewers == 0:
            break
        time.sleep(0.01)
    assert rt.viewers == 0


def test_sem_maquinas(tmp_path, client_as):
    store = MachineStore(str(tmp_path / "m.db"))
    main.app.state.fleet = Fleet(store, _settings(tmp_path))
    try:
        client = client_as("mae")
        assert client.get("/api/machines").json() == {"machines": []}
        assert client.get("/api/machines/1/live").status_code == 404
    finally:
        del main.app.state.fleet


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
    for campo in ("name", "notes", "ams_net_id"):
        assert client.put(f"/api/machines/{b}", json={campo: None}).status_code == 422


def test_put_sem_mudar_conexao_nao_reabre(api):
    client, store, a, b = api
    rt = main.app.state.fleet.runtime(a)
    antes, outra = rt.collector, main.app.state.fleet.runtime(b).collector
    r = client.put(f"/api/machines/{a}", json={"name": "A renomeada", "notes": "x", "client_id": 5})
    assert r.status_code == 200
    assert rt.collector is antes
    info = client.get(f"/api/machines/{a}/info").json()
    assert info["machine_name"] == "A renomeada" and info["client_id"] == 5
    client.put(f"/api/machines/{a}", json={"host": "127.0.0.9"})
    assert rt.collector is not antes
    assert main.app.state.fleet.runtime(b).collector is outra  # a outra máquina nem percebeu


def test_beckhoff_mostra_8_zonas_sem_bico(tmp_path, client_as):
    from app.sources import SimulatedSource
    store = MachineStore(str(tmp_path / "m.db"))
    m = store.add(Machine(None, "Beckhoff", "ads", "127.0.0.1", ams_net_id="5.79.97.240.1.1")).id
    # fonte simulada no lugar do pyads: o teste olha so o formato das zonas
    main.app.state.fleet = Fleet(store, _settings(tmp_path),
                                            source_factory=lambda _m: SimulatedSource())
    try:
        client = client_as("mae")
        zones = client.get(f"/api/machines/{m}/info").json()["zones"]
    finally:
        del main.app.state.fleet
    assert [z["label"] for z in zones] == [f"Zona {i}" for i in range(1, 9)]
    assert zones[7]["setpoint"] == "TempZ8"


class _MissingBridge:
    """Ponte de teste: o servidor não tem as ferramentas da tela da Beckhoff."""

    def __init__(self, m):
        self.state, self.error, self.port = "stopped", None, None

    async def start(self):
        self.state = "missing"
        self.error = "Para a tela da Beckhoff falta no servidor: x11vnc, CERHOST.exe."

    async def stop(self):
        pass


def _bk_api(tmp_path, bridge_factory, **machine):
    from app.sources import SimulatedSource
    store = MachineStore(str(tmp_path / "m.db"))
    m = store.add(Machine(None, "Beckhoff", "ads", "10.0.0.7", ams_net_id="5.79.97.240.1.1",
                          **machine)).id
    main.app.state.fleet = Fleet(store, _settings(tmp_path),
                                            source_factory=lambda _m: SimulatedSource(),
                                            bridge_factory=bridge_factory)
    return store, m


def test_status_mostra_falta_de_ferramenta(tmp_path, client_as):
    _, m = _bk_api(tmp_path, _MissingBridge, vnc_enabled=True, vnc_view_password="SEGREDO1",
                   vnc_control_password="SEGREDO2")
    try:
        client = client_as("mae")
        info = client.get(f"/api/machines/{m}/info").json()
        assert info["vnc"] is True and info["vnc_control"] is True
        for _ in range(100):  # a ponte sobe em segundo plano
            r = client.get("/api/vnc/status", params={"machine": m})
            if r.json().get("bridge", {}).get("state") == "missing":
                break
            time.sleep(0.01)
        assert r.status_code == 200
        st = r.json()
        assert st["bridge"]["state"] == "missing"
        assert "x11vnc" in st["bridge"]["error"] and "CERHOST.exe" in st["bridge"]["error"]
        assert "SEGREDO" not in r.text
        with client.websocket_connect(f"/ws/vnc?machine={m}") as ws:
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.receive_bytes()
        assert exc.value.code == 1008 and "falta no servidor" in exc.value.reason
    finally:
        del main.app.state.fleet


def test_ads_sem_tela_nao_tem_vnc(tmp_path, client_as):
    _, m = _bk_api(tmp_path, _MissingBridge)
    try:
        client = client_as("mae")
        assert client.get(f"/api/machines/{m}/info").json()["vnc"] is False
        assert client.get("/api/vnc/status", params={"machine": m}).status_code == 404
        assert client.get("/api/vnc/status").status_code == 422  # machine é obrigatório
    finally:
        del main.app.state.fleet


def test_status_da_br_sem_ponte(api):
    client, store, a, b = api
    r = client.post("/api/machines", json={"name": "BR", "kind": "em63", "host": "127.0.0.1",
                                           "vnc_port": 59999, "vnc_view_password": "v"})
    st = client.get("/api/vnc/status", params={"machine": r.json()["id"]}).json()
    assert "bridge" not in st and "panel" in st


def test_put_client_id_null_limpa_cliente(api):
    client, store, a, b = api
    assert client.put(f"/api/machines/{b}", json={"client_id": 5}).json()["client_id"] == 5
    r = client.put(f"/api/machines/{b}", json={"client_id": None})
    assert r.status_code == 200 and r.json()["client_id"] is None
    assert store.get(b).client_id is None
