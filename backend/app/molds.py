"""Cadastro de moldes e histórico de troca de molde por máquina.

O contador da máquina conta ciclos (injeções). As peças são ciclos x cavidades ativas do
molde que está montado *naquele momento*, por isso a troca de molde fica registrada: cada
montagem guarda uma cópia do molde (código, nome, cavidades) do jeito que estava ao montar.
"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass

MAX_CAVITIES = 512

MOLD_SCHEMA = """
CREATE TABLE IF NOT EXISTS molds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL DEFAULT '',
    cavities INTEGER NOT NULL DEFAULT 1,
    active_cavities INTEGER NOT NULL DEFAULT 1,
    notes TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS mold_mounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    machine_id INTEGER NOT NULL,
    mold_id INTEGER,
    mold_code TEXT NOT NULL,
    mold_name TEXT NOT NULL DEFAULT '',
    active_cavities INTEGER NOT NULL,
    mounted_at REAL NOT NULL,
    removed_at REAL
);
CREATE INDEX IF NOT EXISTS ix_mounts_machine ON mold_mounts(machine_id, mounted_at);
"""


@dataclass
class Mold:
    id: int | None
    code: str
    name: str = ""
    cavities: int = 1
    active_cavities: int | None = None  # None = todas as cavidades
    notes: str = ""

    def public(self) -> dict:
        return asdict(self)


_MOLD_FIELDS = ("code", "name", "cavities", "active_cavities", "notes")


def _int(v, nome: str) -> int:
    if isinstance(v, bool) or not isinstance(v, int):
        raise ValueError(f"{nome} precisa ser um número inteiro")
    return v


def _validate_mold(m: Mold) -> Mold:
    m.code = (m.code or "").strip()
    m.name = (m.name or "").strip()
    if not m.code:
        raise ValueError("código do molde vazio")
    cav = _int(m.cavities, "cavidades")
    if not 1 <= cav <= MAX_CAVITIES:
        raise ValueError(f"cavidades deve ficar entre 1 e {MAX_CAVITIES}")
    act = cav if m.active_cavities is None else _int(m.active_cavities, "cavidades ativas")
    if not 1 <= act <= cav:
        raise ValueError("cavidades ativas deve ficar entre 1 e o total de cavidades")
    m.active_cavities = act
    return m


def _mount_dict(row) -> dict:
    keys = ("id", "machine_id", "mold_id", "mold_code", "mold_name", "active_cavities",
            "mounted_at", "removed_at")
    return dict(zip(keys, row))


class MoldMixin:
    """Métodos de moldes para o MachineStore (mesma conexão e mesma trava)."""

    _MOUNT_COLS = "id, machine_id, mold_id, mold_code, mold_name, active_cavities, mounted_at, removed_at"

    # ---- moldes ---------------------------------------------------------
    def list_molds(self) -> list[Mold]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, code, name, cavities, active_cavities, notes FROM molds ORDER BY code"
            ).fetchall()
        return [Mold(*r) for r in rows]

    def get_mold(self, id: int) -> Mold | None:
        with self._lock:
            r = self._conn.execute(
                "SELECT id, code, name, cavities, active_cavities, notes FROM molds WHERE id=?", (id,)
            ).fetchone()
        return Mold(*r) if r else None

    def add_mold(self, m: Mold) -> Mold:
        m = _validate_mold(m)
        try:
            with self._lock, self._conn:
                cur = self._conn.execute(
                    "INSERT INTO molds(code, name, cavities, active_cavities, notes) VALUES (?,?,?,?,?)",
                    (m.code, m.name, m.cavities, m.active_cavities, m.notes or ""),
                )
        except Exception as exc:
            if "UNIQUE" in str(exc):
                raise ValueError(f"já existe um molde com o código {m.code}") from exc
            raise
        return self.get_mold(cur.lastrowid)

    def update_mold(self, id: int, changes: dict, now: float | None = None) -> Mold:
        cur = self.get_mold(id)
        if cur is None:
            raise KeyError(id)
        data = {k: v for k, v in changes.items() if k in _MOLD_FIELDS}
        if "cavities" in data and "active_cavities" not in data and cur.active_cavities == cur.cavities:
            data["active_cavities"] = None  # estava com todas ativas: continua com todas
        new = _validate_mold(Mold(**{**asdict(cur), **data}))
        try:
            with self._lock, self._conn:
                self._conn.execute(
                    "UPDATE molds SET code=?, name=?, cavities=?, active_cavities=?, notes=? WHERE id=?",
                    (new.code, new.name, new.cavities, new.active_cavities, new.notes or "", id),
                )
        except Exception as exc:
            if "UNIQUE" in str(exc):
                raise ValueError(f"já existe um molde com o código {new.code}") from exc
            raise
        # Molde montado mudou de cavidades ativas (ex.: cavidade tampada): fecha a montagem e
        # abre outra, para o histórico mostrar a partir de quando valeu o número novo.
        if (new.active_cavities, new.code, new.name) != (cur.active_cavities, cur.code, cur.name):
            for machine_id in self._machines_with_mold(id):
                self._remount(machine_id, new, now)
        return new

    def delete_mold(self, id: int) -> None:
        if self._machines_with_mold(id):
            raise ValueError("o molde está montado numa máquina; troque o molde da máquina antes")
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM molds WHERE id=?", (id,))

    # ---- montagem na máquina -------------------------------------------
    def _machines_with_mold(self, mold_id: int) -> list[int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT machine_id FROM mold_mounts WHERE mold_id=? AND removed_at IS NULL", (mold_id,)
            ).fetchall()
        return [r[0] for r in rows]

    def mounted_mold(self, machine_id: int) -> dict | None:
        with self._lock:
            r = self._conn.execute(
                f"SELECT {self._MOUNT_COLS} FROM mold_mounts "
                "WHERE machine_id=? AND removed_at IS NULL ORDER BY mounted_at DESC LIMIT 1",
                (machine_id,),
            ).fetchone()
        return _mount_dict(r) if r else None

    def active_cavities(self, machine_id: int) -> int:
        """Cavidades ativas do molde montado; sem molde, 1 (conta como antes: 1 peça por ciclo)."""
        m = self.mounted_mold(machine_id)
        return int(m["active_cavities"]) if m else 1

    def _remount(self, machine_id: int, mold: Mold | None, now: float | None) -> None:
        now = time.time() if now is None else now
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE mold_mounts SET removed_at=? WHERE machine_id=? AND removed_at IS NULL",
                (now, machine_id),
            )
            if mold is not None:
                self._conn.execute(
                    "INSERT INTO mold_mounts(machine_id, mold_id, mold_code, mold_name, active_cavities, "
                    "mounted_at) VALUES (?,?,?,?,?,?)",
                    (machine_id, mold.id, mold.code, mold.name, mold.active_cavities, now),
                )

    def mount_mold(self, machine_id: int, mold_id: int | None, now: float | None = None) -> dict | None:
        """Troca o molde da máquina (None = retira o molde). Devolve a montagem atual."""
        if self.get(machine_id) is None:
            raise KeyError(machine_id)
        mold = None
        if mold_id is not None:
            mold = self.get_mold(mold_id)
            if mold is None:
                raise ValueError("molde não encontrado")
            for other in self._machines_with_mold(mold_id):
                if other != machine_id:
                    raise ValueError("esse molde já está montado em outra máquina")
        cur = self.mounted_mold(machine_id)
        if (cur["mold_id"] if cur else None) == mold_id:
            return cur  # mesmo molde: nada muda
        self._remount(machine_id, mold, now)
        return self.mounted_mold(machine_id)

    def mold_history(self, machine_id: int, limit: int = 50) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {self._MOUNT_COLS} FROM mold_mounts WHERE machine_id=? "
                "ORDER BY mounted_at DESC, id DESC LIMIT ?",
                (machine_id, limit),
            ).fetchall()
        return [_mount_dict(r) for r in rows]
