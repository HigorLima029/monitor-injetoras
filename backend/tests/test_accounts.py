import sqlite3
from dataclasses import asdict

import pytest

from app.accounts import ROLES, AccountStore, migrate_machine_clients
from app.machines import Machine, MachineStore

SENHA = "senha12345"
MAE_SENHA = "senha-da-mae-123"


@pytest.fixture
def acc(tmp_path):
    a = AccountStore(str(tmp_path / "cfg.db"))
    yield a
    a.close()


@pytest.fixture
def mae(acc):
    return acc.add_user("root", "Mãe", "mae", MAE_SENHA)


@pytest.fixture
def cliente(acc):
    c = acc.add_client("ABC")
    return acc.add_user("ana", "Ana", "cliente", SENHA, client_id=c.id)


def test_clientes_nome_unico_sem_caixa(acc):
    c = acc.add_client("ABC")
    with pytest.raises(ValueError):
        acc.add_client("abc")
    assert acc.get_client(c.id).name == "ABC" and acc.get_client(99) is None
    assert acc.update_client(c.id, name="XYZ", active=False).active is False
    assert [x.name for x in acc.list_clients()] == ["XYZ"]


def test_usuario_cliente_exige_cliente(acc):
    with pytest.raises(ValueError):
        acc.add_user("ana", "Ana", "cliente", SENHA)
    with pytest.raises(ValueError):
        acc.add_user("t", "T", "tecnico", SENHA, client_id=1)
    with pytest.raises(ValueError):
        acc.add_user("x", "X", "chefe", SENHA)
    with pytest.raises(ValueError):
        acc.add_user("t", "T", "tecnico", "curta")
    acc.add_user("t", "T", "tecnico", SENHA)
    with pytest.raises(ValueError):
        acc.add_user("T", "T2", "tecnico", SENHA)
    assert ROLES == ("mae", "tecnico", "cliente")


def test_login_minusculo_e_senha_fora_do_user(acc, cliente):
    assert acc.check_password("ANA", SENHA).id == cliente.id
    assert acc.check_password("ana", "errada") is None
    assert acc.get_by_username("Ana").username == "ana"
    d = asdict(cliente)
    assert not any("hash" in k or "password" in k or "secret" in k for k in d)
    assert d["has_totp"] is False


def test_totp(acc, cliente):
    assert acc.totp_secret(cliente.id) is None
    acc.set_totp(cliente.id, "ABCDEF")
    assert acc.totp_secret(cliente.id) == "ABCDEF" and acc.get_user(cliente.id).has_totp is True
    acc.set_totp(cliente.id, None)
    assert acc.get_user(cliente.id).has_totp is False


def test_set_password_e_inativo(acc, cliente):
    acc.set_password(cliente.id, "outrasenha1")
    assert acc.check_password("ana", SENHA) is None
    assert acc.check_password("ana", "outrasenha1")
    with pytest.raises(ValueError):
        acc.set_password(cliente.id, "x")
    acc.update_user(cliente.id, active=False)
    assert acc.check_password("ana", "outrasenha1") is None


def test_conta_mae_nao_desativa_nem_rebaixa(acc, mae):
    with pytest.raises(PermissionError, match="conta mãe"):
        acc.update_user(mae.id, active=False)
    with pytest.raises(PermissionError):
        acc.update_user(mae.id, role="tecnico")
    assert acc.update_user(mae.id, name="Nova").name == "Nova"


def test_sessao_expira_por_inatividade_e_maximo(acc, cliente):
    t = acc.create_session(cliente.id, "1.2.3.4", now=1000)
    row = acc._conn.execute("SELECT token_hash FROM sessions").fetchone()[0]
    assert row != t and len(row) == 64
    assert acc.session_user(t, now=1000 + 7 * 3600).id == cliente.id
    assert acc.session_user(t, now=1000 + 7 * 3600 + 8 * 3600 + 1) is None
    t2 = acc.create_session(cliente.id, "ip", now=1000)
    for h in range(1, 25):  # usada de hora em hora
        assert acc.session_user(t2, now=1000 + h * 3600)
    assert acc.session_user(t2, now=1000 + 24 * 3600 + 60) is None
    assert acc.session_user("lixo") is None
    t3 = acc.create_session(cliente.id, "ip")
    acc.delete_session(t3)
    assert acc.session_user(t3) is None


def test_desativar_usuario_derruba_sessao(acc, cliente):
    t = acc.create_session(cliente.id, "ip")
    assert acc.session_user(t)
    acc.update_user(cliente.id, active=False)
    assert acc.session_user(t) is None
    acc.update_user(cliente.id, active=True)
    acc.delete_user_sessions(cliente.id)
    assert acc.session_user(t) is None


def test_autorizacao_expira_e_em_nome_exige_motivo(acc, cliente, mae):
    g = acc.grant_support(7, cliente, minutes=30, now=1000)
    assert g.expires == 1000 + 1800 and g.by_name == "Ana" and g.on_behalf is False
    assert acc.active_grant(7, now=1000 + 29 * 60) and acc.active_grant(7, now=1000 + 31 * 60) is None
    with pytest.raises(ValueError):
        acc.grant_support(7, mae, on_behalf=True, reason=" ")
    g2 = acc.grant_support(8, mae, on_behalf=True, reason="cliente pediu por telefone", now=1000)
    assert g2.on_behalf and g2.reason == "cliente pediu por telefone"
    assert acc.end_support(8, mae, now=1001) is True
    assert acc.active_grant(8, now=1001) is None
    assert acc.end_support(8, mae, now=1002) is False


def test_auditoria_filtra_por_cliente_e_maquina(acc, cliente, mae):
    acc.log("login", cliente, ip="9.9.9.9", now=100)
    acc.log("vnc_abriu", cliente, machine_id=5, detail="x", now=200)
    acc.log("suporte", mae, machine_id=6, client_id=cliente.client_id, highlight=True, now=300)
    acc.log("anonimo", None, now=400)
    todos = acc.audit()
    assert [r["action"] for r in todos] == ["anonimo", "suporte", "vnc_abriu", "login"]
    assert set(todos[0]) >= {"time", "user_name", "role", "client_id", "action", "machine_id",
                             "ip", "detail", "highlight"}
    assert [r["action"] for r in acc.audit(client_id=cliente.client_id)] == ["suporte", "vnc_abriu", "login"]
    assert [r["action"] for r in acc.audit(machine_id=5)] == ["vnc_abriu"]
    assert [r["action"] for r in acc.audit(user_id=mae.id)] == ["suporte"]
    assert [r["action"] for r in acc.audit(since=150, until=350)] == ["suporte", "vnc_abriu"]
    assert len(acc.audit(limit=2)) == 2
    assert todos[1]["highlight"] is True and todos[0]["user_name"] == ""


def test_log_nunca_levanta(acc, cliente):
    acc._conn.execute("DROP TABLE audit")
    acc.log("x", cliente)  # não levanta


def _banco_antigo(path):
    c = sqlite3.connect(path)
    c.executescript("""
    CREATE TABLE machines (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, kind TEXT NOT NULL,
      host TEXT NOT NULL, client TEXT NOT NULL DEFAULT 'Chen Hsong', opcua_port INTEGER NOT NULL DEFAULT 4840,
      namespace INTEGER, ams_net_id TEXT NOT NULL DEFAULT '', ads_port INTEGER NOT NULL DEFAULT 801,
      vnc_enabled INTEGER, vnc_port INTEGER NOT NULL DEFAULT 5900,
      vnc_view_password TEXT NOT NULL DEFAULT '', vnc_control_password TEXT NOT NULL DEFAULT '',
      notes TEXT NOT NULL DEFAULT '');
    CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT);
    INSERT INTO machines(name,kind,host,client,vnc_view_password) VALUES ('M1','em63','1.1.1.1','Chen Hsong','view1');
    INSERT INTO machines(name,kind,host,client) VALUES ('M2','chy','1.1.1.2','Chen Hsong');
    INSERT INTO machines(name,kind,host,client) VALUES ('M3','chy','1.1.1.3','ABC');
    INSERT INTO kv VALUES ('active_id','1');
    """)
    c.commit()
    c.close()


def test_migracao_do_cadastro_antigo(tmp_path):
    p = str(tmp_path / "old.db")
    _banco_antigo(p)
    ms, acc = MachineStore(p), AccountStore(p)
    migrate_machine_clients(ms, acc)
    migrate_machine_clients(ms, acc)
    assert {c.name for c in acc.list_clients()} == {"Chen Hsong", "ABC"}
    assert len(acc.list_clients()) == 2
    assert all(m.client_id for m in ms.list()) and ms.get(1).vnc_view_password == "view1"
    assert ms.get(1).client_id == ms.get(2).client_id != ms.get(3).client_id
    assert ms.active_id() == 1
    # máquina nova (só client_id) não é reatribuída numa nova execução
    n = ms.add(Machine(None, "N", "chy", "1.1.1.9"))
    migrate_machine_clients(ms, acc)
    assert ms.get(n.id).client_id is None
    ms.close(); acc.close()


def test_minutos_invalidos(acc, cliente):
    for m in (0, -5, 481, 1.5, "30", True):
        with pytest.raises(ValueError, match="Duração"):
            acc.grant_support(1, cliente, minutes=m)
    assert acc.grant_support(1, cliente, minutes=480, now=0).expires == 480 * 60


def test_update_user_nome_vazio(acc, cliente):
    with pytest.raises(ValueError, match="Nome vazio"):
        acc.update_user(cliente.id, name="  ")
    assert acc.get_user(cliente.id).name == "Ana"
