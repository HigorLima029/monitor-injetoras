# Seletor de máquinas — design

Data: 2026-11-13 · Branch base: `vnc-hub`

## Por quê

Hoje o Monitor atende **uma máquina**, fixada no `.env` (`OPCUA_URL`, `MACHINE_PROFILE`,
`VNC_*`). Trocar de máquina exige editar o arquivo e reiniciar. Com o RUT901 + ZeroTier
já dá para alcançar mais de um painel (B&R JM228 hoje, Beckhoff XMold em seguida), e a
ideia de longo prazo é: cada **cliente** tem várias máquinas, cada uma atrás de um gateway,
e nós entramos no sistema, vemos a lista (online/offline/standby), escolhemos uma e
visualizamos ou operamos a tela, com duas senhas.

Agora queremos o primeiro passo disso: **cadastrar e escolher a máquina dentro do próprio
Monitor, sem editar código nem `.env`**, deixando o caminho aberto para clientes, login e
banco na nuvem depois.

## Objetivos

- Tela **Máquinas**: cadastrar, editar e remover máquinas pelo navegador.
- Lista com estado de cada máquina (**online / offline**; a ativa mostra também rodando / standby / parada).
- **Selecionar** uma máquina: o Monitor desconecta da atual e conecta na escolhida
  (dados, alarmes, histórico e tela ao vivo), sem reiniciar o servidor.
- Três tipos de máquina:
  - `chy` — CHY lida pelo gateway CHL3200 (OPC-UA, perfil atual "chy");
  - `em63` — painel B&R Euromap 63 (OPC-UA + VNC, perfil atual "em63");
  - `ads` — Beckhoff XMold/TwinCAT 2 (ADS via pyads; só dados na fase 1).
- Cada máquina guarda o campo **cliente** (texto), já pensando no cadastro por cliente.

## Fora do escopo (agora)

- Login de administrador e usuários (fica para depois; o controle da tela já exige a senha de operação).
- Várias máquinas conectadas ao mesmo tempo (abordagem B, ver abaixo).
- Cadastro de clientes como entidade própria, banco na nuvem, integração iChen Cloud.
- Tela ao vivo da Beckhoff (é a **fase 2**, abaixo).
- Configurar o RUT901/ZeroTier pelo Monitor (rota e túnel continuam montados à mão).

## Anti-objetivos (seria falha mesmo funcionando)

- **Escrever em qualquer nó do PLC.** Continua tudo só leitura (OPC-UA e ADS). Nenhuma
  chamada `write` em lugar nenhum.
- **Senhas de painel indo para o navegador.** Hoje elas ficam só no servidor; tem que continuar assim.
- Trocar de máquina misturar histórico de uma com a outra.
- Deixar conexão "pendurada" na máquina anterior ao trocar (gasta 4G e pode travar o painel).

## Restrições

- Backend FastAPI + asyncua, frontend React/Vite/TS, histórico em SQLite — manter a stack.
- Rede: túnel ZeroTier + 4G (latência 50–300 ms, quedas silenciosas). O coletor já tem
  timeout de leitura e detecção de dados velhos (`read_timeout`, `stale`); manter.
- Beckhoff: ADS exige rota cadastrada no painel; pelo RUT com NAT o painel vê as conexões
  vindo do IP LAN do RUT (ex.: `10.86.20.1`), então a rota ADS aponta para esse IP.
  O `pyads` tem uma porta/roteador AMS global por processo — tudo bem com uma máquina ativa por vez.
- CERHost (tela Beckhoff) é protocolo fechado, sem documentação pública: não reimplementar.

## Abordagens consideradas

- **A) Cadastro + uma máquina ativa por vez** — escolhida. Simples, pouco tráfego no 4G,
  histórico separado por máquina. Contra: só a máquina aberta registra histórico, e quem
  troca a máquina troca para todo mundo que estiver com o Monitor aberto.
- **B) Todas conectadas ao mesmo tempo** — histórico de todas e standby real na lista, mas
  muito mais tráfego e pontos de falha. É o futuro "painel de frota"; a organização interna
  de A deve permitir chegar nela sem refazer.
- **C) Um Monitor por máquina + página inicial** — descartada: não escala para clientes.

Escolha do Higor: **A**.

## Como funciona (decidido)

### Cadastro

Tabela `machines` num SQLite próprio de configuração (`data/monitor_config.db`):

| campo | uso |
|---|---|
| `id` | inteiro, chave |
| `client` | texto livre, padrão "Chen Hsong" |
| `name` | nome mostrado no topo |
| `kind` | `chy` \| `em63` \| `ads` \| `demo` |
| `host` | IP do painel ou gateway (ex.: `10.86.20.253`) |
| `opcua_port`, `namespace` | OPC-UA (`chy`/`em63`); padrões 4840 e os de hoje |
| `ams_net_id`, `ads_port` | ADS (`ads`); porta padrão 801 |
| `vnc_enabled`, `vnc_port` | tela ao vivo; padrão ligado em `em63`, 5900 |
| `vnc_view_password`, `vnc_control_password` | só no servidor, nunca retornadas pela API |
| `notes` | texto livre (ex.: "RUT901 serial X, ZeroTier 10.114.222.171") |

Mais uma linha de estado: **máquina ativa** (`settings` key/value), para reabrir a mesma
ao reiniciar o servidor.

**Migração:** na primeira subida, se a tabela estiver vazia, cria uma máquina a partir do
`.env` atual (`OPCUA_URL`, `MACHINE_PROFILE`, `MACHINE_NAME`, `VNC_*`) e a deixa ativa.
Com `SIMULATE=1`, cria uma máquina `demo`. O `.env` passa a guardar só o que é do servidor
(intervalos de leitura, retenção, porta VNC TCP, pasta de dados).

### Troca de máquina

Um `MachineManager` no backend é dono do coletor e do hub VNC ativos:

1. `POST /api/machines/{id}/activate`
2. para o coletor atual e fecha o hub VNC (derruba os espectadores com motivo "máquina trocada");
3. monta a fonte certa (`OpcUaSource`, `Em63Source` ou a nova `AdsSource`), um `Storage`
   próprio da máquina (`data/machines/<id>.db`, cache de tags ao lado) e o hub VNC se houver;
4. inicia; grava como ativa; avisa os WebSockets `/ws` com uma mensagem `machine_changed`
   e os fecha — o frontend já reconecta sozinho.

Todos os endpoints atuais (`/api/live`, `/api/history`, `/api/alarms`, `/ws/vnc`, …)
continuam iguais, só que passam a falar com a máquina ativa do manager em vez de
`app.state.collector` fixo. `/api/info` passa a vir da máquina ativa.

### Estado na lista

Uma tarefa testa todas as máquinas a cada 30 s com **conexão TCP simples** (sem protocolo)
na porta principal (4840 para OPC-UA, 48898 para ADS), timeout 3 s: online/offline +
latência. A máquina ativa usa o estado do coletor (rodando/standby/parada/sem conexão).

### Fonte ADS (Beckhoff, fase 1)

`AdsSource` com a mesma interface `Source` (`connect/close/list_tags/read`), traduzindo o
que já foi decifrado para as tags que o Monitor entende:
- `.CYCLETIME_MQTT` → tempo de ciclo; `.NPARTSCNT_MQTT`, `.NGOODPARTS`, `.NBADPARTS` → contadores;
- `.AAAPFWTEMPPPARAMFROMHMI` (112 bytes/zona) → setpoints das zonas;
- `.STHMIMESSAGE` (96 bytes/linha, ID no byte 0) + `.BALARMACTIVE` → alarmes, com a tabela ID→texto do kit.
Temperatura real, fases, pressão: ficam vazias até serem decifradas com a máquina quente.
`pyads` roda em thread (`asyncio.to_thread`) para não travar o loop.

### API nova

- `GET /api/machines` — lista com estado (sem senhas; só `has_view_password`/`has_control_password`).
- `POST /api/machines`, `PUT /api/machines/{id}`, `DELETE /api/machines/{id}` — senha vazia no
  `PUT` = manter a atual. Não dá para apagar a ativa.
- `POST /api/machines/{id}/activate`.
- `POST /api/machines/{id}/test` — teste de conexão na hora (TCP), para o botão "Testar".

### Frontend

- Item novo no menu: **Máquinas** (primeiro item).
- Lista agrupada por cliente: nome, tipo, IP, bolinha online/offline + latência, botão
  **Abrir** (ativa e vai para Visão geral) e **Editar**.
- Formulário de cadastro com campos que mudam conforme o tipo; senhas como campos de
  senha "preencher para trocar".
- Barra do topo mostra a máquina ativa com um seletor rápido para trocar sem ir à lista.
- Menus que dependem do tipo continuam como hoje (Painel da máquina só CHY, Tela ao vivo só com VNC).

## Fase 2 — tela da Beckhoff no navegador

Para máquinas `ads` com tela ligada: o servidor sobe um display virtual (Xvfb), roda o
`CERHOST.exe` no Wine conectado ao painel (porta 987) e expõe esse display com `x11vnc`
numa porta local; o hub VNC atual aponta para essa porta. Mesma página "Tela ao vivo",
mesmas duas senhas, um operador por vez. Exige Wine 32 bits + Xvfb + x11vnc no servidor.
Ctrl (teclas físicas do painel) enviado pelo noVNC.

## Testes

- Unitários: CRUD, migração do `.env`, senha nunca aparece nas respostas, troca A→B fecha
  o coletor e hub antigos (com os fakes existentes `fake_br.py`/`fake_gateway.py`/`fake_vnc.py`),
  histórico separado por máquina, `AdsSource` com um fake de `pyads`.
- Manual: B&R pelo ZeroTier, troca para Beckhoff e de volta.

## Deixado para quem constrói (decisões menores)

- Nomes exatos de arquivos/classes e layout do formulário.
- Detalhes visuais da lista (seguir o estilo atual do Monitor).

## Chamadas que eu fiz (Claude) — conferir

1. Senhas de painel guardadas **em texto no SQLite local**, como hoje no `.env`, nunca
   enviadas ao navegador. Criptografia fica para quando houver servidor/nuvem.
2. Trocar de máquina troca **para todos** que estão com o Monitor aberto (consequência de A);
   mostramos um aviso "máquina trocada".
3. Histórico de cada máquina em arquivo SQLite separado (`data/machines/<id>.db`); o atual
   `data/monitor.db` vira o histórico da máquina migrada do `.env`.
4. Beckhoff na fase 1 com o que já foi decifrado (KPIs, setpoints, alarmes).
5. A **rota ADS** no painel Beckhoff continua sendo cadastrada uma vez pelo kit (`01_rota.py`);
   o Monitor não faz isso sozinho.
6. A porta VNC TCP do Espelho IHM (5901) **segue a máquina ativa**.
7. Duas trocas de máquina ao mesmo tempo: a segunda espera a primeira terminar (trava simples).
