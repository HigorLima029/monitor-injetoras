"""Paradas de máquina: detecção automática, motivos padrão e o que aconteceu (texto livre).

Uma parada começa quando o contador de ciclos fica parado por mais que o tempo mínimo da máquina
(micro-paradas não contam). O início registrado é a hora do último ciclo; o fim, a hora em que o
contador volta a subir. O motivo vem sugerido quando dá (alarme ativo, máquina desligada, sem
comunicação) e a pessoa confirma, troca ou escreve o que aconteceu.
"""
from __future__ import annotations

import re
import unicodedata

# (código, rótulo) — já vêm no sistema; dá para renomear, desativar ou criar outros.
DEFAULT_REASONS: list[tuple[str, str]] = [
    ("troca_molde", "Troca de molde"),
    ("ajuste", "Ajuste / regulagem"),
    ("falha", "Falha / alarme"),
    ("falta_material", "Falta de material"),
    ("manutencao", "Manutenção preventiva"),
    ("desligada", "Máquina desligada"),
    ("intervalo", "Intervalo / refeição"),
    ("falta_operador", "Falta de operador"),
    ("qualidade", "Problema de qualidade"),
    ("sem_comunicacao", "Sem comunicação"),
    ("outro", "Outro"),
]
OTHER = "outro"

REASON_SCHEMA = """
CREATE TABLE IF NOT EXISTS stop_reasons (
    code TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    sort INTEGER NOT NULL DEFAULT 0
);
"""

STOP_SCHEMA = """
CREATE TABLE IF NOT EXISTS stops (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started REAL NOT NULL,
    ended REAL,
    reason TEXT,
    auto INTEGER NOT NULL DEFAULT 0,
    detail TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_stops_started ON stops(started);
"""


def _slug(text: str) -> str:
    t = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "_", t).strip("_")[:40] or "motivo"


class ReasonMixin:
    """Lista de motivos (global, no cadastro de máquinas)."""

    def seed_reasons(self) -> None:
        with self._lock, self._conn:
            for i, (code, label) in enumerate(DEFAULT_REASONS):
                self._conn.execute(
                    "INSERT OR IGNORE INTO stop_reasons(code, label, active, sort) VALUES (?,?,1,?)",
                    (code, label, i * 10),
                )

    def list_reasons(self, only_active: bool = False) -> list[dict]:
        sql = "SELECT code, label, active, sort FROM stop_reasons"
        if only_active:
            sql += " WHERE active=1"
        with self._lock:
            rows = self._conn.execute(sql + " ORDER BY sort, label").fetchall()
        return [{"code": c, "label": l, "active": bool(a), "sort": s} for c, l, a, s in rows]

    def reason_labels(self) -> dict[str, str]:
        return {r["code"]: r["label"] for r in self.list_reasons()}

    def add_reason(self, label: str) -> dict:
        label = (label or "").strip()
        if not label:
            raise ValueError("escreva o nome do motivo")
        labels = {r["label"].lower(): r for r in self.list_reasons()}
        if label.lower() in labels:
            raise ValueError("já existe um motivo com esse nome")
        base = code = _slug(label)
        n = 2
        with self._lock, self._conn:
            while self._conn.execute("SELECT 1 FROM stop_reasons WHERE code=?", (code,)).fetchone():
                code = f"{base}_{n}"
                n += 1
            last = self._conn.execute("SELECT COALESCE(MAX(sort), 0) FROM stop_reasons").fetchone()[0]
            # "Outro" fica sempre por último na lista
            self._conn.execute("UPDATE stop_reasons SET sort=? WHERE code=?", (last + 20, OTHER))
            self._conn.execute(
                "INSERT INTO stop_reasons(code, label, active, sort) VALUES (?,?,1,?)", (code, label, last + 10)
            )
        return {"code": code, "label": label, "active": True}

    def update_reason(self, code: str, label: str | None = None, active: bool | None = None) -> dict:
        cur = {r["code"]: r for r in self.list_reasons()}.get(code)
        if cur is None:
            raise KeyError(code)
        if code == OTHER and active is False:
            raise ValueError('o motivo "Outro" não pode ser desativado: é o que vale quando a pessoa só escreve')
        if label is not None:
            label = label.strip()
            if not label:
                raise ValueError("o nome do motivo não pode ficar vazio")
            if any(r["label"].lower() == label.lower() and r["code"] != code for r in self.list_reasons()):
                raise ValueError("já existe um motivo com esse nome")
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE stop_reasons SET label=COALESCE(?, label), active=COALESCE(?, active) WHERE code=?",
                (label, None if active is None else int(active), code),
            )
        return {r["code"]: r for r in self.list_reasons()}[code]


class StopStorageMixin:
    """Paradas guardadas no banco de cada máquina (junto do histórico dela)."""

    def open_stop(self) -> dict | None:
        with self._lock:
            r = self._conn.execute(
                "SELECT id, started, ended, reason, auto, detail, note FROM stops WHERE ended IS NULL "
                "ORDER BY started DESC LIMIT 1"
            ).fetchone()
        return _stop(r) if r else None

    def start_stop(self, started: float, reason: str | None = None, detail: str = "",
                   ended: float | None = None) -> int:
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT INTO stops(started, ended, reason, auto, detail) VALUES (?,?,?,?,?)",
                (started, ended, reason, 1 if reason else 0, detail),
            )
        return cur.lastrowid

    def end_stop(self, id: int, ended: float) -> None:
        with self._lock, self._conn:
            self._conn.execute("UPDATE stops SET ended=? WHERE id=? AND ended IS NULL", (ended, id))

    def suggest_reason(self, id: int, reason: str, detail: str = "") -> None:
        """Sugestão automática: só preenche se ninguém escolheu um motivo ainda."""
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE stops SET reason=?, auto=1, detail=? WHERE id=? AND reason IS NULL", (reason, detail, id)
            )

    def classify_stop(self, id: int, reason: str | None, note: str | None) -> dict:
        with self._lock, self._conn:
            if self._conn.execute("SELECT 1 FROM stops WHERE id=?", (id,)).fetchone() is None:
                raise KeyError(id)
            self._conn.execute(
                "UPDATE stops SET reason=?, auto=0, note=COALESCE(?, note) WHERE id=?", (reason, note, id)
            )
            r = self._conn.execute(
                "SELECT id, started, ended, reason, auto, detail, note FROM stops WHERE id=?", (id,)
            ).fetchone()
        return _stop(r)

    def stops(self, since: float, limit: int = 500) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, started, ended, reason, auto, detail, note FROM stops "
                "WHERE started >= ? OR ended IS NULL OR ended >= ? ORDER BY started DESC LIMIT ?",
                (since, since, limit),
            ).fetchall()
        return [_stop(r) for r in rows]

    def last_sample_ts(self) -> float | None:
        with self._lock:
            row = self._conn.execute("SELECT MAX(ts) FROM samples").fetchone()
        return row[0] if row else None

    def prune_stops(self, older_than: float) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM stops WHERE ended IS NOT NULL AND ended < ?", (older_than,))


def _stop(r) -> dict:
    keys = ("id", "started", "ended", "reason", "auto", "detail", "note")
    d = dict(zip(keys, r))
    d["auto"] = bool(d["auto"])
    return d


class StopTracker:
    """Detecta as paradas a partir das leituras (usado pelo coletor a cada leitura)."""

    def __init__(self, storage, min_seconds=lambda: 120):
        self.storage = storage
        self.min_seconds = min_seconds
        self.last_cycle: float | None = None
        # parada que ficou aberta quando o Monitor parou: fecha na última leitura conhecida
        # (não sabemos o que aconteceu enquanto o Monitor esteve fora)
        leftover = storage.open_stop()
        if leftover:
            end = storage.last_sample_ts() or leftover["started"]
            storage.end_stop(leftover["id"], max(end, leftover["started"]))
        self.open: dict | None = None

    def _min(self) -> float:
        try:
            return max(30.0, float(self.min_seconds()))
        except Exception:
            return 120.0

    def on_read(self, now: float, cycled: bool, machine_off: bool, alarms: list[str]) -> None:
        if self.last_cycle is None:
            self.last_cycle = now  # primeira leitura depois de conectar: começa a contar daqui
        if cycled:
            if self.open:
                self.storage.end_stop(self.open["id"], now)
                self.open = None
            self.last_cycle = now
            return
        if self.open is None and now - self.last_cycle >= self._min():
            reason, detail = _suggest(machine_off, alarms)
            sid = self.storage.start_stop(self.last_cycle, reason, detail)
            self.open = {"id": sid, "reason": reason}
        elif self.open is not None and self.open["reason"] is None:
            reason, detail = _suggest(machine_off, alarms)
            if reason:
                self.storage.suggest_reason(self.open["id"], reason, detail)
                self.open["reason"] = reason

    def on_disconnect(self, last_ok: float | None) -> None:
        """Comunicação caiu: a parada aberta termina na última leitura boa."""
        if self.open:
            self.storage.end_stop(self.open["id"], last_ok or self.last_cycle or 0)
            self.open = None
        self.last_cycle = None

    def on_reconnect(self, lost_at: float | None, now: float) -> None:
        """Ficou sem comunicação por mais que o tempo mínimo: registra como 'Sem comunicação'."""
        if lost_at is not None and now - lost_at >= self._min():
            self.storage.start_stop(lost_at, "sem_comunicacao", ended=now)
        self.last_cycle = None


def _suggest(machine_off: bool, alarms: list[str]) -> tuple[str | None, str]:
    if machine_off:
        return "desligada", ""
    if alarms:
        return "falha", ", ".join(alarms)[:300]
    return None, ""
