# Tela ao vivo da Beckhoff (fase 2) — design

Data: 2026-11-13 · Base: `main` (seletor de máquinas já integrado) · Branch: `tela-beckhoff`

## Por quê
O seletor de máquinas trouxe os dados da Beckhoff (ADS), mas a tela ao vivo ficou de fora: o
painel Windows CE só fala o protocolo fechado do CERHost (porta 987), que não roda no navegador
e não tem documentação pública. O Higor quer ver e, em contingência com alguém no local, operar
a tela da Beckhoff pela mesma página "Tela ao vivo" da B&R.

## Como (decidido, comprovado em bancada 13/11)
O servidor do Monitor roda o próprio `CERHOST.exe` e o transforma em VNC:

```
Beckhoff :987 ── CERHOST.exe (Wine 32) em tela virtual Xvfb + Openbox ── x11vnc (127.0.0.1) ── VncHub ── navegador
```

Teste manual na bancada (13/11), passo a passo, todos OK:
- Xvfb `:N -screen 0 800x1000x24` (a tela do painel é em pé: ~600×850 com a barra do CERHost;
  com 768 de altura sobram barras de rolagem).
- Openbox na tela virtual — sem gerenciador de janelas o Wine não recebe foco (menus não abrem
  pelo mouse) e ignora o redimensionamento.
- CERHost **não aceita IP por linha de comando**. Conexão automática por teclado (xdotool):
  janela "Remote Display Control for Windows CE" → foco → `alt+f` → `Return` (Connect) →
  diálogo com Hostname já selecionado → digitar o IP → `Return`. Senha do CERHost vazia.
- Conectado, a janela passa a se chamar o nome do painel (ex.: `CP-72B8D3`); com
  `wmctrl -b add,maximized_vert,maximized_horz` a tela cabe inteira.
- x11vnc sobre o display serve VNC RFB 3.8; com controle, mouse/teclado/menus funcionam.

## Regras
- Só leitura de dados continua igual; a tela segue as regras do hub: senha de visualização e
  senha de operação, um operador por vez, auditoria com o nome da máquina. Por lei, operar só
  em contingência com alguém no local.
- O robô (xdotool) só digita **antes** de conectar (menu/diálogo de conexão). Depois de
  conectado, nada é enviado à tela a não ser o que o operador fizer pelo hub.
- x11vnc só em `127.0.0.1`, porta escolhida pelo servidor, com as senhas da máquina
  (operação = senha completa; visualização = `viewpasswd`), passadas por arquivo apagado após
  a leitura (nunca na linha de comando). Senhas VNC valem até 8 caracteres.
- A ponte só existe enquanto a Beckhoff é a máquina aberta; ao trocar, tudo é encerrado
  (CERHost, x11vnc, Openbox, Xvfb). Se o CERHost cair ou desconectar (título volta a
  "Remote Display Control…"), a ponte reconecta sozinha.
- Faltando alguma ferramenta no servidor (Wine 32, Xvfb, x11vnc, xdotool, openbox, wmctrl ou
  o `CERHOST.exe`), a Tela ao vivo mostra o que falta; dados ADS continuam funcionando.

## Configuração nova
- `CERHOST_EXE` — caminho do `CERHOST.exe` (sem padrão; vazio = tela Beckhoff desligada com aviso).
- `CERHOST_DISPLAY` — número do display virtual (padrão 99). `WINEPREFIX`/`WINEARCH=win32` herdados.

## Fora do escopo
Várias Beckhoff abertas ao mesmo tempo; tela da CHY; reimplementar o protocolo CERHost.
