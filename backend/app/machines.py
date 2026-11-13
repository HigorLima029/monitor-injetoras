"""Cadastro de máquinas em SQLite (substitui a edição do .env para escolher a injetora)."""
from __future__ import annotations

import shutil
import sqlite3
import threading
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from urllib.parse import urlparse

KINDS = ("chy", "em63", "ads", "demo")
_PROFILE = {"chy": "chy", "em63": "em63", "ads": "ads", "demo": "chy"}
_NAMESPACE = {"chy": 1, "em63": 6, "ads": 1, "demo": 1}
_SECRETS = ("vnc_view_password", "vnc_control_password")

SCHEMA = """
CREATE TABLE IF NOT EXISTS machines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    host TEXT NOT NULL,
    client TEXT NOT NULL DEFAULT 'Chen Hsong',
    opcua_port INTEGER NOT NULL DEFAULT 4840,
    namespace INTEGER,
    ams_net_id TEXT NOT NULL DEFAULT '',
    ads_port INTEGER NOT NULL DEFAULT 801,
    vnc_enabled INTEGER,
    vnc_port INTEGER NOT NULL DEFAULT 5900,
    vnc_view_password TEXT NOT NULL DEFAULT '',
    vnc_control_password TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
"""


@dataclass
class Machine:
    id: int | None
    name: str
    kind: str
    host: str
    client: str = "Chen Hsong"
    opcua_port: int = 4840
    namespace: int | None = None  # None = padrão do tipo
    ams_net_id: str = ""
    ads_port: int = 801
    vnc_enabled: bool | None = None  # None = padrão do tipo (só em63)
    vnc_port: int = 5900
    vnc_view_password: str = ""
    vnc_control_password: str = ""
    notes: str = ""

    @property
    def profile(self) -> str:
        return _PROFILE.get(self.kind, "chy")

    @property
    def opcua_url(self) -> str:
        return f"opc.tcp://{self.host}:{self.opcua_port}"

    @property
    def status_port(self) -> int:
        return 48898 if self.kind == "ads" else self.opcua_port

    @property
    def effective_namespace(self) -> int:
        return self.namespace if self.namespace is not None else _NAMESPACE.get(self.kind, 1)

    @property
    def effective_vnc_enabled(self) -> bool:
        return self.vnc_enabled if self.vnc_enabled is not None else self.kind == "em63"

    def public(self) -> dict:
        """Visão segura para o navegador: nunca inclui as senhas VNC."""
        d = {k: v for k, v in asdict(self).items() if k not in _SECRETS}
        d["namespace"] = self.effective_namespace
        d["vnc_enabled"] = self.effective_vnc_enabled
        d["has_view_password"] = bool(self.vnc_view_password)
        d["has_control_password"] = bool(self.vnc_control_password)
        d["profile"] = self.profile
        d["opcua_url"] = self.opcua_url
        return d


_FIELDS = [f.name for f in fields(Machine) if f.name != "id"]


def _validate(m: Machine) -> None:
    if m.kind not in KINDS:
        raise ValueError(f"tipo desconhecido: {m.kind!r}")
    if not (m.host or "").strip():
        raise ValueError("host vazio")
    for nome in ("opcua_port", "ads_port", "vnc_port"):
        p = getattr(m, nome)
        if not isinstance(p, int) or isinstance(p, bool) or not 1 <= p <= 65535:
            raise ValueError(f"porta inválida em {nome}: {p!r}")


def _row_to_machine(row: sqlite3.Row | tuple) -> Machine:
    d = dict(zip(["id", *_FIELDS], row))
    if d["vnc_enabled"] is not None:
        d["vnc_enabled"] = bool(d["vnc_enabled"])
    return Machine(**d)


class MachineStore:
    def __init__(self, path: str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.executescript(SCHEMA)
        self._lock = threading.Lock()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    _COLS = ", ".join(["id", *_FIELDS])

    def list(self) -> list[Machine]:
        with self._lock:
            rows = self._conn.execute(f"SELECT {self._COLS} FROM machines ORDER BY id").fetchall()
        return [_row_to_machine(r) for r in rows]

    def get(self, id: int) -> Machine | None:
        with self._lock:
            row = self._conn.execute(f"SELECT {self._COLS} FROM machines WHERE id=?", (id,)).fetchone()
        return _row_to_machine(row) if row else None

    def add(self, m: Machine) -> Machine:
        _validate(m)
        cols = ", ".join(_FIELDS)
        marks = ", ".join("?" * len(_FIELDS))
        vals = [getattr(m, f) for f in _FIELDS]
        with self._lock, self._conn:
            cur = self._conn.execute(f"INSERT INTO machines({cols}) VALUES ({marks})", vals)
            new_id = cur.lastrowid
        return self.get(new_id)

    def update(self, id: int, changes: dict) -> Machine:
        cur = self.get(id)
        if cur is None:
            raise KeyError(id)
        data = {k: v for k, v in changes.items() if k in _FIELDS}
        for s in _SECRETS:  # senha vazia ou ausente = mantém a atual
            if not data.get(s):
                data.pop(s, None)
        new = Machine(**{**asdict(cur), **data})
        _validate(new)
        sets = ", ".join(f"{f}=?" for f in _FIELDS)
        with self._lock, self._conn:
            self._conn.execute(
                f"UPDATE machines SET {sets} WHERE id=?", [getattr(new, f) for f in _FIELDS] + [id]
            )
        return self.get(id)

    def delete(self, id: int) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM machines WHERE id=?", (id,))
            self._conn.execute("DELETE FROM kv WHERE key='active_id' AND value=?", (str(id),))

    def active_id(self) -> int | None:
        with self._lock:
            row = self._conn.execute("SELECT value FROM kv WHERE key='active_id'").fetchone()
        return int(row[0]) if row else None

    def set_active(self, id: int) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO kv(key, value) VALUES ('active_id', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(id),),
            )


def seed_from_settings(store: MachineStore, settings, machines_dir: str) -> None:
    """Cadastro vazio: cria a máquina descrita no .env (ou demo com SIMULATE=1) e migra o histórico."""
    if store.list():
        return
    u = urlparse(settings.opcua_url)
    host = u.hostname or ""
    port = u.port or 4840
    if settings.simulate:
        kind = "demo"
    else:
        kind = settings.profile if settings.profile in ("chy", "em63", "ads") else "chy"
    ns = settings.namespace if settings.namespace != 1 else None
    m = store.add(Machine(
        None, settings.machine_name, kind, host,
        opcua_port=port, namespace=ns,
        vnc_enabled=getattr(settings, "vnc_enabled", None),
        vnc_port=getattr(settings, "vnc_port", 5900),
        vnc_view_password=getattr(settings, "vnc_view_password", ""),
        vnc_control_password=getattr(settings, "vnc_control_password", ""),
    ))
    store.set_active(m.id)
    mdir = Path(machines_dir)
    mdir.mkdir(parents=True, exist_ok=True)
    old_db = Path(settings.db_path)
    for src, dst in (
        (old_db, mdir / f"{m.id}.db"),
        (old_db.with_name("tags_cache.json"), mdir / f"{m.id}_tags.json"),
    ):
        if src.exists() and not dst.exists():
            shutil.copy2(src, dst)
