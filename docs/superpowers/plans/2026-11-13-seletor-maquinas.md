# Seletor de máquinas — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Cadastrar máquinas (CHY, B&R, Beckhoff) dentro do Monitor e trocar a máquina ativa pelo navegador, sem editar `.env` nem reiniciar.

**Architecture:** Um `MachineStore` (SQLite de configuração) guarda o cadastro e a máquina ativa. Um `MachineManager` é dono do `Collector` e do `VncHub` da máquina ativa e os troca sob uma trava; os endpoints atuais passam a pedir a máquina ativa ao manager. Beckhoff entra por uma nova `AdsSource` (pyads) com a mesma interface `Source`.

**Tech Stack:** FastAPI 0.162 + asyncua 2.1 + pyads, SQLite (`sqlite3`), React 19 + Vite + TypeScript, pytest.

**Spec:** `docs/superpowers/specs/2026-11-13-seletor-maquinas-design.md`

Fase 2 do spec (tela da Beckhoff via CERHost/Wine) é **um plano separado**, depois deste.

## Global Constraints

- **Só leitura.** Nenhuma escrita em PLC: nada de `write_value`/`set_value`/`write_by_name`/`write_control` nem `plc.write(` em `app/`. O teste `test_codigo_nao_escreve_na_maquina` continua passando e ganha o padrão ADS (Task 2).
- Senhas VNC **nunca** saem em resposta HTTP/WS: a API expõe só `has_view_password` / `has_control_password`.
- Tipos de máquina (`kind`): `chy` | `em63` | `ads` | `demo`. Mapeamento para o `profile` atual: `chy`→`chy`, `em63`→`em63`, `ads`→`ads`, `demo`→`chy` (simulador).
- Padrões: `client` = `"Chen Hsong"`; `opcua_port` = 4840; `namespace` = 1 (`chy`) / 6 (`em63`, como hoje); `ads_port` = 801; `vnc_port` = 5900; `vnc_enabled` padrão = `kind == "em63"`.
- Arquivos: cadastro em `data/monitor_config.db` (`CONFIG_DB_PATH`); histórico por máquina em `data/machines/<id>.db` (`MACHINES_DIR`), cache de tags em `data/machines/<id>_tags.json`.
- Status da lista: teste TCP a cada **30 s**, timeout **3 s**, porta 4840 (OPC-UA) ou **48898** (ADS).
- Nova variável de servidor: `ADS_LOCAL_NET_ID` (AMS Net ID do PC do Monitor, ex.: `10.114.222.48.1.1`). (Chamada minha: é do servidor, não da máquina.)
- Textos de interface em português, no tom atual do Monitor.

## Review Focus

1. **Apagar a máquina ativa** → `409` com mensagem clara; a ativa continua rodando.
2. **Ativar máquina inalcançável** (4G caiu, IP errado) → `activate` responde em < 1 s; o coletor fica em "sem conexão" tentando de novo; dá para trocar de volta na hora.
3. **Editar sem redigitar senha** (`PUT` com senha vazia) → mantém a senha antiga; nenhum GET/`/api/info` contém o texto da senha.
4. **Dois cliques rápidos em "Abrir"** em máquinas diferentes → a última vence, sem coletor antigo vivo (sem tarefa vazando conexão no 4G).
5. **Navegador aberto durante a troca** → recebe `machine_changed`, o `/ws` fecha, a página recarrega dados da máquina nova; espectadores VNC são desconectados.

---

### Task 1: Cadastro de máquinas (`MachineStore`)

**Files:**
- Create: `backend/app/machines.py`
- Modify: `backend/app/config.py` (novos campos `config_db_path`, `machines_dir`, `ads_local_net_id`)
- Test: `backend/tests/test_machines.py`

**Interfaces:**
- Produces:
  - `@dataclass Machine(id: int | None, name: str, kind: str, host: str, client: str = "Chen Hsong", opcua_port: int = 4840, namespace: int | None = None, ams_net_id: str = "", ads_port: int = 801, vnc_enabled: bool | None = None, vnc_port: int = 5900, vnc_view_password: str = "", vnc_control_password: str = "", notes: str = "")` com `namespace`/`vnc_enabled` `None` = padrão do tipo.
  - `Machine.public() -> dict` (todos os campos menos as senhas + `has_view_password`, `has_control_password`, `profile`, `opcua_url`).
  - `Machine.opcua_url -> str` (`opc.tcp://{host}:{opcua_port}`), `Machine.profile -> str`, `Machine.status_port -> int` (48898 para `ads`, senão `opcua_port`).
  - `class MachineStore(path: str)`: `list() -> list[Machine]`, `get(id) -> Machine | None`, `add(m) -> Machine`, `update(id, changes: dict) -> Machine` (senha `""` ou ausente = mantém), `delete(id) -> None`, `active_id() -> int | None`, `set_active(id) -> None`, `close()`.
  - `seed_from_settings(store: MachineStore, settings, machines_dir: str) -> None`: se vazio, cria a máquina do `.env` (ou `demo` com `SIMULATE=1`), marca ativa e **copia** (`shutil.copy2`) `settings.db_path` → `machines_dir/<id>.db` e `tags_cache.json` → `<id>_tags.json` se existirem e o destino não existir.
  - `ValueError` para `kind` desconhecido, `host` vazio ou porta fora de 1–65535.

- [ ] **Step 1: Write failing tests** em `test_machines.py`:
  - `test_crud_e_ativa`: `add` duas → `list()` tem 2; `set_active(b.id)`; `active_id() == b.id`; `update(a.id, {"name": "X"}).name == "X"`; `delete(a.id)` → `get(a.id) is None`.
  - `test_senha_vazia_mantem`: `add(... vnc_view_password="v1")`; `update(id, {"vnc_view_password": ""})` → `get(id).vnc_view_password == "v1"`; `update(id, {"vnc_view_password": "v2"})` → `"v2"`.
  - `test_public_sem_senha`: `m.public()` não tem chaves `vnc_view_password`/`vnc_control_password`; `has_view_password is True`; `"v1" not in json.dumps(m.public())`.
  - `test_padroes_por_tipo`: `Machine(None,"B&R","em63","10.86.20.253").public()` → `namespace == 6`, `vnc_enabled is True`, `profile == "em63"`, `opcua_url == "opc.tcp://10.86.20.253:4840"`; `kind="ads"` → `status_port == 48898`, `vnc_enabled is False`; `kind="demo"` → `profile == "chy"`.
  - `test_validacao`: `add(Machine(None,"x","plc","1.2.3.4"))` e `host=""` e `opcua_port=70000` → `ValueError`.
  - `test_seed_do_env(tmp_path)`: settings falso (`SimpleNamespace(opcua_url="opc.tcp://10.86.20.253:4840", profile="em63", machine_name="JM228", vnc_view_password="v", vnc_control_password="", vnc_host="", vnc_port=5900, simulate=False, db_path=str(tmp_path/"monitor.db"), namespace=1)`) com `monitor.db` existente → 1 máquina `em63`, host `10.86.20.253`, ativa, e `tmp_path/"machines"/"1.db"` existe; chamar de novo não duplica.
- [ ] **Step 2:** `cd backend && python -m pytest tests/test_machines.py -v` → FAIL (`ModuleNotFoundError: app.machines`).
- [ ] **Step 3:** Implementar `machines.py` (tabelas `machines` e `kv(key TEXT PRIMARY KEY, value TEXT)`, `check_same_thread=False` + `threading.Lock` como em `storage.py`) e os campos em `config.py`: `config_db_path = _path(os.getenv("CONFIG_DB_PATH", "data/monitor_config.db"))`, `machines_dir = _path(os.getenv("MACHINES_DIR", "data/machines"))`, `ads_local_net_id = os.getenv("ADS_LOCAL_NET_ID", "")`.
- [ ] **Step 4:** `python -m pytest tests/test_machines.py -v` → PASS; `python -m pytest -q` → todos passam.
- [ ] **Step 5:** Commit `feat: cadastro de maquinas em SQLite (MachineStore)`.

---

### Task 2: Fonte ADS da Beckhoff (`AdsSource`)

**Files:**
- Create: `backend/app/ads_source.py`
- Modify: `backend/app/tags.py` (`alarm_label` reconhece `WarnAds<ID>`), `backend/requirements.txt` (+`pyads`, versão exata que o pip resolver, ≥ 3.6), `backend/tests/test_monitor.py` (padrão de escrita)
- Test: `backend/tests/test_ads_source.py`

**Interfaces:**
- Produces:
  - `ADS_ALARMS: dict[int, str]` — os 10 IDs do kit (`10, 11, 12, 15, 161, 162, 225, 226, 318, 419`, textos de `beckhoff-kit/03_dados.py`).
  - `class AdsSource(ams_net_id: str, host: str, port: int = 801, local_net_id: str = "", plc_factory=None)` — `plc_factory(net_id, port, host)` devolve um objeto com `open() / close() / read_by_name(name, plctype) / get_symbol(name) / read(index_group, index_offset, plctype)`; padrão = `pyads.Connection`. Métodos `Source`: `async connect()`, `async close()`, `async list_tags() -> list[str]`, `async read(tags) -> dict[str, object]`; chamadas pyads via `asyncio.to_thread`.
  - Tags produzidas: `CycleMonitor` (`.CYCLETIME_MQTT`, LREAL), `ProductionCount` (`.NPARTSCNT_MQTT`, DINT), `GoodProduction` (`.NGOODPARTS`), `BadProduction` (`.NBADPARTS`), `TempZ1`…`TempZ8` (setpoint, LREAL no byte `(z-1)*112` de `.AAAPFWTEMPPPARAMFROMHMI`), `StandbyTempZ1`…`StandbyTempZ8` (byte `+8`), e `WarnAds<ID>` = 1.0/0.0 (de `.STHMIMESSAGE`, 96 bytes/linha, 100 linhas, ID DINT no byte 0, hora em texto no byte 56; linha com hora vazia = sem alarme).
  - `list_tags()` = tags fixas + `WarnAds<ID>` para cada ID de `ADS_ALARMS` + IDs já vistos ativos.
  - `alarm_label("WarnAds318") == "Porta do Bico esta aberta"`; ID desconhecido → `"Alarme Beckhoff <ID>"`.

- [ ] **Step 1: Write failing tests** em `test_ads_source.py` com um `FakePlc` (bytes montados com `struct.pack`):
  - `test_le_kpis_setpoints_e_alarmes`: fake com `.CYCLETIME_MQTT=24.5`, `.NPARTSCNT_MQTT=120`, zona 5 setpoint 195.0 / standby 150.0, alarmes ativos IDs 318 e 999 → `read([...])` dá `CycleMonitor == 24.5`, `ProductionCount == 120`, `TempZ5 == 195.0`, `StandbyTempZ5 == 150.0`, `WarnAds318 == 1.0`, `WarnAds15 == 0.0`; depois disso `"WarnAds999" in await src.list_tags()`.
  - `test_rotulo_alarme_ads`: valores acima de `alarm_label`.
  - `test_fecha_conexao`: `close()` chama `FakePlc.close()` uma vez; `close()` sem `connect()` não explode.
  - Em `test_monitor.py`, `test_codigo_nao_escreve_na_maquina` ganha `write_by_name|write_control|plc\.write\(` no regex.
- [ ] **Step 2:** `python -m pytest tests/test_ads_source.py -v` → FAIL (módulo inexistente).
- [ ] **Step 3:** Implementar `ads_source.py` e o ramo `WarnAds` em `alarm_label`; adicionar `pyads` ao `requirements.txt` e instalar.
- [ ] **Step 4:** `python -m pytest -q` → todos passam (inclusive o teste de "não escreve").
- [ ] **Step 5:** Commit `feat: fonte ADS para paineis Beckhoff (so leitura)`.

---

### Task 3: `MachineManager` (troca da máquina ativa e status)

**Files:**
- Create: `backend/app/manager.py`
- Modify: `backend/app/collector.py` (novo `notify(payload: dict)`)
- Test: `backend/tests/test_manager.py`

**Interfaces:**
- Consumes: `MachineStore`, `Machine` (Task 1); `AdsSource` (Task 2); `Collector`, `Storage`, `OpcUaSource`, `Em63Source`, `SimulatedSource`, `VncHub` existentes.
- Produces:
  - `Collector.notify(payload: dict) -> None` — coloca `payload` em todas as filas de assinantes (mesma lógica de fila cheia de `_broadcast`).
  - `class MachineManager(store: MachineStore, settings, source_factory=None)`:
    - `machine: Machine | None`, `collector: Collector | None`, `hub: VncHub | None` (da ativa).
    - `async start() -> None` (abre a ativa do store, se houver; inicia o laço de status), `async stop() -> None`.
    - `async activate(machine_id: int) -> Machine` — sob `asyncio.Lock`: `notify({"type": "machine_changed", "machine_id": id})` no coletor velho, `await collector.stop()`, `storage.close()`, `await hub.close()`; monta os novos; `collector.start()`; hub VNC só se `vnc_enabled` e senha de visualização; `hub.start_tcp(settings.vnc_tcp_bind, settings.vnc_tcp_port)` se porta > 0 (erro de porta só vira log); `store.set_active(id)`. **Não espera a conexão** com a máquina. `KeyError` se o id não existe.
    - `build_source(m: Machine) -> Source` (`demo`→`SimulatedSource()`, `em63`→`Em63Source(m.opcua_url, ns)`, `chy`→`OpcUaSource(m.opcua_url, ns, cache_file=<id>_tags.json)`, `ads`→`AdsSource(m.ams_net_id, m.host, m.ads_port, settings.ads_local_net_id)`); `source_factory` sobrescreve nos testes.
    - `status_of(machine_id) -> dict` = `{"online": bool | None, "latency_ms": float | None, "checked": float | None}`; a ativa usa `collector.status()` + `machine_state()`.
    - `async probe(host: str, port: int, timeout: float = 3) -> float | None` (latência em ms via `asyncio.open_connection`, `None` se falhar).
    - laço de status a cada 30 s sobre `store.list()`.

- [ ] **Step 1: Write failing tests** em `test_manager.py` (store em `tmp_path`, settings `SimpleNamespace` com `machines_dir`, `poll_seconds=0.05`, `full_poll_seconds=0.2`, `retention_days=7`, `vnc_tcp_port=0`, `vnc_tcp_bind="127.0.0.1"`, `vnc_control_timeout=300`, `vnc_audit_path=None`, `ads_local_net_id=""`):
  - `test_troca_fecha_o_anterior`: duas máquinas `demo`; `activate(a)` → espera 0.3 s → `collector.values` não vazio; guarda `old = mgr.collector`; `activate(b)` → `old._task.done()` e `mgr.machine.id == b` e `store.active_id() == b`; arquivos `machines/a.db` e `machines/b.db` existem (histórico separado).
  - `test_aviso_machine_changed`: assina o coletor de `a` (`q = mgr.collector.subscribe()`), `activate(b)` → alguma mensagem em `q` tem `type == "machine_changed"` e `machine_id == b`.
  - `test_ativar_inalcancavel_e_rapido`: máquina `em63` com host `10.255.255.1` → `activate` termina em < 1 s; `mgr.collector.connected is False`.
  - `test_cliques_rapidos`: `await asyncio.gather(mgr.activate(a), mgr.activate(b))` → `mgr.machine.id == b`; só um coletor com `_task` não terminado entre os criados (guardar via `source_factory`).
  - `test_probe`: `asyncio.start_server` local → `probe("127.0.0.1", porta)` é float; porta fechada → `None`.
- [ ] **Step 2:** `python -m pytest tests/test_manager.py -v` → FAIL.
- [ ] **Step 3:** Implementar `Collector.notify` e `manager.py`.
- [ ] **Step 4:** `python -m pytest -q` → todos passam.
- [ ] **Step 5:** Commit `feat: MachineManager troca a maquina ativa sem reiniciar`.

---

### Task 4: API — rotas de máquinas e endpoints atuais via manager

**Files:**
- Modify: `backend/app/main.py`, `.env.example`, `README.md` (seção "Máquinas")
- Modify: `backend/tests/test_monitor.py` (`test_api_com_simulador` passa a usar o manager)
- Test: `backend/tests/test_api_machines.py`

**Interfaces:**
- Consumes: `MachineStore`, `seed_from_settings` (Task 1), `MachineManager` (Task 3).
- Produces (HTTP):
  - `GET /api/machines` → `{"active_id": int | None, "machines": [Machine.public() + {"status": status_of(id)}]}`.
  - `POST /api/machines` (corpo = campos de `Machine` sem `id`) → `201` + `public()`; `ValueError` → `422`.
  - `PUT /api/machines/{id}` → `public()`; `404` se não existe. Se for a ativa, reativa para aplicar.
  - `DELETE /api/machines/{id}` → `204`; `409` `"Não dá para apagar a máquina aberta. Abra outra antes."` se for a ativa.
  - `POST /api/machines/{id}/activate` → `public()`; `404` se não existe.
  - `POST /api/machines/{id}/test` → `{"online": bool, "latency_ms": float | None}`.
  - `/api/info` passa a ter `machine_id`, `kind`, `client` e vem da máquina ativa (`opcua_url` = `m.opcua_url`; para `ads`, `"ads://<host>"`).
  - `/ws`: ao receber payload com `type == "machine_changed"`, envia e fecha o socket.
  - `build_manager() -> MachineManager` (store em `settings.config_db_path` + `seed_from_settings`); `lifespan` usa `app.state.manager` se já existir (testes).
  - Sem máquina ativa: `/api/live` etc. respondem `503` `"Nenhuma máquina aberta. Cadastre uma em Máquinas."`.

- [ ] **Step 1: Write failing tests** em `test_api_machines.py` (TestClient; manager com store em `tmp_path`, seed `demo`):
  - `test_crud_pela_api`: `POST` B&R → 201, `has_view_password` true e o texto da senha ausente de `GET /api/machines` e de `/api/info`; `PUT` com `vnc_view_password: ""` mantém (ver no store); `POST` com `kind: "plc"` → 422.
  - `test_nao_apaga_ativa`: `DELETE` da ativa → 409; outra → 204.
  - `test_trocar_pela_api`: `POST /api/machines/{b}/activate` → `GET /api/info` mostra `machine_id == b`; `GET /api/machines` `active_id == b`.
  - `test_ws_fecha_na_troca`: abre `/ws`, ativa outra → recebe mensagem com `type == "machine_changed"` e o socket fecha (`WebSocketDisconnect` no próximo receive).
  - `test_api_com_simulador` adaptado: em vez de `main.app.state.collector = col`, monta manager com máquina `demo` e intervalos rápidos; asserções atuais iguais.
- [ ] **Step 2:** `python -m pytest tests/test_api_machines.py -v` → FAIL.
- [ ] **Step 3:** Implementar em `main.py` (trocar `_c(app)` por um helper que pega `app.state.manager.collector` ou levanta 503; `_vnc_target`/`build_vnc_hub` saem daqui para o manager; `/ws/vnc` e `/api/vnc/*` usam `manager.hub`); atualizar `.env.example` (remover o bloco de máquina, explicar que a primeira subida importa o `.env` antigo; adicionar `CONFIG_DB_PATH`, `MACHINES_DIR`, `ADS_LOCAL_NET_ID`) e README.
- [ ] **Step 4:** `python -m pytest -q` → todos passam. Subir com `SIMULATE=1 ./run.sh`, `curl -s localhost:8000/api/machines` lista a máquina `demo`.
- [ ] **Step 5:** Commit `feat: API de maquinas e troca da maquina ativa`.

---

### Task 5: Frontend — tela Máquinas e seletor no topo

**Files:**
- Create: `frontend/src/MachinesPage.tsx`
- Modify: `frontend/src/api.ts` (tipos + funções), `frontend/src/App.tsx` (menu, seletor, troca), `frontend/src/styles.css`

**Interfaces:**
- Consumes: rotas da Task 4.
- Produces:
  - `api.ts`: `type MachineKind = 'chy' | 'em63' | 'ads' | 'demo'`; `interface MachineStatus { online: boolean | null; latency_ms: number | null; checked: number | null; state?: string }`; `interface Machine { id; client; name; kind; host; opcua_port; namespace; ams_net_id; ads_port; vnc_enabled; vnc_port; notes; has_view_password; has_control_password; profile; opcua_url; status?: MachineStatus }`; `Info` ganha `machine_id`, `kind`, `client`, e `profile` aceita `'ads'`; funções `listMachines()`, `saveMachine(m, id?)`, `deleteMachine(id)`, `activateMachine(id)`, `testMachine(id)`.
  - `useLive()` expõe também `changedTo: number | null` (último `machine_changed` recebido).
  - `<MachinesPage onOpened={() => void} />`.

Comportamento (texto em português):
- Menu: **Máquinas** é o primeiro item (`id: 'maquinas'`, ícone `Server` do lucide). Sem máquina ativa, abre direto nela.
- Lista agrupada por `client`: nome, tipo ("CHY · CHL3200", "B&R · Euromap 63", "Beckhoff · ADS", "Simulada"), IP, bolinha online/offline + "NN ms", selo **Aberta** na ativa; botões **Abrir**, **Editar**, **Testar conexão**, **Apagar** (desabilitado na ativa, com dica).
- Formulário (painel lateral): campos comuns + só OPC-UA (`opcua_port`, `namespace`) para `chy`/`em63`, só ADS (`ams_net_id`, `ads_port`) para `ads`, tela (`vnc_enabled`, `vnc_port`, duas senhas) para `chy`/`em63`. Senhas com placeholder "deixe em branco para manter" quando já existem. Erros 422/409 mostrados no formulário.
- Barra do topo: `<select>` com as máquinas (nome + cliente); trocar chama `activateMachine` e recarrega `/api/info`.
- Ao receber `machine_changed` (de outra pessoa ou de si): recarrega `/api/info`, mostra faixa "A máquina aberta mudou para <nome>." por 8 s, volta para Visão geral se a página atual não existe no novo tipo.
- Menus por tipo: **Painel da máquina** só `chy`/`demo`; **Tela ao vivo** só com `info.vnc`; marca no rail: `em63`→"OPC-UA · painel B&R", `ads`→"ADS · painel Beckhoff", senão "OPC-UA · gateway Mega". Rodapé "Somente leitura…" continua.

- [ ] **Step 1:** `cd frontend && npm run build` → passa antes de mexer (linha de base).
- [ ] **Step 2:** Implementar tipos/funções em `api.ts`, `MachinesPage.tsx`, mudanças em `App.tsx` e estilos (reusar classes existentes: `banner`, `muted`, botões do rail).
- [ ] **Step 3:** `npm run build && npm run lint` → sem erros.
- [ ] **Step 4:** Verificação manual com `SIMULATE=1 ./run.sh`: cadastrar segunda máquina `demo`, abrir, ver faixa de troca em outra aba; tentar apagar a ativa (botão desabilitado; via API 409); editar sem senha mantém `has_view_password`. Screenshot com Playwright em `/#maquinas` (claro e escuro).
- [ ] **Step 5:** Commit `feat: tela Maquinas e seletor de maquina no topo`.

---

### Task 6: Validação na rede real (manual, com o Higor)

- [ ] Cadastrar **JM228 B&R** (`em63`, host `10.86.20.253`, senhas VNC) e abrir pelo ZeroTier: dados e Tela ao vivo funcionando.
- [ ] Cadastrar **Beckhoff** (`ads`, IP que o RUT entregar, Net ID do painel), rota ADS feita com o kit apontando para o IP LAN do RUT, `ADS_LOCAL_NET_ID` no `.env`; abrir: ciclo, contadores, setpoints e alarmes aparecem.
- [ ] Trocar B&R ↔ Beckhoff três vezes; `ss -tnp | grep -E '4840|48898'` mostra só a conexão da máquina aberta.
- [ ] Push da branch.
