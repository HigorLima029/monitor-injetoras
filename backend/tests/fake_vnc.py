"""Painel VNC falso que imita o B&R: RFB 3.3, senha VNC, UM cliente por vez e duas senhas.

Com a senha de visualizacao o painel aceita a conexao mas ignora toques (como o B&R real);
com a de operacao ele aceita. Tudo que chega fica registrado para os testes conferirem.
"""
from __future__ import annotations

import asyncio
import os
import struct
import threading

from app.vnc_des import vnc_response

PF16 = struct.pack(">BBBBHHHBBB3x", 16, 16, 0, 1, 31, 63, 31, 11, 5, 0)  # formato nativo (o hub troca)


def pixel(x: int, y: int) -> bytes:
    return bytes([x & 0xFF, y & 0xFF, 0x80, 0])  # B, G, R, 0 no formato do hub


class FakePanel:
    def __init__(self, view_pw: str, control_pw: str, width: int = 8, height: int = 6,
                 version: bytes = b"RFB 003.003\n") -> None:
        self.view_pw, self.control_pw = view_pw, control_pw
        self.width, self.height, self.version = width, height, version
        self.connections: list[dict] = []
        self.active = 0
        self.max_active = 0
        self.refused_busy = 0
        self.port = 0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._server = None

    async def handle(self, reader, writer):
        if self.active >= 1:  # o B&R derruba quem chega com outro cliente ligado
            self.refused_busy += 1
            writer.close()
            return
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        conn = {"role": None, "applied": [], "ignored": [], "pixel_format": None, "encodings": None}
        self.connections.append(conn)
        try:
            writer.write(self.version)
            await reader.readexactly(12)
            writer.write(struct.pack(">I", 2))
            challenge = os.urandom(16)
            writer.write(challenge)
            answer = await reader.readexactly(16)
            if answer == vnc_response(self.control_pw, challenge):
                conn["role"] = "control"
            elif answer == vnc_response(self.view_pw, challenge):
                conn["role"] = "view"
            else:
                writer.write(struct.pack(">I", 1))
                await writer.drain()
                return
            writer.write(struct.pack(">I", 0))
            await reader.readexactly(1)
            name = b"B&R falso"
            writer.write(struct.pack(">HH", self.width, self.height) + PF16 + struct.pack(">I", len(name)) + name)
            await writer.drain()
            tick = 0
            while True:
                kind = (await reader.readexactly(1))[0]
                if kind == 0:
                    conn["pixel_format"] = (await reader.readexactly(19))[3:]
                elif kind == 2:
                    n = struct.unpack(">H", (await reader.readexactly(3))[1:])[0]
                    conn["encodings"] = list(struct.unpack(f">{n}i", await reader.readexactly(4 * n)))
                elif kind == 3:
                    incremental = (await reader.readexactly(9))[0]
                    if incremental:
                        await asyncio.sleep(0.05)
                        tick += 1
                        rect = struct.pack(">HHHHi", 0, 0, 1, 1, 0) + bytes([tick & 0xFF, 0, 0x80, 0])
                    else:
                        data = b"".join(pixel(x, y) for y in range(self.height) for x in range(self.width))
                        rect = struct.pack(">HHHHi", 0, 0, self.width, self.height, 0) + data
                    writer.write(b"\x00\x00\x00\x01" + rect)
                    await writer.drain()
                elif kind in (4, 5):
                    msg = bytes([kind]) + await reader.readexactly(7 if kind == 4 else 5)
                    (conn["applied"] if conn["role"] == "control" else conn["ignored"]).append(msg)
                else:
                    return
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            self.active -= 1
            try:
                writer.close()
            except RuntimeError:  # loop do teste ja encerrado
                pass

    # roda num thread proprio, para servir tambem os testes com o TestClient
    def start(self) -> int:
        ready = threading.Event()

        def run():
            self._loop = asyncio.new_event_loop()

            async def boot():
                self._server = await asyncio.start_server(self.handle, "127.0.0.1", 0)
                self.port = self._server.sockets[0].getsockname()[1]
                ready.set()

            self._loop.run_until_complete(boot())
            self._loop.run_forever()

        threading.Thread(target=run, daemon=True).start()
        ready.wait(5)
        return self.port

    def stop(self) -> None:
        if self._loop:
            self._loop.call_soon_threadsafe(self._loop.stop)

    def applied(self) -> list[bytes]:
        return [m for c in self.connections for m in c["applied"]]

    def roles(self) -> list[str | None]:
        return [c["role"] for c in self.connections]
