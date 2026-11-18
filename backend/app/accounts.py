"""Contas: clientes, usuários, sessões, autorizações de suporte e auditoria (SQLite)."""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .machines import MachineStore
from .security import hash_password, new_token, password_problem, token_hash, verify_password

log = logging.getLogger(__name__)

ROLES = ("mae", "tecnico", "cliente")
SESSION_IDLE = 8 * 3600
SESSION_MAX = 24 * 3600
MAE_PROTEGIDA = "A conta mãe não pode ser desativada nem rebaixada."

SCHEMA = """
CREATE TABLE IF NOT EXISTS clients (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    role TEXT NOT NULL,
    client_id INTEGER,
    active INTEGER NOT NULL DEFAULT 1,
    pw_hash TEXT NOT NULL,
    totp_secret TEXT
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    created REAL NOT NULL,
    last_seen REAL NOT NULL,
    ip TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS support_grants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    machine_id INTEGER NOT NULL,
    by_user_id INTEGER NOT NULL,
    by_name TEXT NOT NULL,
    on_behalf INTEGER NOT NULL DEFAULT 0,
    reason TEXT NOT NULL DEFAULT '',
    starts REAL NOT NULL,
    expires REAL NOT NULL,
    ended_at REAL
);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    time REAL NOT NULL,
    user_id INTEGER,
    user_name TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL DEFAULT '',
    client_id INTEGER,
    action TEXT NOT NULL,
    machine_id INTEGER,
    ip TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT '',
    highlight INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS audit_time ON audit(time);
"""


@dataclass
class Client:
    id: int
    name: str
    active: bool


@dataclass
class User:
    id: int
    username: str
    name: str
    role: str
    client_id: int | None
    active: bool
    has_totp: bool


@dataclass
class Grant:
    id: int
    machine_id: int
    by_user_id: int
    by_name: str
    on_behalf: bool
    reason: str
    starts: float
    expires: float
    ended_at: float | None


_USER_COLS = "id, username, name, role, client_id, active, (totp_secret IS NOT NULL AND totp_secret != '')"
_GRANT_COLS = "id, machine_id, by_user_id, by_name, on_behalf, reason, starts, expires, ended_at"


def _client(r) -> Client:
    return Client(r[0], r[1], bool(r[2]))


def _user(r) -> User:
    return User(r[0], r[1], r[2], r[3], r[4], bool(r[5]), bool(r[6]))


def _grant(r) -> Grant:
    return Grant(r[0], r[1], r[2], r[3], bool(r[4]), r[5], r[6], r[7], r[8])


class AccountStore:
    def __init__(self, path: str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False, timeout=10)
        if path != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)
        self._lock = threading.Lock()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # --- clientes ---
    def add_client(self, name: str) -> Client:
        name = (name or "").strip()
        if not name:
            raise ValueError("Informe o nome do cliente.")
        try:
            with self._lock, self._conn:
                cur = self._conn.execute("INSERT INTO clients(name) VALUES (?)", (name,))
                new_id = cur.lastrowid
        except sqlite3.IntegrityError:
            raise ValueError("Já existe um cliente com esse nome.") from None
        return self.get_client(new_id)

    def list_clients(self) -> list[Client]:
        with self._lock:
            return [_client(r) for r in self._conn.execute(
                "SELECT id, name, active FROM clients ORDER BY name COLLATE NOCASE")]

    def get_client(self, id: int) -> Client | None:
        with self._lock:
            r = self._conn.execute("SELECT id, name, active FROM clients WHERE id=?", (id,)).fetchone()
        return _client(r) if r else None

    def update_client(self, id: int, name: str | None = None, active: bool | None = None) -> Client:
        cur = self.get_client(id)
        if cur is None:
            raise KeyError(id)
        new_name = cur.name if name is None else name.strip()
        if not new_name:
            raise ValueError("Informe o nome do cliente.")
        try:
            with self._lock, self._conn:
                self._conn.execute("UPDATE clients SET name=?, active=? WHERE id=?",
                                   (new_name, int(cur.active if active is None else active), id))
        except sqlite3.IntegrityError:
            raise ValueError("Já existe um cliente com esse nome.") from None
        return self.get_client(id)

    # --- usuários ---
    @staticmethod
    def _check_role(role: str, client_id: int | None) -> None:
        if role not in ROLES:
            raise ValueError(f"Papel inválido: {role!r}.")
        if role == "cliente" and client_id is None:
            raise ValueError("Usuário cliente precisa de um cliente.")
        if role != "cliente" and client_id is not None:
            raise ValueError("Só usuário cliente pode ter cliente.")

    def add_user(self, username: str, name: str, role: str, password: str,
                 client_id: int | None = None, totp_secret: str | None = None) -> User:
        username = (username or "").strip().lower()
        if not username:
            raise ValueError("Informe o login.")
        self._check_role(role, client_id)
        problem = password_problem(password, role)
        if problem:
            raise ValueError(problem)
        if client_id is not None and self.get_client(client_id) is None:
            raise ValueError("Cliente não encontrado.")
        try:
            with self._lock, self._conn:
                cur = self._conn.execute(
                    "INSERT INTO users(username, name, role, client_id, pw_hash, totp_secret)"
                    " VALUES (?,?,?,?,?,?)",
                    (username, (name or username).strip(), role, client_id,
                     hash_password(password), totp_secret or None))
                new_id = cur.lastrowid
        except sqlite3.IntegrityError:
            raise ValueError("Esse login já existe.") from None
        return self.get_user(new_id)

    def get_user(self, id: int) -> User | None:
        with self._lock:
            r = self._conn.execute(f"SELECT {_USER_COLS} FROM users WHERE id=?", (id,)).fetchone()
        return _user(r) if r else None

    def get_by_username(self, username: str) -> User | None:
        with self._lock:
            r = self._conn.execute(f"SELECT {_USER_COLS} FROM users WHERE username=?",
                                   ((username or "").strip().lower(),)).fetchone()
        return _user(r) if r else None

    def list_users(self) -> list[User]:
        with self._lock:
            return [_user(r) for r in self._conn.execute(f"SELECT {_USER_COLS} FROM users ORDER BY id")]

    def update_user(self, id: int, *, name: str | None = None, active: bool | None = None,
                    client_id: int | None = None, role: str | None = None) -> User:
        if name is not None and not name.strip():
            raise ValueError("Nome vazio.")
        with self._lock, self._conn:
            r = self._conn.execute(f"SELECT {_USER_COLS} FROM users WHERE id=?", (id,)).fetchone()
            if r is None:
                raise KeyError(id)
            cur = _user(r)
            if cur.role == "mae" and (active is False or (role is not None and role != "mae")):
                raise PermissionError(MAE_PROTEGIDA)
            new_role = cur.role if role is None else role
            new_client = cur.client_id if client_id is None else client_id
            if role is not None and role != "cliente" and client_id is None:
                new_client = None  # deixou de ser cliente
            self._check_role(new_role, new_client)
            if new_client is not None and not self._conn.execute(
                    "SELECT 1 FROM clients WHERE id=?", (new_client,)).fetchone():
                raise ValueError("Cliente não encontrado.")
            new_active = cur.active if active is None else bool(active)
            self._conn.execute(
                "UPDATE users SET name=?, active=?, client_id=?, role=? WHERE id=?",
                (cur.name if name is None else name.strip(), int(new_active), new_client, new_role, id))
            if not new_active:
                self._conn.execute("DELETE FROM sessions WHERE user_id=?", (id,))
        return self.get_user(id)

    def set_password(self, id: int, pw: str) -> None:
        u = self.get_user(id)
        if u is None:
            raise KeyError(id)
        problem = password_problem(pw, u.role)
        if problem:
            raise ValueError(problem)
        with self._lock, self._conn:
            self._conn.execute("UPDATE users SET pw_hash=? WHERE id=?", (hash_password(pw), id))

    def set_totp(self, id: int, secret: str | None) -> None:
        with self._lock, self._conn:
            self._conn.execute("UPDATE users SET totp_secret=? WHERE id=?", (secret or None, id))

    def totp_secret(self, id: int) -> str | None:
        with self._lock:
            r = self._conn.execute("SELECT totp_secret FROM users WHERE id=?", (id,)).fetchone()
        return (r[0] or None) if r else None

    def check_password(self, username: str, pw: str) -> User | None:
        with self._lock:
            r = self._conn.execute(
                f"SELECT {_USER_COLS}, pw_hash FROM users WHERE username=?",
                ((username or "").strip().lower(),)).fetchone()
        if r is None:
            verify_password(pw, "scrypt$16384$8$1$AAAA$AAAA")  # gasta tempo parecido: não revela se o login existe
            return None
        ok = verify_password(pw, r[7])
        u = _user(r[:7])
        return u if ok and u.active else None

    # --- sessões ---
    def create_session(self, user_id: int, ip: str, now: float | None = None) -> str:
        now = time.time() if now is None else now
        token = new_token()
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO sessions(token_hash, user_id, created, last_seen, ip) VALUES (?,?,?,?,?)",
                (token_hash(token), user_id, now, now, ip))
        return token

    def session_user(self, token: str, now: float | None = None) -> User | None:
        if not token:
            return None
        now = time.time() if now is None else now
        h = token_hash(token)
        with self._lock:
            r = self._conn.execute("SELECT user_id, created, last_seen FROM sessions WHERE token_hash=?",
                                   (h,)).fetchone()
        if r is None:
            return None
        if now - r[1] > SESSION_MAX or now - r[2] > SESSION_IDLE:
            with self._lock, self._conn:
                self._conn.execute("DELETE FROM sessions WHERE token_hash=?", (h,))
            return None
        u = self.get_user(r[0])
        if u is None or not u.active:
            return None
        with self._lock, self._conn:
            self._conn.execute("UPDATE sessions SET last_seen=? WHERE token_hash=?", (now, h))
        return u

    def delete_session(self, token: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash(token or ""),))

    def delete_user_sessions(self, user_id: int) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))

    # --- autorizações de suporte ---
    def grant_support(self, machine_id: int, by: User, minutes: int = 30, on_behalf: bool = False,
                      reason: str = "", now: float | None = None) -> Grant:
        reason = (reason or "").strip()
        if not isinstance(minutes, int) or isinstance(minutes, bool) or not 1 <= minutes <= 480:
            raise ValueError("Duração da autorização inválida.")
        if on_behalf and not reason:
            raise ValueError("Informe o motivo para autorizar em nome do cliente.")
        now = time.time() if now is None else now
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT INTO support_grants(machine_id, by_user_id, by_name, on_behalf, reason, starts, expires)"
                " VALUES (?,?,?,?,?,?,?)",
                (machine_id, by.id, by.name, int(on_behalf), reason, now, now + minutes * 60))
            gid = cur.lastrowid
            r = self._conn.execute(f"SELECT {_GRANT_COLS} FROM support_grants WHERE id=?", (gid,)).fetchone()
        return _grant(r)

    def active_grant(self, machine_id: int, now: float | None = None) -> Grant | None:
        now = time.time() if now is None else now
        with self._lock:
            r = self._conn.execute(
                f"SELECT {_GRANT_COLS} FROM support_grants WHERE machine_id=? AND ended_at IS NULL "
                "AND starts<=? AND expires>? ORDER BY expires DESC LIMIT 1", (machine_id, now, now)).fetchone()
        return _grant(r) if r else None

    def end_support(self, machine_id: int, by: User, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE support_grants SET ended_at=? WHERE machine_id=? AND ended_at IS NULL AND expires>?",
                (now, machine_id, now))
        return cur.rowcount > 0

    # --- auditoria ---
    def log(self, action: str, user: User | None, *, machine_id: int | None = None,
            client_id: int | None = None, ip: str = "", detail: str = "", highlight: bool = False,
            now: float | None = None) -> None:
        """Nunca levanta: falha de auditoria não pode derrubar a ação."""
        try:
            now = time.time() if now is None else now
            if client_id is None and user is not None:
                client_id = user.client_id
            with self._lock, self._conn:
                self._conn.execute(
                    "INSERT INTO audit(time, user_id, user_name, role, client_id, action, machine_id, ip,"
                    " detail, highlight) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (now, user.id if user else None, user.name if user else "", user.role if user else "",
                     client_id, action, machine_id, ip, detail, int(highlight)))
        except Exception as exc:
            log.warning("falha ao gravar auditoria (%s): %s", action, exc)

    def audit(self, *, user_id: int | None = None, client_id: int | None = None,
              machine_id: int | None = None, since: float | None = None, until: float | None = None,
              limit: int = 500) -> list[dict]:
        where, args = [], []
        for col, val in (("user_id", user_id), ("client_id", client_id), ("machine_id", machine_id)):
            if val is not None:
                where.append(f"{col}=?")
                args.append(val)
        if since is not None:
            where.append("time>=?")
            args.append(since)
        if until is not None:
            where.append("time<=?")
            args.append(until)
        sql = ("SELECT time, user_name, role, client_id, action, machine_id, ip, detail, highlight FROM audit"
               + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY time DESC, id DESC LIMIT ?")
        with self._lock:
            rows = self._conn.execute(sql, [*args, limit]).fetchall()
        keys = ("time", "user_name", "role", "client_id", "action", "machine_id", "ip", "detail")
        return [{**dict(zip(keys, r[:8])), "highlight": bool(r[8])} for r in rows]


def migrate_machine_clients(machines: MachineStore, accounts: AccountStore) -> None:
    """Cada texto distinto de `machines.client` vira um Client e preenche client_id. Roda uma vez."""
    if machines.get_flag("clients_migrated") == "1":
        return
    existing = {c.name.lower(): c.id for c in accounts.list_clients()}
    mapping: dict[int, int] = {}
    for mid, text in machines.legacy_client_names():
        name = (text or "").strip() or "Chen Hsong"
        if name.lower() not in existing:
            existing[name.lower()] = accounts.add_client(name).id
        mapping[mid] = existing[name.lower()]
    machines.assign_clients(mapping)
    machines.set_flag("clients_migrated", "1")
