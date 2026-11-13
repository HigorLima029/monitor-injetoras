"""Ponte CERHost: a tela da Beckhoff (Windows CE) vira VNC para a "Tela ao vivo".

O painel da Beckhoff so fala o protocolo fechado do CERHost (porta 987). Entao o proprio servidor
roda o CERHOST.exe no Wine, numa tela virtual, e serve essa tela por VNC so em 127.0.0.1, onde o
VncHub a consome como se fosse um painel VNC comum:

    Beckhoff :987 -- CERHOST.exe (Wine 32) em Xvfb + Openbox -- x11vnc (127.0.0.1) -- VncHub

Regras (comprovadas em bancada):
  * O CERHost nao aceita IP por linha de comando: o robo (xdotool) abre o menu e digita o IP.
    Ele so digita ENQUANTO NAO ESTA CONECTADO, e confere isso ANTES DE CADA TECLA: se conectou,
    ou se o foco nao esta numa janela do processo do CERHost, ou se nao deu para conferir, para
    na hora. Qualquer comando do robo que falhe aborta a sequencia.
  * Enquanto o robo digita o x11vnc fica desligado: ninguem assiste nem opera no meio.
  * Conectado = a janela principal do CERHost deixa de se chamar "Remote Display Control for
    Windows CE" e o processo do CERHost tem uma janela com o nome do painel (ex.: CP-72B8D3).
    So contam janelas do processo do CERHost (xdotool --pid); listagem que falha = "nao sei",
    e ai a volta do vigia nao faz nada (nem derruba o CERHost, nem digita).
  * Sem gerenciador de janelas o Wine nao recebe foco nem redimensiona: por isso o Openbox.
  * A tela virtual tem cookie proprio (Xauthority na pasta privada): outro usuario local nao a
    controla. As senhas VNC vao para o x11vnc por arquivo (0600, mesma pasta, apagado depois de
    lido), nunca pela linha de comando nem no log.
  * Nada aqui escreve no CLP.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import secrets
import shutil
import signal
import socket
import struct
import tempfile
from pathlib import Path

log = logging.getLogger("cerhost")

TOOLS = ("Xvfb", "openbox", "wine", "xdotool", "wmctrl", "x11vnc")
MAIN_TITLE = "Remote Display Control for Windows CE"
IGNORED_TITLES = frozenset({MAIN_TITLE, "Default IME"})
SCREEN = "800x1000x24"  # o painel e em pe (~600x850 com a barra do CERHost)
ALL = ("x11vnc", "cerhost", "openbox", "xvfb")  # ordem de encerramento


def missing_tools(cerhost_exe: str) -> list[str]:
    """O que falta no servidor para a tela da Beckhoff (vazio = tudo certo)."""
    missing = [tool for tool in TOOLS if shutil.which(tool) is None]
    if not cerhost_exe or not os.path.isfile(cerhost_exe):
        missing.append("CERHOST.exe")
    return missing


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            return False
        return True


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _write_xauthority(path: Path, display: int, cookie: bytes) -> None:
    """Arquivo Xauthority com uma entrada FamilyWild (vale para qualquer endereco) para :N."""
    def field(b: bytes) -> bytes:
        return struct.pack(">H", len(b)) + b

    data = (struct.pack(">H", 0xFFFF) + field(b"") + field(str(display).encode())
            + field(b"MIT-MAGIC-COOKIE-1") + field(cookie))
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)


class SubprocessRunner:
    """Executor de verdade (os testes trocam por um falso)."""

    async def spawn(self, argv: list[str], env: dict[str, str]):
        # sessao propria: o processo vira lider de grupo e o stop encerra o grupo inteiro
        return await asyncio.create_subprocess_exec(
            *argv, env=env, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True)

    def signal(self, proc, sig: int) -> None:
        """Sinal para o grupo do processo (o wine deixa filhos)."""
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            pass
        except PermissionError:
            with contextlib.suppress(ProcessLookupError):
                proc.send_signal(sig)

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

    def __init__(self, message: str, restart_cerhost: bool = False, backoff: bool = False) -> None:
        super().__init__(message)
        self.restart_cerhost = restart_cerhost
        self.backoff = backoff


class CerhostBridge:
    """Sobe Xvfb + Openbox + CERHost, conecta no painel e serve a tela por x11vnc em 127.0.0.1."""

    # tempos (os testes encurtam)
    watch_interval = 5.0     # vigia: a cada quantos segundos confere a ponte
    busy_backoff = 30.0      # tela virtual ocupada por outro X: espera antes de tentar de novo
    poll_interval = 0.5      # espera de janelas/tela
    ready_timeout = 10.0     # Xvfb e Openbox prontos
    window_timeout = 30.0    # janela do CERHost aparecer (na bancada: 5-8 s)
    connect_timeout = 30.0   # conectar no painel depois de digitar o IP
    key_delay = 0.5          # pausa entre os comandos do robo (menu e dialogo abrirem)
    kill_timeout = 3.0       # SIGTERM -> SIGKILL
    cmd_timeout = 5.0        # cada xdotool/wmctrl
    x11_tmp = "/tmp"         # onde ficam .X{N}-lock e .X11-unix

    def __init__(self, host: str, display: int, cerhost_exe: str, view_password: str,
                 control_password: str = "", runner=None, wineprefix: str = "") -> None:
        self.host = host
        self.display = display
        self.cerhost_exe = cerhost_exe
        self.view_password = view_password
        self.control_password = control_password
        # prefixo Wine DEDICADO (opcional): so com ele o stop roda `wineserver -k`
        self.wineprefix = wineprefix
        self.runner = runner or SubprocessRunner()
        self.state = "stopped"
        self.error: str | None = None
        self.port: int | None = None
        self._procs: dict[str, object] = {}
        self._task: asyncio.Task | None = None
        self._lock: asyncio.Lock | None = None
        self._tmpdir: str | None = None
        self._stopped = True
        self._not_before = 0.0
        self._last_port: int | None = None

    # ------------------------------------------------------------------ ciclo de vida
    @property
    def _env(self) -> dict[str, str]:
        env = {**os.environ, "DISPLAY": f":{self.display}", "WINEARCH": "win32"}
        if self.wineprefix:
            env["WINEPREFIX"] = self.wineprefix
        if self._tmpdir:
            env["XAUTHORITY"] = str(Path(self._tmpdir) / "Xauthority")
        return env

    def _refuse(self, message: str) -> None:
        self.state, self.error = "error", message
        log.warning("tela da Beckhoff desligada: %s", message)

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
            return self._refuse("Tela da Beckhoff sem senha de visualização (VNC_VIEW_PASSWORD).")
        if any(c in pw for pw in (self.view_password, self.control_password) for c in "\r\n"):
            return self._refuse("Tela da Beckhoff: senha VNC com quebra de linha; corrija no .env.")
        if any(len(pw) > 8 for pw in (self.view_password, self.control_password)):
            log.warning("senha VNC da Beckhoff com mais de 8 caracteres: o VNC usa só os 8 primeiros")
        self._stopped = False
        self._not_before = 0.0
        self._lock = asyncio.Lock()
        try:
            self._tmpdir = tempfile.mkdtemp(prefix="cerhost-")  # 0700
            _write_xauthority(Path(self._tmpdir) / "Xauthority", self.display,
                              secrets.token_bytes(16))
        except OSError as exc:
            self._drop_tmpdir()
            self._stopped = True
            return self._refuse(f"Tela da Beckhoff: não deu para criar a pasta privada ({exc}).")
        first = asyncio.Event()
        self._task = asyncio.create_task(self._run(first))
        await first.wait()

    async def stop(self) -> None:
        self._stopped = True
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        # o encerramento final termina mesmo que quem chamou o stop seja cancelado
        await asyncio.shield(self._teardown())

    async def _teardown(self) -> None:
        if self._lock is not None:
            async with self._lock:
                await self._stop_procs(ALL)
        else:
            await self._stop_procs(ALL)
        self._drop_tmpdir()
        if self.state != "missing":
            self.state, self.error = "stopped", None

    async def check(self) -> None:
        """Uma volta do vigia: refaz o que caiu. So digita se nao estiver conectado."""
        if self._stopped or self._lock is None:
            return
        if asyncio.get_running_loop().time() < self._not_before:
            return
        async with self._lock:
            if not self._stopped:
                await self._ensure()

    async def _run(self, first: asyncio.Event) -> None:
        try:
            await self._guarded_check()
        finally:
            first.set()
        while True:
            await asyncio.sleep(self.watch_interval)
            await self._guarded_check()

    async def _guarded_check(self) -> None:
        try:
            await self.check()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # o vigia nunca morre
            log.exception("falha no vigia do CERHost")
            self.state, self.error = "error", f"Tela da Beckhoff: falha inesperada ({exc})."

    # ------------------------------------------------------------------ vigia
    async def _ensure(self) -> None:
        try:
            if not (self._alive("xvfb") and self._alive("openbox")):
                await self._stop_procs(ALL)
                await self._start_display()
            if not self._alive("cerhost"):
                self.state = "starting"
                log.info("abrindo o CERHost para %s", self.host)
                await self._spawn("cerhost", ["wine", self.cerhost_exe])
            windows = await self._cerhost_windows()
            if windows is None:
                log.debug("não deu para listar as janelas do CERHost; volta ignorada")
                return
            if self._connected_title(windows) is None:
                # o robo digita com o VNC desligado: ninguem assiste nem opera no meio
                await self._stop_procs(("x11vnc",))
                await self._connect()
            if not self._alive("x11vnc"):
                await self._start_vnc()
            if self.state != "connected":
                log.info("tela da Beckhoff pronta em 127.0.0.1:%s", self.port)
            self.state, self.error = "connected", None
        except (BridgeError, OSError) as exc:
            self.state, self.error = "error", f"Tela da Beckhoff: {exc}"
            if isinstance(exc, BridgeError) and exc.backoff:
                self._not_before = asyncio.get_running_loop().time() + self.busy_backoff
                log.warning("CERHost: %s (nova tentativa em %ss)", exc, self.busy_backoff)
            else:
                log.warning("CERHost: %s (nova tentativa em %ss)", exc, self.watch_interval)
            if isinstance(exc, BridgeError) and exc.restart_cerhost:
                # comeca limpo na proxima volta (sem menu ou dialogo aberto no caminho)
                await self._stop_procs(("x11vnc", "cerhost"))

    def _display_in_use(self) -> str | None:
        """Trava de outro servidor X vivo em :N (trava de processo morto e ignorada)."""
        lock = Path(self.x11_tmp) / f".X{self.display}-lock"
        try:
            pid = int(lock.read_text().strip())
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            return str(lock)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return None  # trava velha: o Xvfb a substitui sozinho
        except PermissionError:
            pass  # vivo, de outro usuario
        return str(lock)

    async def _start_display(self) -> None:
        self.state = "starting"
        d = f":{self.display}"
        lock = self._display_in_use()
        if lock:
            raise BridgeError(f"a tela virtual {d} já está em uso por outro servidor X ({lock}); "
                              "encerre-o ou escolha outro CERHOST_DISPLAY", backoff=True)
        await self._spawn("xvfb", ["Xvfb", d, "-screen", "0", SCREEN, "-nolisten", "tcp",
                                   "-auth", self._env["XAUTHORITY"]])
        if not await self._poll(self._display_ready, self.ready_timeout):
            raise BridgeError(f"a tela virtual {d} (Xvfb) não subiu")
        await self._spawn("openbox", ["openbox"])
        if not await self._poll(self._wm_ready, self.ready_timeout):
            raise BridgeError("o Openbox não subiu na tela virtual")

    async def _connect(self) -> None:
        """Conecta pelo teclado. Chamado SO quando o CERHost nao esta conectado."""
        self.state = "connecting"
        wid, unknown = await self._wait_windows(self._main_window, self.window_timeout)
        if wid is None:
            raise BridgeError(f"a janela do CERHost não apareceu em {self.window_timeout:g} s",
                              restart_cerhost=not unknown)
        log.info("conectando o CERHost em %s", self.host)
        steps = (["windowfocus", "--sync", wid], ["key", "alt+f"], ["key", "Return"],
                 ["type", "--delay", "50", self.host], ["key", "Return"])
        for step in steps:
            if step[0] != "windowfocus" and not await self._safe_to_type():
                log.info("CERHost conectou durante a sequência; o robô parou de digitar")
                break
            rc, _ = await self._cmd(["xdotool", *step])
            if rc != 0:
                raise BridgeError(f"o robô falhou em 'xdotool {step[0]}' (código {rc})",
                                  restart_cerhost=True)
            if self.key_delay:
                await asyncio.sleep(self.key_delay)
        title, unknown = await self._wait_windows(self._connected_title, self.connect_timeout)
        if title is None:
            raise BridgeError(f"o CERHost não conectou em {self.host} em {self.connect_timeout:g} s",
                              restart_cerhost=not unknown)
        await self._cmd(["wmctrl", "-r", title, "-b", "add,maximized_vert,maximized_horz"])
        log.info("CERHost conectado em %s (%s)", self.host, title)

    async def _safe_to_type(self) -> bool:
        """Antes de cada tecla: ainda desconectado e com o foco no CERHost? Conectou = False."""
        windows = await self._cerhost_windows()
        if windows is None:
            raise BridgeError("não deu para conferir as janelas do CERHost; o robô não digitou")
        if self._connected_title(windows) is not None:
            return False
        proc = self._procs.get("cerhost")
        rc, wid = await self._cmd(["xdotool", "getactivewindow"])
        if rc == 0 and wid.strip():
            rc, pid = await self._cmd(["xdotool", "getwindowpid", wid.strip()])
            if rc == 0 and proc is not None and pid.strip() == str(proc.pid):
                return True
        raise BridgeError("o foco não está no CERHost; o robô não digitou", restart_cerhost=True)

    async def _start_vnc(self) -> None:
        path = Path(self._tmpdir) / "senhas"
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
        lines = ([self.control_password, "__BEGIN_VIEWONLY__", self.view_password]
                 if self.control_password else [self.view_password])
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        # volta na mesma porta quando der (o hub nao precisa trocar)
        port = self._last_port if self._last_port and _port_free(self._last_port) else _free_port()
        argv = ["x11vnc", "-display", f":{self.display}", "-localhost", "-rfbport", str(port),
                "-forever", "-shared", "-quiet", "-passwdfile", f"rm:{path}"]
        if not self.control_password:
            argv.append("-viewonly")
        await self._spawn("x11vnc", argv)
        self.port = self._last_port = port

    # ------------------------------------------------------------------ consultas
    async def _cmd(self, argv: list[str]) -> tuple[int, str]:
        return await self.runner.run(argv, self._env, self.cmd_timeout)

    async def _display_ready(self) -> bool:
        if not self._alive("xvfb"):  # nosso Xvfb, nao outro servidor no mesmo :N
            return False
        return (await self._cmd(["xdotool", "getdisplaygeometry"]))[0] == 0

    async def _wm_ready(self) -> bool:
        return self._alive("openbox") and (await self._cmd(["wmctrl", "-m"]))[0] == 0

    async def _cerhost_windows(self) -> dict[str, str] | None:
        """Janelas do processo do CERHost (id -> titulo); None = nao deu para saber."""
        proc = self._procs.get("cerhost")
        if proc is None or proc.returncode is not None:
            return {}
        rc, out = await self._cmd(["xdotool", "search", "--all", "--pid", str(proc.pid),
                                   "--name", "."])
        if rc != 0:
            # 1 sem saida = nenhuma janela; qualquer outra coisa (estouro, tela fora) = nao sei
            if rc == 1 and not out.strip() and await self._display_ready():
                return {}
            return None
        titles = {}
        for wid in out.split():
            rc, name = await self._cmd(["xdotool", "getwindowname", wid])
            if rc != 0:
                return None
            titles[wid] = name.rstrip("\r\n")
        return titles

    @staticmethod
    def _main_window(windows: dict[str, str]) -> str | None:
        return next((wid for wid, t in windows.items() if t == MAIN_TITLE), None)

    @staticmethod
    def _connected_title(windows: dict[str, str]) -> str | None:
        """Nome do painel, ou None se nao estiver conectado."""
        titles = list(windows.values())
        if MAIN_TITLE in titles:  # janela principal com o titulo original = nao conectado
            return None
        return next((t for t in titles if t.strip() and t not in IGNORED_TITLES), None)

    async def _wait_windows(self, pick, timeout: float):
        """Espera `pick(janelas)` dar algo. Devolve (valor ou None, ultima listagem falhou?)."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            windows = await self._cerhost_windows()
            if windows is not None:
                value = pick(windows)
                if value:
                    return value, False
            if loop.time() >= deadline:
                return None, windows is None
            await asyncio.sleep(self.poll_interval)

    async def _poll(self, probe, timeout: float) -> bool:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while not await probe():
            if loop.time() >= deadline:
                return False
            await asyncio.sleep(self.poll_interval)
        return True

    # ------------------------------------------------------------------ processos
    async def _spawn(self, name: str, argv: list[str]) -> None:
        """Inicia e REGISTRA o processo, mesmo se o vigia for cancelado no meio do spawn."""
        fut = asyncio.ensure_future(self.runner.spawn(argv, self._env))
        try:
            self._procs[name] = await asyncio.shield(fut)
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                self._procs[name] = await fut
            raise

    def _alive(self, name: str) -> bool:
        proc = self._procs.get(name)
        return proc is not None and proc.returncode is None

    async def _stop_procs(self, names) -> None:
        for name in names:
            proc = self._procs.get(name)
            if proc is not None and proc.returncode is None:
                self.runner.signal(proc, signal.SIGTERM)
                try:
                    await asyncio.wait_for(proc.wait(), self.kill_timeout)
                except asyncio.TimeoutError:
                    log.warning("%s não saiu com SIGTERM em %ss; forçando", name, self.kill_timeout)
                    self.runner.signal(proc, signal.SIGKILL)
                    with contextlib.suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(proc.wait(), self.kill_timeout)
            self._procs.pop(name, None)
            if name == "x11vnc":
                self.port = None
                if self._tmpdir:
                    with contextlib.suppress(FileNotFoundError):
                        (Path(self._tmpdir) / "senhas").unlink()
            if name == "cerhost" and proc is not None and self.wineprefix:
                # so num prefixo dedicado: no padrao mataria outros programas Wine do usuario
                await self._cmd(["wineserver", "-k"])

    def _drop_tmpdir(self) -> None:
        if self._tmpdir:
            shutil.rmtree(self._tmpdir, ignore_errors=True)
            self._tmpdir = None
