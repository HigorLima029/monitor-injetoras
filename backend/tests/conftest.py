"""Fixtures comuns: banco de contas em memória e cliente HTTP já logado (`client_as`)."""
from __future__ import annotations

import itertools
import time
from contextlib import ExitStack

import pytest
from fastapi.testclient import TestClient

from app import main
from app.accounts import AccountStore
from app.security import new_totp_secret, totp_code

SENHA_TESTE = "senha-de-teste-123"
BASE_URL = "https://testserver"  # https: o cookie de sessão é Secure


class HttpsClient(TestClient):
    """TestClient em https/wss: o cookie Secure vai também no WebSocket."""

    def __init__(self, app=None, **kw):
        kw.setdefault("base_url", BASE_URL)
        super().__init__(app or main.app, **kw)

    def websocket_connect(self, url, *args, **kw):
        if url.startswith("/"):
            url = "wss://testserver" + url
        return super().websocket_connect(url, *args, **kw)


@pytest.fixture(autouse=True)
def accounts():
    """Cada teste tem o seu AccountStore em memória (o app nunca abre o banco real)."""
    store = AccountStore(":memory:")
    main.app.state.accounts = store
    try:
        yield store
    finally:
        if getattr(main.app.state, "accounts", None) is store:
            del main.app.state.accounts
        store.close()


def login_user(client, accounts: AccountStore, username: str, password: str = SENHA_TESTE):
    """Faz o login completo (com o passo do código, se o usuário tiver TOTP)."""
    r = client.post("/api/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    body = r.json()
    if body.get("need_code"):
        u = accounts.get_by_username(username)
        code = totp_code(accounts.totp_secret(u.id), time.time())
        r = client.post("/api/login/code", json={"ticket": body["ticket"], "code": code})
        assert r.status_code == 200, r.text
    return r


@pytest.fixture
def client_as(accounts):
    """`client_as(role, client_id=None) -> TestClient` logado como um usuário novo daquele papel.

    A primeira chamada liga o app (lifespan) com o `main.app.state.manager` que o teste montou;
    as seguintes criam outros navegadores (cookies próprios) no mesmo app.
    """
    stack = ExitStack()
    base: list[TestClient] = []
    seq = itertools.count(1)

    def make(role: str, client_id: int | None = None) -> HttpsClient:
        c = HttpsClient()
        if not base:
            stack.enter_context(c)
            base.append(c)
        else:
            c.portal = base[0].portal  # mesmo loop do app já ligado
        n = next(seq)
        if role == "cliente" and client_id is None:
            client_id = accounts.add_client(f"Cliente teste {n}").id
        accounts.add_user(f"{role}{n}", f"{role.title()} {n}", role, SENHA_TESTE, client_id=client_id,
                          totp_secret=new_totp_secret() if role == "mae" else None)
        login_user(c, accounts, f"{role}{n}")
        return c

    try:
        yield make
    finally:
        stack.close()
