"""API do Monitor de Injetoras: REST + WebSocket, e serve o frontend compilado."""
from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .collector import Collector, to_number
from .config import settings
from .machines import Machine, MachineStore, seed_from_settings
from .manager import MachineManager
from .vnc_hub import VncHub, WebSocketTransport
from .tags import ALARM_PREFIX, BARREL_ZONES, KPI_META, alarm_label, humanize

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

SEM_MAQUINA = "Nenhuma máquina aberta. Cadastre uma em Máquinas."


def build_manager() -> MachineManager:
    store = MachineStore(settings.config_db_path)
    seed_from_settings(store, settings, settings.machines_dir)
    return MachineManager(store, settings)


@asynccontextmanager
async def lifespan(app: FastAPI):
    manager = getattr(app.state, "manager", None) or build_manager()
    app.state.manager = manager
    await manager.start()
    yield
    await manager.stop()
    manager.store.close()


app = FastAPI(title="Monitor de Injetoras", lifespan=lifespan)


def _mgr(app_: FastAPI) -> MachineManager:
    return app_.state.manager


def _c(app_: FastAPI) -> Collector:
    """Coletor da maquina aberta; 503 se nao houver nenhuma."""
    c = _mgr(app_).collector
    if c is None:
        raise HTTPException(503, SEM_MAQUINA)
    return c


def _machine(app_: FastAPI) -> Machine:
    m = _mgr(app_).machine
    if m is None:
        raise HTTPException(503, SEM_MAQUINA)
    return m


# ---- maquinas -------------------------------------------------------------
@app.exception_handler(RequestValidationError)
async def _validation_error(_request, exc: RequestValidationError):
    """422 sem eco do corpo enviado: ele pode conter as senhas VNC."""
    errors = [{"loc": list(e.get("loc", ())), "msg": e.get("msg", ""), "type": e.get("type", "")}
              for e in exc.errors()]
    return JSONResponse({"detail": errors}, status_code=422)


class MachineIn(BaseModel):
    name: str
    kind: str
    host: str
    client: str = "Chen Hsong"
    opcua_port: int = 4840
    namespace: int | None = None
    ams_net_id: str = ""
    ads_port: int = 801
    vnc_enabled: bool | None = None
    vnc_port: int = 5900
    vnc_view_password: str = ""
    vnc_control_password: str = ""
    notes: str = ""


class MachineUpdate(BaseModel):
    """PUT parcial: so o que o cliente enviou muda."""
    name: str | None = None
    kind: str | None = None
    host: str | None = None
    client: str | None = None
    opcua_port: int | None = None
    namespace: int | None = None
    ams_net_id: str | None = None
    ads_port: int | None = None
    vnc_enabled: bool | None = None
    vnc_port: int | None = None
    vnc_view_password: str | None = None
    vnc_control_password: str | None = None
    notes: str | None = None


def _get_or_404(id: int) -> Machine:
    m = _mgr(app).store.get(id)
    if m is None:
        raise HTTPException(404, "Máquina não encontrada")
    return m


@app.get("/api/machines")
def list_machines():
    mgr = _mgr(app)
    return {
        "active_id": mgr.store.active_id(),
        "machines": [{**m.public(), "status": mgr.status_of(m.id)} for m in mgr.store.list()],
    }


@app.post("/api/machines", status_code=201)
def add_machine(body: MachineIn):
    try:
        m = _mgr(app).store.add(Machine(None, **body.model_dump()))
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    return m.public()


@app.put("/api/machines/{id}")
async def update_machine(id: int, body: MachineUpdate):
    mgr = _mgr(app)
    _get_or_404(id)
    try:
        m = mgr.store.update(id, body.model_dump(exclude_unset=True))
    except KeyError:
        raise HTTPException(404, "Máquina não encontrada")
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    if mgr.machine is not None and mgr.machine.id == id:
        await mgr.activate(id)  # aplica as mudancas na maquina aberta
    return m.public()


@app.delete("/api/machines/{id}", status_code=204)
def delete_machine(id: int):
    mgr = _mgr(app)
    _get_or_404(id)
    if (mgr.machine is not None and mgr.machine.id == id) or mgr.store.active_id() == id:
        raise HTTPException(409, "Não dá para apagar a máquina aberta. Abra outra antes.")
    mgr.store.delete(id)
    return Response(status_code=204)


@app.post("/api/machines/{id}/activate")
async def activate_machine(id: int):
    _get_or_404(id)
    try:
        m = await _mgr(app).activate(id)
    except KeyError:
        raise HTTPException(404, "Máquina não encontrada")
    return m.public()


@app.post("/api/machines/{id}/test")
async def test_machine(id: int):
    m = _get_or_404(id)
    latency = await _mgr(app).check(m)
    return {"online": latency is not None, "latency_ms": latency}


@app.get("/api/info")
def info():
    m = _machine(app)
    mgr = _mgr(app)
    demo = m.kind == "demo"
    vnc = m.effective_vnc_enabled and not demo
    return {
        "machine_id": m.id,
        "kind": m.kind,
        "client": m.client,
        "machine_name": m.name,
        "opcua_url": f"ads://{m.host}" if m.kind == "ads" else m.opcua_url,
        "simulate": demo,
        "poll_seconds": mgr.settings.poll_seconds,
        "kpi_meta": KPI_META,
        "profile": m.profile,
        "vnc": vnc,
        "vnc_control": bool(m.vnc_control_password) and vnc,
        "zones": [{"label": z[0], "actual": z[1], "setpoint": z[2], "min": z[3], "max": z[4]}
                  for z in _zones()],
    }


def _zones():
    """CHY: bico + 5 zonas fixas. B&R (Euromap 63): so as zonas ligadas no painel, sem bico."""
    m = _machine(app)
    if m.profile != "em63":
        return BARREL_ZONES
    zones = [z for z in BARREL_ZONES if z[0] != "Bico"]
    vals = _c(app).values
    active = [z for z in zones if vals.get(z[2]) is not None]
    return active or zones

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
    c = _mgr(app).collector
    if c is None:
        await socket.close(code=1013, reason=SEM_MAQUINA)
        return
    q = c.subscribe()
    try:
        await socket.send_json(c.live())
        while True:
            payload = await q.get()
            await socket.send_json(payload)
            if payload.get("type") == "machine_changed":
                await socket.close()  # o navegador reconecta e assina o coletor novo
                break
    except (WebSocketDisconnect, asyncio.CancelledError, RuntimeError):
        pass
    finally:
        c.unsubscribe(q)


@app.websocket("/ws/vnc")
async def ws_vnc(socket: WebSocket, sid: str = Query("", max_length=64)):
    """Tela ao vivo pelo hub VNC: a senha digitada define se a pessoa so assiste ou pode operar."""
    offered = socket.headers.get("sec-websocket-protocol", "")
    await socket.accept(subprotocol="binary" if "binary" in offered else None)
    mgr = _mgr(app)
    hub: VncHub | None = mgr.hub
    if hub is None:
        m = mgr.machine
        if m is None:
            reason = SEM_MAQUINA
        elif not m.effective_vnc_enabled or m.kind == "demo":
            reason = "Tela ao vivo desligada para esta máquina"
        else:
            reason = "Defina a senha VNC de visualização no cadastro da máquina"
        await socket.close(code=1008, reason=reason)
        return
    peer = socket.client.host if socket.client else "navegador"
    await hub.serve(WebSocketTransport(socket), sid or f"ws-{id(socket):x}", peer)


class VncControl(BaseModel):
    sid: str
    action: str  # "take" | "release"


def _hub_or_404() -> VncHub:
    hub = _mgr(app).hub
    if hub is None:
        raise HTTPException(404, "Tela ao vivo desligada")
    return hub


@app.get("/api/vnc/status")
def vnc_status(sid: str = Query("", max_length=64)):
    return _hub_or_404().status(sid)


@app.post("/api/vnc/control")
async def vnc_control(body: VncControl):
    hub = _hub_or_404()
    if body.action == "take":
        ok, msg = await hub.take(body.sid)
        if not ok:
            raise HTTPException(403 if "visualizacao" in msg or "desligada" in msg else 409, msg)
        return {"ok": True, "message": msg}
    if body.action == "release":
        return {"ok": await hub.release(body.sid, "liberado pelo operador")}
    raise HTTPException(400, "acao invalida")


dist = Path(settings.frontend_dist)
if (dist / "index.html").exists():
    app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str):
        target = (dist / path).resolve()
        if path and target.is_file() and target.is_relative_to(dist.resolve()):
            return FileResponse(target)
        return FileResponse(dist / "index.html")
