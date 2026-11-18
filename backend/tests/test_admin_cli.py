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


def _ask_ok(buf):
    return lambda p="": totp_code(secret_from(buf.getvalue()), time.time())


def test_criar_mae_atomica_se_gravacao_falha(monkeypatch):
    store = AccountStore(":memory:")

    def boom(*a, **k):
        raise RuntimeError("falha")

    monkeypatch.setattr(store, "set_totp", boom)  # não pode ser usado em criar-mae
    buf = io.StringIO()
    rc = admin.main(["criar-mae", "--usuario", "higor", "--nome", "H"],
                    getpass=fake_getpass(SENHA, SENHA), out=buf, store=store, ask=_ask_ok(buf))
    assert rc == 0
    u = store.get_by_username("higor")
    assert u.has_totp  # usuário e 2FA gravados juntos

    store2 = AccountStore(":memory:")
    # falha no add_user: nada fica gravado e a mensagem é impressa
    monkeypatch.setattr(store2, "add_user", lambda *a, **k: (_ for _ in ()).throw(ValueError("Erro x.")))
    buf = io.StringIO()
    rc = admin.main(["criar-mae", "--usuario", "h", "--nome", "H"],
                    getpass=fake_getpass(SENHA, SENHA), out=buf, store=store2, ask=_ask_ok(buf))
    assert rc == 1 and "Erro x." in buf.getvalue() and store2.list_users() == []


def test_add_user_com_totp_e_atomico():
    store = AccountStore(":memory:")
    u = store.add_user("m", "M", "mae", SENHA, totp_secret="ABC")
    assert store.totp_secret(u.id) == "ABC"
    try:
        store.add_user("m", "M", "mae", SENHA, totp_secret="XYZ")
    except ValueError:
        pass
    assert store.totp_secret(u.id) == "ABC" and len(store.list_users()) == 1


def test_nome_vazio_e_login_normalizado():
    store = AccountStore(":memory:")
    rc, out = run(["criar-mae", "--usuario", "x", "--nome", "  "], store, senhas=(SENHA, SENHA))
    assert rc == 1 and "nome" in out.lower() and store.list_users() == []
    store.add_user("higor", "H", "tecnico", "senha-123456")
    rc, out = run(["criar-mae", "--usuario", " Higor ", "--nome", "H"], store, senhas=(SENHA, SENHA))
    assert rc == 1 and "já existe" in out.lower()
    # label do otpauth em minúsculas
    buf = io.StringIO()
    admin.main(["criar-mae", "--usuario", "MAE1", "--nome", "M"], getpass=fake_getpass(SENHA, SENHA),
               out=buf, store=store, ask=_ask_ok(buf))
    assert "mae1" in buf.getvalue() and "MAE1" not in buf.getvalue()
    assert store.get_by_username("mae1").role == "mae"


def test_cancelar_nao_grava():
    store = AccountStore(":memory:")

    def interrompe(p=""):
        raise KeyboardInterrupt

    out = io.StringIO()
    rc = admin.main(["criar-mae", "--usuario", "h", "--nome", "H"], getpass=fake_getpass(SENHA, SENHA),
                    out=out, store=store, ask=interrompe)
    assert rc == 1 and "Cancelado." in out.getvalue() and store.list_users() == []

    def eof(*a):
        raise EOFError

    out = io.StringIO()
    rc = admin.main(["criar-mae", "--usuario", "h", "--nome", "H"], getpass=eof, out=out, store=store)
    assert rc == 1 and "Cancelado." in out.getvalue() and store.list_users() == []
