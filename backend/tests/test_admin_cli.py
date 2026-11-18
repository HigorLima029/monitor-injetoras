import io
import time

from app import admin
from app.accounts import AccountStore
from app.security import totp_code, verify_totp

SENHA = "senha-da-mae-123"


def fake_getpass(*senhas):
    it = iter(senhas)
    return lambda prompt="": next(it)


def run(argv, store, senhas=(), codigo="", out=None):
    out = out or io.StringIO()
    rc = admin.main(argv, getpass=fake_getpass(*senhas), out=out, store=store, ask=lambda p="": codigo)
    return rc, out.getvalue()


def secret_from(output):
    for line in output.splitlines():
        if line.lower().startswith("segredo"):
            return line.split(":", 1)[1].strip()
    raise AssertionError("segredo não impresso")


def test_criar_mae_imprime_segredo_e_exige_codigo():
    store = AccountStore(":memory:")
    # primeira rodada para descobrir o segredo: código errado, nada gravado
    rc, out = run(["criar-mae", "--usuario", "higor", "--nome", "Higor"], store,
                  senhas=(SENHA, SENHA), codigo="000000")
    assert rc != 0
    assert store.get_by_username("higor") is None
    assert SENHA not in out
    assert "otpauth://" in out and "Google Authenticator" in out

    # segunda rodada: lê o segredo impresso e responde com o código certo
    buf = io.StringIO()
    holder = {}

    def ask(prompt=""):
        holder["secret"] = secret_from(buf.getvalue())
        return totp_code(holder["secret"], time.time())

    rc = admin.main(["criar-mae", "--usuario", "higor", "--nome", "Higor"],
                    getpass=fake_getpass(SENHA, SENHA), out=buf, store=store, ask=ask)
    assert rc == 0
    u = store.get_by_username("higor")
    assert u.role == "mae" and u.has_totp
    assert store.totp_secret(u.id) == holder["secret"]
    assert store.check_password("higor", SENHA) is not None


def test_segunda_mae_recusada():
    store = AccountStore(":memory:")
    store.add_user("higor", "Higor", "mae", SENHA)
    rc, out = run(["criar-mae", "--usuario", "outro", "--nome", "Outro"], store,
                  senhas=(SENHA, SENHA))
    assert rc == 1
    assert store.get_by_username("outro") is None
    assert "já existe" in out.lower()


def test_criar_mae_senhas_diferentes_ou_fracas():
    store = AccountStore(":memory:")
    rc, _ = run(["criar-mae", "--usuario", "a", "--nome", "A"], store, senhas=(SENHA, SENHA + "x"))
    assert rc == 1
    rc, out = run(["criar-mae", "--usuario", "a", "--nome", "A"], store, senhas=("curta", "curta"))
    assert rc == 1 and "12" in out
    assert store.list_users() == []


def test_resetar_senha_derruba_sessoes():
    store = AccountStore(":memory:")
    u = store.add_user("tec", "Téc", "tecnico", "senha-antiga-1")
    token = store.create_session(u.id, "1.2.3.4")
    rc, _ = run(["resetar-senha", "--usuario", "tec"], store, senhas=("senha-nova-123", "senha-nova-123"))
    assert rc == 0
    assert store.session_user(token) is None
    assert store.check_password("tec", "senha-nova-123") is not None
    # política do perfil: mãe exige 12
    store.add_user("mae", "Mãe", "mae", SENHA)
    rc, out = run(["resetar-senha", "--usuario", "mae"], store, senhas=("curta123", "curta123"))
    assert rc == 1 and "12" in out
    rc, out = run(["resetar-senha", "--usuario", "ninguem"], store, senhas=("x", "x"))
    assert rc == 1 and "não encontrado" in out.lower()


def test_resetar_2fa():
    store = AccountStore(":memory:")
    u = store.add_user("mae", "Mãe", "mae", SENHA)
    store.set_totp(u.id, "VELHOSEGREDO")
    buf = io.StringIO()

    def ask(prompt=""):
        return totp_code(secret_from(buf.getvalue()), time.time())

    assert admin.main(["resetar-2fa", "--usuario", "mae"], getpass=fake_getpass(), out=buf,
                      store=store, ask=ask) == 0
    novo = store.totp_secret(u.id)
    assert novo != "VELHOSEGREDO" and verify_totp(novo, totp_code(novo, time.time()))
    # código errado: mantém o segredo antigo
    rc, _ = run(["resetar-2fa", "--usuario", "mae"], store, codigo="000000")
    assert rc == 1 and store.totp_secret(u.id) == novo
    rc, out = run(["resetar-2fa", "--usuario", "x"], store)
    assert rc == 1 and "não encontrado" in out.lower()
