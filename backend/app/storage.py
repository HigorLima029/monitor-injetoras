"""Histórico local em SQLite (amostras de KPIs e eventos de alarme)."""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    ts REAL NOT NULL,
    tag TEXT NOT NULL,
    value REAL
);
CREATE INDEX IF NOT EXISTS ix_samples_tag_ts ON samples(tag, ts);
CREATE TABLE IF NOT EXISTS alarm_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tag TEXT NOT NULL,
    started REAL NOT NULL,
    ended REAL
);
CREATE INDEX IF NOT EXISTS ix_alarm_started ON alarm_events(started);
"""


class Storage:
    def __init__(self, path: str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.executescript(SCHEMA)
        self._lock = threading.Lock()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---- amostras -------------------------------------------------------
    def add_samples(self, ts: float, values: dict[str, float | None]) -> None:
        rows = [(ts, tag, v) for tag, v in values.items()]
        with self._lock, self._conn:
            self._conn.executemany("INSERT INTO samples(ts, tag, value) VALUES (?,?,?)", rows)

    def history(self, tags: list[str], since: float, max_points: int = 300) -> dict[str, list[list[float]]]:
        """Série por tag como [[ts, valor], ...], reduzida para no máximo ~max_points."""
        if not tags:
            return {}
        span = max(time.time() - since, 1.0)
        bucket = max(span / max_points, 1.0)
        marks = ",".join("?" * len(tags))
        sql = (
            f"SELECT tag, CAST(ts / ? AS INTEGER) * ? AS b, AVG(value) "
            f"FROM samples WHERE tag IN ({marks}) AND ts >= ? "
            f"GROUP BY tag, b ORDER BY b"
        )
        with self._lock:
            rows = self._conn.execute(sql, (bucket, bucket, *tags, since)).fetchall()
        out: dict[str, list[list[float]]] = {t: [] for t in tags}
        for tag, b, v in rows:
            out[tag].append([b, v])
        return out

    def increments(self, tag: str, since: float, bucket: float) -> list[list[float]]:
        """Quanto um contador subiu em cada intervalo (ex.: peças por hora).

        Soma só os aumentos entre leituras seguidas, então um contador zerado
        na máquina não vira um número negativo nem um pico falso.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT b, SUM(CASE WHEN d > 0 THEN d ELSE 0 END) FROM ("
                "  SELECT CAST(ts / ? AS INTEGER) * ? AS b,"
                "         value - LAG(value) OVER (ORDER BY ts) AS d"
                "  FROM samples WHERE tag=? AND ts>=? AND value IS NOT NULL"
                ") GROUP BY b ORDER BY b",
                (bucket, bucket, tag, since),
            ).fetchall()
        return [[b, float(v or 0)] for b, v in rows]

    def first_value_since(self, tag: str, since: float) -> float | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM samples WHERE tag=? AND ts>=? ORDER BY ts LIMIT 1", (tag, since)
            ).fetchone()
        return None if row is None else row[0]

    # ---- alarmes --------------------------------------------------------
    def open_alarm(self, tag: str, ts: float) -> None:
        with self._lock, self._conn:
            self._conn.execute("INSERT INTO alarm_events(tag, started) VALUES (?,?)", (tag, ts))

    def close_alarm(self, tag: str, ts: float) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE alarm_events SET ended=? WHERE tag=? AND ended IS NULL", (ts, tag)
            )

    def open_alarm_tags(self) -> set[str]:
        with self._lock:
            rows = self._conn.execute("SELECT tag FROM alarm_events WHERE ended IS NULL").fetchall()
        return {r[0] for r in rows}

    def alarm_events(self, since: float, limit: int = 500) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT tag, started, ended FROM alarm_events "
                "WHERE started >= ? OR ended IS NULL ORDER BY started DESC LIMIT ?",
                (since, limit),
            ).fetchall()
        return [{"tag": t, "started": s, "ended": e} for t, s, e in rows]

    # ---- manutenção -----------------------------------------------------
    def prune(self, older_than: float) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM samples WHERE ts < ?", (older_than,))
            self._conn.execute(
                "DELETE FROM alarm_events WHERE ended IS NOT NULL AND ended < ?", (older_than,)
            )
