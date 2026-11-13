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
  * Processo que morre sozinho vai para o log (nome, codigo e o fim do stderr, que fica num
    arquivo da pasta privada). Os processos morrem junto com o Monitor (PR_SET_PDEATHSIG) e ficam
    anotados num arquivo de pids: se o Monitor cair feio (kill -9, falta de memoria), a proxima
    subida encerra as sobras dele (so as que ainda sao o que ele abriu), em vez de deixar o
    CERHost ligado na IHM e a tela :N ocupada.
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
import sys
import tempfile
from pathlib import Path

log = logging.getLogger("cerhost")

TOOLS = ("Xvfb", "openbox", "wine", "xdotool", "wmctrl", "x11vnc")
MAIN_TITLE = "Remote Display Control for Windows CE"
IGNORED_TITLES = frozenset({MAIN_TITLE, "Default IME"})
SCREEN = "800x1000x24"  # o painel e em pe (~600x850 com a barra do CERHost)
ALL = ("x11vnc", "cerhost", "openbox", "xvfb")  # ordem de encerramento
LOG_TAIL_LINES = 10      # linhas do stderr de um processo que morreu, no log
LOG_TAIL_BYTES = 4096


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


def _resolve_prctl():
    """Resolve o prctl uma vez, no processo pai (no filho, entre fork e exec, so chamamos)."""
    if not sys.platform.startswith("linux"):
        return None
    try:
        import ctypes
        return ctypes.CDLL(None, use_errno=True).prctl
    except Exception:  # sem prctl: o processo sobe sem o aviso de morte do pai
        return None


_PRCTL = _resolve_prctl()


def _die_with_parent_for(parent_pid: int):
    """preexec_fn: recebe SIGTERM se o Monitor morrer. So chama funcoes ja resolvidas."""
    def _preexec() -> None:
        if _PRCTL is None:
            return
        try:
            _PRCTL(1, int(signal.SIGTERM), 0, 0, 0)  # PR_SET_PDEATHSIG
            if os.getppid() != parent_pid:  # o Monitor ja morreu entre o fork e o prctl
                os.kill(os.getpid(), signal.SIGTERM)
        except Exception:  # nunca impede o processo de subir
            pass
    return _preexec


class SubprocessRunner:
    """Executor de verdade (os testes trocam por um falso)."""

    async def spawn(self, argv: list[str], env: dict[str, str], stderr_path: str | None = None):
        # sessao propria: o processo vira lider de grupo e o stop encerra o grupo inteiro
        err = open(stderr_path, "wb") if stderr_path else None  # o filho herda o descritor
        try:
            return await asyncio.create_subprocess_exec(
                *argv, env=env, stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=err if err is not None else asyncio.subprocess.DEVNULL,
                start_new_session=True, preexec_fn=_die_with_parent_for(os.getpid()))
        finally:
            if err is not None:
                err.close()

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

    proc_root = "/proc"      # onde ler o cmdline das sobras (os testes trocam)

    def __init__(self, host: str, display: int, cerhost_exe: str, view_password: str,
                 control_password: str = "", runner=None, wineprefix: str = "",
                 pidfile: str = "") -> None:
        self.host = host
        self.display = display
        self.cerhost_exe = cerhost_exe
        self.view_password = view_password
        self.control_password = control_password
        # prefixo Wine DEDICADO (opcional): so com ele o stop roda `wineserver -k`
        self.wineprefix = wineprefix
        self.runner = runner or SubprocessRunner()
        # arquivo de pids (ex.: data/cerhost.pids): as sobras de um Monitor que caiu feio
        self.pidfile = pidfile
        self.state = "stopped"
        self.error: str | None = None
        self.port: int | None = None
        self._procs: dict[str, object] = {}
        self.pgid_of = os.getpgid  # trocado nos testes
        self._task: asyncio.Task | None = None
        self._lock: asyncio.Lock | None = None
        self._tmpdir: str | None = None
        self._stopped = True
        self._not_before = 0.0
        self._last_port: int | None = None
        self._reported: set[int] = set()  # pids cuja morte ja foi para o log

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
        await self._kill_leftovers()
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
        caller_cancelled = False
        if task is not None:
            task.cancel()
            try:
                # shield: se quem chamou o stop for cancelado, o cancelamento nao chega de novo ao
                # vigia (que ainda pode estar registrando um processo recem-aberto)
                await asyncio.shield(task)
            except asyncio.CancelledError:
                # o CancelledError do vigia e esperado; o de quem chamou o stop, nao: repassa
                current = asyncio.current_task()
                caller_cancelled = current is not None and current.cancelling() > 0
        # o encerramento final termina mesmo que quem chamou o stop seja cancelado
        await asyncio.shield(self._teardown())
        if caller_cancelled:
            raise asyncio.CancelledError

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
            await self._report_dead()
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
            self._log_exit("xvfb")
            raise BridgeError(f"a tela virtual {d} (Xvfb) não subiu")
        await self._spawn("openbox", ["openbox"])
        if not await self._poll(self._wm_ready, self.ready_timeout):
            self._log_exit("openbox")
            raise BridgeError("o Openbox não subiu na tela virtual")

    async def _connect(self) -> None:
        """Conecta pelo teclado. Chamado SO quando o CERHost nao esta conectado."""
        self.state = "connecting"
        wid, unknown = await self._wait_windows(self._main_window, self.window_timeout)
        if wid is None:
            raise BridgeError(f"a janela do CERHost não apareceu em {self.window_timeout:g} s",
                              restart_cerhost=not unknown)
        log.info("conectando o CERHost em %s", self.host)
        # (comando, onde o foco tem de estar): menu e "Connect" na janela principal; o IP e o
        # Enter final no dialogo de conexao (outra janela do mesmo processo do CERHost)
        steps = ((["windowfocus", "--sync", wid], None), (["key", "alt+f"], "main"),
                 (["key", "Return"], "main"), (["type", "--delay", "50", self.host], "dialog"),
                 (["key", "Return"], "dialog"))
        for step, where in steps:
            if where is not None and not await self._safe_to_type(wid, where):
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

    async def _safe_to_type(self, main_wid: str, where: str) -> bool:
        """Antes de cada tecla: ainda desconectado e com o foco na janela certa? Conectou = False.

        where="main": a janela ativa e a principal (main_wid), com o titulo original lido nela.
        where="dialog": a ativa e OUTRA janela do processo do CERHost (o dialogo de conexao), e a
        principal continua com o titulo original. Qualquer divergencia: aborta sem digitar.
        """
        windows = await self._cerhost_windows()
        if windows is None:
            raise BridgeError("não deu para conferir as janelas do CERHost; o robô não digitou")
        main_name = await self._window_name(main_wid)
        if where == "dialog":
            # com o dialogo aberto o CERHost nao conecta: titulo mudou = algo estranho, aborta
            if main_name != MAIN_TITLE:
                raise BridgeError("a janela principal do CERHost mudou no meio da sequência; "
                                  "o robô não digitou", restart_cerhost=True)
        elif self._connected_title(windows) is not None:
            return False
        proc = self._procs.get("cerhost")
        rc, out = await self._cmd(["xdotool", "getactivewindow"])
        active = out.strip() if rc == 0 else ""
        if active and proc is not None:
            active_name = await self._window_name(active)
            if where == "main":
                ok = active == main_wid and main_name == MAIN_TITLE and active_name == MAIN_TITLE
            else:
                rc, pid = await self._cmd(["xdotool", "getwindowpid", active])
                ok = (active != main_wid and rc == 0 and self._owned(pid)
                      and active_name is not None and active_name not in IGNORED_TITLES)
            if ok:
                return True
        janela = "na janela principal" if where == "main" else "no diálogo de conexão"
        raise BridgeError(f"o foco não está {janela} do CERHost; o robô não digitou",
                          restart_cerhost=True)

    async def _window_name(self, wid: str) -> str | None:
        rc, name = await self._cmd(["xdotool", "getwindowname", wid])
        return name.rstrip("\r\n") if rc == 0 else None

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

    def _owned(self, pid_text: str) -> bool:
        """A janela e do CERHost? O wine desenha pelo processo filho (wine-preloader), entao
        vale qualquer processo do grupo do CERHost (cada ponte abre o seu grupo)."""
        proc = self._procs.get("cerhost")
        if proc is None:
            return False
        try:
            pid = int(pid_text.strip())
        except ValueError:
            return False
        if pid == proc.pid:
            return True
        try:
            return self.pgid_of(pid) == proc.pid
        except (ProcessLookupError, PermissionError, OSError):
            return False

    async def _cerhost_windows(self) -> dict[str, str] | None:
        """Janelas do processo do CERHost (id -> titulo); None = nao deu para saber."""
        proc = self._procs.get("cerhost")
        if proc is None or proc.returncode is not None:
            return {}
        # a tela virtual e so nossa; filtra pelo grupo do processo do CERHost
        rc, out = await self._cmd(["xdotool", "search", "--name", "."])
        if rc != 0:
            # 1 sem saida = nenhuma janela; qualquer outra coisa (estouro, tela fora) = nao sei
            if rc == 1 and not out.strip() and await self._display_ready():
                return {}
            return None
        titles = {}
        for wid in out.split():
            rc, pid = await self._cmd(["xdotool", "getwindowpid", wid])
            if rc != 0 or not self._owned(pid):
                continue  # janela sem dono conhecido (ex.: do openbox) ou de outro processo
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
            proc = self._procs.get("cerhost")
            if proc is not None and proc.returncode is not None:
                self._log_exit("cerhost")
                raise BridgeError(f"o CERHost (Wine) fechou sozinho (código {proc.returncode}); "
                                  "confira o Wine 32 bits (wine32:i386) e o WINEPREFIX",
                                  restart_cerhost=True, backoff=True)
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
        stderr = str(Path(self._tmpdir) / f"{name}.log") if self._tmpdir else None
        fut = asyncio.ensure_future(self.runner.spawn(argv, self._env, stderr_path=stderr))
        try:
            self._procs[name] = await asyncio.shield(fut)
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                self._procs[name] = await fut
            raise
        finally:
            self._write_pidfile()

    def _alive(self, name: str) -> bool:
        proc = self._procs.get(name)
        return proc is not None and proc.returncode is None

    async def _report_dead(self) -> None:
        """Processo que a ponte abriu e morreu sozinho: vai para o log e sai da lista."""
        dead = [n for n in ALL if n in self._procs and not self._alive(n)]
        for name in dead:
            self._log_exit(name)
        if dead:
            await self._stop_procs(dead)

    def _log_exit(self, name: str) -> None:
        proc = self._procs.get(name)
        if proc is None or proc.returncode is None or proc.pid in self._reported:
            return
        self._reported.add(proc.pid)
        tail = self._stderr_tail(name)
        log.warning("CERHost: o processo %s saiu sozinho (código %s)%s", name, proc.returncode,
                    (": últimas linhas do stderr:\n" + tail) if tail else "")

    def _stderr_tail(self, name: str) -> str:
        """Fim do stderr do processo (so as mensagens dele; as senhas so existem no arquivo rm:)."""
        if not self._tmpdir:
            return ""
        try:
            with open(Path(self._tmpdir) / f"{name}.log", "rb") as fh:
                fh.seek(0, os.SEEK_END)
                fh.seek(max(0, fh.tell() - LOG_TAIL_BYTES))
                data = fh.read()
        except OSError:
            return ""
        lines = data.decode("utf-8", "replace").splitlines()[-LOG_TAIL_LINES:]
        return "\n".join("  " + line for line in lines if line.strip())

    # ------------------------------------------------------------------ sobras (arquivo de pids)
    def _write_pidfile(self) -> None:
        if not self.pidfile:
            return
        procs = {n: p for n, p in self._procs.items() if p is not None}
        try:
            path = Path(self.pidfile)
            if not procs:
                with contextlib.suppress(FileNotFoundError):
                    path.unlink()
                return
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            lines = [f"owner {os.getpid()}"] + [f"{n} {p.pid}" for n, p in procs.items()]
            tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
            os.replace(tmp, path)
        except OSError as exc:
            log.warning("CERHost: não deu para gravar %s (%s)", self.pidfile, exc)

    def _cmdline(self, pid: int) -> list[str] | None:
        """cmdline do pid, so se o processo e do nosso usuario (None = nao existe/zumbi/de outro)."""
        base = Path(self.proc_root) / str(pid)
        try:
            if base.stat().st_uid != os.getuid():
                return None
            raw = (base / "cmdline").read_bytes()
        except OSError:
            return None
        argv = [a.decode("utf-8", "replace") for a in raw.split(b"\0") if a]
        return argv or None

    def _is_ours(self, name: str, argv: list[str] | None) -> bool:
        """O pid ainda e o que a ponte abriu? (pid reaproveitado por outro programa = nao)."""
        if not argv:
            return False
        exe, d = os.path.basename(argv[0]), f":{self.display}"
        if name == "xvfb":
            return exe == "Xvfb" and d in argv[1:2]
        if name == "openbox":
            return exe == "openbox"
        if name == "x11vnc":
            return exe == "x11vnc" and "-display" in argv and argv[argv.index("-display") + 1:][:1] == [d]
        if name == "cerhost":
            # so o proprio CERHOST.exe (nome exato), nunca um arquivo que so contenha "cerhost"
            alvo = {"cerhost.exe", os.path.basename(self.cerhost_exe.replace("\\", "/")).lower()}
            return any(os.path.basename(a.replace("\\", "/")).lower() in alvo for a in argv)
        return False

    @staticmethod
    def _kill(pid: int, sig: int) -> None:
        if pid == os.getpid() or os.getpgid(0) == pid:
            return  # nunca o proprio Monitor nem o grupo dele
        try:
            if os.getpgid(pid) == pid:
                os.killpg(pid, sig)  # era lider do grupo: leva os filhos junto
            else:
                os.kill(pid, sig)
        except (ProcessLookupError, PermissionError):
            pass

    async def _kill_leftovers(self) -> None:
        """Encerra as sobras de um Monitor anterior que caiu sem desligar a ponte."""
        if not self.pidfile:
            return
        path = Path(self.pidfile)
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return
        except OSError as exc:
            log.warning("CERHost: não deu para ler %s (%s)", self.pidfile, exc)
            return
        entries: dict[str, int] = {}
        owner = None
        for line in lines:
            parts = line.split()
            if len(parts) == 2 and parts[1].isdigit():
                if parts[0] == "owner":
                    owner = int(parts[1])
                else:
                    entries[parts[0]] = int(parts[1])
        if owner is not None and owner != os.getpid() and self._cmdline(owner) is not None:
            log.warning("CERHost: %s é de outro Monitor ainda vivo (pid %s); sobras não mexidas",
                        self.pidfile, owner)
            return
        found = [(n, entries[n]) for n in ALL
                 if n in entries and self._is_ours(n, self._cmdline(entries[n]))]
        for name, pid in found:
            log.warning("CERHost: encerrando sobra de um Monitor anterior: %s (pid %s)", name, pid)
            self._kill(pid, signal.SIGTERM)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.kill_timeout
        while found:
            found = [(n, p) for n, p in found if self._is_ours(n, self._cmdline(p))]
            if not found:
                break
            if loop.time() >= deadline:
                for name, pid in found:
                    log.warning("CERHost: sobra %s (pid %s) não saiu com SIGTERM; forçando", name, pid)
                    self._kill(pid, signal.SIGKILL)
                break
            await asyncio.sleep(self.poll_interval)
        with contextlib.suppress(OSError):
            path.unlink()

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
        self._write_pidfile()

    def _drop_tmpdir(self) -> None:
        if self._tmpdir:
            shutil.rmtree(self._tmpdir, ignore_errors=True)
            self._tmpdir = None
