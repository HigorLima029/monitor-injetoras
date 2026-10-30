# Coletor OPC-UA (somente leitura)

Programa de uma tela para tirar os dados OPC-UA de uma injetora e salvar em CSV.
Funciona com o gateway Mega CHL3200 (CHY, variáveis `ns=1;s=...`) e com painéis
B&R (Euromap 63, `ns=6;s=::EM63Mapp:...`). **Nunca escreve na máquina.**

## Como usar (Windows)

1. Baixe `ColetorOPCUA.exe` em *Actions → Coletor OPC-UA (Windows .exe)* (artefato
   `ColetorOPCUA-windows`). Não precisa instalar nada.
2. O PC precisa estar na mesma rede do gateway (ou ter rota até ele).
3. Abra o programa, ajuste o endereço (padrão `opc.tcp://192.168.0.8:4840`) e
   clique em **Testar conexão**.
4. **Exportar variáveis (CSV)**: uma linha por variável, com NodeId, caminho, tipo
   e valor atual.
5. **Gravar valores (CSV)**: uma linha a cada intervalo, uma coluna por variável,
   durante o tempo escolhido. Use o filtro (ex.: `Temp,Production`) para gravar só
   algumas.

Os arquivos ficam na pasta `coletas`, ao lado do `.exe`. O CSV usa `;` e abre
direto no Excel.

## Linha de comando

```
python coletor_opcua.py --url opc.tcp://192.168.0.8:4840 --exportar
python coletor_opcua.py --url opc.tcp://192.168.0.8:4840 --gravar 60 --intervalo 5 --filtro Temp
```
