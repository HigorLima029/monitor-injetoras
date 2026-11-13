"""Ponte CERHost: Xvfb + Openbox + CERHOST.exe (Wine) + x11vnc, testada com um executor falso."""
from __future__ import annotations

import asyncio
import os
import stat

import pytest

from app import cerhost
from app.cerhost import CerhostBridge, missing_tools

MAIN = "Remote Display Control for Windows CE"
PANEL = "CP-72B8D3"
HOST = "10.86.20.186"
VIEW, CONTROL = "olhar", "operar"


class FakeProc:
    def __init__(self, argv):
        self.argv = argv
        self.returncode = None
        self.terminated = False
        self.killed = False

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def kill(self):
        self.killed = True
        self.returncode = -9

    async def wait(self):
        return self.returncode


class FakeRunner:
    """Registra os comandos e simula as janelas do CERHost vistas pelo xdotool."""

    def __init__(self, connects: bool = True):
        self.connects = connects
        self.spawned: list[FakeProc] = []
        self.envs: list[dict] = []
        self.commands: list[list[str]] = []
        self.windows: dict[str, str] = {}
        self.passwd: list[tuple[str, int, int, str]] = []  # (conteudo, modo, modo da pasta, caminho)
        self._typed = False

    def procs(self, name: str) -> list[FakeProc]:
        return [p for p in self.spawned if p.argv[0] == name]

    def _wine_alive(self) -> bool:
        return any(p.returncode is None for p in self.procs("wine"))

    async def spawn(self, argv, env):
        p = FakeProc(list(argv))
        self.spawned.append(p)
        self.envs.append(dict(env))
        if argv[0] == "wine":
            self.windows = {"0x400001": MAIN, "0x400002": "Default IME"}
        if argv[0] == "x11vnc":
            arg = argv[argv.index("-passwdfile") + 1]
            path = arg.removeprefix("rm:")
            with open(path, encoding="utf-8") as fh:
                self.passwd.append((fh.read(), stat.S_IMODE(os.stat(path).st_mode),
                                    stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode), path))
            os.remove(path)  # o x11vnc de verdade apaga o arquivo depois de ler (rm:)
        return p

    async def run(self, argv, env, timeout):
        argv = list(argv)
        self.commands.append(argv)
        if argv[:2] == ["xdotool", "search"]:
            if not self._wine_alive() or not self.windows:
                return 1, ""
            return 0, "".join(f"{int(w, 16)}\n" for w in self.windows)
        if argv[:2] == ["xdotool", "getwindowname"]:
            wid = hex(int(argv[2]))
            return 0, self.windows.get(wid, "") + "\n"
        if argv[:2] == ["xdotool", "type"]:
            self._typed = True
        if argv == ["xdotool", "key", "Return"] and self._typed:
            self._typed = False
            if self.connects:
                self.windows["0x400001"] = PANEL
        return 0, ""

    def keys(self) -> list[list[str]]:
        return [c[1:] for c in self.commands
                if c[0] == "xdotool" and c[1] in ("windowfocus", "key", "type")]

    def all_argv(self) -> list[list[str]]:
        return [p.argv for p in self.spawned] + self.commands


def make_bridge(tmp_path, monkeypatch, control=CONTROL, runner=None, **kw):
    exe = tmp_path / "CERHOST.exe"
    exe.write_bytes(b"MZ")
    monkeypatch.setattr(cerhost.shutil, "which", lambda name: f"/usr/bin/{name}")
    runner = runner or FakeRunner()
    b = CerhostBridge(HOST, 99, str(exe), VIEW, control, runner=runner)
    b.watch_interval = 3600  # nos testes o vigia roda a mao com check()
    b.poll_interval = 0.001
    b.key_delay = 0
    b.ready_timeout = b.window_timeout = b.connect_timeout = 0.2
    b.kill_timeout = 0.05
    for k, v in kw.items():
        setattr(b, k, v)
    return b, runner


def test_falta_ferramenta(tmp_path, monkeypatch):
    b, runner = make_bridge(tmp_path, monkeypatch)
    monkeypatch.setattr(cerhost.shutil, "which",
                        lambda name: None if name == "x11vnc" else f"/usr/bin/{name}")

    async def go():
        await b.start()
        assert b.state == "missing"
        assert "x11vnc" in b.error
        assert b.error.startswith("Para a tela da Beckhoff falta no servidor:")
        assert runner.spawned == [] and runner.commands == []
        assert b.port is None
        await b.stop()

    asyncio.run(go())


def test_missing_tools_lista_cerhost(tmp_path, monkeypatch):
    monkeypatch.setattr(cerhost.shutil, "which", lambda name: None if name == "wine" else "/x")
    assert missing_tools(str(tmp_path / "nao-existe.exe")) == ["wine", "CERHOST.exe"]
    assert missing_tools("") == ["wine", "CERHOST.exe"]
    exe = tmp_path / "CERHOST.exe"
    exe.write_bytes(b"MZ")
    assert missing_tools(str(exe)) == ["wine"]


def test_sequencia_de_conexao(tmp_path, monkeypatch):
    b, runner = make_bridge(tmp_path, monkeypatch)

    async def go():
        await b.start()
        try:
            assert b.state == "connected", b.error
            assert isinstance(b.port, int) and b.port > 0
            assert b.error is None
            assert runner.keys() == [
                ["windowfocus", "--sync", str(0x400001)],
                ["key", "alt+f"],
                ["key", "Return"],
                ["type", "--delay", "50", HOST],
                ["key", "Return"],
            ]
            xvfb = runner.procs("Xvfb")[0].argv
            assert xvfb[1] == ":99" and xvfb[2:5] == ["-screen", "0", "800x1000x24"]
            assert runner.procs("openbox") and runner.procs("x11vnc")
            wine = runner.procs("wine")[0].argv
            assert wine[1] == str(tmp_path / "CERHOST.exe")
            assert ["wmctrl", "-r", PANEL, "-b", "add,maximized_vert,maximized_horz"] in runner.commands
            # ordem: Xvfb, openbox, CERHost, (conecta + maximiza), x11vnc
            assert [p.argv[0] for p in runner.spawned] == ["Xvfb", "openbox", "wine", "x11vnc"]
            for env in runner.envs:
                assert env["DISPLAY"] == ":99" and env["WINEARCH"] == "win32"
            vnc = runner.procs("x11vnc")[0].argv
            assert vnc[vnc.index("-display") + 1] == ":99"
            assert vnc[vnc.index("-rfbport") + 1] == str(b.port)
        finally:
            await b.stop()

    asyncio.run(go())


def test_senhas_fora_da_linha_de_comando(tmp_path, monkeypatch):
    b, runner = make_bridge(tmp_path, monkeypatch)

    async def go():
        await b.start()
        await b.stop()

    asyncio.run(go())
    for argv in runner.all_argv():
        assert not any(VIEW in a or CONTROL in a for a in argv), argv
    vnc = runner.procs("x11vnc")[0].argv
    assert "-localhost" in vnc and "-viewonly" not in vnc
    assert vnc[vnc.index("-passwdfile") + 1].startswith("rm:")
    content, mode, dir_mode, path = runner.passwd[0]
    assert content.splitlines() == [CONTROL, "__BEGIN_VIEWONLY__", VIEW]
    assert mode == 0o600 and dir_mode == 0o700
    assert not os.path.exists(os.path.dirname(path))  # pasta temporaria apagada no stop

    # sem senha de operacao: so visualizacao
    b2, runner2 = make_bridge(tmp_path, monkeypatch, control="")

    async def go2():
        await b2.start()
        await b2.stop()

    asyncio.run(go2())
    vnc2 = runner2.procs("x11vnc")[0].argv
    assert "-viewonly" in vnc2
    assert runner2.passwd[0][0].splitlines() == [VIEW]


def test_sem_senha_de_visualizacao_nao_sobe(tmp_path, monkeypatch):
    b, runner = make_bridge(tmp_path, monkeypatch)
    b.view_password = ""

    async def go():
        await b.start()
        assert b.state == "error" and "senha de visualização" in b.error
        assert runner.spawned == []
        await b.stop()

    asyncio.run(go())


def test_nao_digita_depois_de_conectado(tmp_path, monkeypatch):
    b, runner = make_bridge(tmp_path, monkeypatch)

    async def go():
        await b.start()
        try:
            assert b.state == "connected"
            before = len(runner.keys())
            spawned = len(runner.spawned)
            await b.check()
            await b.check()
            assert len(runner.keys()) == before
            assert len(runner.spawned) == spawned
            assert b.state == "connected"
        finally:
            await b.stop()

    asyncio.run(go())


def test_reconecta_quando_desconecta(tmp_path, monkeypatch):
    b, runner = make_bridge(tmp_path, monkeypatch)

    async def go():
        await b.start()
        try:
            port = b.port
            runner.windows["0x400001"] = MAIN  # painel caiu: titulo volta ao original
            await b.check()
            assert b.state == "connected"
            assert runner.keys()[5:] == runner.keys()[:5]  # a sequencia inteira de novo
            assert len(runner.procs("x11vnc")) == 1 and b.port == port  # VNC segue o mesmo
        finally:
            await b.stop()

    asyncio.run(go())


def test_reinicia_cerhost_que_morreu(tmp_path, monkeypatch):
    b, runner = make_bridge(tmp_path, monkeypatch)

    async def go():
        await b.start()
        try:
            runner.procs("wine")[0].returncode = 1
            await b.check()
            assert len(runner.procs("wine")) == 2
            assert b.state == "connected"
            assert len(runner.keys()) == 10
        finally:
            await b.stop()

    asyncio.run(go())


def test_estouro_de_conexao_vira_erro_e_tenta_de_novo(tmp_path, monkeypatch):
    runner = FakeRunner(connects=False)
    b, _ = make_bridge(tmp_path, monkeypatch, runner=runner)

    async def go():
        await b.start()
        try:
            assert b.state == "error"
            assert HOST in b.error
            assert b.port is None
            # o CERHost travado e encerrado para a proxima volta comecar limpa
            assert runner.procs("wine")[0].terminated
            runner.connects = True
            await b.check()
            assert b.state == "connected"
            assert len(runner.procs("wine")) == 2
        finally:
            await b.stop()

    asyncio.run(go())


def test_janela_nao_aparece_vira_erro(tmp_path, monkeypatch):
    runner = FakeRunner()
    b, _ = make_bridge(tmp_path, monkeypatch, runner=runner)
    orig = runner.spawn

    async def spawn(argv, env):
        p = await orig(argv, env)
        if argv[0] == "wine":
            runner.windows = {}
        return p

    runner.spawn = spawn

    async def go():
        await b.start()
        try:
            assert b.state == "error" and "janela" in b.error
            assert runner.keys() == []
        finally:
            await b.stop()

    asyncio.run(go())


def test_stop_encerra_tudo(tmp_path, monkeypatch):
    b, runner = make_bridge(tmp_path, monkeypatch)
    order = []

    async def go():
        await b.start()
        for p in runner.spawned:
            orig = p.terminate

            def term(p=p, orig=orig):
                order.append(p.argv[0])
                orig()

            p.terminate = term
        await b.stop()
        assert all(p.returncode is not None for p in runner.spawned)
        assert order == ["x11vnc", "wine", "openbox", "Xvfb"]
        assert b.state == "stopped" and b.port is None
        await b.stop()  # idempotente
        assert order == ["x11vnc", "wine", "openbox", "Xvfb"]
        assert b._task is None

    asyncio.run(go())


def test_stop_mata_quem_nao_sai(tmp_path, monkeypatch):
    b, runner = make_bridge(tmp_path, monkeypatch)

    async def go():
        await b.start()
        teimoso = runner.procs("wine")[0]
        teimoso.terminate = lambda: None  # ignora o SIGTERM
        async def wait():
            while teimoso.returncode is None:
                await asyncio.sleep(0.005)
            return teimoso.returncode

        teimoso.wait = wait
        await b.stop()
        assert teimoso.killed

    asyncio.run(go())


def test_vigia_roda_sozinho(tmp_path, monkeypatch):
    b, runner = make_bridge(tmp_path, monkeypatch)
    b.watch_interval = 0.01

    async def go():
        await b.start()
        try:
            runner.windows["0x400001"] = MAIN
            for _ in range(200):
                await asyncio.sleep(0.01)
                if len(runner.keys()) >= 10 and b.state == "connected":
                    break
            assert len(runner.keys()) == 10 and b.state == "connected"
        finally:
            await b.stop()

    asyncio.run(go())
