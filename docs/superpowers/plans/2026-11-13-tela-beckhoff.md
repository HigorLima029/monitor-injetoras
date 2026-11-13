# Tela ao vivo da Beckhoff — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Mostrar (e operar em contingência) a tela da Beckhoff na página "Tela ao vivo", rodando o CERHost no servidor e servindo-o por VNC ao hub existente.

**Architecture:** Um `CerhostBridge` sobe Xvfb + Openbox + `CERHOST.exe` (Wine) + x11vnc num display virtual, conecta o CERHost ao painel por teclado (xdotool) e expõe VNC em 127.0.0.1. O `MachineManager`, ao abrir uma máquina `ads` com tela ligada, inicia a ponte e aponta um `VncHub` normal para ela; ao trocar de máquina, encerra tudo.

**Tech Stack:** Python asyncio (`asyncio.create_subprocess_exec`), Xvfb, openbox, Wine 32, xdotool, wmctrl, x11vnc; FastAPI; React/TS.

**Spec:** `docs/superpowers/specs/2026-11-13-tela-beckhoff-design.md` (+ `2026-11-13-seletor-maquinas-design.md` para o contexto do seletor)

## Global Constraints

- Xvfb `-screen 0 800x1000x24`; display padrão `:99` (`CERHOST_DISPLAY`).
- Conexão do CERHost: janela `Remote Display Control for Windows CE` → `windowfocus --sync` → `key alt+f` → `key Return` → `type --delay 50 <ip>` → `key Return`; conectado = existe janela cujo título não é `Remote Display Control for Windows CE` nem `Default IME` (ex.: `CP-72B8D3`); depois `wmctrl -r <título> -b add,maximized_vert,maximized_horz`.
- O robô só envia teclas enquanto **não** conectado. Nunca depois.
- x11vnc: `-display :N -localhost -rfbport <porta livre> -forever -shared -quiet -passwdfile rm:<arquivo>`; arquivo com a senha de operação na 1ª linha, `__BEGIN_VIEWONLY__`, e a senha de visualização. Sem senha de operação → x11vnc com `-viewonly` e só a senha de visualização. Senhas nunca na linha de comando nem em log.
- Ferramentas exigidas: `Xvfb`, `openbox`, `wine`, `xdotool`, `wmctrl`, `x11vnc` (checadas com `shutil.which`) e `CERHOST_EXE` existente. Falta algo → ponte em estado `missing` com mensagem em português listando o que falta.
- Variáveis de ambiente para os processos: `DISPLAY=:N`, `WINEARCH=win32` (demais herdadas).
- Só leitura de dados ADS inalterada; nada escreve no PLC.
- Textos em português, tom atual do Monitor.

## Review Focus

1. **Trocar de máquina com a ponte no meio da conexão** → todos os processos (Xvfb, openbox, wine/CERHost, x11vnc) encerrados; nenhum sobra (`pgrep` vazio para o display).
2. **CERHost desconecta ou fecha** (rede 4G caiu) → a ponte percebe (título volta ou processo morre) e reconecta sozinha, sem digitar nada numa tela já conectada.
3. **Ferramenta faltando / `CERHOST_EXE` vazio** → Monitor sobe normalmente, dados ADS funcionam, Tela ao vivo explica o que falta.
4. **Display `:99` já ocupado** (Xvfb esquecido de um teste) → erro claro ou limpeza, não travar.
5. **Senhas** → não aparecem em `ps`, log, nem resposta HTTP; arquivo de senhas some após o x11vnc ler.

---

### Task 1: `CerhostBridge` (orquestração dos processos)

**Files:**
- Create: `backend/app/cerhost.py`
- Modify: `backend/app/config.py` (`cerhost_exe`, `cerhost_display`)
- Test: `backend/tests/test_cerhost.py`

**Interfaces:**
- Produces:
  - `missing_tools(cerhost_exe: str) -> list[str]` — nomes do que falta (ferramentas + "CERHOST.exe").
  - `class CerhostBridge(host: str, display: int, cerhost_exe: str, view_password: str, control_password: str = "", runner=None)`:
    - `state: str` em `"stopped" | "missing" | "starting" | "connecting" | "connected" | "error"`, `error: str | None`, `port: int | None` (porta VNC local quando pronto).
    - `async start() -> None` — se faltar algo: `state="missing"`, `error="Para a tela da Beckhoff falta no servidor: ..."` e retorna. Senão sobe Xvfb, openbox, CERHost, conecta, maximiza, sobe x11vnc numa porta livre; inicia um laço de vigia (a cada 5 s) que reconecta se a janela conectada sumir ou o processo do CERHost morrer.
    - `async stop() -> None` — encerra vigia e processos (SIGTERM, depois SIGKILL após 3 s), na ordem x11vnc, CERHost/wine, openbox, Xvfb; idempotente.
  - `runner` = objeto com `async spawn(argv, env) -> proc` e `async run(argv, env, timeout) -> (rc, stdout)`; padrão usa `asyncio.create_subprocess_exec`. Testes injetam um fake.

- [ ] **Step 1: Write failing tests** em `test_cerhost.py` com um `FakeRunner` que registra comandos e simula `xdotool search` (devolve id da janela "Remote Display Control…" e, depois do `Return` final, a janela `CP-72B8D3`):
  - `test_falta_ferramenta`: `monkeypatch` `shutil.which` → `None` para `x11vnc`; `start()` → `state == "missing"` e `"x11vnc" in error`; nenhum processo iniciado.
  - `test_sequencia_de_conexao`: comandos xdotool enviados exatamente na ordem `windowfocus --sync <id>`, `key alt+f`, `key Return`, `type --delay 50 10.86.20.186`, `key Return`; Xvfb com `-screen 0 800x1000x24`; `wmctrl ... CP-72B8D3 ... maximized_vert,maximized_horz`; `state == "connected"` e `port` int.
  - `test_senhas_fora_da_linha_de_comando`: nenhum argv contém as senhas; argv do x11vnc tem `-localhost` e `-passwdfile rm:`; o arquivo criado tem as linhas `ctl`, `__BEGIN_VIEWONLY__`, `view`; sem senha de operação → `-viewonly`.
  - `test_nao_digita_depois_de_conectado`: com a janela conectada presente, uma volta do vigia não envia nenhum `xdotool key/type`.
  - `test_reconecta_quando_desconecta`: fake troca o título de volta para "Remote Display Control…" → o vigia refaz a sequência de conexão.
  - `test_stop_encerra_tudo`: `stop()` encerra todos os processos iniciados (fake registra terminate) e pode ser chamado duas vezes.
- [ ] **Step 2:** `cd backend && .venv/bin/python -m pytest tests/test_cerhost.py -v` → FAIL (módulo inexistente).
- [ ] **Step 3:** Implementar `cerhost.py` e config (`CERHOST_EXE`, `CERHOST_DISPLAY` padrão 99). Porta livre: bind em `127.0.0.1:0`, ler a porta, fechar. Esperas com limite (janela aparecer ≤ 30 s, conectado ≤ 30 s); estouro → `state="error"` e nova tentativa no vigia.
- [ ] **Step 4:** `.venv/bin/python -m pytest -q` → tudo passa.
- [ ] **Step 5:** Commit `feat: ponte CERHost (Xvfb + Wine + x11vnc) para a tela da Beckhoff`.

---

### Task 2: Manager e API usam a ponte

**Files:**
- Modify: `backend/app/manager.py` (`_new_hub`, `_close_current`, ativação), `backend/app/main.py` (`/api/info` e `/api/vnc/status` mostram estado da ponte), `README.md`, `.env.example`
- Test: `backend/tests/test_manager.py`, `backend/tests/test_api_machines.py`

**Interfaces:**
- Consumes: `CerhostBridge`, `missing_tools` (Task 1).
- Produces:
  - `MachineManager.bridge: CerhostBridge | None`; `bridge_factory` injetável (como `source_factory`).
  - Máquina `ads` com `effective_vnc_enabled` e senha de visualização → `activate` cria a ponte, `await bridge.start()` **em segundo plano** (não bloqueia o activate), e o hub é `VncHub("127.0.0.1", <porta da ponte>, view, control, ...)` criado quando a ponte fica `connected` (o hub pode ser criado já com porta conhecida e reconectar sozinho — escolha do implementador, documentar).
  - `_close_current` também faz `bridge.stop()` com o mesmo limite de tempo (`_bounded`).
  - `/api/info.vnc` true para `ads` com tela ligada; `/api/vnc/status` inclui `bridge: {state, error}` quando há ponte.
  - O padrão de `vnc_enabled` para `ads` continua `False` (o usuário liga no cadastro).

- [ ] **Step 1: Write failing tests**: `test_ads_com_tela_sobe_ponte_e_hub` (fake bridge → após activate, `mgr.bridge` iniciada e `mgr.hub.host == "127.0.0.1"` com a porta da ponte); `test_troca_para_br_para_ponte` (activate ads → activate em63: `bridge.stop` chamado); `test_ads_sem_tela_nao_sobe_ponte`; `test_status_mostra_falta_de_ferramenta` (fake bridge `missing` → `/api/vnc/status` traz a mensagem).
- [ ] **Step 2:** rodar → FAIL. **Step 3:** implementar. **Step 4:** suite inteira passa. README: seção "Tela da Beckhoff" com `sudo apt-get install -y xvfb openbox xdotool wmctrl x11vnc wine32:i386` e `CERHOST_EXE=`.
- [ ] **Step 5:** Commit `feat: tela ao vivo da Beckhoff pelo hub VNC`.

---

### Task 3: Frontend

**Files:**
- Modify: `frontend/src/MachinesPage.tsx`, `frontend/src/App.tsx`, `frontend/src/TelaAoVivo.tsx`

- Formulário: para `ads`, mostrar a seção de tela com o rótulo **"Mostrar a tela da Beckhoff (CERHost) no monitor"**, as duas senhas (dica: "senhas desta tela no Monitor, até 8 caracteres"), sem campo de porta (a porta é interna).
- Menu **Tela ao vivo** aparece para `ads` quando `info.vnc`.
- `TelaAoVivo`: quando o status trouxer `bridge.state` diferente de `connected`, mostrar o estado em português ("Abrindo a tela da Beckhoff…", "Conectando ao painel…", ou a mensagem de ferramenta faltando) em vez do erro genérico de VNC.
- [ ] `npm run build` e `npm run lint` sem erros novos; verificação manual em modo simulado com uma ponte falsa (ou só build + revisão, já que a ponte real depende do Wine no PC do Higor). Commit `feat: tela da Beckhoff no formulario e na Tela ao vivo`.

---

### Task 4: Bancada (manual, com o Higor)

- [ ] `CERHOST_EXE` no `.env`, ligar a tela na Beckhoff XMold com as duas senhas, abrir: tela aparece na Tela ao vivo; assumir controle com a senha de operação, rolar/clicar; Ctrl para teclas físicas.
- [ ] Trocar para a B&R e voltar 3×: `pgrep -a -f "Xvfb :99|x11vnc|CERHOST"` vazio quando a B&R está aberta.
- [ ] Tirar o cabo da Beckhoff por 30 s e recolocar: a tela volta sozinha.
- [ ] `_NET_WM_PID` da janela do CERHost é o pid do `wine` que a ponte abriu: `xdotool search --pid <pid do wine> --name .` (com `DISPLAY=:99` e o `XAUTHORITY` da pasta privada) lista a janela principal; se não listar, o robô nunca digita (ajustar antes de seguir).
- [ ] Título do diálogo de conexão (alt+f, Return): anotar o nome (`xdotool getactivewindow getwindowname`); conferir que é outra janela do mesmo pid e que a principal segue "Remote Display Control for Windows CE" enquanto ele está aberto.
- [ ] Durante o cabo puxado: anotar o que acontece com o título da janela principal (volta ao original? some? aparece diálogo de erro?) e o que o log do Monitor mostra até a tela voltar.
- [ ] Fechar o terminal do `run.sh` (sem Ctrl+C): `pgrep -a -f "Xvfb :99|x11vnc|CERHOST"` vazio em poucos segundos; subir de novo e a tela volta sem "já está em uso".
