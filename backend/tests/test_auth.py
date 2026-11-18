import time
from types import SimpleNamespace

import pytest
from starlette.websockets import WebSocketDisconnect

from app import auth, main
from app.auth import LoginLimiter
from app.machines import Machine, MachineStore
from app.manager import Fleet
from app.security import new_totp_secret, totp_code

from .conftest import HttpsClient


def _settings(tmp_path):
    return SimpleNamespace(
        machines_dir=str(tmp_path / "machines"), poll_seconds=0.05, full_poll_seconds=0.2,
        retention_days=7, vnc_control_timeout=300,
        vnc_audit_path=None, ads_local_net_id="",
    )


@pytest.fixture
def client(tmp_path, accounts):
    store = MachineStore(str(tmp_path / "m.db"))
    store.set_active(store.add(Machine(None, "A", "demo", "127.0.0.1")).id)
    main.app.state.fleet = Fleet(store, _settings(tmp_path))
    accounts.add_user("ana", "Ana", "tecnico", "senha12345")
    try:
        with HttpsClient(main.app) as c:
            yield c
    finally:
        del main.app.state.fleet


def login(client, username, password):
    return client.post("/api/login", json={"username": username, "password": password})


def test_rotas_exigem_login(client):
    for path in ("/api/machines", "/api/machines/1/info", "/api/machines/1/live", "/api/me",
                 "/api/vnc/status?machine=1"):
        r = client.get(path)
        assert r.status_code == 401, path
        assert r.json()["detail"] == "Faça login."
    assert client.post("/api/machines", json={"name": "x", "kind": "demo", "host": "h"}).status_code == 401
    assert client.post("/api/logout").status_code == 401


def test_login_com_cookie_seguro(client):
    r = login(client, "ana", "senha12345")
    assert r.status_code == 200
    c = r.headers["set-cookie"]
    assert c.startswith("monitor_sessao=")
    assert "HttpOnly" in c and "Secure" in c and "Path=/" in c
    assert "samesite=strict" in c.lower()  # o Starlette escreve "strict"; atributo sem caixa (RFC 6265)
    me = r.json()["user"]
    assert me == {"id": me["id"], "username": "ana", "name": "Ana", "role": "tecnico",
                  "client_id": None, "client_name": "", "has_totp": False}
    assert client.get("/api/me").json() == me
    assert client.get("/api/machines").status_code == 200
    assert "senha12345" not in r.text


def test_me_do_cliente_traz_nome_do_cliente(client, accounts):
    cid = accounts.add_client("Plásticos Sul").id
    accounts.add_user("joao", "João", "cliente", "senha12345", client_id=cid)
    me = login(client, "joao", "senha12345").json()["user"]
    assert me["client_id"] == cid and me["client_name"] == "Plásticos Sul" and me["role"] == "cliente"


def test_mesmo_erro_para_usuario_inexistente(client, accounts):
    a = login(client, "ana", "errada-errada")
    b = login(client, "ninguem", "errada-errada")
    assert a.status_code == b.status_code == 401
    assert a.json() == b.json() == {"detail": "Usuário ou senha incorretos."}
    assert "set-cookie" not in a.headers
    uid = accounts.get_by_username("ana").id
    accounts.update_user(uid, active=False)
    r = login(client, "ana", "senha12345")
    assert r.status_code == 401 and r.json()["detail"] == "Usuário ou senha incorretos."


def test_bloqueio_progressivo(client):
    for _ in range(5):
        assert login(client, "ana", "errada-errada").status_code == 401
    r = login(client, "ana", "senha12345")  # até a senha certa espera
    assert r.status_code == 429
    assert r.json()["detail"] == "Muitas tentativas. Tente de novo em 30 s."

    lim = LoginLimiter(max_failures=5, base_wait=30, max_wait=900)
    for _ in range(5):
        lim.failure("ana", "1.2.3.4", now=1000)
    assert lim.wait_for("ana", "1.2.3.4", now=1000) == 30
    assert lim.wait_for("ana", "9.9.9.9", now=1000) == 30  # por usuário, venha de onde vier
    assert lim.wait_for("ana", "1.2.3.4", now=1010) == 20
    lim.failure("ana", "1.2.3.4", now=1030)
    assert lim.wait_for("ana", "1.2.3.4", now=1030) == 60
    for _ in range(20):
        lim.failure("ana", "1.2.3.4", now=2000)
    assert lim.wait_for("ana", "1.2.3.4", now=2000) == 900
    lim.success("ana", "1.2.3.4")
    assert lim.wait_for("ana", "5.5.5.5", now=2000) == 0
    assert lim.wait_for("bia", "5.5.5.5", now=2000) == 0


def test_bloqueio_por_ip():
    lim = LoginLimiter(max_failures=5, base_wait=30, max_wait=900)
    for i in range(5):  # um login diferente por tentativa, do mesmo IP
        lim.failure(f"u{i}", "6.6.6.6", now=1000)
    assert lim.wait_for("outro", "6.6.6.6", now=1000) == 30
    assert lim.wait_for("outro", "7.7.7.7", now=1000) == 0
    lim.success("u0", "6.6.6.6")  # um acerto não limpa o IP
    assert lim.wait_for("outro", "6.6.6.6", now=1000) == 30


def test_reserva_atomica():
    lim = LoginLimiter(max_failures=5, base_wait=30, max_wait=900)
    for _ in range(5):
        assert lim.reserve("ana", "1.1.1.1", now=1000) == 0
    assert lim.reserve("ana", "1.1.1.1", now=1000) == 30
    lim.release("ana", "1.1.1.1")  # a última estava certa: desfaz
    assert lim.reserve("ana", "1.1.1.1", now=1000) == 0
    lim2 = LoginLimiter()
    assert lim2.reserve("bia", "2.2.2.2", now=1000) == 0
    lim2.release("bia", "2.2.2.2")
    for _ in range(4):
        lim2.failure("bia", "2.2.2.2", now=1000)
    assert lim2.wait_for("bia", "2.2.2.2", now=1000) == 0  # a reserva desfeita não contou


def test_limiter_poda_e_limita_memoria(monkeypatch):
    lim = LoginLimiter(max_failures=5, base_wait=30, max_wait=900)
    for i in range(50):
        lim.failure(f"u{i}", f"ip{i}", now=1000)
    lim.wait_for("x", "y", now=1000 + 1801)  # passou de 2 x max_wait: tudo caduca
    assert len(lim._users) == 0 and len(lim._ips) == 0
    monkeypatch.setattr(LoginLimiter, "MAX_KEYS", 10)
    for i in range(30):
        lim.failure(f"v{i}", f"ip{i}", now=5000 + i)
    assert len(lim._users) == 10 and len(lim._ips) == 10
    assert "v29" in lim._users and "v0" not in lim._users  # sai a mais antiga


def test_logins_em_paralelo_nao_furam_o_limite(client, accounts, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    calls = []
    lock = threading.Lock()
    real = accounts.check_password

    def slow(username, pw):
        with lock:
            calls.append(username)
        time.sleep(0.3)  # o scrypt demora: os pedidos se sobrepõem
        return real(username, pw)

    monkeypatch.setattr(accounts, "check_password", slow)
    clients = []
    for _ in range(12):
        c = HttpsClient(main.app)
        c.portal = client.portal
        clients.append(c)
    with ThreadPoolExecutor(max_workers=12) as ex:
        codes = list(ex.map(lambda c: login(c, "ana", "errada-errada").status_code, clients))
    assert len(calls) <= 5
    assert codes.count(401) == len(calls) and codes.count(429) == 12 - len(calls)


def test_sem_docs_publicas(client):
    for path in ("/docs", "/redoc", "/openapi.json"):
        r = client.get(path)
        assert "swagger" not in r.text.lower() and "redoc" not in r.text.lower(), path
        assert '"openapi"' not in r.text and '"paths"' not in r.text, path
    assert main.app.openapi_url is None and main.app.docs_url is None and main.app.redoc_url is None


def test_websockets_registrados_por_app(client):
    login(client, "ana", "senha12345")
    reg = auth.state(main.app).sockets
    with client.websocket_connect("/ws/machines/1") as ws:
        ws.receive_json()
        token = client.cookies.get("monitor_sessao")
        assert reg.tokens() == [token] and len(reg.sockets(token)) == 1
    for _ in range(100):
        if not reg.tokens():
            break
        time.sleep(0.01)
    assert reg.tokens() == []
    assert auth.AuthState().sockets.tokens() == []  # nada global


def test_mae_precisa_do_codigo(client, accounts):
    secret = new_totp_secret()
    accounts.add_user("mae", "Mãe", "mae", "senha-forte-123", totp_secret=secret)
    r = login(client, "mae", "senha-forte-123")
    assert r.status_code == 200
    body = r.json()
    assert body["need_code"] is True and body["ticket"] and "user" not in body
    assert "set-cookie" not in r.headers
    assert client.get("/api/me").status_code == 401
    now = time.time()
    errado = "000000" if totp_code(secret, now) != "000000" else "111111"
    r = client.post("/api/login/code", json={"ticket": body["ticket"], "code": errado})
    assert r.status_code == 401 and r.json()["detail"] == "Senha ou código incorretos."
    r = client.post("/api/login/code", json={"ticket": body["ticket"], "code": totp_code(secret, now)})
    assert r.status_code == 200 and r.json()["user"]["role"] == "mae"
    assert r.json()["user"]["has_totp"] is True
    assert client.get("/api/me").json()["role"] == "mae"
    # ticket é de uso único
    r = client.post("/api/login/code", json={"ticket": body["ticket"], "code": totp_code(secret, now)})
    assert r.status_code == 401


def test_codigo_nao_pode_ser_reusado(client, accounts):
    secret = new_totp_secret()
    accounts.add_user("mae", "Mãe", "mae", "senha-forte-123", totp_secret=secret)
    code = totp_code(secret, time.time())
    t1 = login(client, "mae", "senha-forte-123").json()["ticket"]
    assert client.post("/api/login/code", json={"ticket": t1, "code": code}).status_code == 200
    other = HttpsClient(main.app)
    other.portal = client.portal
    t2 = login(other, "mae", "senha-forte-123").json()["ticket"]
    r = other.post("/api/login/code", json={"ticket": t2, "code": code})
    assert r.status_code == 401 and r.json()["detail"] == "Senha ou código incorretos."


def test_ticket_expira():
    tickets = auth.TicketStore(ttl=300)
    t = tickets.issue(7, now=1000)
    assert tickets.peek(t, now=1299) == 7
    assert tickets.peek(t, now=1301) is None
    assert tickets.peek("inventado", now=1000) is None


def test_codigo_errado_conta_no_bloqueio(client, accounts):
    secret = new_totp_secret()
    accounts.add_user("mae", "Mãe", "mae", "senha-forte-123", totp_secret=secret)
    ticket = login(client, "mae", "senha-forte-123").json()["ticket"]
    errado = "000000" if totp_code(secret, time.time()) != "000000" else "111111"
    for _ in range(5):
        assert client.post("/api/login/code", json={"ticket": ticket, "code": errado}).status_code == 401
    r = client.post("/api/login/code", json={"ticket": ticket, "code": totp_code(secret, time.time())})
    assert r.status_code == 429


def test_mae_sem_totp_recusada(client, accounts):
    accounts.add_user("mae", "Mãe", "mae", "senha-forte-123")
    r = login(client, "mae", "senha-forte-123")
    assert r.status_code == 403
    assert r.json()["detail"] == "Configure o 2º fator pelo comando do servidor."
    assert "set-cookie" not in r.headers


def test_logout_invalida(client):
    login(client, "ana", "senha12345")
    token = client.cookies.get("monitor_sessao")
    assert client.get("/api/me").status_code == 200
    r = client.post("/api/logout")
    assert r.status_code == 204
    assert client.get("/api/me").status_code == 401
    other = HttpsClient(main.app, cookies={"monitor_sessao": token})
    other.portal = client.portal
    assert other.get("/api/me").status_code == 401  # o token velho não vale mais


def test_origin_de_outro_site_bloqueado(client):
    login(client, "ana", "senha12345")
    r = client.post("/api/logout", headers={"Origin": "https://evil.example"})
    assert r.status_code == 403 and r.json()["detail"] == "Origem não permitida."
    assert client.get("/api/me").status_code == 200  # não deslogou
    r = client.post(
        "/api/login", json={"username": "ana", "password": "senha12345"},
        headers={"Origin": "https://testserver:8443"})
    assert r.status_code == 403
    r = client.post("/api/logout", headers={"Origin": "https://testserver"})
    assert r.status_code == 204


def test_ws_sem_login_fecha_4401(client):
    for path in ("/ws/machines/1", "/ws/vnc?machine=1"):
        with client.websocket_connect(path) as ws:
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.receive_text()
        assert exc.value.code == 4401, path


def test_ws_com_login_funciona(client):
    login(client, "ana", "senha12345")
    with client.websocket_connect("/ws/machines/1") as ws:
        assert "status" in ws.receive_json()


def test_ws_de_outra_origem_recusado(client):
    login(client, "ana", "senha12345")
    with client.websocket_connect("/ws/machines/1", headers={"Origin": "https://evil.example"}) as ws:
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_text()
    assert exc.value.code == 4403


def test_usuario_desativado_perde_ws_em_ate_10s(client, accounts, monkeypatch):
    monkeypatch.setattr(auth, "SWEEP_SECONDS", 0.1)
    login(client, "ana", "senha12345")
    with client.websocket_connect("/ws/machines/1") as ws:
        ws.receive_json()
        accounts.update_user(accounts.get_by_username("ana").id, active=False)
        start = time.monotonic()
        with pytest.raises(WebSocketDisconnect) as exc:
            for _ in range(10000):
                ws.receive_json()
        assert exc.value.code == 4401
        assert time.monotonic() - start < 10


def test_logout_fecha_ws(client):
    login(client, "ana", "senha12345")
    with client.websocket_connect("/ws/machines/1") as ws:
        ws.receive_json()
        assert client.post("/api/logout").status_code == 204
        with pytest.raises(WebSocketDisconnect) as exc:
            for _ in range(10000):
                ws.receive_json()
        assert exc.value.code == 4401


def test_login_registrado_na_auditoria(client, accounts):
    login(client, "ana", "errada-errada")
    login(client, "fulano", "errada-errada")
    login(client, "ana", "senha12345")
    client.post("/api/logout")
    rows = list(reversed(accounts.audit()))
    assert [r["action"] for r in rows] == ["login_falhou", "login_falhou", "login", "logout"]
    assert rows[0]["detail"] == "ana" and rows[1]["detail"] == "fulano"
    assert rows[2]["user_name"] == "Ana" and rows[3]["user_name"] == "Ana"
    assert all(r["ip"] == "testclient" for r in rows)
    assert not any("senha" in r["detail"] or "errada" in r["detail"] for r in rows)


def test_frontend_publico(client):
    # sem frontend compilado a rota coringa pode não existir; o que importa é não pedir login
    assert client.get("/").status_code != 401
    assert client.get("/login").status_code != 401


def test_client_as(client_as, tmp_path, accounts):
    store = MachineStore(str(tmp_path / "m2.db"))
    main.app.state.fleet = Fleet(store, _settings(tmp_path))
    try:
        mae = client_as("mae")
        cli = client_as("cliente")
        assert mae.get("/api/me").json()["role"] == "mae"
        me = cli.get("/api/me").json()
        assert me["role"] == "cliente" and me["client_id"] is not None
        assert mae.get("/api/me").json()["role"] == "mae"  # cookies separados
    finally:
        del main.app.state.fleet
