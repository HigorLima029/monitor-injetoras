# Base: login, perfis, clientes e todas as máquinas conectadas — design

Data: 2026-11-18 · Branch base: `main`

## Por quê

O Monitor hoje não tem login, escuta em `0.0.0.0:8000` (qualquer um no Wi-Fi da empresa abre) e
tem **uma máquina aberta para todos**: quem troca de máquina troca para todo mundo. Isso serviu
para a bancada, mas não serve para o que vem a seguir:

- a visão do gestor separa **cliente** (só olha a máquina dele) e **técnico** (dá suporte e, só em
  caso importante, assume a tela);
- os próximos passos — **alertas (SMS etc.), relatórios de produtividade e a IA** — precisam de
  dados de **todas as máquinas o tempo todo**, mesmo quando ninguém está olhando;
- a parte legal exige saber **quem** viu e **quem** mexeu, e garantir que o controle remoto só
  aconteça com alguém no local.

Este é o passo 1 ("base") do plano: sem ele, nenhum dos outros passos é seguro nem possível.

## Objetivos

1. **Login** obrigatório para tudo (páginas, API, WebSockets, tela ao vivo).
2. **Três perfis:**
   - **Conta mãe** (Higor): tudo — clientes, máquinas, usuários, técnicos, registros.
   - **Técnico**: vê as máquinas dos clientes que atende (na fase de teste: todos), filtra por
     cliente, tem "ver como o cliente" (só leitura) e pode **assumir o controle da tela** quando o
     cliente autorizou.
   - **Cliente**: vê só as máquinas do cliente dele (dados, histórico, painel e tela ao vivo
     **só visualização**). Nunca controla. É quem **autoriza o suporte**.
3. **Clientes** como cadastro próprio; cada máquina pertence a um cliente ou está
   **"em preparação"** (sem cliente: só conta mãe e técnicos veem).
4. **Todas as máquinas conectadas sempre** (dados), com intervalo de leitura de **10 s** quando
   ninguém olha e intervalo rápido quando alguém está com ela aberta. Histórico contínuo de cada uma.
5. **Cada pessoa olha a sua máquina** (a máquina está na URL/sessão de quem olha); ninguém troca a
   tela de ninguém.
6. **Tela ao vivo sob demanda, por máquina** — B&R direto no VNC do painel; cada Beckhoff com a sua
   própria tela virtual do CERHost. Várias máquinas com tela aberta ao mesmo tempo.
7. **Autorização de suporte:** um usuário cliente clica "Autorizar suporte por 30 min" numa máquina;
   enquanto valer, técnicos podem assumir o controle daquela máquina. O cliente pode encerrar antes.
8. **Assumir o controle exige confirmar a própria senha** (ou o código do 2º fator, se o usuário
   tiver) no momento — mesmo estando logado.
9. **Registro (auditoria)** de: login/logout/falha de login, abrir tela ao vivo, autorizar e
   encerrar suporte, assumir/liberar controle (e liberação automática), cadastros e mudanças feitas
   pela conta mãe. A conta mãe consulta e filtra (pessoa, cliente, máquina, período).
10. **Conta mãe protegida:** senha forte + **2º fator (TOTP, app no celular) obrigatório**; não
    pode ser apagada nem rebaixada pela tela; recuperação só por **comando no servidor**.
11. **HTTPS** com certificado próprio (uso interno).

## Fluxo de cadastro (decidido)

1. Conta mãe cria o **cliente**.
2. Conta mãe cadastra a **máquina** (tipo, IP, porta/namespace ou AMS Net ID, senhas da tela),
   testa e vê os dados — a máquina fica **em preparação**.
3. Conta mãe **vincula** a máquina ao cliente → passa a aparecer para os usuários daquele cliente.
4. Conta mãe cria os **usuários** (clientes e técnicos).

Mover uma máquina para outro cliente leva o histórico junto (o histórico é da máquina).
Na fase 1, **só a conta mãe** cadastra/vincula máquinas e cria usuários.

## Fora do escopo (agora)

- Alertas/SMS, relatórios, OEE, IA, integração Auvo, vídeos e manuais (passos seguintes).
- Servidor na nuvem, domínio e certificado público (o desenho não pode impedir; é instalação).
- Divisão de técnicos por região/cliente (estrutura preparada; na fase de teste todos atendem todos).
- "Esqueci a senha" por e-mail; cadastro aberto; login com Google/Microsoft.
- Bloquear botões dentro da tela espelhada (não é confiável por VNC — a proteção é o processo:
  autorização do cliente + confirmação + registro).
- Gravação em vídeo das sessões de controle.

## Anti-objetivos (falha mesmo funcionando)

- **Escrever em qualquer nó do PLC.** Continua só leitura (OPC-UA/ADS). O teste
  `test_codigo_nao_escreve_na_maquina` continua valendo.
- **Cliente conseguir controlar a tela** por qualquer caminho (inclusive chamando a API/WebSocket
  na mão). A regra vale **no servidor**, não só escondendo botão.
- **Cliente ver dados de conexão** (IP, AMS Net ID, porta, notas, senhas) ou máquinas/registros de
  outro cliente — inclusive por ID na URL, WebSocket ou histórico.
- Senhas (de usuários ou de painéis) indo para o navegador, log, linha de comando ou resposta HTTP.
- Uma máquina offline/lenta (4G) travar as outras ou a página.
- Perder a conta mãe sem caminho de volta.

## Restrições

- Stack atual: FastAPI + asyncua + pyads, React/Vite/TS, SQLite. Manter.
- Rede: ZeroTier + 4G (50–700 ms, quedas). Dados são leves; o que pesa é a tela ao vivo.
- `pyads`: uma porta AMS local por processo; várias conexões a PLCs diferentes ao mesmo tempo
  são suportadas, mas precisa ser verificado na bancada (XMold + CBmold juntas).
- CERHost: protocolo fechado; uma tela virtual (Xvfb + openbox + Wine + x11vnc) **por máquina
  Beckhoff com tela aberta**, desligada alguns minutos depois que todos saem.
- Uso interno: servidor = PC do Higor ou PC da empresa; acesso pela rede/ZeroTier.
- Textos em português, tom atual do Monitor.

## Abordagens consideradas

- **A) Conectar sob demanda** (só quando alguém abre) — pouco 4G, mas sem histórico contínuo,
  alertas nem IA. Descartada.
- **B) Todas as máquinas sempre conectadas (dados) + tela ao vivo sob demanda** — **escolhida pelo
  Higor**: histórico contínuo, base dos alertas/relatórios/IA; 4G controlado pelo intervalo de 10 s.
- **C) Um Monitor por cliente** — descartada (não escala).

Login: **dentro do próprio Monitor** (usuários no banco). Keycloak/Authentik descartados agora
(mais um servidor para manter); o desenho deve permitir trocar depois.

Suporte do técnico: **conta própria com filtro por cliente** (escolhido) em vez de "entrar na conta
do cliente" (rastreabilidade ruim). "Ver como o cliente" existe, só leitura.

Senhas de painel: **não há senha de painel por pessoa**. As senhas do VNC/CERHost ficam no cadastro
da máquina e só o servidor as usa; o acesso é decidido pelo login + perfil + autorização.

## Como funciona (decidido)

### Usuários e sessão
- Tabela de **usuários**: nome, login (e-mail ou usuário), perfil (`mae` | `tecnico` | `cliente`),
  cliente (só para perfil cliente), ativo/inativo, senha (hash forte, nunca texto), segredo do 2º
  fator (opcional; obrigatório na conta mãe).
- **Sessão por cookie** `HttpOnly`, `Secure`, `SameSite=Strict`, que expira por inatividade e tem
  validade máxima; logout derruba a sessão. Desativar um usuário derruba as sessões dele
  (inclusive WebSockets e tela ao vivo abertos).
- Limite de tentativas de login (por usuário e por IP) com espera crescente.
- **Primeira subida:** se não há conta mãe, o servidor **não** cria uma com senha padrão; a conta
  mãe é criada por **comando no servidor** (mesmo comando serve para recuperar/resetar senha e 2º
  fator).

### Clientes e máquinas
- Tabela de **clientes**. O campo de texto `client` das máquinas vira vínculo com o cadastro
  (migração: cada texto distinto vira um cliente; "Chen Hsong" incluso). Máquina sem cliente =
  em preparação.
- A **lista/cadastro** de máquinas responde conforme o perfil: conta mãe vê tudo (inclusive dados de
  conexão, sem as senhas, como hoje); técnico vê as máquinas e os dados de conexão; cliente vê, no
  cadastro, só nome, modelo, tipo e estado das máquinas dele.
- **Dados da máquina** (`/api/live`, `/api/history`, `/api/alarms`, `/api/values`, `/ws`, painel):
  o cliente vê **todas as tags e alarmes** das máquinas dele, igual ao técnico. O filtro é só
  **por máquina**; o que se esconde do cliente são os dados de conexão (IP, AMS Net ID, porta,
  namespace, notas, senhas), que não fazem parte dessas respostas.

### Todas as máquinas conectadas
- O "gerente" passa de **uma máquina ativa** para **um coletor por máquina**, todos rodando.
  Cada um com seu `Storage` (já é por máquina: `data/machines/<id>.db`).
- Intervalo de leitura **10 s** quando ninguém olha; **rápido** (configurável, meta 1–2 s) enquanto
  houver alguém com a máquina aberta; volta a 10 s pouco depois que todos saem.
- Máquina offline não bloqueia nenhuma outra (cada coletor isolado, com os limites de tempo atuais).
- O probe TCP da lista continua para máquinas em preparação/sem coletor; para as conectadas, o
  estado vem do coletor (rodando/standby/parada/alarme/sem conexão).
- A "máquina ativa" global, o `POST /activate` e o aviso "máquina trocada" deixam de existir; as
  rotas de dados (`/api/live`, `/api/history`, `/api/alarms`, `/ws`, `/ws/vnc`, …) passam a
  receber **qual máquina** e checam se o usuário pode vê-la.

### Tela ao vivo por máquina
- Um hub VNC **por máquina com tela aberta**, criado sob demanda e encerrado depois que todos saem.
- B&R: hub direto no VNC do painel (como hoje). Beckhoff: ponte CERHost própria (display Xvfb
  distinto por máquina) + hub.
- O handshake de senha do VNC entre navegador e hub deixa de usar as senhas do painel: o acesso é
  pela sessão do login; o hub sabe o perfil do usuário. Cliente entra sempre em visualização.
- **Assumir o controle** (`POST` dedicado): exige perfil técnico ou conta mãe **e** autorização
  ativa do cliente para aquela máquina **e** confirmação da senha (ou código) do próprio usuário
  naquele momento. Um operador por vez; volta a visualização sozinho por inatividade (como hoje)
  e **quando a autorização acaba** ou é encerrada pelo cliente.
- Conta mãe também precisa da autorização do cliente para controlar máquina **vinculada** a
  cliente. Máquina **em preparação** (sem cliente): conta mãe e técnico controlam só com a
  confirmação de senha (é a bancada).

### Autorização de suporte (decidido pelo Higor)
- **Caminho normal:** qualquer usuário ativo do cliente clica "Autorizar suporte por 30 min" (por
  máquina) e pode "Encerrar suporte" — inclusive uma autorização dada por um colega.
- **Último caso, pela conta mãe:** a conta mãe pode registrar a autorização **em nome do cliente**,
  com **motivo obrigatório** (ex.: "cliente pediu por telefone, falei com Maria às 14:02"). Fica
  **destacada** na auditoria e **aparece para os usuários do cliente** na tela da máquina
  ("Suporte autorizado pela Chen Hsong em seu nome — motivo: ...") e na lista de autorizações.
  Técnico não tem essa opção.
- Enquanto ativa, aparece para o cliente e para os técnicos ("Suporte autorizado até 14:35 por
  Maria"). Toda autorização (normal ou em nome do cliente) fica registrada para nos proteger.
- Futuro (passo da IA): o cliente pede suporte pelo próprio sistema (primeiro a IA, depois um
  técnico); a autorização fica ligada a esse pedido. Nesta fase existe só o botão.

### Senhas VNC dos painéis B&R
- O hub já funciona com painel **sem senha** VNC (verificado no código: aceita segurança "None").
- Máquinas novas: senha VNC configurada **na fábrica**, **única por máquina** (não uma senha comum
  a todas), guardada só no cadastro do Monitor. O cliente não recebe a senha — se soubesse, poderia
  controlar o painel com qualquer programa VNC, sem passar pelas regras do Monitor.
- Máquinas já em clientes sem senha: o Monitor mostra para a conta mãe/técnicos um aviso
  **"painel sem senha VNC"** na máquina. A senha é colocada na visita de instalação do RUT (que já
  é presencial), com o cliente ciente. Até lá, a proteção é a rede: o painel fica só na rede do RUT.
- Fora do código, entra no **roteiro de instalação**: firewall do RUT liberando o ZeroTier só do
  servidor do Monitor para as portas necessárias do painel (5900/987/48898/4840).

### Registro (auditoria)
- Tabela única de eventos: quando, quem (usuário + perfil + cliente), o quê, máquina, IP de
  origem, detalhe. Só a conta mãe consulta (com filtros). Retenção longa (não apagar junto com o
  histórico de 7 dias das tags).

### Frontend
- Tela de **login** (e de código do 2º fator quando houver).
- Lista de máquinas por perfil: técnico/conta mãe com **filtro por cliente** e indicação de alarme;
  cliente vê só as dele.
- A máquina aberta fica **na URL** (`#/maquina/<id>/...`), cada aba/pessoa a sua.
- Páginas novas para a conta mãe: **Clientes**, **Usuários**, **Registros**.
- Tela ao vivo: cliente sem nenhum controle; botão de autorizar suporte; técnico com "Assumir
  controle" (pede a senha) habilitado só com autorização ativa.
- "Ver como o cliente" para técnico/conta mãe: mostra a máquina como o cliente vê (sem dados de
  conexão, sem controle).

### HTTPS
- Uvicorn com certificado próprio (gerado por comando/README), redirecionando HTTP→HTTPS ou
  escutando só em HTTPS. Cookie `Secure` exige isso.

### Onde ficam os dados
- Usuários, clientes, autorizações, sessões e auditoria vão para o banco de configuração que já
  existe (`data/monitor_config.db`, do `MachineStore`), com migração no próprio arquivo. O histórico
  de tags continua em `data/machines/<id>.db`.

### Servir a página
- Como hoje: o `run.sh` serve o build do frontend pelo próprio Uvicorn; o Vite dev é só para
  desenvolvimento. O HTTPS fica no Uvicorn.

## Testes

- Unitários/API: cada rota e WebSocket com os três perfis (pode / não pode), inclusive cliente
  tentando ID de máquina de outro cliente; cliente tentando assumir controle por API/WebSocket;
  controle sem autorização, com autorização expirada, sem confirmar senha; desativar usuário
  derruba sessão; nenhuma senha em respostas; migração do `client` texto → clientes; vários
  coletores ao mesmo tempo com uma fonte travada não afetando as outras; intervalo 10 s ↔ rápido.
- Bancada: B&R + XMold + CBmold + CHY conectadas juntas; dois navegadores (técnico e cliente de
  teste) olhando máquinas diferentes ao mesmo tempo; duas Beckhoffs com tela aberta juntas;
  fluxo completo de autorização e controle; registro conferido.

## Deixado para quem constrói

- Biblioteca de hash de senha e de TOTP, formato do token de sessão, nomes de tabelas/rotas.
- Tempo exato de expiração da sessão e de desligar a tela virtual ociosa.
- Layout das páginas novas, seguindo o estilo atual.

## Chamadas que eu fiz (Claude) — conferir

1. **2º fator obrigatório só na conta mãe**; opcional para técnicos e clientes nesta fase.
2. **Conta mãe também precisa da autorização do cliente** para controlar máquina vinculada a
   cliente (a regra legal vale para todos); máquina em preparação dispensa autorização.
3. Técnico vê **dados de conexão** (IP etc.) das máquinas, mas não as senhas — para dar suporte de
   rede. Cliente nunca vê.
4. Registros só a conta mãe consulta nesta fase.
5. Login por **usuário/e-mail + senha**; sem "esqueci a senha" — a conta mãe redefine senhas de
   técnicos e clientes.
6. Intervalo rápido configurável (meta 1–2 s); o atual `POLL_SECONDS` é 5 s. Conta como
   "alguém olhando" qualquer página/WebSocket aberto daquela máquina (inclusive "ver como o
   cliente"); volta a 10 s 60 s depois que o último sai.
7. **Várias Beckhoffs juntas no `pyads`:** a primeira tarefa verifica na bancada (XMold + CBmold).
   Se não funcionar no mesmo processo, plano B: cada coletor ADS roda num subprocesso — a interface
   de fonte (`connect/close/list_tags/read`) não muda.
8. **Telas da Beckhoff ao mesmo tempo:** limite configurável (padrão 2). Ao atingir, a nova é
   recusada com mensagem clara ("limite de telas da Beckhoff abertas"), sem derrubar ninguém.
   Coletor ADS e CERHost na mesma máquina já convivem (bancada).
10. Ordem de recomendação para as senhas VNC: fábrica > visita de instalação; nunca pedir ao cliente
   para configurar.
9. **Confirmação de senha vale para uma tomada de controle**: se o controle volta a visualização
   (inatividade, fim da autorização ou liberação), assumir de novo pede confirmação de novo.
