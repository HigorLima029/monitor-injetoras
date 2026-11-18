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


# ---- permissões por perfil --------------------------------------------------
from app import authz  # noqa: E402
from app.accounts import Grant, User  # noqa: E402

SENHA_VNC = "SENHA-VNC-9f3k"
ROTAS = ["/info", "/live", "/history?tags=CycleMonitor", "/production", "/alarms",
         "/values?tags=CycleMonitor", "/tags"]
CONEXAO = ("host", "opcua_port", "namespace", "ams_net_id", "ads_port", "vnc_port", "notes",
           "opcua_url")


@pytest.fixture
def multi(tmp_path, client_as, accounts):
    """Cliente A com a máquina MA, cliente B com MB e uma máquina em preparação (MP)."""
    ca = accounts.add_client("Cliente A").id
    cb = accounts.add_client("Cliente B").id
    store = MachineStore(str(tmp_path / "m.db"))
    ma = store.add(Machine(None, "MA", "em63", "10.1.1.1", client_id=ca, notes="nota secreta",
                           vnc_view_password=SENHA_VNC, vnc_control_password=SENHA_VNC + "c")).id
    mb = store.add(Machine(None, "MB", "demo", "10.2.2.2", client_id=cb)).id
    mp = store.add(Machine(None, "MP", "demo", "10.3.3.3")).id
    from app.sources import SimulatedSource
    main.app.state.fleet = Fleet(store, _settings(tmp_path), source_factory=lambda m: SimulatedSource())
    try:
        yield SimpleNamespace(client_as=client_as, store=store, ca=ca, cb=cb, ma=ma, mb=mb, mp=mp)
    finally:
        del main.app.state.fleet


def _sem_conexao(d: dict):
    for k in CONEXAO:
        assert k not in d, k


@pytest.mark.parametrize("rota", ROTAS)
def test_cliente_nao_ve_maquina_de_outro_cliente(multi, rota):
    cli = multi.client_as("cliente", multi.ca)
    assert cli.get(f"/api/machines/{multi.ma}{rota}").status_code == 200
    for other in (multi.mb, multi.mp, 999):
        r = cli.get(f"/api/machines/{other}{rota}")
        assert r.status_code == 404 and r.json()["detail"] == "Máquina não encontrada", (other, rota)


def test_cliente_nao_recebe_dados_de_conexao(multi):
    cli = multi.client_as("cliente", multi.ca)
    ms = cli.get("/api/machines").json()["machines"]
    assert [m["id"] for m in ms] == [multi.ma]
    assert set(ms[0]) == {"id", "name", "kind", "profile", "client_id", "client_name", "status", "vnc"}
    assert ms[0]["client_name"] == "Cliente A"
    info = cli.get(f"/api/machines/{multi.ma}/info").json()
    _sem_conexao(info)
    _sem_conexao(info["machine"])
    assert info["vnc_control"] is False
    assert "10.1.1.1" not in cli.get(f"/api/machines/{multi.ma}/info").text
    # técnico vê conexão (sem senhas)
    tec = multi.client_as("tecnico")
    ms = {m["id"]: m for m in tec.get("/api/machines").json()["machines"]}
    assert set(ms) == {multi.ma, multi.mb, multi.mp}
    assert ms[multi.ma]["host"] == "10.1.1.1" and ms[multi.ma]["notes"] == "nota secreta"
    assert ms[multi.ma]["client_name"] == "Cliente A" and "status" in ms[multi.ma]
    assert tec.get(f"/api/machines/{multi.ma}/info").json()["opcua_url"]


def test_ver_como_cliente(multi):
    tec = multi.client_as("tecnico")
    normal = tec.get(f"/api/machines/{multi.ma}/info").json()
    assert normal["machine"]["host"] == "10.1.1.1"
    info = tec.get(f"/api/machines/{multi.ma}/info", params={"as": "cliente"}).json()
    _sem_conexao(info)
    _sem_conexao(info["machine"])
    assert info["vnc_control"] is False
    cli = multi.client_as("cliente", multi.ca)  # cliente com ?as=: nada muda
    assert cli.get(f"/api/machines/{multi.ma}/info", params={"as": "cliente"}).json()["machine"] == \
        cli.get(f"/api/machines/{multi.ma}/info").json()["machine"]


def test_filtro_por_cliente(multi):
    tec = multi.client_as("tecnico")
    ids = lambda q: [m["id"] for m in tec.get("/api/machines", params=q).json()["machines"]]  # noqa: E731
    assert ids({"client_id": multi.ca}) == [multi.ma]
    assert ids({"client_id": 0}) == [multi.mp]
    cli = multi.client_as("cliente", multi.ca)
    assert [m["id"] for m in cli.get("/api/machines", params={"client_id": multi.cb}).json()["machines"]] == []
    assert [m["id"] for m in cli.get("/api/machines", params={"client_id": 0}).json()["machines"]] == []


def test_so_mae_cadastra_e_vincula(multi, accounts):
    body = {"name": "Nova", "kind": "demo", "host": "10.9.9.9"}
    tec = multi.client_as("tecnico")
    cli = multi.client_as("cliente", multi.cb)
    for c in (tec, cli):
        assert c.post("/api/machines", json=body).status_code == 403
        assert c.put(f"/api/machines/{multi.mp}", json={"client_id": multi.cb}).status_code in (403, 404)
        assert c.delete(f"/api/machines/{multi.mb}").status_code in (403, 404)
    assert tec.put(f"/api/machines/{multi.mp}", json={"client_id": multi.cb}).status_code == 403
    assert tec.delete(f"/api/machines/{multi.mb}").status_code == 403
    assert multi.store.get(multi.mb) is not None
    assert cli.get(f"/api/machines/{multi.mp}/live").status_code == 404
    mae = multi.client_as("mae")
    r = mae.put(f"/api/machines/{multi.mp}", json={"client_id": multi.cb})
    assert r.status_code == 200 and r.json()["client_id"] == multi.cb
    assert cli.get(f"/api/machines/{multi.mp}/live").status_code == 200
    assert {m["id"] for m in cli.get("/api/machines").json()["machines"]} == {multi.mb, multi.mp}
    r = mae.post("/api/machines", json={**body, "vnc_view_password": SENHA_VNC})
    assert r.status_code == 201
    new = r.json()["id"]
    assert mae.delete(f"/api/machines/{new}").status_code == 204
    rows = [a for a in accounts.audit() if a["action"] == "cadastro"]
    assert len(rows) == 3 and {a["machine_id"] for a in rows} == {multi.mp, new}
    assert not any(SENHA_VNC in a["detail"] for a in rows)
    assert all(a["detail"] for a in rows)


def test_test_so_mae_e_tecnico(multi):
    cli = multi.client_as("cliente", multi.ca)
    assert cli.post(f"/api/machines/{multi.ma}/test").status_code == 403
    assert cli.post(f"/api/machines/{multi.mb}/test").status_code == 404
    tec = multi.client_as("tecnico")
    assert tec.post(f"/api/machines/{multi.ma}/test").status_code == 200


def test_em_preparacao_invisivel_para_cliente(multi):
    cli = multi.client_as("cliente", multi.ca)
    assert multi.mp not in [m["id"] for m in cli.get("/api/machines").json()["machines"]]
    assert cli.get(f"/api/machines/{multi.mp}/info").status_code == 404
    with cli.websocket_connect(f"/ws/machines/{multi.mp}") as ws:
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
    assert exc.value.code == 4404
    tec = multi.client_as("tecnico")
    assert tec.get(f"/api/machines/{multi.mp}/info").status_code == 200


def test_ws_de_maquina_de_outro_cliente_fecha(multi):
    cli = multi.client_as("cliente", multi.ca)
    rt = main.app.state.fleet.runtime(multi.mb)
    for url in (f"/ws/machines/{multi.mb}", f"/ws/vnc?machine={multi.mb}"):
        with cli.websocket_connect(url) as ws:
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.receive_bytes() if "vnc" in url else ws.receive_json()
        assert exc.value.code == 4404 and exc.value.reason == "Máquina não encontrada", url
    assert rt.viewers == 0
    for path in (f"/api/vnc/status?machine={multi.mb}",):
        assert cli.get(path).status_code == 404
    r = cli.post(f"/api/vnc/control?machine={multi.mb}", json={"sid": "x", "action": "take"})
    assert r.status_code == 404
    with cli.websocket_connect(f"/ws/machines/{multi.ma}") as ws:
        assert "status" in ws.receive_json()


def test_ws_fecha_quando_maquina_sai_do_cliente(multi):
    cli = multi.client_as("cliente", multi.ca)
    mae = multi.client_as("mae")
    with cli.websocket_connect(f"/ws/machines/{multi.ma}") as ws:
        assert "status" in ws.receive_json()
        assert mae.put(f"/api/machines/{multi.ma}", json={"client_id": multi.cb}).status_code == 200
        with pytest.raises(WebSocketDisconnect) as exc:
            for _ in range(50):
                ws.receive_json()
        assert exc.value.code == 4404


def test_controle_da_tela_por_perfil(multi):
    cli = multi.client_as("cliente", multi.ca)
    assert main.app.state.fleet.runtime(multi.ma).hub is not None
    r = cli.post(f"/api/vnc/control?machine={multi.ma}", json={"sid": "x", "action": "take"})
    assert r.status_code == 403 and r.json()["detail"] == "Cliente só visualiza a tela."
    tec = multi.client_as("tecnico")
    r = tec.post(f"/api/vnc/control?machine={multi.ma}", json={"sid": "x", "action": "take"})
    assert r.status_code == 403 and r.json()["detail"] == "Precisa da autorização do cliente."
    assert cli.get("/api/vnc/status", params={"machine": multi.ma}).status_code == 200


def test_nenhuma_senha_em_resposta(multi):
    mae = multi.client_as("mae")
    tec = multi.client_as("tecnico")
    cli = multi.client_as("cliente", multi.ca)
    for c in (mae, tec, cli):
        texts = [c.get("/api/machines").text]
        texts += [c.get(f"/api/machines/{multi.ma}{rota}").text for rota in ROTAS]
        texts.append(c.get(f"/api/machines/{multi.ma}/info", params={"as": "cliente"}).text)
        assert not any(SENHA_VNC in t for t in texts)
    r = mae.put(f"/api/machines/{multi.ma}", json={"name": "MA2"})
    assert SENHA_VNC not in r.text


# ---- authz (unidade) -------------------------------------------------------
def _u(role, client_id=None):
    return User(1, "u", "U", role, client_id, True, False)


def test_authz_regras():
    m_a = Machine(1, "A", "demo", "h", client_id=5)
    m_p = Machine(2, "P", "demo", "h")
    assert authz.can_view(_u("mae"), m_a) and authz.can_view(_u("tecnico"), m_p)
    assert authz.can_view(_u("cliente", 5), m_a)
    assert not authz.can_view(_u("cliente", 6), m_a)
    assert not authz.can_view(_u("cliente", None), m_p)  # None nunca casa com None
    assert not authz.can_view(_u("estranho"), m_a)
    assert authz.sees_connection(_u("mae")) and authz.sees_connection(_u("tecnico"))
    assert not authz.sees_connection(_u("cliente", 5))
    grant = Grant(1, 1, 9, "Maria", False, "", 0, 9e12, None)
    assert authz.control_problem(_u("cliente", 5), m_a, grant) == "Cliente só visualiza a tela."
    assert authz.control_problem(_u("tecnico"), m_a, None) == "Precisa da autorização do cliente."
    assert authz.control_problem(_u("mae"), m_a, None) == "Precisa da autorização do cliente."
    assert authz.control_problem(_u("tecnico"), m_a, grant) is None
    assert authz.control_problem(_u("mae"), m_p, None) is None
    assert authz.control_problem(_u("tecnico"), m_p, None) is None


def test_authz_machine_view():
    m = Machine(1, "A", "em63", "10.0.0.1", client_id=5, notes="n", vnc_view_password="x")
    st = {"online": True}
    v = authz.machine_view(_u("cliente", 5), m, st, "Cli")
    assert v == {"id": 1, "name": "A", "kind": "em63", "profile": "em63", "client_id": 5,
                 "client_name": "Cli", "status": st, "vnc": True}
    assert authz.machine_view(_u("tecnico"), m, st, "Cli", as_client=True) == v
    full = authz.machine_view(_u("mae"), m, st, "Cli")
    assert full["host"] == "10.0.0.1" and full["client_name"] == "Cli" and full["status"] == st
    assert "vnc_view_password" not in full
