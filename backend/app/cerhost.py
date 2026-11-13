"""Ponte CERHost: a tela da Beckhoff (Windows CE) vira VNC para a "Tela ao vivo".

O painel da Beckhoff so fala o protocolo fechado do CERHost (porta 987). Entao o proprio servidor
roda o CERHOST.exe no Wine, numa tela virtual, e serve essa tela por VNC so em 127.0.0.1, onde o
VncHub a consome como se fosse um painel VNC comum:

    Beckhoff :987 -- CERHOST.exe (Wine 32) em Xvfb + Openbox -- x11vnc (127.0.0.1) -- VncHub

Regras (comprovadas em bancada):
  * O CERHost nao aceita IP por linha de comando: o robo (xdotool) abre o menu e digita o IP.
    Ele so digita ENQUANTO NAO ESTA CONECTADO. Conectado, nada mais e enviado a tela a nao ser o
    que o operador fizer pelo hub.
  * Conectado = a janela principal deixa de se chamar "Remote Display Control for Windows CE" e
    aparece uma janela com o nome do painel (ex.: CP-72B8D3).
  * Sem gerenciador de janelas o Wine nao recebe foco nem redimensiona: por isso o Openbox.
  * As senhas VNC vao para o x11vnc por arquivo (0600, pasta privada, apagado depois de lido),
    nunca pela linha de comando nem no log.
  * Nada aqui escreve no CLP.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import socket
import tempfile
from pathlib import Path

log = logging.getLogger("cerhost")

TOOLS = ("Xvfb", "openbox", "wine", "xdotool", "wmctrl", "x11vnc")
MAIN_TITLE = "Remote Display Control for Windows CE"
IGNORED_TITLES = frozenset({MAIN_TITLE, "Default IME"})
SCREEN = "800x1000x24"  # o painel e em pe (~600x850 com a barra do CERHost)


def missing_tools(cerhost_exe: str) -> list[str]:
    """O que falta no servidor para a tela da Beckhoff (vazio = tudo certo)."""
    missing = [tool for tool in TOOLS if shutil.which(tool) is None]
    if not cerhost_exe or not os.path.isfile(cerhost_exe):
        missing.append("CERHOST.exe")
    return missing


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class SubprocessRunner:
    """Executor de verdade (os testes trocam por um falso)."""

    async def spawn(self, argv: list[str], env: dict[str, str]):
        return await asyncio.create_subprocess_exec(
            *argv, env=env, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True)

    async def run(self, argv: list[str], env: dict[str, str], timeout: float) -> tuple[int, str]:
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, env=env, stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        except OSError:
            return -1, ""
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout)
        except asyncio.TimeoutError:
            await self._reap(proc)
            return -1, ""
        except asyncio.CancelledError:
            await self._reap(proc)
            raise
        return proc.returncode, out.decode("utf-8", "replace")

    @staticmethod
    async def _reap(proc) -> None:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()


class BridgeError(Exception):
    """Falha ao subir/conectar, com mensagem para mostrar ao usuario."""

    def __init__(self, message: str, restart_cerhost: bool = False) -> None:
        super().__init__(message)
        self.restart_cerhost = restart_cerhost


class CerhostBridge:
    """Sobe Xvfb + Openbox + CERHost, conecta no painel e serve a tela por x11vnc em 127.0.0.1."""

    # tempos (os testes encurtam)
    watch_interval = 5.0     # vigia: a cada quantos segundos confere a ponte
    poll_interval = 0.5      # espera de janelas/tela
    ready_timeout = 10.0     # Xvfb e Openbox prontos
    window_timeout = 30.0    # janela do CERHost aparecer (na bancada: 5-8 s)
    connect_timeout = 30.0   # conectar no painel depois de digitar o IP
    key_delay = 0.5          # pausa entre as teclas do robo (menu e dialogo abrirem)
    kill_timeout = 3.0       # SIGTERM -> SIGKILL
    cmd_timeout = 5.0        # cada xdotool/wmctrl

    def __init__(self, host: str, display: int, cerhost_exe: str, view_password: str,
                 control_password: str = "", runner=None) -> None:
        self.host = host
        self.display = display
        self.cerhost_exe = cerhost_exe
        self.view_password = view_password
        self.control_password = control_password
        self.runner = runner or SubprocessRunner()
        self.state = "stopped"
        self.error: str | None = None
        self.port: int | None = None
        self._procs: dict[str, object] = {}
        self._task: asyncio.Task | None = None
        self._lock: asyncio.Lock | None = None
        self._tmpdir: str | None = None

    # ------------------------------------------------------------------ ciclo de vida
    @property
    def _env(self) -> dict[str, str]:
        return {**os.environ, "DISPLAY": f":{self.display}", "WINEARCH": "win32"}

    async def start(self) -> None:
        if self._task is not None:
            return
        missing = missing_tools(self.cerhost_exe)
        if missing:
            self.state = "missing"
            self.error = "Para a tela da Beckhoff falta no servidor: " + ", ".join(missing) + "."
            log.warning("tela da Beckhoff desligada: falta %s", ", ".join(missing))
            return
        if not self.view_password:
            self.state = "error"
            self.error = "Tela da Beckhoff sem senha de visualização (VNC_VIEW_PASSWORD)."
            log.warning("tela da Beckhoff desligada: sem senha de visualização")
            return
        self._lock = asyncio.Lock()
        first = asyncio.Event()
        self._task = asyncio.create_task(self._run(first))
        await first.wait()

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await self._stop_procs(("x11vnc", "cerhost", "openbox", "xvfb"))
        if self.state != "missing":
            self.state = "stopped"
            self.error = None

    async def check(self) -> None:
        """Uma volta do vigia: refaz o que caiu. So digita se nao estiver conectado."""
        if self._lock is None:
            return
        async with self._lock:
            await self._ensure()

    async def _run(self, first: asyncio.Event) -> None:
        try:
            await self.check()
        finally:
            first.set()
        while True:
            await asyncio.sleep(self.watch_interval)
            try:
                await self.check()
            except asyncio.CancelledError:
                raise
            except Exception:  # o vigia nunca morre
                log.exception("falha no vigia do CERHost")

    # ------------------------------------------------------------------ vigia
    async def _ensure(self) -> None:
        try:
            if not (self._alive("xvfb") and self._alive("openbox")):
                await self._stop_procs(("x11vnc", "cerhost", "openbox", "xvfb"))
                await self._start_display()
            if not self._alive("cerhost"):
                self.state = "starting"
                log.info("abrindo o CERHost para %s", self.host)
                self._procs["cerhost"] = await self.runner.spawn(["wine", self.cerhost_exe], self._env)
            if await self._connected_title() is None:
                await self._connect()
            if not self._alive("x11vnc"):
                await self._start_vnc()
            if self.state != "connected":
                log.info("tela da Beckhoff pronta em 127.0.0.1:%s", self.port)
            self.state, self.error = "connected", None
        except (BridgeError, OSError) as exc:
            self.state, self.error = "error", f"Tela da Beckhoff: {exc}"
            log.warning("CERHost: %s (nova tentativa em %ss)", exc, self.watch_interval)
            if isinstance(exc, BridgeError) and exc.restart_cerhost:
                # comeca limpo na proxima volta (sem dialogo de erro aberto no caminho)
                await self._stop_procs(("cerhost",))

    async def _start_display(self) -> None:
        self.state = "starting"
        d = f":{self.display}"
        self._procs["xvfb"] = await self.runner.spawn(
            ["Xvfb", d, "-screen", "0", SCREEN, "-nolisten", "tcp"], self._env)
        if not await self._wait(self._display_ready, self.ready_timeout):
            raise BridgeError(f"a tela virtual {d} (Xvfb) não subiu")
        self._procs["openbox"] = await self.runner.spawn(["openbox"], self._env)
        if not await self._wait(self._wm_ready, self.ready_timeout):
            raise BridgeError("o Openbox não subiu na tela virtual")

    async def _connect(self) -> None:
        """Conecta pelo teclado. Chamado SO quando a janela conectada nao existe."""
        self.state = "connecting"
        wid = await self._wait(self._main_window, self.window_timeout)
        if wid is None:
            raise BridgeError(f"a janela do CERHost não apareceu em {self.window_timeout:g} s",
                              restart_cerhost=True)
        log.info("conectando o CERHost em %s", self.host)
        for keys in (["windowfocus", "--sync", wid], ["key", "alt+f"], ["key", "Return"],
                     ["type", "--delay", "50", self.host], ["key", "Return"]):
            await self._cmd(["xdotool", *keys])
            if self.key_delay:
                await asyncio.sleep(self.key_delay)
        title = await self._wait(self._connected_title, self.connect_timeout)
        if title is None:
            raise BridgeError(f"o CERHost não conectou em {self.host} em {self.connect_timeout:g} s",
                              restart_cerhost=True)
        await self._cmd(["wmctrl", "-r", title, "-b", "add,maximized_vert,maximized_horz"])
        log.info("CERHost conectado em %s (%s)", self.host, title)

    async def _start_vnc(self) -> None:
        self._drop_tmpdir()
        self._tmpdir = tempfile.mkdtemp(prefix="cerhost-")  # 0700
        path = Path(self._tmpdir) / "senhas"
        lines = ([self.control_password, "__BEGIN_VIEWONLY__", self.view_password]
                 if self.control_password else [self.view_password])
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        port = _free_port()
        argv = ["x11vnc", "-display", f":{self.display}", "-localhost", "-rfbport", str(port),
                "-forever", "-shared", "-quiet", "-passwdfile", f"rm:{path}"]
        if not self.control_password:
            argv.append("-viewonly")
        self._procs["x11vnc"] = await self.runner.spawn(argv, self._env)
        self.port = port

    # ------------------------------------------------------------------ consultas
    async def _cmd(self, argv: list[str]) -> tuple[int, str]:
        return await self.runner.run(argv, self._env, self.cmd_timeout)

    async def _display_ready(self) -> bool:
        return (await self._cmd(["xdotool", "getdisplaygeometry"]))[0] == 0

    async def _wm_ready(self) -> bool:
        return (await self._cmd(["wmctrl", "-m"]))[0] == 0

    async def _windows(self) -> dict[str, str]:
        """Janelas visiveis da tela virtual: id -> titulo."""
        rc, out = await self._cmd(["xdotool", "search", "--onlyvisible", "--name", "."])
        if rc != 0:
            return {}
        titles = {}
        for wid in out.split():
            rc, name = await self._cmd(["xdotool", "getwindowname", wid])
            if rc == 0:
                titles[wid] = name.strip()
        return titles

    async def _main_window(self) -> str | None:
        for wid, title in (await self._windows()).items():
            if title == MAIN_TITLE:
                return wid
        return None

    async def _connected_title(self) -> str | None:
        """Titulo da janela conectada (nome do painel) ou None se nao estiver conectado."""
        if not self._alive("cerhost"):
            return None
        titles = list((await self._windows()).values())
        if MAIN_TITLE in titles:  # janela principal com o titulo original = nao conectado
            return None
        return next((t for t in titles if t and t not in IGNORED_TITLES), None)

    async def _wait(self, probe, timeout: float):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            result = await probe()
            if result:
                return result
            if loop.time() >= deadline:
                return None
            await asyncio.sleep(self.poll_interval)

    # ------------------------------------------------------------------ processos
    def _alive(self, name: str) -> bool:
        proc = self._procs.get(name)
        return proc is not None and proc.returncode is None

    async def _stop_procs(self, names) -> None:
        for name in names:
            proc = self._procs.pop(name, None)
            if name == "x11vnc":
                self.port = None
                self._drop_tmpdir()
            if proc is None or proc.returncode is not None:
                continue
            with contextlib.suppress(ProcessLookupError):
                proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), self.kill_timeout)
            except asyncio.TimeoutError:
                log.warning("%s não saiu com SIGTERM em %ss; forçando", name, self.kill_timeout)
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(proc.wait(), self.kill_timeout)

    def _drop_tmpdir(self) -> None:
        if self._tmpdir:
            shutil.rmtree(self._tmpdir, ignore_errors=True)
            self._tmpdir = None
