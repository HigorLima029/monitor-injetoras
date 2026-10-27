# Monitor de Injetoras

Painel web para acompanhar injetoras Chen Hsong em tempo real, lendo os dados
do servidor **OPC-UA do gateway Mega (CHL3200)**. Mostra estado da máquina,
produção, tempo de ciclo, temperaturas do canhão e alarmes, e guarda histórico
local. A ideia é ter dashboards no estilo do Things OS (Tomorrow Things), sem
depender de uma plataforma paga.

![Visão geral](docs/visao-geral.png)

> **Somente leitura.** O monitor só lê variáveis OPC-UA. Não existe nenhuma
> chamada de escrita no código, e um teste automático
> (`test_codigo_nao_escreve_na_maquina`) falha se alguém incluir uma.

## Telas

| Tela | O que mostra |
|---|---|
| Painel da máquina | Réplica das telas da IHM CHY (Início, Molde, Extrator, Macho, Injeção, Plast., Temp., Set rápido) com os valores reais do gateway |
| Visão geral | Peças em 8 h, último ciclo, refugo, alarmes ativos, canhão (zona 5 → bico), ciclo e peças por hora |
| Temperaturas | Real × programado por zona, com a faixa de tolerância da máquina; óleo hidráulico |
| Produção | Peças por intervalo, refugo, ciclo médio, meta da ordem, fases do último ciclo, pressão e colchão |
| Alarmes | Ativos agora, ranking por tempo parado, histórico com início e duração |
| Variáveis | As ~1000 variáveis do gateway, com busca |

Períodos: 1 h, 8 h, 24 h e 7 dias. Tema claro/escuro. Funciona no celular.

## Painel da máquina

![Réplica da tela Molde](docs/painel-molde.png)

Recria as principais telas do painel CHY (layout baseado nas fotos do painel e no
manual *Multifunctional Injection Molding Computer Operating Instructions*). Os
valores vêm do OPC-UA e atualizam a cada poucos segundos: a tela aberta pede as
suas variáveis em `GET /api/values`, que as inclui na leitura rápida.

- Trocar de tela aqui **não** muda a tela do painel físico; nenhum valor é editável.
- Campos com **—** existem no painel, mas o gateway não publica o dado (posições
  em tempo real, delays, nível de acesso). Passe o mouse sobre um valor para ver
  a variável OPC-UA de origem.
- O número de estágios vem do byte baixo das variáveis `*StageSelect`
  (ex.: `HoldPresStageSelect = 131074` → 2 estágios).
- Modos com texto (ex.: `PlastMode 1 = Pos.`, `ColdMode 2 = Durant. Plast.`,
  `EjectMode 3 = Avanç.`) foram conferidos com as fotos; outros códigos aparecem como "modo N".

## Como funciona

```
Injetora ──(porta 23)── Gateway Mega ──OPC-UA :4840── Backend (Python) ──WebSocket/REST── Navegador
                                                          │
                                                       SQLite (histórico)
```

- **Backend** (`backend/`, Python + FastAPI + asyncua): conecta no gateway, lê
  KPIs e alarmes a cada 5 s e todas as variáveis a cada 60 s, grava o histórico
  em SQLite, transforma cada alarme em evento (início/fim) e empurra os valores
  ao vivo por WebSocket. Reconecta sozinho se o Wi-Fi/4G cair.
- **Frontend** (`frontend/`, React + Vite + TypeScript + Recharts): os dashboards.
  Em produção, é compilado e servido pelo próprio backend (uma porta só).

O estado da máquina é estimado: **Em alarme** se houver alarme ativo,
**Produzindo** se o contador de peças subiu recentemente, **Parada** caso
contrário, e **Sem conexão** se o gateway não responder.

## Rodar

Pré-requisitos: Python 3.11+ e Node.js 20+. O PC precisa alcançar o gateway
(mesma rede, ou rota/túnel até ele).

```bash
git clone <este repositório>
cd monitor-injetoras
cp .env.example .env      # ajuste OPCUA_URL e MACHINE_NAME
./run.sh                  # abre em http://localhost:8000
```

Sem gateway por perto? Rode com dados simulados e 8 h de histórico fictício:

```bash
./run.sh --demo
```

### Desenvolvimento

```bash
# backend
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
SIMULATE=1 .venv/bin/uvicorn app.main:app --reload --port 8000

# frontend (outro terminal) — abre em http://localhost:5173
cd frontend && npm install && npm run dev
```

Testes (incluem um servidor OPC-UA falso que imita o gateway):

```bash
cd backend && .venv/bin/python -m pytest -q
```

## Configuração (`.env`)

| Variável | Padrão | Para quê |
|---|---|---|
| `OPCUA_URL` | `opc.tcp://10.202.148.102:4840` | Endereço do OPC-UA do gateway |
| `MACHINE_NAME` | `Injetora Chen Hsong (bancada)` | Nome no topo do painel |
| `POLL_SECONDS` | `5` | Intervalo de leitura de KPIs e alarmes |
| `FULL_POLL_SECONDS` | `60` | Intervalo de leitura completa |
| `SIMULATE` | `0` | `1` usa o simulador no lugar do gateway |
| `DB_PATH` | `data/monitor.db` | Arquivo do histórico |
| `RETENTION_DAYS` | `7` | Dias de histórico guardados |

## API

| Rota | Retorno |
|---|---|
| `GET /api/info` | Nome da máquina, endereço, zonas do canhão, rótulos |
| `GET /api/live` | Estado + valores atuais dos KPIs |
| `GET /api/history?tags=A,B&minutes=60` | Séries no tempo (reduzidas para ~300 pontos) |
| `GET /api/production?hours=8` | Peças e refugo por intervalo, ciclo médio |
| `GET /api/alarms?hours=24` | Alarmes ativos e eventos |
| `GET /api/values?tags=A,B` | Valores atuais de tags específicas (usado pelo Painel da máquina) |
| `GET /api/tags?q=temp` | Todas as variáveis com valor atual |
| `WS /ws` | Valores ao vivo a cada leitura |

## Observações sobre os dados

- As variáveis ficam em `ns=1;s=<Nome>` no gateway (ex.: `ns=1;s=TempZ1Now`).
- Unidades e significados (s, bar, mm, °C) foram deduzidos pelos nomes.
  Confirme com a Chen Hsong antes de usar para decisões.
- `Temp*Now` é a temperatura real, `TempZ*` o setpoint e `MinTemp*`/`MaxTemp*`
  a tolerância abaixo/acima.
- Na bancada, sem a máquina produzindo, os valores reais ficam em zero. É
  normal: os gráficos se preenchem quando a injetora roda.
- A primeira conexão mapeia todas as variáveis e guarda a lista em
  `data/tags_cache.json`; apague esse arquivo se o gateway mudar.

## Próximos passos

- Várias máquinas (um coletor por gateway) e tela de frota.
- Acesso remoto pelo túnel (WireGuard/Teltonika) até o gateway.
- Exportar CSV/Excel por turno.
