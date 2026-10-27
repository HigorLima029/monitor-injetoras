"""Preenche o banco com 8 h de histórico fictício, para demonstrar os dashboards.

Uso:  python -m app.demo_seed  (usa DB_PATH do .env; não toca na máquina)
Depois rode o monitor com SIMULATE=1 para continuar o fluxo ao vivo.
"""
from __future__ import annotations

import math
import random
import sys
import time

from .config import settings
from .storage import Storage

SETPOINTS = {"TempNozzle": 230.0, "TempZ1": 270.0, "TempZ2": 260.0, "TempZ3": 250.0,
             "TempZ4": 200.0, "TempZ5": 200.0, "TempOil": 45.0}


def seed(storage: Storage, hours: float = 8, step: float = 30, seed_value: int = 7) -> None:
    rng = random.Random(seed_value)
    now = time.time()
    start = now - hours * 3600
    count, bad, power = 1000.0, 12.0, 3800.0
    stop_windows = [(start + 2.1 * 3600, start + 2.5 * 3600, "WarnFrontSafetyDoorOpen"),
                    (start + 5.3 * 3600, start + 5.45 * 3600, "WarnProductDropNot"),
                    (start + 6.6 * 3600, start + 6.7 * 3600, "WarnCycleTooLong")]
    t = start
    while t < now - step:
        stopped = next((w for w in stop_windows if w[0] <= t < w[1]), None)
        cycle = 24 + 1.5 * math.sin(t / 2400) + rng.uniform(-0.6, 0.9)
        if not stopped:
            made = step / cycle
            count += made
            bad += made * (0.02 + 0.02 * rng.random())
            power += 0.08
        vals = {
            "ProductionCount": float(int(count)), "BadProduction": float(int(bad)),
            "ProductionGoodCount": float(int(count) - int(bad)), "DefectCount": float(int(bad)),
            "CycleMonitor": None if stopped else round(cycle, 2),
            "InjectMaxPress": round(96 + rng.uniform(-3, 3), 1),
            "InjectEndLastPos": round(6.2 + 0.25 * math.sin(t / 900) + rng.uniform(-0.1, 0.1), 2),
            "PowerConsume": round(power, 2), "TargetProductionCount": 2000.0,
        }
        for sp_tag, sp in SETPOINTS.items():
            now_tag = "TempOilNow" if sp_tag == "TempOil" else sp_tag + "Now"
            drift = 4 if (stopped and sp_tag == "TempZ3") else 0
            vals[sp_tag] = sp
            vals[now_tag] = round(sp - drift + 1.8 * math.sin(t / 700 + len(sp_tag)) + rng.uniform(-0.5, 0.5), 1)
            if sp_tag != "TempOil":
                vals["Min" + sp_tag] = 10.0
                vals["Max" + sp_tag] = 10.0
        storage.add_samples(t, vals)
        t += step
    for a, b, tag in stop_windows:
        storage.open_alarm(tag, a)
        storage.close_alarm(tag, b)


if __name__ == "__main__":
    hours = float(sys.argv[1]) if len(sys.argv) > 1 else 8
    s = Storage(settings.db_path)
    seed(s, hours)
    s.close()
    print(f"histórico de demonstração gravado em {settings.db_path}")
