"""Hub VNC da "Tela ao vivo": UMA conexao com o painel, varios espectadores, UM operador por vez.

O painel B&R aceita um so cliente VNC. Aqui o Monitor e esse cliente: guarda a imagem da tela em
memoria e a reenvia para quantas pessoas quiserem assistir (navegador ou Espelho IHM).

Papeis pela senha, como no Espelho:
  * senha de VISUALIZACAO (VNC_VIEW_PASSWORD) -> so assiste;
  * senha de OPERACAO (VNC_CONTROL_PASSWORD)  -> pode assumir o controle.

Seguranca em duas camadas:
  1. Enquanto ninguem opera, o hub fica ligado ao painel com a senha de VISUALIZACAO: o proprio
     painel recusa qualquer toque.
  2. Teclas e cliques so seguem para o painel quando vem do operador atual e o hub esta ligado com
     a senha de operacao. De qualquer outra pessoa, sao descartados aqui.
O controle volta sozinho para visualizacao depois de VNC_CONTROL_TIMEOUT segundos sem uso.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import struct
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from .vnc_des import vnc_response

log = logging.getLogger("vnc")

# formato de imagem do hub: 32 bits por pixel, true-colour, little-endian (bytes B, G, R, 0)
PIXEL_FORMAT = struct.pack(">BBBBHHHBBB3x", 32, 24, 0, 1, 255, 255, 255, 16, 8, 0)
OUR_INDEX = {"r": 2, "g": 1, "b": 0}
# formatos basicos que qualquer painel entende (o B&R trava com Tight/ZRLE): CopyRect, Raw, DesktopSize
SAFE_ENCODINGS = (1, 0, -223)
ENC_RAW, ENC_COPYRECT, ENC_DESKTOPSIZE = 0, 1, -223


class VncError(Exception):
    """Falha de protocolo ou de senha, com mensagem para mostrar ao usuario."""


# ------------------------------------------------------------------ transportes
class StreamTransport:
    """Conexao TCP comum (Espelho IHM ou outro cliente VNC na porta do hub)."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.reader, self.writer = reader, writer

    async def read(self, n: int) -> bytes:
        try:
            return await self.reader.readexactly(n)
        except asyncio.IncompleteReadError as exc:
            raise ConnectionError("cliente fechou a conexao") from exc

    async def write(self, data: bytes) -> None:
        self.writer.write(data)
        await self.writer.drain()

    async def close(self, reason: str | None = None) -> None:
        # VNC comum nao tem como dar o motivo depois do aperto de maos: so fecha
        self.writer.close()
        with contextlib.suppress(Exception):
            await self.writer.wait_closed()


class WebSocketTransport:
    """WebSocket do navegador (noVNC)."""

    def __init__(self, ws) -> None:
        self.ws = ws
        self.buf = bytearray()

    async def read(self, n: int) -> bytes:
        while len(self.buf) < n:
            msg = await self.ws.receive()
            if msg["type"] == "websocket.disconnect":
                raise ConnectionError("navegador fechou a pagina")
            self.buf += msg.get("bytes") or (msg.get("text") or "").encode("latin-1")
        out = bytes(self.buf[:n])
        del self.buf[:n]
        return out

    async def write(self, data: bytes) -> None:
        await self.ws.send_bytes(data)

    async def close(self, reason: str | None = None) -> None:
        with contextlib.suppress(Exception):
            if reason:
                await self.ws.close(code=1000, reason=reason)  # o navegador mostra o motivo
            else:
                await self.ws.close()


# ------------------------------------------------------------------ imagem
class Framebuffer:
    def __init__(self, width: int, height: int) -> None:
        self.width, self.height = width, height
        self.data = bytearray(width * height * 4)

    def _clip(self, x: int, y: int, w: int, h: int) -> tuple[int, int, int, int]:
        x, y = min(x, self.width), min(y, self.height)
        return x, y, max(0, min(w, self.width - x)), max(0, min(h, self.height - y))

    def put_raw(self, x: int, y: int, w: int, h: int, pixels: bytes) -> None:
        cx, cy, cw, ch = self._clip(x, y, w, h)
        for row in range(ch):
            src = row * w * 4
            dst = ((cy + row) * self.width + cx) * 4
            self.data[dst:dst + cw * 4] = pixels[src:src + cw * 4]

    def region(self, x: int, y: int, w: int, h: int) -> bytes:
        x, y, w, h = self._clip(x, y, w, h)
        stride = self.width * 4
        return b"".join(bytes(self.data[(y + r) * stride + x * 4:(y + r) * stride + (x + w) * 4]) for r in range(h))

    def copy_rect(self, x: int, y: int, w: int, h: int, sx: int, sy: int) -> None:
        self.put_raw(x, y, w, h, self.region(sx, sy, w, h))  # copia antes: as areas podem se sobrepor


def _bbox(rects: list[tuple[int, int, int, int]]) -> tuple[int, int, int, int]:
    x0 = min(r[0] for r in rects)
    y0 = min(r[1] for r in rects)
    x1 = max(r[0] + r[2] for r in rects)
    y1 = max(r[1] + r[3] for r in rects)
    return x0, y0, x1 - x0, y1 - y0


def pixel_map(pf: bytes) -> list[tuple[int, int]] | None:
    """Como converter do formato do hub para o pedido pelo cliente (so 32 bpp true-colour).

    Devolve pares (byte destino, byte origem) ou None se for o mesmo formato. Levanta VncError
    se o formato nao for suportado."""
    bpp, _depth, big, tc, rmax, gmax, bmax, rs, gs, bs = struct.unpack(">BBBBHHHBBB3x", pf)
    if bpp != 32 or not tc or (rmax, gmax, bmax) != (255, 255, 255) or any(s % 8 for s in (rs, gs, bs)):
        raise VncError(f"formato de cor nao suportado ({bpp} bpp)")

    def idx(shift: int) -> int:
        return 3 - shift // 8 if big else shift // 8

    pairs = [(idx(rs), OUR_INDEX["r"]), (idx(gs), OUR_INDEX["g"]), (idx(bs), OUR_INDEX["b"])]
    return None if all(d == s for d, s in pairs) else pairs


def convert(pixels: bytes, pairs: list[tuple[int, int]] | None) -> bytes:
    if pairs is None:
        return pixels
    out = bytearray(len(pixels))
    for dst, src in pairs:
        out[dst::4] = pixels[src::4]
    return bytes(out)


# ------------------------------------------------------------------ espectador
@dataclass(eq=False)
class Session:
    hub: "VncHub"
    transport: object
    sid: str
    peer: str
    auto_take: bool = False  # cliente VNC comum: a senha de operacao ja opera (como no Espelho)
    role: str | None = None
    pairs: list[tuple[int, int]] | None = None
    supports_resize: bool = False
    pending: bool = False
    full: bool = True
    resized: bool = False
    dirty: list = field(default_factory=list)
    wake: asyncio.Event = field(default_factory=asyncio.Event)
    last_input: float = 0.0
    control_since: float | None = None
    dropped: int = 0

    async def handshake(self) -> None:
        t = self.transport
        await t.write(b"RFB 003.008\n")
        ver = await t.read(12)
        if not ver.startswith(b"RFB "):
            raise VncError("handshake VNC invalido")
        version = (int(ver[4:7]), int(ver[8:11]))
        v38 = version >= (3, 8)
        if version < (3, 7):
            await t.write(struct.pack(">I", 2))
        else:
            await t.write(b"\x01\x02")  # so "senha VNC"
            if (await t.read(1)) != b"\x02":
                raise VncError("cliente escolheu um tipo de seguranca nao suportado")
        challenge = os.urandom(16)
        await t.write(challenge)
        answer = await t.read(16)
        hub = self.hub
        if hub.control_password and answer == vnc_response(hub.control_password, challenge):
            self.role = "control"
        elif hub.view_password and answer == vnc_response(hub.view_password, challenge):
            self.role = "view"
        else:
            hub.audit(f"senha recusada para {self.peer}")
            await self._fail("Senha incorreta.", v38)
            raise VncError("senha incorreta")
        if not await hub.wait_ready(hub.connect_timeout):
            await self._fail(f"Painel VNC sem resposta ({hub.error or 'conectando'}).", v38)
            raise VncError("painel sem resposta")
        await t.write(struct.pack(">I", 0))
        await t.read(1)  # ClientInit (compartilhado ou nao: o hub e sempre compartilhado)
        fb = hub.fb
        name = hub.name.encode("utf-8")
        await t.write(struct.pack(">HH", fb.width, fb.height) + PIXEL_FORMAT + struct.pack(">I", len(name)) + name)

    async def _fail(self, reason: str, v38: bool) -> None:
        msg = struct.pack(">I", 1)
        if v38:
            text = reason.encode("utf-8")
            msg += struct.pack(">I", len(text)) + text
        await self.transport.write(msg)

    async def read_loop(self) -> None:
        t = self.transport
        while True:
            kind = (await t.read(1))[0]
            if kind == 0:  # SetPixelFormat
                pf = (await t.read(19))[3:]
                try:
                    self.pairs = pixel_map(pf)
                except VncError as exc:
                    log.warning("VNC %s: %s; mantendo o formato do hub", self.peer, exc)
                self.full = True
            elif kind == 2:  # SetEncodings
                head = await t.read(3)
                n = struct.unpack(">H", head[1:])[0]
                encs = struct.unpack(f">{n}i", await t.read(4 * n)) if n else ()
                self.supports_resize = ENC_DESKTOPSIZE in encs
            elif kind == 3:  # FramebufferUpdateRequest
                body = await t.read(9)
                if not body[0]:
                    self.full = True
                self.pending = True
                self.wake.set()
            elif kind in (4, 5):  # KeyEvent / PointerEvent
                msg = bytes([kind]) + await t.read(7 if kind == 4 else 5)
                self.hub.forward_input(self, msg)
            elif kind == 6:  # ClientCutText: area de transferencia nunca vai para o painel
                head = await t.read(7)
                await t.read(struct.unpack(">I", head[3:])[0])
            elif kind == 150:  # EnableContinuousUpdates
                await t.read(9)
            elif kind == 248:  # Fence
                head = await t.read(8)
                await t.read(head[7])
            elif kind == 251:  # SetDesktopSize: o painel tem tamanho fixo
                head = await t.read(7)
                await t.read(16 * head[5])
            elif kind == 255:  # QEMU (tecla estendida)
                sub = await t.read(1)
                if sub != b"\x00":
                    raise VncError("mensagem QEMU desconhecida")
                await t.read(10)
                self.dropped += 1
            else:
                raise VncError(f"mensagem VNC desconhecida do cliente: tipo {kind}")

    def mark(self, rects: list[tuple[int, int, int, int]]) -> None:
        self.dirty.extend(rects)
        if len(self.dirty) > 32:
            self.dirty = [_bbox(self.dirty)]
        self.wake.set()

    async def send_loop(self) -> None:
        while True:
            await self.wake.wait()
            self.wake.clear()
            fb = self.hub.fb
            if not self.pending or fb is None or not self.hub.ready:
                continue
            if self.full:
                rects = [(0, 0, fb.width, fb.height)]
            elif self.dirty:
                rects = self.dirty if len(self.dirty) <= 8 else [_bbox(self.dirty)]
            else:
                continue
            self.full, self.dirty, self.pending = False, [], False
            parts = []
            if self.resized and self.supports_resize:
                parts.append(struct.pack(">HHHHi", 0, 0, fb.width, fb.height, ENC_DESKTOPSIZE))
            self.resized = False
            for x, y, w, h in rects:
                x, y, w, h = fb._clip(x, y, w, h)
                if w and h:
                    parts.append(struct.pack(">HHHHi", x, y, w, h, ENC_RAW) + convert(fb.region(x, y, w, h), self.pairs))
            await self.transport.write(b"\x00\x00" + struct.pack(">H", len(parts)) + b"".join(parts))


# ------------------------------------------------------------------ hub
class VncHub:
    retry_delay = 3.0  # espera entre tentativas de ligar no painel (os testes encurtam)

    def __init__(self, host: str, port: int, view_password: str, control_password: str = "",
                 control_timeout: float = 300, name: str = "Painel", audit_path: str | None = None,
                 idle_grace: float = 20, connect_timeout: float = 12, min_interval: float = 0.1,
                 port_provider=None) -> None:
        self.host, self.port = host, port
        # Beckhoff: a porta e a do x11vnc da ponte CERHost, que pode ainda nao existir ou mudar.
        # O provedor e consultado a cada tentativa; None = tela ainda nao pronta (tenta de novo).
        self.port_provider = port_provider
        self.view_password, self.control_password = view_password, control_password
        self.control_timeout, self.name, self.audit_path = control_timeout, name, audit_path
        self.idle_grace, self.connect_timeout, self.min_interval = idle_grace, connect_timeout, min_interval
        self.sessions: dict[str, Session] = {}
        self.fb: Framebuffer | None = None
        self.controller: Session | None = None
        self.mode = "view"
        self.state = "idle"  # idle | connecting | connected | error
        self.error: str | None = None
        self.events: deque = deque(maxlen=50)
        self._ready = asyncio.Event()
        self._panel_task: asyncio.Task | None = None
        self._stop_task: asyncio.Task | None = None
        self._watch_task: asyncio.Task | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._tcp_server: asyncio.base_events.Server | None = None
        self._closed = False
        self.tcp_port = 0
        self.tcp_auto_take = True

    @property
    def ready(self) -> bool:
        return self._ready.is_set()

    def current_port(self) -> int | None:
        return self.port_provider() if self.port_provider is not None else self.port

    # ---------------- registro
    def audit(self, text: str) -> None:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        self.events.append({"time": time.time(), "text": text})
        machine = f"[{self.name} {self.host}:{self.port}]"  # o registro e um so para todas as maquinas
        log.info("VNC %s %s", machine, text)
        if self.audit_path:
            try:
                Path(self.audit_path).parent.mkdir(parents=True, exist_ok=True)
                with open(self.audit_path, "a", encoding="utf-8") as fh:
                    fh.write(f"{stamp} {machine} {text}\n")
            except OSError as exc:
                log.warning("VNC: nao consegui gravar o registro (%s)", exc)

    # ---------------- espectadores
    async def serve(self, transport, sid: str, peer: str, auto_take: bool = False) -> None:
        if sid in self.sessions:
            sid = f"{sid}-{os.urandom(3).hex()}"
        s = Session(self, transport, sid, peer, auto_take)
        self.sessions[sid] = s
        self._wake_panel()
        try:
            await s.handshake()
            self.audit(f"{peer} entrou ({'operacao' if s.role == 'control' else 'visualizacao'})")
            tasks = [asyncio.create_task(s.read_loop()), asyncio.create_task(s.send_loop())]
            try:
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    exc = task.exception()
                    if exc and not isinstance(exc, ConnectionError):
                        log.info("VNC %s encerrado: %s", peer, exc)
            finally:
                for task in tasks:
                    task.cancel()
        except (VncError, ConnectionError) as exc:
            log.info("VNC %s: %s", peer, exc)
        finally:
            self.sessions.pop(sid, None)
            if s.role:
                self.audit(f"{peer} saiu")
            if self.controller is s:
                await self.release(s, "operador desconectou")
            await transport.close()
            if not self.sessions:
                self._schedule_stop()

    def _wake_panel(self) -> None:
        if self._closed:
            return
        if self._stop_task:
            self._stop_task.cancel()
            self._stop_task = None
        if self._panel_task is None or self._panel_task.done():
            self._panel_task = asyncio.create_task(self._panel_main())
        if self._watch_task is None or self._watch_task.done():
            self._watch_task = asyncio.create_task(self._watch())

    def _schedule_stop(self) -> None:
        if self._closed:
            return

        async def later():
            await asyncio.sleep(self.idle_grace)
            if not self.sessions:
                await self._stop_panel()
                self.state, self.error = "idle", None
                log.info("VNC: ninguem assistindo, painel liberado")
        self._stop_task = asyncio.create_task(later())

    async def wait_ready(self, timeout: float) -> bool:
        try:
            await asyncio.wait_for(self._ready.wait(), timeout)
            return True
        except asyncio.TimeoutError:
            return False

    # ---------------- controle
    async def take(self, sid: str) -> tuple[bool, str]:
        s = self.sessions.get(sid)
        if s is None or s.role is None:
            return False, "Sessao nao encontrada. Recarregue a Tela ao vivo."
        if s.role != "control":
            return False, "Esta sessao entrou com a senha de visualizacao."
        if not self.control_password:
            return False, "Operacao desligada (VNC_CONTROL_PASSWORD vazio)."
        if self.controller is s:
            return True, "Voce ja esta operando."
        if self.controller is not None:
            return False, f"Em uso por outro operador ({self.controller.peer})."
        self.controller = s
        s.last_input = time.monotonic()
        s.control_since = time.time()
        self.audit(f"{s.peer} assumiu o controle")
        await self._switch_mode("control")
        return True, "Controle assumido."

    async def release(self, s: Session | str, reason: str = "liberado") -> bool:
        if isinstance(s, str):
            s = self.sessions.get(s)
        if s is None or self.controller is not s:
            return False
        self.controller = None
        s.control_since = None
        self.audit(f"{s.peer} deixou o controle ({reason})")
        await self._switch_mode("view")
        return True

    def forward_input(self, s: Session, msg: bytes) -> None:
        if self.controller is s and self.mode == "control" and self.ready and self._writer is not None:
            self._writer.write(msg)
            s.last_input = time.monotonic()
            return
        s.dropped += 1
        if s.auto_take and s.role == "control" and self.controller is None and self.control_password:
            asyncio.create_task(self.take(s.sid))  # cliente VNC comum: o primeiro toque assume

    async def _watch(self) -> None:
        while True:
            await asyncio.sleep(1)
            c = self.controller
            if c is not None and time.monotonic() - c.last_input > self.control_timeout:
                await self.release(c, f"{int(self.control_timeout // 60)} min sem uso")

    def status(self, sid: str | None = None) -> dict:
        s = self.sessions.get(sid) if sid else None
        c = self.controller
        out = {
            "panel": {"state": self.state, "mode": self.mode, "error": self.error},
            "viewers": sum(1 for x in self.sessions.values() if x.role),
            "role": s.role if s else None,
            "control_available": bool(self.control_password),
            "timeout": self.control_timeout,
            "controller": None,
        }
        if c is not None:
            out["controller"] = {
                "me": c is s,
                "peer": c.peer,
                "since": c.control_since,
                "seconds_left": max(0, int(self.control_timeout - (time.monotonic() - c.last_input))),
            }
        if s is not None and s.role == "control":
            out["events"] = list(self.events)[-10:]
        return out

    # ---------------- painel
    async def _switch_mode(self, mode: str) -> None:
        if mode == self.mode:
            return
        self.mode = mode
        await self._stop_panel()
        if self.sessions:
            self._wake_panel()

    async def _stop_panel(self) -> None:
        self._ready.clear()
        task, self._panel_task = self._panel_task, None
        if task and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def _panel_main(self) -> None:
        while self.sessions:
            mode = self.mode
            password = self.control_password if mode == "control" else self.view_password
            self.state = "connecting"
            writer = None
            try:
                port = self.current_port()
                if port is None:
                    raise ConnectionError("a tela da Beckhoff ainda não está pronta")
                self.port = port
                reader, writer = await asyncio.wait_for(asyncio.open_connection(self.host, self.port), 10)
                await self._panel_handshake(reader, writer, password)
                self._writer = writer
                self.state, self.error = "connected", None
                log.info("VNC: ligado ao painel %s:%s (%s)", self.host, self.port,
                         "operacao" if mode == "control" else "visualizacao")
                self._ready.set()
                await self._panel_loop(reader, writer)
                raise ConnectionError("o painel encerrou a conexao")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - qualquer falha vira estado de erro e nova tentativa
                self._ready.clear()
                self.state, self.error = "error", f"{type(exc).__name__}: {exc}"[:200]
                log.warning("VNC: painel %s:%s: %s", self.host, self.port, self.error)
                if mode == "control" and isinstance(exc, VncError):
                    self.mode = "view"  # senha de operacao recusada: volta para so visualizacao
                    if self.controller is not None:
                        self.audit(f"painel recusou a senha de operacao; {self.controller.peer} perdeu o controle")
                        self.controller = None
                    continue
                await asyncio.sleep(self.retry_delay)
            finally:
                self._writer = None
                if writer is not None:
                    writer.close()

    async def _panel_handshake(self, reader, writer, password: str) -> None:
        ver = await reader.readexactly(12)
        if not ver.startswith(b"RFB "):
            raise VncError("o painel nao respondeu como VNC")
        version = (int(ver[4:7]), int(ver[8:11]))
        if version < (3, 7):
            writer.write(b"RFB 003.003\n")
            sec = struct.unpack(">I", await reader.readexactly(4))[0]
            if sec == 0:
                raise VncError(await _reason(reader))
            has_result = sec == 2
        else:
            mine = b"RFB 003.008\n" if version >= (3, 8) else b"RFB 003.007\n"
            writer.write(mine)
            n = (await reader.readexactly(1))[0]
            if n == 0:
                raise VncError(await _reason(reader))
            offered = list(await reader.readexactly(n))
            if 2 in offered:
                sec = 2
            elif 1 in offered:
                sec = 1
            else:
                raise VncError(f"o painel so oferece seguranca nao suportada {offered}")
            writer.write(bytes([sec]))
            has_result = sec == 2 or version >= (3, 8)
        if sec == 2:
            challenge = await reader.readexactly(16)
            writer.write(vnc_response(password, challenge))
        elif sec != 1:
            raise VncError(f"seguranca VNC nao suportada: {sec}")
        if has_result:
            result = struct.unpack(">I", await reader.readexactly(4))[0]
            if result != 0:
                reason = await _reason(reader) if version >= (3, 8) else ""
                raise VncError(f"o painel recusou a senha {reason}".strip())
        writer.write(b"\x01")  # ClientInit compartilhado
        width, height = struct.unpack(">HH", await reader.readexactly(4))
        await reader.readexactly(16)  # formato nativo do painel (trocamos logo abaixo)
        name_len = struct.unpack(">I", await reader.readexactly(4))[0]
        await reader.readexactly(name_len)
        if self.fb is None or (self.fb.width, self.fb.height) != (width, height):
            self.fb = Framebuffer(width, height)
            for s in self.sessions.values():
                s.full, s.resized = True, True
        writer.write(b"\x00\x00\x00\x00" + PIXEL_FORMAT)
        writer.write(b"\x02\x00" + struct.pack(">H", len(SAFE_ENCODINGS)) + struct.pack(f">{len(SAFE_ENCODINGS)}i", *SAFE_ENCODINGS))
        writer.write(b"\x03\x00" + struct.pack(">HHHH", 0, 0, width, height))
        await writer.drain()

    async def _panel_loop(self, reader, writer) -> None:
        while True:
            kind = (await reader.readexactly(1))[0]
            if kind == 0:
                started = time.monotonic()
                n = struct.unpack(">H", (await reader.readexactly(3))[1:])[0]
                changed = []
                for _ in range(n):
                    x, y, w, h, enc = struct.unpack(">HHHHi", await reader.readexactly(12))
                    if enc == ENC_RAW:
                        self.fb.put_raw(x, y, w, h, await reader.readexactly(w * h * 4))
                        changed.append((x, y, w, h))
                    elif enc == ENC_COPYRECT:
                        sx, sy = struct.unpack(">HH", await reader.readexactly(4))
                        self.fb.copy_rect(x, y, w, h, sx, sy)
                        changed.append((x, y, w, h))
                    elif enc == ENC_DESKTOPSIZE:
                        self.fb = Framebuffer(w, h)
                        for s in self.sessions.values():
                            s.full, s.resized = True, True
                            s.wake.set()
                    else:
                        raise VncError(f"o painel mandou um formato de imagem inesperado ({enc})")
                if changed:
                    for s in self.sessions.values():
                        if s.role:
                            s.mark(changed)
                await asyncio.sleep(max(0.0, self.min_interval - (time.monotonic() - started)))
                writer.write(b"\x03\x01" + struct.pack(">HHHH", 0, 0, self.fb.width, self.fb.height))
                await writer.drain()
            elif kind == 1:  # SetColourMapEntries (nao usamos paleta)
                head = await reader.readexactly(5)
                await reader.readexactly(6 * struct.unpack(">H", head[3:])[0])
            elif kind == 2:  # Bell
                continue
            elif kind == 3:  # ServerCutText: ignorado
                head = await reader.readexactly(7)
                await reader.readexactly(struct.unpack(">I", head[3:])[0])
            else:
                raise VncError(f"mensagem desconhecida do painel: tipo {kind}")

    # ---------------- porta VNC comum (Espelho IHM)
    async def start_tcp(self, bind: str, port: int, auto_take: bool = True) -> None:
        """Porta VNC comum. auto_take=False: a senha de operacao so assiste (o primeiro toque nao assume)."""
        self.tcp_auto_take = auto_take

        async def handle(reader, writer):
            peer = writer.get_extra_info("peername")
            label = f"{peer[0]} (VNC)" if peer else "VNC"
            await self.serve(StreamTransport(reader, writer), f"tcp-{os.urandom(6).hex()}", label,
                             auto_take=self.tcp_auto_take)

        self._tcp_server = await asyncio.start_server(handle, bind, port)
        self.tcp_port = self._tcp_server.sockets[0].getsockname()[1]
        log.info("VNC: hub aceitando clientes VNC em %s:%s%s", bind, self.tcp_port,
                 "" if auto_take else " (so visualizacao)")

    async def close(self, reason: str | None = None) -> None:
        self._closed = True
        if self._tcp_server:
            self._tcp_server.close()
        for s in list(self.sessions.values()):
            await s.transport.close(reason)
        for task in (self._watch_task, self._stop_task):
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        await self._stop_panel()


async def _reason(reader) -> str:
    n = struct.unpack(">I", await reader.readexactly(4))[0]
    return (await reader.readexactly(n)).decode("utf-8", "replace")
