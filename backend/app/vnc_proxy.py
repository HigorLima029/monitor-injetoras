"""Ponte WebSocket -> VNC (porta 5900 do painel) para a "Tela ao vivo", SOMENTE VISUALIZACAO.

O navegador (noVNC) fala RFB por WebSocket; aqui o trafego e repassado ao painel por TCP.
No sentido navegador -> painel, o protocolo e lido mensagem a mensagem e tudo que mexeria
na maquina e descartado: teclas (KeyEvent), cliques/movimento (PointerEvent), area de
transferencia, mudanca de resolucao e mensagens desconhecidas (essas encerram a conexao).
Assim a tela nunca recebe um toque, mesmo que alguem altere o codigo do navegador.
"""
from __future__ import annotations

import struct


class ReadOnlyFilter:
    """Filtra o fluxo cliente -> servidor do RFB, deixando passar so handshake e pedidos de imagem."""

    # tipos de mensagem do cliente (RFC 6143 + extensoes que o noVNC usa)
    PASS_FIXED = {0: 20, 3: 10, 150: 10}  # SetPixelFormat, FramebufferUpdateRequest, EnableContinuousUpdates

    def __init__(self) -> None:
        self.buf = b""
        self.stage = "version"  # version -> security -> auth -> init -> messages
        self.version = (3, 8)
        self.security_type: int | None = None
        self.dropped = 0
        self._sbuf = b""
        self._server_done = False
        self.offered: list[int] = []
        self._version_sent = False
        self._server_version = (3, 8)

    SUPPORTED_SECURITY = (1, 2)  # "sem senha" e "senha VNC"

    def server_filter(self, data: bytes) -> bytes:
        """Repassa o que o painel envia. No inicio, deixa na lista de seguranca so os tipos
        que este filtro entende (sem senha / senha VNC), para o navegador nao escolher outro
        (ex.: Tight, VeNCrypt), cujo handshake o filtro nao saberia acompanhar.
        A versao (12 bytes) segue na hora: o painel espera a resposta antes de mandar a lista."""
        if self._server_done:
            return data
        self._sbuf += data
        out = b""
        if not self._version_sent:
            if len(self._sbuf) < 12:
                return b""
            head, self._sbuf = self._sbuf[:12], self._sbuf[12:]
            self._version_sent = True
            out += head
            try:
                self._server_version = (int(head[4:7]), int(head[8:11]))
            except ValueError:
                self._server_done = True
                return out + self._sbuf
        b = self._sbuf
        if self._server_version < (3, 7):  # 3.3: o servidor ja diz o tipo em 4 bytes
            if len(b) < 4:
                return out
            self.security_type = struct.unpack(">I", b[:4])[0]
            self.offered = [self.security_type]
            self._server_done = True
            return out + b
        if len(b) < 1:
            return out
        n = b[0]
        if n == 0:  # o painel recusou (segue o motivo em texto)
            self._server_done = True
            return out + b
        if len(b) < 1 + n:
            return out
        self.offered = list(b[1:1 + n])
        keep = [t for t in self.offered if t in self.SUPPORTED_SECURITY] or self.offered
        self._server_done = True
        return out + bytes([len(keep)]) + bytes(keep) + b[1 + n:]

    def feed(self, data: bytes) -> bytes:
        """Recebe bytes do navegador e devolve so o que pode seguir para o painel."""
        self.buf += data
        out = b""
        while True:
            if self.stage == "version":
                if len(self.buf) < 12:
                    break
                chunk, self.buf = self.buf[:12], self.buf[12:]
                if not chunk.startswith(b"RFB "):
                    raise ValueError("handshake VNC invalido")
                self.version = (int(chunk[4:7]), int(chunk[8:11]))
                out += chunk
                self.stage = "security" if self.version >= (3, 7) else "auth"
            elif self.stage == "security":
                if len(self.buf) < 1:
                    break
                self.security_type = self.buf[0]
                out += self.buf[:1]
                self.buf = self.buf[1:]
                self.stage = "auth"
            elif self.stage == "auth":
                if self.security_type is None:
                    break  # versao 3.3: espera o servidor dizer o tipo
                if self.security_type == 2:  # VNC Authentication: resposta de 16 bytes
                    if len(self.buf) < 16:
                        break
                    out += self.buf[:16]
                    self.buf = self.buf[16:]
                elif self.security_type != 1:  # so "sem senha" e "senha VNC" sao aceitos
                    raise ValueError(f"tipo de seguranca VNC nao suportado: {self.security_type} (painel ofereceu {self.offered})")
                self.stage = "init"
            elif self.stage == "init":
                if len(self.buf) < 1:
                    break
                out += b"\x01"  # ClientInit: sessao compartilhada (nao derruba outro visualizador)
                self.buf = self.buf[1:]
                self.stage = "messages"
            else:
                n = self._message_length()
                if n is None or len(self.buf) < n:
                    break
                kind = self.buf[0]
                msg, self.buf = self.buf[:n], self.buf[n:]
                if kind in (0, 2, 3, 150, 248):
                    out += msg
                else:
                    self.dropped += 1  # tecla, clique, clipboard, resolucao: descartado
        return out

    def _message_length(self) -> int | None:
        b = self.buf
        if not b:
            return None
        kind = b[0]
        if kind in self.PASS_FIXED:
            return self.PASS_FIXED[kind]
        if kind == 2:  # SetEncodings
            return None if len(b) < 4 else 4 + 4 * struct.unpack(">H", b[2:4])[0]
        if kind == 4:  # KeyEvent
            return 8
        if kind == 5:  # PointerEvent
            return 6
        if kind == 6:  # ClientCutText
            return None if len(b) < 8 else 8 + struct.unpack(">I", b[4:8])[0]
        if kind == 248:  # Fence
            return None if len(b) < 9 else 9 + b[8]
        if kind == 251:  # SetDesktopSize
            return None if len(b) < 8 else 8 + 16 * b[6]
        if kind == 255 and len(b) >= 2 and b[1] == 0:  # QEMU extended key event
            return 12
        raise ValueError(f"mensagem VNC desconhecida do navegador: tipo {kind}")
