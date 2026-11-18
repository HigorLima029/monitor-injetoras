# Base: login, perfis, clientes e todas as máquinas conectadas — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Login obrigatório com três perfis (conta mãe, técnico, cliente), clientes cadastrados, todas as máquinas coletando o tempo todo, cada pessoa olhando a sua máquina, tela ao vivo por máquina com controle só com autorização do cliente + confirmação de senha, auditoria e HTTPS.

**Architecture:** Novo `accounts.py` (usuários, clientes, sessões, autorizações, auditoria no `data/monitor_config.db`) e `security.py` (hash, TOTP, tokens). O `MachineManager` de uma máquina ativa vira `Fleet` com um `MachineRuntime` por máquina (coletor sempre ligado; hub/ponte CERHost sob demanda). Todas as rotas passam a ser por máquina (`/api/machines/{id}/...`) com checagem central em `authz.py`; o hub VNC deixa de autenticar por senha VNC e recebe o papel do login.

**Tech Stack:** FastAPI, asyncua, pyads, SQLite (stdlib), `hashlib.scrypt`/`hmac` (stdlib, sem dependência nova), React/Vite/TS, noVNC, openssl (certificado).

**Spec:** `docs/superpowers/specs/2026-11-18-base-login-multicliente-design.md`

## Global Constraints

- Perfis exatos: `mae`, `tecnico`, `cliente`. Textos de interface: "Conta mãe", "Técnico", "Cliente".
- Leitura: **10 s** sem ninguém olhando (`IDLE_POLL_SECONDS=10`); rápida com alguém olhando (`POLL_SECONDS`, padrão passa a **2**); volta a 10 s **60 s** depois que o último sai (`VIEWER_LINGER_SECONDS=60`).
- Autorização de suporte padrão: **30 min**. Conta mãe "em nome do cliente" exige **motivo** não vazio e fica destacada.
- 2º fator (TOTP, 6 dígitos, 30 s, SHA1) **obrigatório na conta mãe**; opcional nos demais.
- Sessão: cookie `monitor_sessao`, `HttpOnly`, `Secure`, `SameSite=Strict`; expira com **8 h** sem uso e **24 h** no máximo.
- Login: depois de **5** falhas (por usuário ou por IP), espera de **30 s** que dobra a cada nova falha até **15 min**; acerto zera.
- Telas da Beckhoff abertas ao mesmo tempo: no máximo `CERHOST_MAX_SCREENS=2`; ao atingir: recusa com "Limite de telas da Beckhoff abertas ao mesmo tempo. Tente de novo em alguns minutos."
- Nunca escrever no PLC (`test_codigo_nao_escreve_na_maquina` continua passando).
- Senhas (usuários e painéis), hash, segredo TOTP e token de sessão nunca em resposta HTTP, log ou argv.
- Cliente nunca vê `host`, `opcua_port`, `namespace`, `ams_net_id`, `ads_port`, `vnc_port`, `notes`, nem máquinas/registros de outro cliente. Máquina de outro cliente responde **404** (não 403).
- Sem conta mãe com senha padrão: a conta mãe nasce só pelo comando `python -m app.admin`.
- Textos em português, tom atual do Monitor.

## Review Focus

1. Cliente pedindo, por ID na URL, WebSocket ou histórico, uma máquina de outro cliente → 404 em todas as rotas (testes nas Tasks 7 e 9).
2. Usuário desativado ou sessão expirada com WebSocket de dados e tela ao vivo abertos → conexões caem em até 10 s (testes nas Tasks 5 e 9).
3. Autorização de suporte expira ou é encerrada com o técnico no controle → volta a visualização em até 5 s, registrado (teste na Task 9).
4. Uma máquina offline ou com leitura travada (4G) → as outras continuam coletando e as rotas respondem (teste na Task 6).
5. Banco de configuração existente (campo `client` em texto, máquina ativa no `kv`, senhas VNC) → sobe migrado sem perder máquinas nem histórico (teste na Task 3).

## Arquivos

- Create: `backend/app/security.py`, `backend/app/accounts.py`, `backend/app/authz.py`, `backend/app/auth.py` (rotas e dependências de login), `backend/app/admin.py` (comando), `backend/app/admin_api.py` (rotas da conta mãe), `backend/scripts/teste_duas_beckhoff.py`, `scripts/gerar_certificado.sh`, `frontend/src/LoginPage.tsx`, `frontend/src/AdminPages.tsx`, `frontend/src/auth.ts`.
- Modify: `backend/app/machines.py`, `manager.py` (vira `Fleet`), `collector.py`, `vnc_hub.py`, `cerhost.py`, `config.py`, `main.py`, `demo_seed.py`, `run.sh`, `.env.example`, `README.md`, `frontend/src/{App,api,MachinesPage,TelaAoVivo,pages}.tsx|ts`.
- Tests: `backend/tests/test_security.py`, `test_accounts.py`, `test_auth.py`, `test_fleet.py` (substitui `test_manager.py`), `test_api_machines.py`, `test_vnc_hub.py`, `test_cerhost.py`, `test_admin_api.py`. Fixture comum em `backend/tests/conftest.py`: `client_as(role, client_id=None)` → `TestClient` já logado.

---

### Task 1: Verificação na bancada — duas Beckhoffs no mesmo processo (manual)

**Files:** Create `backend/scripts/teste_duas_beckhoff.py`

- [ ] **Step 1:** Script que abre duas `pyads.Connection` no mesmo processo (`set_local_address(ADS_LOCAL_NET_ID)` uma vez), lê por 60 s a cada 2 s `.CYCLETIME_MQTT` e `.AAAPFWTEMPTOHMI[1].ACTUALTEMP` das duas e imprime `ok/erro` por leitura; argumentos `--a NETID@IP --b NETID@IP`. Só leitura (`read_by_name`), nenhum `write`.
- [ ] **Step 2:** Higor roda com o Monitor parado:
  `.venv/bin/python scripts/teste_duas_beckhoff.py --a 5.79.97.240.1.1@10.86.20.186 --b 5.112.28.141.1.1@192.168.0.111`
  Esperado: as duas com leituras `ok` intercaladas. Registrar o resultado no ledger. Se falhar: as tasks seguem iguais, e a Task 6 cria o coletor ADS num subprocesso (plano B do spec) — a interface `Source` não muda.
- [ ] **Step 3:** Commit `chore: script de verificação de duas Beckhoffs no mesmo processo`.

### Task 2: `security.py` — senha, TOTP e tokens

**Files:** Create `backend/app/security.py`; Test `backend/tests/test_security.py`

**Interfaces — Produces:**
- `hash_password(pw: str) -> str` — `hashlib.scrypt(n=2**14, r=8, p=1, dklen=32)`, sal de 16 bytes; formato `scrypt$16384$8$1$<sal_b64>$<hash_b64>`.
- `verify_password(pw: str, stored: str) -> bool` — `hmac.compare_digest`; formato inválido → `False`.
- `password_problem(pw: str, role: str) -> str | None` — mínimo 12 caracteres para `mae`, 8 para os demais; devolve o texto do problema.
- `new_totp_secret() -> str` (base32, 20 bytes), `totp_code(secret: str, at: float, digits: int = 6, step: int = 30) -> str`, `verify_totp(secret: str, code: str, at: float | None = None, window: int = 1) -> bool`, `otpauth_uri(secret: str, username: str, issuer: str = "Monitor de Injetoras") -> str`.
- `new_token() -> str` (`secrets.token_urlsafe(32)`), `token_hash(token: str) -> str` (sha256 hex).

- [ ] **Step 1: Testes**
```python
def test_hash_e_verificacao():
    h = hash_password("senha-forte-123")
    assert h.startswith("scrypt$") and "senha-forte-123" not in h
    assert verify_password("senha-forte-123", h) and not verify_password("outra", h)
    assert hash_password("x" * 12) != hash_password("x" * 12)   # sal aleatório
    assert verify_password("x", "lixo") is False

def test_politica_de_senha():
    assert password_problem("curta123", "mae") and password_problem("curta123", "tecnico") is None

def test_totp_vetor_rfc6238():
    secret = base64.b32encode(b"12345678901234567890").decode()
    assert totp_code(secret, 59) == "287082"
    assert verify_totp(secret, "287082", at=59 + 30)          # janela de 1 passo
    assert not verify_totp(secret, "287082", at=59 + 90)

def test_uri_otpauth():
    assert otpauth_uri("ABC", "higor").startswith("otpauth://totp/Monitor%20de%20Injetoras:higor?secret=ABC")
```
- [ ] **Step 2:** `cd backend && .venv/bin/python -m pytest tests/test_security.py -v` → FAIL (módulo inexistente).
- [ ] **Step 3:** Implementar só com stdlib.
- [ ] **Step 4:** Mesmo comando → PASS.
- [ ] **Step 5:** Commit `feat: senhas (scrypt), TOTP e tokens`.

### Task 3: `accounts.py` — clientes, usuários, sessões, autorizações, auditoria + migração das máquinas

**Files:** Create `backend/app/accounts.py`; Modify `backend/app/machines.py`; Test `backend/tests/test_accounts.py`, `test_machines.py`

**Interfaces — Consumes:** Task 2. **Produces:**
- Dataclasses: `Client(id, name, active)`, `User(id, username, name, role, client_id, active, has_totp)` (sem hash nem segredo), `Grant(id, machine_id, by_user_id, by_name, on_behalf, reason, starts, expires, ended_at)`.
- `ROLES = ("mae", "tecnico", "cliente")`; `AccountStore(path: str)` no mesmo arquivo do `MachineStore`.
- Clientes: `add_client(name) -> Client` (nome único, sem diferenciar maiúsculas), `list_clients() -> list[Client]`, `get_client(id) -> Client | None`, `update_client(id, name=None, active=None) -> Client`.
- Usuários: `add_user(username, name, role, password, client_id=None) -> User` (`ValueError` se papel inválido, cliente sem `client_id`, não-cliente com `client_id`, senha fraca, login repetido; login guardado em minúsculas); `get_user(id)`, `get_by_username(username)`, `list_users()`, `update_user(id, *, name=None, active=None, client_id=None) -> User`, `set_password(id, pw)`, `set_totp(id, secret: str | None)`, `totp_secret(id) -> str | None`, `check_password(username, pw) -> User | None` (inativo → None).
- Conta mãe: `update_user` com `active=False` numa `mae` e qualquer mudança de papel → `PermissionError("A conta mãe não pode ser desativada nem rebaixada.")`.
- Sessões: `create_session(user_id, ip) -> str` (guarda só `token_hash`), `session_user(token, now=None) -> User | None` (8 h sem uso, 24 h máximo, usuário ativo; atualiza `last_seen`), `delete_session(token)`, `delete_user_sessions(user_id)`.
- Autorizações: `grant_support(machine_id, by: User, minutes=30, on_behalf=False, reason="") -> Grant` (`ValueError` se `on_behalf` sem motivo), `active_grant(machine_id, now=None) -> Grant | None`, `end_support(machine_id, by: User) -> bool`.
- Auditoria: `log(action: str, user: User | None, *, machine_id=None, client_id=None, ip="", detail="", highlight=False)`, `audit(*, user_id=None, client_id=None, machine_id=None, since=None, until=None, limit=500) -> list[dict]` (mais recentes primeiro; cada item com `time, user_name, role, client_id, action, machine_id, ip, detail, highlight`).
- `migrate_machine_clients(machines: MachineStore, accounts: AccountStore) -> None` — cada texto distinto em `machines.client` vira um `Client`; preenche `client_id`; idempotente.
- `machines.py`: coluna nova `client_id INTEGER` (ALTER TABLE se faltar); `Machine.client` (texto) é substituído por `client_id: int | None = None`; `public()` traz `client_id` e `vnc_sem_senha = kind == "em63" and effective_vnc_enabled and not vnc_view_password and not vnc_control_password`. `active_id/set_active` deixam de ser usados (podem ficar no `kv`, sem leitura).

- [ ] **Step 1: Testes** (nomes e asserções principais)
```python
def test_usuario_cliente_exige_cliente(): # add_user("ana","Ana","cliente","senha12345") -> ValueError
def test_login_minusculo_e_senha_fora_do_user(): # check_password("ANA", ...) acha; asdict(User) sem "hash"/"password"
def test_conta_mae_nao_desativa_nem_rebaixa(): # PermissionError
def test_sessao_expira_por_inatividade_e_maximo(): # now=+8h01 -> None ; now=+24h01 mesmo usado -> None
def test_desativar_usuario_derruba_sessao(): # update_user(active=False) -> session_user None
def test_autorizacao_expira_e_em_nome_exige_motivo():
    g = acc.grant_support(7, cliente, minutes=30, now=1000)
    assert acc.active_grant(7, now=1000 + 29*60) and acc.active_grant(7, now=1000 + 31*60) is None
    with pytest.raises(ValueError): acc.grant_support(7, mae, on_behalf=True, reason=" ")
def test_auditoria_filtra_por_cliente_e_maquina(): ...
def test_migracao_do_cadastro_antigo(tmp_path):
    # banco no formato atual: 2 máquinas client="Chen Hsong", 1 client="ABC", senhas VNC e kv active
    # depois de abrir MachineStore + AccountStore + migrate_machine_clients (2x):
    assert {c.name for c in acc.list_clients()} == {"Chen Hsong", "ABC"}
    assert all(m.client_id for m in ms.list()) and ms.get(1).vnc_view_password == "view1"
```
- [ ] **Step 2:** `.venv/bin/python -m pytest tests/test_accounts.py tests/test_machines.py -v` → FAIL.
- [ ] **Step 3:** Implementar. Datas em `time.time()`; parâmetro `now` opcional nos métodos com tempo (testes).
- [ ] **Step 4:** Mesmo comando → PASS (ajustar `test_machines.py` para `client_id`).
- [ ] **Step 5:** Commit `feat: contas, clientes, sessões, autorizações e auditoria`.

### Task 4: Comando da conta mãe (`python -m app.admin`)

**Files:** Create `backend/app/admin.py`; Modify `README.md`; Test `backend/tests/test_admin_cli.py`

**Interfaces — Produces:** `main(argv: list[str], *, getpass=getpass.getpass, out=sys.stdout) -> int` com subcomandos:
- `criar-mae --usuario LOGIN --nome NOME`: recusa se já existe uma conta mãe; pede a senha 2x (política de `mae`); cria o TOTP e imprime o segredo e o `otpauth://` para cadastrar no app do celular; pede um código para confirmar antes de gravar.
- `resetar-senha --usuario LOGIN` (qualquer perfil; derruba as sessões), `resetar-2fa --usuario LOGIN` (novo segredo, mesma confirmação).
- Usa `settings.config_db_path`. Senha nunca em argumento de linha de comando.

- [ ] **Step 1: Testes**: `test_criar_mae_imprime_segredo_e_exige_codigo` (getpass falso; código errado → nada gravado, retorno ≠ 0); `test_segunda_mae_recusada`; `test_resetar_senha_derruba_sessoes`.
- [ ] **Step 2:** pytest → FAIL. **Step 3:** implementar (argparse). **Step 4:** PASS.
- [ ] **Step 5:** README "Primeiro acesso": `cd backend && .venv/bin/python -m app.admin criar-mae --usuario higor --nome "Higor"`. Commit `feat: comando para criar e recuperar a conta mãe`.

### Task 5: Login, sessão e proteção de todas as rotas

**Files:** Create `backend/app/auth.py`; Modify `backend/app/main.py`, `config.py`; Create `backend/tests/conftest.py`; Test `backend/tests/test_auth.py`

**Interfaces — Consumes:** Tasks 2–3. **Produces:**
- `LoginLimiter(max_failures=5, base_wait=30, max_wait=900)`: `wait_for(username, ip, now) -> float` (segundos até poder tentar), `failure(username, ip, now)`, `success(username, ip)`.
- Rotas: `POST /api/login {username, password}` → 200 `{"user": me}` ou, se o usuário tem TOTP, 200 `{"need_code": true, "ticket": "<str>"}` (ticket vale 5 min, em memória); `POST /api/login/code {ticket, code}` → `{"user": me}`; `POST /api/logout` → 204; `GET /api/me` → `{id, username, name, role, client_id, client_name, has_totp}`. Erro de login: 401 `"Usuário ou senha incorretos."` (mesmo texto para usuário inexistente); bloqueado: 429 `"Muitas tentativas. Tente de novo em N s."`. Conta mãe sem TOTP: 403 `"Configure o 2º fator pelo comando do servidor."`.
- Dependências: `current_user(request) -> User` (401 `"Faça login."`), `ws_user(websocket) -> User | None` (lê o cookie antes do `accept`; sem usuário → `close(code=4401)`).
- Proteção: todas as rotas `/api/*` exceto `/api/login*` exigem sessão; arquivos do frontend continuam públicos. Métodos POST/PUT/DELETE com cabeçalho `Origin` de outro host → 403.
- `app.state.accounts: AccountStore` criado no `lifespan`; `settings`: `cookie_secure` (`COOKIE_SECURE`, padrão `1`), `session_idle_hours=8`, `session_max_hours=24`.
- Sessões de usuário desativado/expirado: uma tarefa no `lifespan` a cada 5 s fecha os WebSockets (`/ws/*`) desses usuários (registro de sockets abertos por token em `auth.py`: `track(token, socket)`, `untrack`).
- Auditoria: `login`, `login_falhou` (com o login tentado em `detail`), `logout`.
- `conftest.py`: `client_as(role: str, client_id: int | None = None) -> TestClient` (cria usuário no `AccountStore` do app de teste e faz login; para `mae` cria com TOTP e faz o passo do código).

- [ ] **Step 1: Testes**
```python
def test_rotas_exigem_login(client):  # GET /api/machines sem cookie -> 401
def test_login_com_cookie_seguro(client):
    r = login(client, "ana", "senha12345")
    c = r.headers["set-cookie"]; assert "HttpOnly" in c and "Secure" in c and "SameSite=Strict" in c
def test_mesmo_erro_para_usuario_inexistente(): ...
def test_bloqueio_progressivo(): # 5 falhas -> 429; LoginLimiter.wait_for == 30, depois 60 ...
def test_mae_precisa_do_codigo(): # need_code; código errado -> 401; certo -> me.role == "mae"
def test_logout_invalida(): ...
def test_origin_de_outro_site_bloqueado(): # POST /api/logout com Origin https://evil.example -> 403
def test_ws_sem_login_fecha_4401(): ...
def test_usuario_desativado_perde_ws_em_ate_10s(): # WS aberto; desativa; recv -> fechado
def test_login_registrado_na_auditoria(): ...
```
- [ ] **Step 2:** pytest → FAIL. **Step 3:** implementar; ajustar testes existentes da API para usar `client_as`. **Step 4:** suite inteira passa.
- [ ] **Step 5:** Commit `feat: login com sessão, 2º fator e proteção das rotas`.

### Task 6: `Fleet` — todas as máquinas coletando, intervalo 10 s ↔ rápido

**Files:** Modify `backend/app/manager.py` (classe `Fleet` substitui `MachineManager`), `backend/app/collector.py`, `backend/app/config.py`; Test `backend/tests/test_fleet.py` (substitui `test_manager.py`, aproveitando os casos de fechamento/limite de tempo que ainda se aplicam)

**Interfaces — Produces:**
- `Collector.set_poll_seconds(seconds: float) -> None` — vale na próxima volta e acorda o laço se estiver dormindo.
- `MachineRuntime`: atributos `machine: Machine`, `collector: Collector`, `hub: VncHub | None`, `bridge`; `enter() -> None` / `leave() -> None` (contador de quem está olhando: dados ou tela). Com ≥1: `poll_seconds` rápido; 0 por `viewer_linger` s: `idle_poll_seconds`.
- `Fleet(store, settings, accounts, source_factory=None, bridge_factory=None)`: `start()` (um runtime por máquina cadastrada), `stop()`, `runtime(id) -> MachineRuntime | None`, `async add(id)`, `async reload(id, connection_changed: bool)` (só reabre esse runtime; nome/cliente/notas → só atualiza), `async remove(id)` (fecha coletor, hub, ponte; WebSockets da máquina fecham), `status_of(id) -> dict` (`online, state, latency_ms, checked, active_alarms: int`).
- Sem máquina ativa global: removidos `activate`, `machine`, `collector`, `hub`, `bridge` e o aviso `machine_changed`.
- `settings`: `poll_seconds` padrão `2`, `idle_poll_seconds=10` (`IDLE_POLL_SECONDS`), `viewer_linger_seconds=60` (`VIEWER_LINGER_SECONDS`). Remover `vnc_tcp_port`/`vnc_tcp_bind` e `start_tcp` do uso (a porta VNC sem login deixa de existir — o Espelho IHM não conecta mais direto).

- [ ] **Step 1: Testes**
```python
async def test_todas_as_maquinas_coletam(): # 3 máquinas com fontes falsas -> 3 coletores lendo
async def test_intervalo_rapido_com_espectador_e_volta_a_10s():
    rt.enter(); assert rt.collector.poll_seconds == 2
    rt.leave(); avançar 59 s -> 2 ; 61 s -> 10
async def test_maquina_travada_nao_atrasa_as_outras(): # fonte A com read() que nunca volta; B segue gravando
async def test_reload_so_reabre_a_maquina_mudada(): ...
async def test_remove_fecha_tudo_da_maquina(): ...
```
- [ ] **Step 2:** FAIL. **Step 3:** implementar; se a Task 1 falhou, o `build_source` de `ads` usa um adaptador de subprocesso com a mesma interface. **Step 4:** suite passa.
- [ ] **Step 5:** Commit `feat: todas as máquinas conectadas, leitura de 10 s quando ninguém olha`.

### Task 7: Rotas por máquina com permissão por perfil

**Files:** Create `backend/app/authz.py`; Modify `backend/app/main.py`; Test `backend/tests/test_api_machines.py`

**Interfaces — Consumes:** Tasks 3, 5, 6. **Produces:**
- `authz.can_view(user, machine) -> bool` (mae/tecnico: todas; cliente: `machine.client_id == user.client_id`, nunca `None`).
- `authz.sees_connection(user) -> bool` (mae, tecnico).
- `authz.control_problem(user, machine, grant) -> str | None` — cliente: `"Cliente só visualiza a tela."`; máquina com cliente e sem autorização ativa: `"Precisa da autorização do cliente."`; máquina em preparação: `None` para mae/tecnico.
- `authz.machine_view(user, machine, status, client_name, *, as_client=False) -> dict` — cliente (ou `as_client`): `{id, name, kind, profile, client_id, client_name, status, vnc}`; mae/tecnico: `machine.public()` + `client_name` + `status`.
- Rotas (todas com `current_user`; máquina invisível ou inexistente → 404 `"Máquina não encontrada"`):
  - `GET /api/machines?client_id=` — lista filtrada por perfil; `client_id=0` = em preparação (só mae/tecnico).
  - `POST/PUT/DELETE /api/machines[/{id}]` — só `mae` (403 para os outros); `PUT` aceita `client_id` (vínculo; `null` = em preparação) e chama `fleet.reload`; `DELETE` chama `fleet.remove`. Auditoria `cadastro`.
  - `POST /api/machines/{id}/test` — mae/tecnico.
  - `GET /api/machines/{id}/info?as=cliente`, `/live`, `/history`, `/production`, `/alarms`, `/values`, `/tags` — mesmo conteúdo de hoje, por máquina; `info` traz `support` (Task 9) e `machine_view`.
  - `WS /ws/machines/{id}` — dados ao vivo; `enter()` ao abrir e `leave()` ao fechar.
  - Removidos: `/api/info`, `/api/live`, `/api/history`, `/api/production`, `/api/alarms`, `/api/values`, `/api/tags`, `/ws`, `/api/machines/{id}/activate`.

- [ ] **Step 1: Testes**
```python
@pytest.mark.parametrize("rota", ["/info", "/live", "/history?tags=CycleMonitor", "/production",
                                  "/alarms", "/values?tags=CycleMonitor", "/tags"])
def test_cliente_nao_ve_maquina_de_outro_cliente(rota): # cliente de A em máquina de B -> 404
def test_cliente_nao_recebe_dados_de_conexao(): # /api/machines e /info sem host, ams_net_id, notes, portas
def test_ver_como_cliente(): # tecnico ?as=cliente -> sem host e vnc_control False
def test_so_mae_cadastra_e_vincula(): # tecnico POST -> 403; mae PUT client_id -> cliente passa a ver
def test_em_preparacao_invisivel_para_cliente(): ...
def test_ws_de_maquina_de_outro_cliente_fecha(): ...
def test_nenhuma_senha_em_resposta(): # nenhuma resposta contém a senha VNC cadastrada
```
- [ ] **Step 2:** FAIL. **Step 3:** implementar. **Step 4:** suite passa (incluindo `test_codigo_nao_escreve_na_maquina`).
- [ ] **Step 5:** Commit `feat: rotas por máquina e permissões por perfil`.

### Task 8: Ponte CERHost por máquina e limite de telas

**Files:** Modify `backend/app/cerhost.py`, `backend/app/manager.py`, `config.py`; Test `backend/tests/test_cerhost.py`, `test_fleet.py`

**Interfaces — Produces:**
- `CerhostBridge(host, display, cerhost_exe, view_password="", control_password="", ..., pidfile=...)`: sem senhas recebidas, gera as suas (`secrets`, 8 caracteres) — o x11vnc continua `-localhost` com `-passwdfile rm:`; propriedades `view_password`/`control_password` para o hub.
- `Fleet.open_screen(id) -> VncHub` — cria hub (e ponte, se `ads`) sob demanda; displays alocados de `cerhost_display` em diante (um por ponte viva); pidfile por display (`data/cerhost_<display>.pids`). Acima de `cerhost_max_screens` pontes vivas → `ScreenLimit` com o texto das Global Constraints.
- Tela ociosa: sem ninguém por `screen_idle_seconds` (`SCREEN_IDLE_SECONDS`, padrão 120) → hub e ponte fechados, display liberado.
- `_wants_screen` deixa de exigir senha de visualização (painel B&R sem senha funciona — o hub já aceita segurança "None" do painel).

- [ ] **Step 1: Testes**: `test_duas_pontes_usam_displays_diferentes`; `test_terceira_tela_beckhoff_recusada_com_mensagem`; `test_ponte_sem_senha_gera_as_proprias_fora_do_argv`; `test_tela_ociosa_fecha_e_libera_display`; `test_br_sem_senha_vnc_abre_tela`.
- [ ] **Step 2:** FAIL. **Step 3:** implementar. **Step 4:** suite passa.
- [ ] **Step 5:** Commit `feat: uma tela da Beckhoff por máquina, com limite`.

### Task 9: Tela ao vivo pelo login, autorização de suporte e controle com confirmação

**Files:** Modify `backend/app/vnc_hub.py`, `backend/app/main.py`, `manager.py`; Test `backend/tests/test_vnc_hub.py`, `test_api_machines.py`

**Interfaces — Consumes:** Tasks 3, 5, 7, 8. **Produces:**
- `VncHub.serve(transport, sid, peer, role: str, label: str)` — `role` é `"view"` ou `"control"` (capaz de controlar), vindo do login; o handshake com o navegador oferece segurança **None (tipo 1)** e não pede senha. `audit_sink: Callable[[str], None]` substitui `audit_path`.
- `VncHub.take(sid)` só aceita sessão `role == "control"`; a decisão de permissão fica na rota.
- Rotas:
  - `WS /ws/machines/{id}/vnc?sid=` — `role = "view"` para cliente ou `as=cliente`; `"control"` para mae/tecnico; `ScreenLimit` → fecha com o texto do limite; `enter()/leave()` no runtime; auditoria `tela_abriu`/`tela_saiu`.
  - `GET /api/machines/{id}/vnc/status?sid=` — como hoje + `support` + `bridge`.
  - `POST /api/machines/{id}/vnc/control {sid, action: "take"|"release", password?, code?}` — `take`: `authz.control_problem` (403 com o texto) **e** `verify_password` da própria senha (+ `verify_totp` se o usuário tem TOTP; 401 `"Senha ou código incorretos."`); depois `hub.take`. Cada `take` pede confirmação de novo. Auditoria `controle_assumido`/`controle_liberado`.
  - `POST /api/machines/{id}/support {minutes?: 30, on_behalf?: false, reason?: ""}` — cliente da máquina (normal) ou mae com `on_behalf=true` + motivo (destacado); técnico → 403. `DELETE /api/machines/{id}/support` — cliente da máquina ou mae. Auditoria `suporte_autorizado`, `suporte_autorizado_em_nome` (highlight), `suporte_encerrado`.
  - `info.support = {active, expires, by_name, on_behalf, reason}` visível para cliente e técnico.
- Vigia no `Fleet` a cada 5 s: controlador em máquina com cliente e sem autorização ativa → `hub.release(s, "autorização do cliente encerrada")`.

- [ ] **Step 1: Testes**
```python
async def test_cliente_entra_so_visualizando_e_nao_assume(): # take -> 403 "Cliente só visualiza a tela."
async def test_tecnico_sem_autorizacao_nao_assume(): # 403 "Precisa da autorização do cliente."
async def test_tecnico_com_autorizacao_e_senha_assume(): # senha errada 401; certa -> ok; auditoria com nome
async def test_cada_tomada_pede_senha_de_novo(): ...
async def test_autorizacao_expirada_tira_o_controle_em_ate_5s(): ...
async def test_mae_em_nome_do_cliente_exige_motivo_e_aparece_para_o_cliente(): ...
async def test_maquina_em_preparacao_dispensa_autorizacao(): ...
async def test_tela_de_maquina_de_outro_cliente_404(): ...
async def test_handshake_sem_senha_vnc_para_o_navegador(): # fake_vnc: oferece tipo 1
```
- [ ] **Step 2:** FAIL. **Step 3:** implementar. **Step 4:** suite passa.
- [ ] **Step 5:** Commit `feat: tela ao vivo pelo login, autorização do cliente e controle com confirmação`.

### Task 10: Rotas da conta mãe — clientes, usuários, registros

**Files:** Create `backend/app/admin_api.py` (router incluído no `main`); Test `backend/tests/test_admin_api.py`

**Interfaces — Produces:**
- `GET /api/clients` (mae, tecnico) → `[{id, name, active, machines: int}]`; `POST /api/clients {name}`, `PUT /api/clients/{id} {name?, active?}` (mae).
- `GET /api/users` (mae) → sem hash/segredo; `POST /api/users {username, name, role, password, client_id?}` (não cria `mae`: 422 `"A conta mãe só é criada pelo comando do servidor."`); `PUT /api/users/{id} {name?, active?, client_id?}`; `POST /api/users/{id}/password {password}` (derruba sessões).
- `POST /api/me/password {current, new}` (qualquer usuário).
- `GET /api/audit?user_id=&client_id=&machine_id=&since=&until=&limit=` (mae).
- Toda escrita registrada em `cadastro` com `detail` sem senhas.

- [ ] **Step 1: Testes**: `test_so_mae_administra` (tecnico/cliente → 403); `test_nao_cria_mae_pela_api`; `test_desativar_usuario_derruba_sessao`; `test_resposta_sem_hash`; `test_trocar_a_propria_senha_exige_a_atual`; `test_auditoria_filtros`.
- [ ] **Step 2:** FAIL. **Step 3:** implementar. **Step 4:** PASS.
- [ ] **Step 5:** Commit `feat: administração de clientes, usuários e registros`.

### Task 11: HTTPS, `run.sh`, demo e documentação

**Files:** Create `scripts/gerar_certificado.sh`; Modify `run.sh`, `backend/app/demo_seed.py`, `.env.example`, `README.md`

- [ ] **Step 1:** `scripts/gerar_certificado.sh [IP ...]`: `openssl req -x509 -newkey rsa:2048 -nodes -days 825 -subj "/CN=monitor-injetoras" -addext "subjectAltName=DNS:localhost,IP:127.0.0.1,IP:<cada IP>"` em `data/tls/{cert,key}.pem`, chave com `chmod 600`; recusa sobrescrever sem `--forcar`.
- [ ] **Step 2:** `run.sh`: se faltar `data/tls/cert.pem`, roda o script com os IPs da máquina (`hostname -I`); sobe `uvicorn ... --ssl-certfile data/tls/cert.pem --ssl-keyfile data/tls/key.pem`; se não houver conta mãe, imprime a instrução do comando `criar-mae` e sobe mesmo assim.
- [ ] **Step 3:** `--demo`: o `demo_seed` cria uma conta mãe `demo` com senha e TOTP aleatórios impressos no terminal (só no modo demo).
- [ ] **Step 4:** `.env.example` e README: `IDLE_POLL_SECONDS`, `VIEWER_LINGER_SECONDS`, `CERHOST_MAX_SCREENS`, `SCREEN_IDLE_SECONDS`, `COOKIE_SECURE`; seção "Acesso e perfis"; aviso de que a porta VNC direta (Espelho IHM) saiu; roteiro de instalação do RUT (firewall do ZeroTier só do servidor do Monitor para 5900/987/48898/4840; senha VNC única por máquina, colocada na fábrica ou na visita).
- [ ] **Step 5:** Verificação: `./run.sh --demo` → `curl -sk https://localhost:8000/api/me` dá 401. Commit `feat: HTTPS com certificado próprio e documentação de acesso`.

### Task 12: Frontend — login, sessão e máquina na URL

**Files:** Create `frontend/src/LoginPage.tsx`, `frontend/src/auth.ts`; Modify `frontend/src/App.tsx`, `api.ts`, `pages.tsx`

**Interfaces — Produces:**
- `auth.ts`: `useMe() -> { me: Me | null, loading, refresh, logout }`; `Me = {id, username, name, role: 'mae'|'tecnico'|'cliente', client_id, client_name, has_totp}`.
- `api.ts`: `getJson`/`postJson`/`useLive`/`usePoll` com `credentials: 'same-origin'`; resposta 401 dispara o evento `sessao-expirada` (App volta ao login). Hooks de dados recebem `machineId: number`.
- Rotas por hash: `#/maquinas` (lista), `#/m/<id>/<pagina>` (`geral|painel|tela|temperaturas|producao|alarmes|variaveis`), `#/clientes`, `#/usuarios`, `#/registros`. Duas abas em máquinas diferentes funcionam juntas.
- Lista de máquinas: mae/tecnico com filtro "Cliente" (todos, cada cliente, "Em preparação") e "Só com alarme"; cliente vê só as dele, sem filtro. Coluna de estado e nº de alarmes.
- Barra do topo: nome do usuário e perfil, "Sair"; mae/tecnico dentro de uma máquina: botão "Ver como o cliente" (liga `?as=cliente` nas chamadas de `info` e na tela).
- Remover: seletor de máquina ativa, faixa "máquina trocada", `activateMachine`.

- [ ] **Step 1:** Implementar. **Step 2:** `npm run build` e `npm run lint` sem erro novo. **Step 3:** Verificação manual com `./run.sh --demo`: login com código; duas abas em máquinas diferentes; sair e voltar. **Step 4:** Commit `feat: tela de login e máquina por endereço`.

### Task 13: Frontend — páginas da conta mãe e cadastro com cliente

**Files:** Create `frontend/src/AdminPages.tsx`; Modify `frontend/src/MachinesPage.tsx`, `App.tsx`

- Menu (só mae): **Clientes** (listar, criar, renomear, desativar), **Usuários** (listar com filtro por perfil/cliente, criar com perfil e cliente, ativar/desativar, redefinir senha), **Registros** (tabela com filtros pessoa/cliente/máquina/período; linhas destacadas para `suporte_autorizado_em_nome`).
- `MachinesPage`: campo "Cliente" vira lista dos clientes + "Em preparação"; só mae vê Nova/Editar/Apagar; aviso **"Painel sem senha VNC"** quando `vnc_sem_senha`.
- Minha conta (todos): trocar a própria senha.
- [ ] Build/lint sem erro novo; verificação manual: criar cliente, vincular máquina, criar usuário cliente e conferir que ele só vê aquela. Commit `feat: páginas de clientes, usuários e registros`.

### Task 14: Frontend — Tela ao vivo com autorização e controle confirmado

**Files:** Modify `frontend/src/TelaAoVivo.tsx`

- Conecta em `/ws/machines/<id>/vnc` sem pedir senha VNC.
- **Cliente:** nunca vê "Assumir controle". Caixa "Suporte remoto": "Autorizar suporte por 30 min" / "Encerrar suporte"; mostra a autorização ativa ("Autorizado até 14:35 por Maria"); se `on_behalf`: "Suporte autorizado pela Chen Hsong em seu nome — motivo: …".
- **Técnico/mae:** "Assumir controle" habilitado só com autorização ativa (ou máquina em preparação); abre diálogo pedindo a própria senha (e o código, se `has_totp`); erros do servidor mostrados como vêm. Mae vê "Autorizar em nome do cliente" com motivo obrigatório.
- Mensagens do limite de telas da Beckhoff e de controle retirado ("autorização do cliente encerrada") aparecem na faixa de aviso existente.
- [ ] Build/lint sem erro novo; verificação manual (Task 15). Commit `feat: autorização de suporte e controle com confirmação na Tela ao vivo`.

### Task 15: Bancada (manual, com o Higor)

- [ ] Criar a conta mãe pelo comando; entrar em `https://<IP>:8000` (aceitar o certificado uma vez).
- [ ] B&R, XMold, CBmold e CHY conectadas juntas; a lista mostra as quatro com estado real; leituras a cada 10 s sem ninguém olhando (log), rápidas ao abrir.
- [ ] Cliente de teste "Teste ABC" com a CBmold vinculada; usuário cliente em outro navegador vê só ela; tentar `#/m/<id da B&R>` → "Máquina não encontrada".
- [ ] Técnico tenta assumir a CBmold sem autorização → bloqueado; cliente autoriza; técnico assume com senha; cliente encerra → controle volta a visualização; tudo em **Registros**.
- [ ] XMold e CBmold com tela aberta ao mesmo tempo; terceira Beckhoff (se houver) recusada com a mensagem.
- [ ] Desativar o usuário cliente com a tela aberta → cai em até 10 s.
- [ ] `pgrep -a -f "Xvfb|x11vnc|CERHOST"` vazio alguns minutos depois de fechar as telas.
