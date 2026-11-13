"""Ponte CERHost: Xvfb + Openbox + CERHOST.exe (Wine) + x11vnc, testada com um executor falso."""
from __future__ import annotations

import asyncio
import itertools
import logging
import os
import signal
import stat
import struct

from app import cerhost
from app.cerhost import CerhostBridge, missing_tools

MAIN = "Remote Display Control for Windows CE"
PANEL = "CP-72B8D3"
HOST = "10.86.20.186"
VIEW, CONTROL = "olhar", "operar"
MAIN_WID, IME_WID = "4194305", "4194306"
_pids = itertools.count(5000)


class FakeProc:
    def __init__(self, argv):
        self.argv = argv
        self.pid = next(_pids)
        self.returncode = None
        self.stubborn = False  # ignora SIGTERM

    async def wait(self):
        while self.returncode is None:
            await asyncio.sleep(0.002)
        return self.returncode


class FakeRunner:
    """Registra os comandos e simula as janelas do CERHost vistas pelo xdotool.

    `windows` sao as janelas do processo do CERHost (wid -> titulo); `foreign` sao janelas de
    outros processos na mesma tela (nao contam).
    """

    def __init__(self, connects: bool = True):
        self.connects = connects
        self.spawned: list[FakeProc] = []
        self.envs: list[dict] = []
        self.commands: list[list[str]] = []
        self.run_envs: list[dict] = []
        self.signals: list[tuple[str, int]] = []
        self.windows: dict[str, str] = {}
        self.foreign: dict[str, str] = {}
        self.passwd: list[tuple[str, int, int]] = []  # (conteudo, modo, modo da pasta)
        self.search_rc: int | None = None  # forca falha do xdotool search
        self.active: str | None = None     # janela ativa (None = principal do CERHost)
        self.hooks: dict[tuple, callable] = {}
        self.spawn_delay: dict[str, float] = {}
        self._typed = False

    def procs(self, name: str) -> list[FakeProc]:
        return [p for p in self.spawned if p.argv[0] == name]

    def _wine(self) -> FakeProc | None:
        alive = [p for p in self.procs("wine") if p.returncode is None]
        return alive[-1] if alive else None

    def signal(self, proc, sig):
        self.signals.append((proc.argv[0], sig))
        if sig == signal.SIGKILL or not proc.stubborn:
            proc.returncode = -sig

    async def spawn(self, argv, env):
        p = FakeProc(list(argv))
        self.spawned.append(p)
        self.envs.append(dict(env))
        if argv[0] == "wine":
            self.windows = {MAIN_WID: MAIN, IME_WID: "Default IME"}
        if argv[0] == "x11vnc":
            path = argv[argv.index("-passwdfile") + 1].removeprefix("rm:")
            with open(path, encoding="utf-8") as fh:
                self.passwd.append((fh.read(), stat.S_IMODE(os.stat(path).st_mode),
                                    stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode)))
            os.remove(path)  # o x11vnc de verdade apaga o arquivo depois de ler (rm:)
        if argv[0] in self.spawn_delay:
            await asyncio.sleep(self.spawn_delay[argv[0]])
        return p

    async def run(self, argv, env, timeout):
        argv = list(argv)
        self.commands.append(argv)
        self.run_envs.append(dict(env))
        hook = self.hooks.get(tuple(argv))
        if hook:
            hook()
        wine = self._wine()
        if argv[:2] == ["xdotool", "search"]:
            if self.search_rc is not None:
                return self.search_rc, ""
            pid = argv[argv.index("--pid") + 1]
            if wine is None or pid != str(wine.pid) or not self.windows:
                return 1, ""
            return 0, "".join(f"{w}\n" for w in self.windows)
        if argv[:2] == ["xdotool", "getwindowname"]:
            return 0, {**self.windows, **self.foreign}.get(argv[2], "") + "\n"
        if argv[:2] == ["xdotool", "getactivewindow"]:
            return 0, (self.active or MAIN_WID) + "\n"
        if argv[:2] == ["xdotool", "getwindowpid"]:
            if argv[2] in self.windows and wine is not None:
                return 0, f"{wine.pid}\n"
            return 0, "1\n"
        if argv[:2] == ["xdotool", "type"]:
            self._typed = True
        if argv == ["xdotool", "key", "Return"] and self._typed:
            self._typed = False
            if self.connects:
                self.windows[MAIN_WID] = PANEL
        return 0, ""

    def keys(self) -> list[list[str]]:
        return [c[1:] for c in self.commands
                if c[0] == "xdotool" and c[1] in ("windowfocus", "key", "type")]

    def all_argv(self) -> list[list[str]]:
        return [p.argv for p in self.spawned] + self.commands


SEQ = [["windowfocus", "--sync", MAIN_WID], ["key", "alt+f"], ["key", "Return"],
       ["type", "--delay", "50", HOST], ["key", "Return"]]


def make_bridge(tmp_path, monkeypatch, control=CONTROL, runner=None, **kw):
    exe = tmp_path / "CERHOST.exe"
    exe.write_bytes(b"MZ")
    monkeypatch.setattr(cerhost.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(cerhost.tempfile, "tempdir", str(tmp_path))
    x11 = tmp_path / "x11"
    x11.mkdir(exist_ok=True)
    runner = runner or FakeRunner()
    b = CerhostBridge(HOST, 99, str(exe), VIEW, control, runner=runner,
                      wineprefix=kw.pop("wineprefix", ""))
    b.x11_tmp = str(x11)
    b.watch_interval = 3600  # nos testes o vigia roda a mao com check()
    b.poll_interval = 0.001
    b.key_delay = 0
    b.ready_timeout = b.window_timeout = b.connect_timeout = 0.2
    b.kill_timeout = 0.05
    for k, v in kw.items():
        setattr(b, k, v)
    return b, runner


def alive(runner):
    return [p.argv[0] for p in runner.spawned if p.returncode is None]


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
            assert runner.keys() == SEQ
            xvfb = runner.procs("Xvfb")[0].argv
            assert xvfb[1] == ":99" and xvfb[2:5] == ["-screen", "0", "800x1000x24"]
            assert runner.procs("openbox") and runner.procs("x11vnc")
            wine = runner.procs("wine")[0]
            assert wine.argv[1] == str(tmp_path / "CERHOST.exe")
            assert ["wmctrl", "-r", PANEL, "-b", "add,maximized_vert,maximized_horz"] in runner.commands
            # janelas so do processo do CERHost
            searches = [c for c in runner.commands if c[:2] == ["xdotool", "search"]]
            assert searches and all(c[c.index("--pid") + 1] == str(wine.pid) for c in searches)
            assert [p.argv[0] for p in runner.spawned] == ["Xvfb", "openbox", "wine", "x11vnc"]
            for env in runner.envs + runner.run_envs:
                assert env["DISPLAY"] == ":99" and env["WINEARCH"] == "win32"
                assert "WINEPREFIX" not in env or env["WINEPREFIX"] == os.environ.get("WINEPREFIX")
            vnc = runner.procs("x11vnc")[0].argv
            assert vnc[vnc.index("-display") + 1] == ":99"
            assert vnc[vnc.index("-rfbport") + 1] == str(b.port)
        finally:
            await b.stop()

    asyncio.run(go())


def test_xauthority_privado(tmp_path, monkeypatch):
    b, runner = make_bridge(tmp_path, monkeypatch)

    async def go():
        await b.start()
        try:
            xvfb = runner.procs("Xvfb")[0].argv
            auth = xvfb[xvfb.index("-auth") + 1]
            assert stat.S_IMODE(os.stat(auth).st_mode) == 0o600
            assert stat.S_IMODE(os.stat(os.path.dirname(auth)).st_mode) == 0o700
            with open(auth, "rb") as fh:
                data = fh.read()
            family, = struct.unpack(">H", data[:2])
            assert family == 0xFFFF and b"MIT-MAGIC-COOKIE-1" in data and b"99" in data
            assert struct.unpack(">H", data[-18:-16])[0] == 16  # cookie de 16 bytes
            for env in runner.envs + runner.run_envs:
                assert env["XAUTHORITY"] == auth
            return auth
        finally:
            await b.stop()

    auth = asyncio.run(go())
    assert not os.path.exists(os.path.dirname(auth))  # pasta privada apagada no stop


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
    content, mode, dir_mode = runner.passwd[0]
    assert content.splitlines() == [CONTROL, "__BEGIN_VIEWONLY__", VIEW]
    assert mode == 0o600 and dir_mode == 0o700
    assert [p for p in os.listdir(tmp_path) if p.startswith("cerhost-")] == []

    # sem senha de operacao: so visualizacao
    b2, runner2 = make_bridge(tmp_path, monkeypatch, control="")

    async def go2():
        await b2.start()
        await b2.stop()

    asyncio.run(go2())
    assert "-viewonly" in runner2.procs("x11vnc")[0].argv
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


def test_senha_com_quebra_de_linha_recusada(tmp_path, monkeypatch):
    b, runner = make_bridge(tmp_path, monkeypatch, control="ab\ncd")

    async def go():
        await b.start()
        assert b.state == "error" and "quebra de linha" in b.error
        assert "ab" not in b.error
        assert runner.spawned == []
        await b.stop()

    asyncio.run(go())
    assert [p for p in os.listdir(tmp_path) if p.startswith("cerhost-")] == []


def test_senha_longa_avisa_sem_mostrar(tmp_path, monkeypatch, caplog):
    longa = "senhamuitolonga"
    b, runner = make_bridge(tmp_path, monkeypatch, control=longa)

    async def go():
        with caplog.at_level(logging.WARNING, logger="cerhost"):
            await b.start()
        await b.stop()

    asyncio.run(go())
    assert any("8 caracteres" in r.getMessage() for r in caplog.records)
    assert longa not in caplog.text


def test_nao_digita_depois_de_conectado(tmp_path, monkeypatch):
    b, runner = make_bridge(tmp_path, monkeypatch)

    async def go():
        await b.start()
        try:
            assert b.state == "connected"
            before, spawned = len(runner.keys()), len(runner.spawned)
            await b.check()
            await b.check()
            assert len(runner.keys()) == before
            assert len(runner.spawned) == spawned
            assert b.state == "connected"
        finally:
            await b.stop()

    asyncio.run(go())


def test_para_de_digitar_se_conectar_no_meio(tmp_path, monkeypatch):
    b, runner = make_bridge(tmp_path, monkeypatch)
    # alguem (ou o proprio CERHost) conecta logo depois do alt+f
    runner.hooks[("xdotool", "key", "alt+f")] = lambda: runner.windows.update({MAIN_WID: PANEL})

    async def go():
        await b.start()
        try:
            assert runner.keys() == SEQ[:2]  # nada de Return, type nem Return final
            assert b.state == "connected"
        finally:
            await b.stop()

    asyncio.run(go())


def test_foco_fora_do_cerhost_nao_digita(tmp_path, monkeypatch):
    b, runner = make_bridge(tmp_path, monkeypatch)
    runner.active = "999"  # outra janela com o foco

    async def go():
        await b.start()
        try:
            assert runner.keys() == SEQ[:1]  # so o windowfocus; nenhuma tecla
            assert b.state == "error" and "foco" in b.error
            assert runner.procs("wine")[0].returncode is not None  # CERHost reiniciado
            assert runner.procs("x11vnc") == []
        finally:
            await b.stop()

    asyncio.run(go())


def test_falha_do_xdotool_aborta_sequencia(tmp_path, monkeypatch):
    runner = FakeRunner()
    b, _ = make_bridge(tmp_path, monkeypatch, runner=runner)
    orig = runner.run

    async def run(argv, env, timeout):
        if list(argv)[:2] == ["xdotool", "windowfocus"]:
            runner.commands.append(list(argv))
            return -1, ""  # --sync estourou
        return await orig(argv, env, timeout)

    runner.run = run

    async def go():
        await b.start()
        try:
            assert runner.keys() == SEQ[:1]
            assert b.state == "error"
        finally:
            await b.stop()

    asyncio.run(go())


def test_reconecta_quando_desconecta_sem_vnc_durante(tmp_path, monkeypatch):
    b, runner = make_bridge(tmp_path, monkeypatch)
    seen = []

    async def go():
        await b.start()
        try:
            port = b.port
            # enquanto o robo digita, ninguem esta no x11vnc
            for key in (("xdotool", "key", "alt+f"), ("xdotool", "type", "--delay", "50", HOST)):
                runner.hooks[key] = lambda: seen.append(alive(runner).count("x11vnc"))
            runner.windows[MAIN_WID] = MAIN  # painel caiu: titulo volta ao original
            await b.check()
            assert b.state == "connected"
            assert runner.keys()[5:] == SEQ  # a sequencia inteira de novo
            assert seen == [0, 0]
            assert len(runner.procs("x11vnc")) == 2 and b.port == port  # mesma porta de volta
            assert alive(runner).count("x11vnc") == 1
        finally:
            await b.stop()

    asyncio.run(go())


def test_listagem_que_falha_nao_derruba_nem_digita(tmp_path, monkeypatch):
    b, runner = make_bridge(tmp_path, monkeypatch)

    async def go():
        await b.start()
        try:
            keys = len(runner.keys())
            runner.search_rc = -1  # xdotool search estourou
            await b.check()
            await b.check()
            assert len(runner.keys()) == keys
            assert all(p.returncode is None for p in runner.spawned)
            assert b.state == "connected"
        finally:
            await b.stop()

    asyncio.run(go())


def test_janela_de_outro_processo_nao_conta(tmp_path, monkeypatch):
    b, runner = make_bridge(tmp_path, monkeypatch)

    async def go():
        await b.start()
        try:
            runner.windows.clear()  # janela do CERHost sumiu (sem estar conectado)
            runner.foreign["777"] = "Program Error"  # dialogo de erro do Wine, outro processo
            await b.check()
            assert b.state == "error"  # nao fingiu estar conectado
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
            assert runner.keys() == SEQ + SEQ
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
            assert runner.procs("wine")[0].returncode is not None
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


def test_tela_virtual_em_uso(tmp_path, monkeypatch):
    b, runner = make_bridge(tmp_path, monkeypatch)
    (tmp_path / "x11" / ".X99-lock").write_text(f"{os.getpid():>10}\n")  # outro X vivo

    async def go():
        await b.start()
        try:
            assert b.state == "error" and "já está em uso" in b.error and ":99" in b.error
            assert runner.spawned == []
            await b.check()  # espera o back-off: nada de laco apertado
            assert runner.spawned == []
        finally:
            await b.stop()

    asyncio.run(go())


def test_trava_velha_nao_impede(tmp_path, monkeypatch):
    b, runner = make_bridge(tmp_path, monkeypatch)
    (tmp_path / "x11" / ".X99-lock").write_text("2147483646\n")  # processo que nao existe

    async def go():
        await b.start()
        try:
            assert b.state == "connected"
        finally:
            await b.stop()

    asyncio.run(go())


def test_xvfb_que_morre_nao_passa(tmp_path, monkeypatch):
    runner = FakeRunner()
    b, _ = make_bridge(tmp_path, monkeypatch, runner=runner)
    orig = runner.spawn

    async def spawn(argv, env):
        p = await orig(argv, env)
        if argv[0] == "Xvfb":
            p.returncode = 1  # saiu na hora (tela ocupada por outro servidor)
        return p

    runner.spawn = spawn

    async def go():
        await b.start()
        try:
            assert b.state == "error" and "Xvfb" in b.error
            assert runner.procs("openbox") == [] and runner.procs("wine") == []
        finally:
            await b.stop()

    asyncio.run(go())


def test_stop_encerra_tudo(tmp_path, monkeypatch):
    b, runner = make_bridge(tmp_path, monkeypatch)

    async def go():
        await b.start()
        await b.stop()
        assert all(p.returncode is not None for p in runner.spawned)
        order = [(n, s) for n, s in runner.signals]
        assert order == [(n, signal.SIGTERM) for n in ("x11vnc", "wine", "openbox", "Xvfb")]
        assert b.state == "stopped" and b.port is None
        assert ["wineserver", "-k"] not in runner.commands  # prefixo padrao: nunca
        await b.stop()  # idempotente
        assert len(runner.signals) == 4
        assert b._task is None
        await b.check()  # depois do stop, o vigia nao sobe nada
        assert len(runner.spawned) == 4

    asyncio.run(go())


def test_stop_mata_quem_nao_sai(tmp_path, monkeypatch):
    b, runner = make_bridge(tmp_path, monkeypatch)

    async def go():
        await b.start()
        runner.procs("wine")[0].stubborn = True
        await b.stop()
        assert ("wine", signal.SIGKILL) in runner.signals
        assert all(p.returncode is not None for p in runner.spawned)

    asyncio.run(go())


def test_wineprefix_dedicado_usa_wineserver_k(tmp_path, monkeypatch):
    prefix = str(tmp_path / "prefixo")
    b, runner = make_bridge(tmp_path, monkeypatch, wineprefix=prefix)

    async def go():
        await b.start()
        await b.stop()

    asyncio.run(go())
    assert all(env["WINEPREFIX"] == prefix for env in runner.envs)
    i = runner.commands.index(["wineserver", "-k"])
    assert runner.run_envs[i]["WINEPREFIX"] == prefix


def test_stop_durante_start_nao_deixa_processo(tmp_path, monkeypatch):
    b, runner = make_bridge(tmp_path, monkeypatch, window_timeout=30)
    runner.spawn_delay["wine"] = 0.05  # cancelado no meio do spawn

    async def go():
        t = asyncio.create_task(b.start())
        while not runner.procs("wine"):
            await asyncio.sleep(0.001)
        await b.stop()
        await t
        assert [p.argv[0] for p in runner.spawned] == ["Xvfb", "openbox", "wine"]
        assert alive(runner) == []
        assert b.state == "stopped"

    asyncio.run(go())


def test_stop_esperando_janela_nao_deixa_processo(tmp_path, monkeypatch):
    runner = FakeRunner()
    b, _ = make_bridge(tmp_path, monkeypatch, runner=runner, window_timeout=30)
    orig = runner.spawn

    async def spawn(argv, env):
        p = await orig(argv, env)
        if argv[0] == "wine":
            runner.windows = {}
        return p

    runner.spawn = spawn

    async def go():
        t = asyncio.create_task(b.start())
        while not any(c[:2] == ["xdotool", "search"] for c in runner.commands):
            await asyncio.sleep(0.001)
        await b.stop()
        await t
        assert alive(runner) == []

    asyncio.run(go())


def test_vigia_roda_sozinho(tmp_path, monkeypatch):
    b, runner = make_bridge(tmp_path, monkeypatch)
    b.watch_interval = 0.01

    async def go():
        await b.start()
        try:
            runner.windows[MAIN_WID] = MAIN
            for _ in range(200):
                await asyncio.sleep(0.01)
                if len(runner.keys()) >= 10 and b.state == "connected":
                    break
            assert len(runner.keys()) == 10 and b.state == "connected"
        finally:
            await b.stop()

    asyncio.run(go())


def test_vigia_sobrevive_a_excecao(tmp_path, monkeypatch):
    b, runner = make_bridge(tmp_path, monkeypatch)
    b.watch_interval = 0.01
    calls = []
    orig = b._ensure

    async def boom():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("inesperado")
        await orig()

    b._ensure = boom

    async def go():
        await b.start()
        try:
            assert b.state == "error" and b._task is not None and not b._task.done()
            for _ in range(200):
                await asyncio.sleep(0.01)
                if b.state == "connected":
                    break
            assert b.state == "connected"
        finally:
            await b.stop()

    asyncio.run(go())
