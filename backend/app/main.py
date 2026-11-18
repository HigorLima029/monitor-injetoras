"""API do Monitor de Injetoras: REST + WebSocket, e serve o frontend compilado."""
from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import auth, authz
from .accounts import AccountStore, User, migrate_machine_clients
from .collector import Collector, to_number
from .config import settings
from .machines import CONNECTION_FIELDS, Machine, MachineStore, seed_from_settings
from .manager import Fleet, MachineRuntime
from .vnc_hub import WebSocketTransport
from .tags import ADS_ZONES, ALARM_PREFIX, BARREL_ZONES, KPI_META, alarm_label, humanize

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

NAO_ENCONTRADA = "Máquina não encontrada"
SO_MAE = "Só a conta mãe pode fazer isso."
SO_EQUIPE = "Só a conta mãe e os técnicos podem fazer isso."
SEM_COLETA = "A coleta desta máquina não abriu. Veja o registro do servidor."
WS_NAO_ENCONTRADA = 4404


def build_fleet() -> Fleet:
    store = MachineStore(settings.config_db_path)
    seed_from_settings(store, settings, settings.machines_dir)
    accounts = AccountStore(settings.config_db_path)
    migrate_machine_clients(store, accounts)
    return Fleet(store, settings, accounts)


def _client_name(fleet: Fleet, client_id: int | None) -> str:
    if fleet.accounts is None or client_id is None:
        return ""
    c = fleet.accounts.get_client(client_id)
    return c.name if c else ""


@asynccontextmanager
async def lifespan(app: FastAPI):
    injected = getattr(app.state, "fleet", None)
    fleet = injected or build_fleet()
    app.state.fleet = fleet
    accounts = getattr(app.state, "accounts", None) or fleet.accounts
    if accounts is None:
        raise RuntimeError("Sem banco de contas: o login não tem como funcionar.")
    fleet.accounts = accounts
    accounts.session_idle = settings.session_idle_hours * 3600
    accounts.session_max = settings.session_max_hours * 3600
    app.state.accounts = accounts
    app.state.auth = auth.AuthState()
    await fleet.start()
    sweeper = asyncio.create_task(auth.sweeper(accounts, app.state.auth.sockets))
    try:
        yield
    finally:
        sweeper.cancel()
        await asyncio.gather(sweeper, return_exceptions=True)
        await fleet.stop()
        if injected is None:  # quem injetou (testes) fecha o que é seu
            fleet.store.close()
            accounts.close()


app = FastAPI(title="Monitor de Injetoras", lifespan=lifespan,
              docs_url=None, redoc_url=None, openapi_url=None)  # sem docs públicas
app.add_middleware(auth.AuthMiddleware)
app.include_router(auth.router)


def _fleet() -> Fleet:
    return app.state.fleet


def _visible(user: User, m: Machine) -> bool:
    return authz.can_view(user, m)


def _machine_view(user: User, m: Machine, *, as_client: bool = False) -> dict:
    fleet = _fleet()
    return authz.machine_view(user, m, fleet.status_of(m.id), _client_name(fleet, m.client_id),
                              as_client=as_client)


def _require(user: User, *roles: str) -> None:
    if user.role not in roles:
        raise HTTPException(403, SO_MAE if roles == ("mae",) else SO_EQUIPE)


def _audit(request: Request, user: User, m: Machine, detail: str) -> None:
    """Cadastro de máquina na auditoria (com o cliente da máquina, para filtrar por cliente).
    O detalhe nunca leva senha: só nomes de campos."""
    _fleet().accounts.log("cadastro", user, machine_id=m.id, client_id=m.client_id,
                          ip=auth._ip(request), detail=detail)


def _machine_or_404(user: User, id: int) -> Machine:
    """A máquina, se existir e o usuário puder vê-la; senão 404 (nunca 403: não revela que existe)."""
    m = _fleet().store.get(id)
    if m is None or not _visible(user, m):
        raise HTTPException(404, NAO_ENCONTRADA)
    return m


def _rt(user: User, id: int) -> MachineRuntime:
    m = _machine_or_404(user, id)
    rt = _fleet().runtime(m.id)
    if rt is None or rt.collector is None:
        raise HTTPException(503, SEM_COLETA)
    return rt


def _c(user: User, id: int) -> Collector:
    return _rt(user, id).collector


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
    client_id: int | None = None
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
    client_id: int | None = None
    opcua_port: int | None = None
    namespace: int | None = None
    ams_net_id: str | None = None
    ads_port: int | None = None
    vnc_enabled: bool | None = None
    vnc_port: int | None = None
    vnc_view_password: str | None = None
    vnc_control_password: str | None = None
    notes: str | None = None
    clear_vnc_view_password: bool = False
    clear_vnc_control_password: bool = False


@app.get("/api/machines")
def list_machines(client_id: int | None = None, user: User = Depends(auth.current_user)):
    """`client_id=0` = máquinas em preparação (sem cliente)."""
    out = []
    for m in _fleet().store.list():  # cliente: só as do próprio cliente, mesmo pedindo outro
        if not _visible(user, m):
            continue
        if client_id is not None and (m.client_id or 0) != client_id:
            continue
        out.append(_machine_view(user, m))
    return {"machines": out}


@app.post("/api/machines", status_code=201)
async def add_machine(body: MachineIn, request: Request, user: User = Depends(auth.current_user)):
    _require(user, "mae")
    fleet = _fleet()
    try:
        m = fleet.store.add(Machine(None, **body.model_dump()))
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    _audit(request, user, m, f"criou a máquina {m.name} ({m.kind}, {_client_label(m)})")
    await fleet.add(m.id)
    return m.public()


@app.put("/api/machines/{id}")
async def update_machine(id: int, body: MachineUpdate, request: Request,
                         user: User = Depends(auth.current_user)):
    fleet = _fleet()
    before = _machine_or_404(user, id)
    _require(user, "mae")
    changes = body.model_dump(exclude_unset=True)
    try:
        m = fleet.store.update(id, changes)
    except KeyError:
        raise HTTPException(404, NAO_ENCONTRADA)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    fields = sorted(f for f in _changed_fields(before, m))
    if fields:
        _audit(request, user, m, f"alterou a máquina {m.name} ({_client_label(m)}): {', '.join(fields)}")
    changed = any(getattr(before, f) != getattr(m, f) for f in CONNECTION_FIELDS)
    await fleet.reload(id, connection_changed=changed)
    return m.public()


@app.delete("/api/machines/{id}", status_code=204)
async def delete_machine(id: int, request: Request, user: User = Depends(auth.current_user)):
    fleet = _fleet()
    m = _machine_or_404(user, id)
    _require(user, "mae")
    fleet.store.delete(id)
    _audit(request, user, m, f"apagou a máquina {m.name} ({_client_label(m)})")
    await fleet.remove(id)
    return Response(status_code=204)


def _client_label(m: Machine) -> str:
    if m.client_id is None:
        return "em preparação"
    return f"cliente {_client_name(_fleet(), m.client_id) or m.client_id}"


def _changed_fields(before: Machine, after: Machine):
    """Nomes dos campos que mudaram (senhas aparecem só pelo nome, nunca pelo valor)."""
    for f in ("name", "kind", "host", "client_id", "opcua_port", "namespace", "ams_net_id",
              "ads_port", "vnc_enabled", "vnc_port", "vnc_view_password", "vnc_control_password",
              "notes"):
        if getattr(before, f) != getattr(after, f):
            yield f


@app.post("/api/machines/{id}/test")
async def test_machine(id: int, user: User = Depends(auth.current_user)):
    m = _machine_or_404(user, id)
    _require(user, *authz.STAFF)
    latency = await _fleet().check(m)
    return {"online": latency is not None, "latency_ms": latency}


@app.get("/api/machines/{id}/info")
def info(id: int, as_: str = Query("", alias="as"), user: User = Depends(auth.current_user)):
    rt = _rt(user, id)
    m = rt.machine
    as_client = user.role == "cliente" or as_ == "cliente"
    demo = m.kind == "demo"
    vnc = m.effective_vnc_enabled and not demo
    out = {
        "machine_id": m.id,
        "kind": m.kind,
        "client_id": m.client_id,
        "client": _client_name(_fleet(), m.client_id),
        "machine_name": m.name,
        "simulate": demo,
        "poll_seconds": _fleet().settings.poll_seconds,
        "kpi_meta": KPI_META,
        "profile": m.profile,
        "vnc": vnc,
        "vnc_control": (not as_client) and bool(m.vnc_control_password) and vnc,
        "zones": [{"label": z[0], "actual": z[1], "setpoint": z[2], "min": z[3], "max": z[4]}
                  for z in _zones(m, rt.collector)],
        "machine": _machine_view(user, m, as_client=as_client),
    }
    if not as_client:
        out["opcua_url"] = f"ads://{m.host}" if m.kind == "ads" else m.opcua_url
    return out


def _zones(m: Machine, c: Collector):
    """CHY: bico + 5 zonas fixas. Beckhoff: 8 zonas, sem bico.
    B&R (Euromap 63): so as zonas ligadas no painel, sem bico."""
    if m.profile == "ads":
        return ADS_ZONES
    if m.profile != "em63":
        return BARREL_ZONES
    zones = [z for z in BARREL_ZONES if z[0] != "Bico"]
    active = [z for z in zones if c.values.get(z[2]) is not None]
    return active or zones


@app.get("/api/machines/{id}/live")
def live(id: int, user: User = Depends(auth.current_user)):
    return _c(user, id).live()


@app.get("/api/machines/{id}/history")
def history(id: int, tags: str = Query(..., description="tags separadas por vírgula"),
            minutes: float = Query(60, ge=1, le=60 * 24 * 31), user: User = Depends(auth.current_user)):
    c = _c(user, id)
    wanted = [t for t in (s.strip() for s in tags.split(",")) if t][:40]
    since = time.time() - minutes * 60
    return {"since": since, "series": c.storage.history(wanted, since)}


@app.get("/api/machines/{id}/production")
def production(id: int, hours: float = Query(8, ge=1, le=24 * 31), user: User = Depends(auth.current_user)):
    c = _c(user, id)
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


@app.get("/api/machines/{id}/alarms")
def alarms(id: int, hours: float = Query(24, ge=1, le=24 * 31), user: User = Depends(auth.current_user)):
    c = _c(user, id)
    events = c.storage.alarm_events(time.time() - hours * 3600)
    for e in events:
        e["label"] = alarm_label(e["tag"])
    return {
        "active": [{"tag": t, "label": alarm_label(t)} for t in sorted(c.active_alarms)],
        "monitored": len(c.alarm_tags),
        "events": events,
    }


@app.get("/api/machines/{id}/values")
def values(id: int, tags: str = Query(..., description="tags separadas por vírgula"),
           user: User = Depends(auth.current_user)):
    """Valores atuais de uma lista de tags (usado pelas telas do painel).

    As tags pedidas entram na leitura rápida por 30 s; assim a tela aberta
    atualiza a cada POLL_SECONDS em vez de esperar a leitura completa.
    """
    c = _c(user, id)
    wanted = [t for t in (s.strip() for s in tags.split(",")) if t][:300]
    c.watch(wanted)
    vals = c.current(wanted)
    return {
        "status": c.status(),
        "values": {t: (v if to_number(v) is not None or v is None else str(v)) for t, v in vals.items()},
    }


@app.get("/api/machines/{id}/tags")
def tags(id: int, q: str = "", limit: int = Query(2000, ge=1, le=5000), user: User = Depends(auth.current_user)):
    c = _c(user, id)
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


# ---- WebSockets -------------------------------------------------------------
def _ws_runtime(user: User, id: int) -> MachineRuntime | None:
    rt = _fleet().runtime(id)
    if rt is None or rt.closed or not _visible(user, rt.machine):
        return None
    return rt


@app.websocket("/ws/machines/{id}")
async def ws_machine(socket: WebSocket, id: int):
    """Dados ao vivo de uma máquina. Enquanto aberto, a máquina lê no intervalo rápido."""
    user = await auth.ws_user(socket)
    if user is None:
        return
    token = socket.state.session_token
    auth.track(app, token, socket)
    try:
        await _ws_live(socket, user, id)
    finally:
        auth.untrack(app, token, socket)


async def _ws_live(socket: WebSocket, user: User, id: int):
    await socket.accept()
    rt = _ws_runtime(user, id)
    if rt is None:
        await socket.close(code=WS_NAO_ENCONTRADA, reason=NAO_ENCONTRADA)
        return
    rt.enter()
    try:
        # cada volta assina o coletor atual; mudança de cadastro (version) confere tudo de novo
        while _ws_runtime(user, id) is rt and rt.collector is not None:
            c, seen = rt.collector, rt.version
            q = c.subscribe()
            try:
                await socket.send_json(c.live())
                while rt.version == seen:
                    payload = await q.get()
                    if "_fleet" in payload:
                        continue
                    await socket.send_json(payload)
            finally:
                c.unsubscribe(q)
        await socket.close(code=WS_NAO_ENCONTRADA, reason=NAO_ENCONTRADA)
    except (WebSocketDisconnect, asyncio.CancelledError, RuntimeError):
        pass
    finally:
        rt.leave()


@app.websocket("/ws/vnc")
async def ws_vnc(socket: WebSocket, machine: int = Query(...), sid: str = Query("", max_length=64)):
    """Tela ao vivo pelo hub VNC: a senha digitada define se a pessoa so assiste ou pode operar."""
    user = await auth.ws_user(socket)
    if user is None:
        return
    token = socket.state.session_token
    auth.track(app, token, socket)
    try:
        await _ws_vnc(socket, user, machine, sid)
    finally:
        auth.untrack(app, token, socket)


async def _ws_vnc(socket: WebSocket, user: User, machine_id: int, sid: str):
    offered = socket.headers.get("sec-websocket-protocol", "")
    await socket.accept(subprotocol="binary" if "binary" in offered else None)
    rt = _ws_runtime(user, machine_id)
    if rt is None:
        await socket.close(code=WS_NAO_ENCONTRADA, reason=NAO_ENCONTRADA)
        return
    hub, bridge, m = rt.hub, rt.bridge, rt.machine
    if hub is not None and bridge is not None and bridge.state == "missing":
        # Beckhoff sem as ferramentas no servidor: não adianta esperar, diz o que falta
        await socket.close(code=1008, reason=_close_reason(bridge.error or "Tela da Beckhoff indisponível"))
        return
    if hub is None:
        if not m.effective_vnc_enabled or m.kind == "demo":
            reason = "Tela ao vivo desligada para esta máquina"
        else:
            reason = "Defina a senha VNC de visualização no cadastro da máquina"
        await socket.close(code=1008, reason=reason)
        return
    peer = socket.client.host if socket.client else "navegador"
    rt.enter()
    try:
        await hub.serve(WebSocketTransport(socket), sid or f"ws-{id(socket):x}", peer)
    finally:
        rt.leave()


def _close_reason(text: str) -> str:
    """O motivo do fechamento do WebSocket cabe em 123 bytes (UTF-8)."""
    raw = text.encode("utf-8")
    return text if len(raw) <= 123 else raw[:120].decode("utf-8", "ignore") + "..."


class VncControl(BaseModel):
    sid: str
    action: str  # "take" | "release"


def _hub_rt(user: User, machine_id: int) -> MachineRuntime:
    rt = _rt(user, machine_id)
    if rt.hub is None:
        raise HTTPException(404, "Tela ao vivo desligada")
    return rt


@app.get("/api/vnc/status")
def vnc_status(machine: int = Query(...), sid: str = Query("", max_length=64),
               user: User = Depends(auth.current_user)):
    rt = _hub_rt(user, machine)
    out = rt.hub.status(sid)
    if rt.bridge is not None:  # Beckhoff: estado da ponte CERHost (o que falta, se conectou)
        out["bridge"] = {"state": rt.bridge.state, "error": rt.bridge.error}
    return out


@app.post("/api/vnc/control")
async def vnc_control(body: VncControl, machine: int = Query(...), user: User = Depends(auth.current_user)):
    rt = _hub_rt(user, machine)
    hub = rt.hub
    if body.action == "take":
        problem = authz.control_problem(user, rt.machine, _fleet().accounts.active_grant(rt.machine.id))
        if problem:
            raise HTTPException(403, problem)
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
        if path == "api" or path.startswith("api/"):  # rota de API inexistente não vira a página
            raise HTTPException(404, "Not Found")
        target = (dist / path).resolve()
        if path and target.is_file() and target.is_relative_to(dist.resolve()):
            return FileResponse(target)
        return FileResponse(dist / "index.html")
