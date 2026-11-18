"""Login, sessão por cookie, 2º fator (TOTP) e proteção das rotas e WebSockets.

Nunca registrar em log senhas, tickets, códigos ou tokens.
"""
from __future__ import annotations

import asyncio
import hmac
import logging
import math
import secrets
import threading
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request, WebSocket
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from .accounts import AccountStore, User
from .config import settings
from .security import totp_code

log = logging.getLogger(__name__)

COOKIE = "monitor_sessao"
SWEEP_SECONDS = 5.0  # intervalo da varredura que derruba WebSockets de sessões que acabaram
TICKET_TTL = 300.0
PUBLIC_API = frozenset({"/api/login", "/api/login/code"})
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

LOGIN_ERRADO = "Usuário ou senha incorretos."
CODIGO_ERRADO = "Senha ou código incorretos."
FACA_LOGIN = "Faça login."
ORIGEM_PROIBIDA = "Origem não permitida."
MAE_SEM_TOTP = "Configure o 2º fator pelo comando do servidor."
WS_SEM_LOGIN = 4401
WS_ORIGEM = 4403


# ---- limite de tentativas ---------------------------------------------------
@dataclass
class _Failures:
    count: int = 0
    last: float = 0.0


class LoginLimiter:
    """Espera crescente por login tentado e por IP: depois de `max_failures` falhas,
    `base_wait` s, dobrando a cada nova falha, até `max_wait`. O IP tolera mais falhas
    (várias pessoas atrás do mesmo NAT) antes de começar a esperar."""

    IP_FACTOR = 4
    MAX_KEYS = 10000

    def __init__(self, max_failures: int = 5, base_wait: float = 30, max_wait: float = 900):
        self.max_failures = max_failures
        self.base_wait = base_wait
        self.max_wait = max_wait
        self._users: dict[str, _Failures] = {}
        self._ips: dict[str, _Failures] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _norm(username: str) -> str:
        return (username or "").strip().lower()

    def _wait(self, f: _Failures | None, threshold: int, now: float) -> float:
        if f is None or f.count < threshold:
            return 0.0
        wait = min(self.base_wait * 2 ** (f.count - threshold), self.max_wait)
        return max(0.0, f.last + wait - now)

    def _forget_old(self, table: dict[str, _Failures], now: float) -> None:
        if len(table) <= self.MAX_KEYS:
            return
        for k in [k for k, f in table.items() if now - f.last > self.max_wait]:
            del table[k]

    def wait_for(self, username: str, ip: str, now: float | None = None) -> float:
        now = time.time() if now is None else now
        with self._lock:
            return max(self._wait(self._users.get(self._norm(username)), self.max_failures, now),
                       self._wait(self._ips.get(ip), self.max_failures * self.IP_FACTOR, now))

    def failure(self, username: str, ip: str, now: float | None = None) -> None:
        now = time.time() if now is None else now
        with self._lock:
            for table, key in ((self._users, self._norm(username)), (self._ips, ip)):
                f = table.get(key)
                if f is None or now - f.last > self.max_wait * 2:  # falhas antigas caducam
                    f = table[key] = _Failures()
                f.count += 1
                f.last = now
                self._forget_old(table, now)

    def success(self, username: str, ip: str) -> None:
        """Zera só o login: um acerto não limpa as falhas acumuladas pelo IP."""
        with self._lock:
            self._users.pop(self._norm(username), None)


# ---- ticket do 2º fator e reuso de código --------------------------------------
class TicketStore:
    """Ticket aleatório, em memória, de uso único, ligado a um usuário; vale `ttl` s."""

    def __init__(self, ttl: float = TICKET_TTL):
        self.ttl = ttl
        self._t: dict[str, tuple[int, float]] = {}
        self._lock = threading.Lock()

    def issue(self, user_id: int, now: float | None = None) -> str:
        now = time.time() if now is None else now
        ticket = secrets.token_urlsafe(32)
        with self._lock:
            for k in [k for k, (_, exp) in self._t.items() if exp <= now]:
                del self._t[k]
            self._t[ticket] = (user_id, now + self.ttl)
        return ticket

    def peek(self, ticket: str, now: float | None = None) -> int | None:
        now = time.time() if now is None else now
        with self._lock:
            got = self._t.get(ticket or "")
            if got is None:
                return None
            if got[1] <= now:
                del self._t[ticket]
                return None
            return got[0]

    def consume(self, ticket: str) -> None:
        with self._lock:
            self._t.pop(ticket or "", None)


class TotpGuard:
    """Aceita o código do passo atual (±window) só se o passo for mais novo que o último aceito
    daquele usuário: o mesmo código não serve duas vezes."""

    def __init__(self):
        self._last: dict[int, int] = {}
        self._lock = threading.Lock()

    def check(self, user_id: int, secret: str, code: str, now: float | None = None, window: int = 1) -> bool:
        code = (code or "").strip()
        if len(code) != 6 or not (code.isascii() and code.isdigit()):
            return False
        now = time.time() if now is None else now
        base = int(now // 30)
        matched = None
        for step in range(base - window, base + window + 1):
            if hmac.compare_digest(totp_code(secret, step * 30), code):
                matched = step
        if matched is None:
            return False
        with self._lock:
            if matched <= self._last.get(user_id, -1):
                return False
            self._last[user_id] = matched
        return True


class AuthState:
    """Estado em memória do login, um por app (criado no lifespan)."""

    def __init__(self):
        self.limiter = LoginLimiter()
        self.tickets = TicketStore()
        self.totp = TotpGuard()


def _state(app) -> AuthState:
    st = getattr(app.state, "auth", None)
    if st is None:
        st = app.state.auth = AuthState()
    return st


# ---- WebSockets abertos por sessão -------------------------------------------
_open: dict[str, set[WebSocket]] = {}


def track(token: str, socket: WebSocket) -> None:
    _open.setdefault(token, set()).add(socket)


def untrack(token: str, socket: WebSocket) -> None:
    socks = _open.get(token)
    if socks is None:
        return
    socks.discard(socket)
    if not socks:
        _open.pop(token, None)


async def close_token(token: str) -> None:
    for s in list(_open.pop(token, ())):
        try:
            await s.close(code=WS_SEM_LOGIN, reason="Sessão encerrada")
        except Exception:  # já fechado
            pass


async def sweep_once(accounts: AccountStore) -> None:
    for token in list(_open):
        if accounts.session_user(token) is None:
            await close_token(token)


async def sweeper(accounts: AccountStore) -> None:
    while True:
        await asyncio.sleep(SWEEP_SECONDS)
        try:
            await sweep_once(accounts)
        except Exception as exc:
            log.warning("falha na varredura de sessões: %s", type(exc).__name__)


# ---- helpers ------------------------------------------------------------------
def _accounts(app) -> AccountStore:
    return app.state.accounts


def _ip(conn) -> str:
    return conn.client.host if conn.client else ""


def _default_port(scheme: str) -> str:
    return "443" if scheme in ("https", "wss") else "80"


def _hostport(netloc: str, scheme: str) -> str:
    netloc = (netloc or "").strip().lower()
    if netloc.startswith("["):  # IPv6
        host, _, rest = netloc.partition("]")
        host += "]"
        port = rest[1:] if rest.startswith(":") else ""
    else:
        host, _, port = netloc.partition(":")
    return f"{host}:{port or _default_port(scheme)}"


def origin_ok(headers, scheme: str) -> bool:
    """Sem `Origin`: ok (clientes que não são navegador). Com ele: host:porta = Host do pedido."""
    origin = headers.get("origin")
    if origin is None:
        return True
    try:
        o = urlsplit(origin)
    except ValueError:
        return False
    if o.scheme not in ("http", "https") or not o.netloc:
        return False
    host = headers.get("host", "")
    return _hostport(o.netloc, o.scheme) == _hostport(host, scheme)


def me(accounts: AccountStore, u: User) -> dict:
    client_name = ""
    if u.client_id is not None:
        c = accounts.get_client(u.client_id)
        client_name = c.name if c else ""
    return {"id": u.id, "username": u.username, "name": u.name, "role": u.role,
            "client_id": u.client_id, "client_name": client_name, "has_totp": u.has_totp}


# ---- dependências ---------------------------------------------------------------
def current_user(request: Request) -> User:
    u = request.scope.get("state", {}).get("user")
    if u is None:
        u = _accounts(request.app).session_user(request.cookies.get(COOKIE, ""))
    if u is None:
        raise HTTPException(401, FACA_LOGIN)
    return u


async def ws_user(websocket: WebSocket) -> User | None:
    """Lê o cookie ANTES do accept. Sem sessão: aceita só para fechar com 4401 (o navegador
    precisa do handshake completo para ver o código); nada do app roda para esse socket."""
    token = websocket.cookies.get(COOKIE, "")
    u = None
    code = WS_SEM_LOGIN
    if not origin_ok(websocket.headers, websocket.url.scheme):
        code = WS_ORIGEM
    else:
        u = _accounts(websocket.app).session_user(token) if token else None
    if u is not None:
        websocket.state.session_token = token
        return u
    offered = websocket.headers.get("sec-websocket-protocol", "")
    await websocket.accept(subprotocol="binary" if "binary" in offered else None)
    await websocket.close(code=code, reason=FACA_LOGIN if code == WS_SEM_LOGIN else ORIGEM_PROIBIDA)
    return None


# ---- middleware: Origin + sessão em /api/* --------------------------------------
class AuthMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        if scope["method"] in UNSAFE_METHODS and not origin_ok(request.headers, scope.get("scheme", "http")):
            await JSONResponse({"detail": ORIGEM_PROIBIDA}, status_code=403)(scope, receive, send)
            return
        path = scope["path"]
        if path.startswith("/api/") and path not in PUBLIC_API:
            token = request.cookies.get(COOKIE, "")
            u = _accounts(scope["app"]).session_user(token) if token else None
            if u is None:
                await JSONResponse({"detail": FACA_LOGIN}, status_code=401)(scope, receive, send)
                return
            scope.setdefault("state", {})["user"] = u
        await self.app(scope, receive, send)


# ---- rotas ------------------------------------------------------------------------
router = APIRouter()


class LoginIn(BaseModel):
    username: str = Field(max_length=200)
    password: str = Field(max_length=1024)


class CodeIn(BaseModel):
    ticket: str = Field(max_length=200)
    code: str = Field(max_length=20)


def _muitas(wait: float) -> HTTPException:
    return HTTPException(429, f"Muitas tentativas. Tente de novo em {max(1, math.ceil(wait))} s.")


def _start_session(request: Request, accounts: AccountStore, u: User) -> JSONResponse:
    ip = _ip(request)
    token = accounts.create_session(u.id, ip)
    _state(request.app).limiter.success(u.username, ip)
    accounts.log("login", u, ip=ip)
    resp = JSONResponse({"user": me(accounts, u)})
    resp.set_cookie(COOKIE, token, httponly=True, secure=settings.cookie_secure,
                    samesite="strict", path="/")
    return resp


@router.post("/api/login")
def login(body: LoginIn, request: Request):
    accounts = _accounts(request.app)
    limiter = _state(request.app).limiter
    ip = _ip(request)
    wait = limiter.wait_for(body.username, ip)
    if wait > 0:
        raise _muitas(wait)
    u = accounts.check_password(body.username, body.password)
    if u is None:
        limiter.failure(body.username, ip)
        accounts.log("login_falhou", None, ip=ip, detail=(body.username or "").strip()[:100])
        raise HTTPException(401, LOGIN_ERRADO)
    if u.has_totp:
        return {"need_code": True, "ticket": _state(request.app).tickets.issue(u.id)}
    if u.role == "mae":
        raise HTTPException(403, MAE_SEM_TOTP)
    return _start_session(request, accounts, u)


@router.post("/api/login/code")
def login_code(body: CodeIn, request: Request):
    accounts = _accounts(request.app)
    st = _state(request.app)
    limiter, tickets = st.limiter, st.tickets
    ip = _ip(request)
    uid = tickets.peek(body.ticket)
    u = accounts.get_user(uid) if uid is not None else None
    if u is None or not u.active:
        raise HTTPException(401, "Entre de novo com usuário e senha.")
    wait = limiter.wait_for(u.username, ip)
    if wait > 0:
        raise _muitas(wait)
    secret = accounts.totp_secret(u.id)
    if not secret or not st.totp.check(u.id, secret, body.code):
        limiter.failure(u.username, ip)
        accounts.log("login_falhou", u, ip=ip, detail=f"{u.username} (código)")
        raise HTTPException(401, CODIGO_ERRADO)
    tickets.consume(body.ticket)
    return _start_session(request, accounts, u)


@router.post("/api/logout", status_code=204)
async def logout(request: Request):
    u = current_user(request)
    accounts = _accounts(request.app)
    token = request.cookies.get(COOKIE, "")
    accounts.delete_session(token)
    accounts.log("logout", u, ip=_ip(request))
    await close_token(token)
    resp = Response(status_code=204)
    resp.delete_cookie(COOKIE, path="/", secure=settings.cookie_secure, httponly=True, samesite="strict")
    return resp


@router.get("/api/me")
def get_me(request: Request):
    u = current_user(request)
    return me(_accounts(request.app), u)
