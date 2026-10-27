"""API do Monitor de Injetoras: REST + WebSocket, e serve o frontend compilado."""
from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .collector import Collector, to_number
from .config import settings
from .sources import OpcUaSource, SimulatedSource
from .storage import Storage
from .tags import ALARM_PREFIX, BARREL_ZONES, KPI_META, alarm_label, humanize

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")


def build_collector() -> Collector:
    storage = Storage(settings.db_path)
    if settings.simulate:
        source = SimulatedSource()
    else:
        cache = str(Path(settings.db_path).with_name("tags_cache.json"))
        source = OpcUaSource(settings.opcua_url, settings.namespace, cache_file=cache)
    return Collector(source, storage, settings.poll_seconds, settings.full_poll_seconds,
                     settings.retention_days)


@asynccontextmanager
async def lifespan(app: FastAPI):
    collector = app.state.collector if hasattr(app.state, "collector") else build_collector()
    app.state.collector = collector
    collector.start()
    yield
    await collector.stop()
    collector.storage.close()


app = FastAPI(title="Monitor de Injetoras", lifespan=lifespan)


def _c(app_: FastAPI) -> Collector:
    return app_.state.collector


@app.get("/api/info")
def info():
    return {
        "machine_name": settings.machine_name,
        "opcua_url": settings.opcua_url,
        "simulate": settings.simulate,
        "poll_seconds": settings.poll_seconds,
        "kpi_meta": KPI_META,
        "zones": [{"label": z[0], "actual": z[1], "setpoint": z[2], "min": z[3], "max": z[4]}
                  for z in BARREL_ZONES],
    }


@app.get("/api/live")
def live():
    return _c(app).live()


@app.get("/api/history")
def history(tags: str = Query(..., description="tags separadas por vírgula"),
            minutes: float = Query(60, ge=1, le=60 * 24 * 31)):
    wanted = [t for t in (s.strip() for s in tags.split(",")) if t][:40]
    since = time.time() - minutes * 60
    return {"since": since, "series": _c(app).storage.history(wanted, since)}


@app.get("/api/production")
def production(hours: float = Query(8, ge=1, le=24 * 31)):
    c = _c(app)
    since = time.time() - hours * 3600
    bucket = 3600 if hours > 2 else 600
    per_bucket = c.storage.increments("ProductionCount", since, bucket)
    bad_bucket = c.storage.increments("BadProduction", since, bucket)
    cycles = c.storage.history(["CycleMonitor"], since, max_points=200)["CycleMonitor"]
    cycle_vals = [v for _, v in cycles if v]
    return {
        "since": since,
        "bucket_seconds": bucket,
        "per_bucket": per_bucket,
        "bad_per_bucket": bad_bucket,
        "produced": sum(v for _, v in per_bucket),
        "bad": sum(v for _, v in bad_bucket),
        "avg_cycle": (sum(cycle_vals) / len(cycle_vals)) if cycle_vals else None,
    }


@app.get("/api/alarms")
def alarms(hours: float = Query(24, ge=1, le=24 * 31)):
    c = _c(app)
    events = c.storage.alarm_events(time.time() - hours * 3600)
    for e in events:
        e["label"] = alarm_label(e["tag"])
    return {
        "active": [{"tag": t, "label": alarm_label(t)} for t in sorted(c.active_alarms)],
        "monitored": len(c.alarm_tags),
        "events": events,
    }


@app.get("/api/values")
def values(tags: str = Query(..., description="tags separadas por vírgula")):
    """Valores atuais de uma lista de tags (usado pelas telas do painel).

    As tags pedidas entram na leitura rápida por 30 s; assim a tela aberta
    atualiza a cada POLL_SECONDS em vez de esperar a leitura completa.
    """
    c = _c(app)
    wanted = [t for t in (s.strip() for s in tags.split(",")) if t][:300]
    c.watch(wanted)
    vals = c.current(wanted)
    return {
        "status": c.status(),
        "values": {t: (v if to_number(v) is not None or v is None else str(v)) for t, v in vals.items()},
    }


@app.get("/api/tags")
def tags(q: str = "", limit: int = Query(2000, ge=1, le=5000)):
    c = _c(app)
    needle = q.strip().lower()
    rows = []
    for t in c.tags:
        if needle and needle not in t.lower():
            continue
        value = c.values.get(t)
        rows.append({
            "tag": t,
            "label": alarm_label(t) if t.startswith(ALARM_PREFIX) else KPI_META.get(t, {}).get("label") or humanize(t),
            "value": value if (to_number(value) is not None or value is None) else str(value),
            "unit": KPI_META.get(t, {}).get("unit", ""),
        })
        if len(rows) >= limit:
            break
    return {"total": len(c.tags), "rows": rows}


@app.websocket("/ws")
async def ws(socket: WebSocket):
    await socket.accept()
    c = _c(app)
    q = c.subscribe()
    try:
        await socket.send_json(c.live())
        while True:
            payload = await q.get()
            await socket.send_json(payload)
    except (WebSocketDisconnect, asyncio.CancelledError, RuntimeError):
        pass
    finally:
        c.unsubscribe(q)


dist = Path(settings.frontend_dist)
if (dist / "index.html").exists():
    app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str):
        target = (dist / path).resolve()
        if path and target.is_file() and target.is_relative_to(dist.resolve()):
            return FileResponse(target)
        return FileResponse(dist / "index.html")
