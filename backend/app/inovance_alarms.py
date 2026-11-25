"""Nomes dos alarmes do painel ESTO (maquinas Spark com servo Inovance).

Fonte: pastas warnhelp (chines) e warnhelp_eng (ingles) do pacote de atualizacao do
painel "inova" (P15EHS3_M792-T), traduzidas para o portugues. A chave e o codigo que o
painel publica em Alarm1/Alarm2 no OPC-UA (ex.: 0x00100101 -> 0x100101).

"(nao usado)" segue a marcacao do fabricante: o codigo existe mas o painel nao o emite.
"""
from __future__ import annotations

INO_ALARMS: dict[int, str] = {
    # sistema / painel
    0x100101: "Solte o botão de emergência / manual",
    0x100102: "Pressione o botão de partida",
    0x100103: "Ciclo completo não terminou no tempo",
    0x100104: "Memória insuficiente",
    0x100105: "Lista de tarefas cheia",
    0x100106: "Lista de erros de inserção de ação cheia",
    0x100107: "Comando de tarefa de ação inválido (não usado)",
    0x100108: "Ação combinada sem tabela de prioridade de óleo (não usado)",
    0x100109: "Medidor de energia não conectado (não usado)",
    0x10010A: "Falha de comunicação com a placa de expansão de I/O",
    0x10010B: "Falha de comunicação com a placa de expansão DA",
    0x10010C: "Falha de comunicação com a placa de expansão de temperatura",
    0x10010E: "Dados do painel incompletos",
    # injeção e dosagem
    0x10100101: "Injeção incompleta ou excessiva",
    0x10100102: "Proteção da injeção aberta",
    0x10100103: "Dosagem não terminou no tempo",
    0x10100104: "Temperatura do cilindro baixa",
    0x10100105: "Parada por sobrepressão na injeção (não usado)",
    0x10100106: "Descompressão não terminou no tempo",
    0x10100107: "Tempo de espera após atingir a temperatura não cumprido (não usado)",
    0x10100108: "Dosagem atingiu o limite máximo",
    0x10100109: "Injeção chegou ao fim de curso",
    0x1010010A: "Descompressão chegou ao fim de curso",
    0x1010010B: "Pressão alta no cilindro antes da injeção",
    0x1010010C: "Pressão alta no cilindro antes da dosagem",
    0x1010010D: "Purga automática concluída",
    0x1010010E: "Pressione o botão de injeção para zerar o sensor de força",
    0x1010010F: "Zeragem do sensor de força concluída",
    0x10100110: "Torque alto no eixo de injeção, faça descompressão para aliviar",
    0x1010011B: "Pressão alta no cilindro antes da injeção",
    0x10100201: "Temperatura do cilindro alta",
    0x10100202: "Bico entupido",
    0x10100203: "Desvio na posição de transbordo",
    0x10100204: "Falha no alívio de pressão antes da dosagem",
    0x10100205: "Alimentação de material anormal",
    # carro de injeção
    0x11100101: "Falha no limite do carro de injeção",
    0x11100102: "Sobrepressão no avanço do carro",
    0x11100103: "Falha no avanço do carro",
    0x11100104: "Sobrecarga no motor do carro de injeção",
    0x11100105: "Carro chegou ao fim do avanço",
    0x11100106: "Carro chegou ao fim do recuo",
    0x11100107: "Alarme do inversor de frequência",
    0x11100108: "Contator do carro de injeção anormal",
    0x11100109: "Régua eletrônica do carro no ponto zero",
    0x1110010A: "Régua eletrônica do carro em 0 V",
    0x1110010B: "Régua eletrônica do carro em 10 V",
    0x1110010C: "Alarme de recuo do carro de injeção",
    0x1110010D: "Tempo esgotado no avanço do carro",
    0x1B100101: "Contagem de moldagens atingida na estatística de parâmetros",
    # fechamento / abertura
    0x20100101: "Fechamento não chegou à posição",
    0x20100102: "Tempo esgotado no fechamento",
    0x20100103: "Tempo esgotado no fechamento em baixa pressão",
    0x20100104: "Tempo esgotado no fechamento em alta pressão",
    0x20100105: "Abertura não chegou à posição",
    0x20100106: "Tempo esgotado na abertura",
    0x20100107: "Tempo esgotado na abertura lenta",
    0x20100108: "Segurança mecânica anormal",
    0x20100109: "Sobrepressão na força de fechamento",
    0x2010010A: "Erro no cálculo da distância de ajuste",
    0x2010010B: "Número de aberturas atingido",
    0x2010010C: "Quantidade por embalagem atingida",
    0x2010010D: "Abertura chegou ao fim de curso",
    0x2010010E: "Desvio na temperatura do molde",
    0x2010010F: "Falha na proteção de travamento do fechamento",
    0x20100110: "Fechamento chegou ao fim de curso",
    0x20100201: "Tampa superior anormal no fechamento",
    0x20100202: "Proteção de fechamento, aguarde",
    0x20100203: "Semiautomático: porta liberada após a abertura",
    0x20100204: "Automático: destravamento não permitido",
    0x20100205: "Semiautomático: porta liberada após a abertura",
    0x20100206: "Ajuste manual do molde não permitido, use o ajuste automático",
    0x20100207: "Fixação hidráulica do molde anormal",
    # extrator
    0x21100101: "Extrator não chegou à posição",
    0x21100102: "Modo do extrator incorreto",
    0x21100103: "Extrator chegou ao fim do avanço",
    0x21100104: "Extrator chegou ao fim do recuo",
    0x21100105: "Função de inserto: use semiautomático",
    0x21100106: "Função de inserto: use extrator com número fixo de vezes",
    0x21100107: "Aguardando sinal de liberação do inserto",
    # machos (A gerado à parte; B a F abaixo)
    0x22100101: "Macho A não chegou à posição",
    0x22100102: "Desvio de posição no movimento do macho A",
    0x22100103: "Aguardando robô liberar entrada do macho A",
    0x22100104: "Aguardando robô liberar saída do macho A",
    0x22100105: "Robô anormal: liberação de entrada do macho A",
    0x22100106: "Robô anormal: liberação de saída do macho A",
    # sopro
    0x23100101: "Aguardando fim do sopro",
    0x23100102: "Aguardando válvula sequencial (não usado)",
    # ajuste de molde e zeragem
    0x24100101: "Ajuste automático do molde concluído",
    0x24100102: "Ajuste do molde chegou ao fim do recuo",
    0x24100103: "Ajuste do molde chegou ao fim do avanço",
    0x24100104: "Ajustando o zero da injeção",
    0x24100105: "Ajustando o zero do extrator",
    0x24100106: "Ajustando o zero do fechamento",
    0x24100107: "Ajustando a força de fechamento",
    0x24100108: "Ajustando a espessura do molde",
    0x24100109: "Tempo esgotado na zeragem (não usado)",
    0x2410010A: "Definindo fim do avanço do ajuste (não usado)",
    0x2410010B: "Erro na zeragem da abertura/fechamento (não usado)",
    0x2410010C: "Motor de injeção não zerado",
    0x2410010D: "Motor do extrator não zerado",
    0x2410010E: "Motor de abertura/fechamento não zerado",
    0x2410010F: "Ajuste do molde anormal",
    0x24100110: "Ajuste do molde concluído",
    0x24100201: "Sobrecarga no motor de ajuste do molde",
    0x24100202: "Zeragem da injeção concluída",
    0x24100203: "Zeragem da abertura/fechamento concluída",
    0x24100204: "Zeragem do extrator concluída",
    0x24100205: "Ajuste da espessura do molde concluído",
    0x24100206: "Contator do ajuste de molde anormal",
    0x24100207: "Dados do encoder do ajuste de molde fora do limite",
    # portas de segurança
    0x29100101: "Porta de segurança aberta",
    0x29100102: "Abra a porta de segurança",
    0x29100103: "Porta de segurança traseira aberta",
    0x29100104: "Falha na porta de segurança",
    0x29100105: "Trava eletromagnética da porta anormal",
    0x29100106: "Porta de segurança 2 aberta (não usado)",
    0x29100107: "Proteção da porta de segurança (não usado)",
    0x29100108: "Detecção da porta de segurança anormal (não usado)",
    0x29100109: "Detecção da porta traseira anormal (não usado)",
    0x2910010A: "Segurança da porta automática anormal (não usado)",
    0x2910010B: "Chave da válvula da porta SQ42 anormal",
    0x2910010C: "Válvula de segurança anormal",
    0x2910010D: "Válvula piloto da válvula de segurança anormal",
    0x2910010E: "Pressione o botão de fechamento (não usado)",
    0x2910010F: "Falha no fechamento (não usado)",
    0x29100110: "Falha no controlador do motor da porta",
    0x29100201: "Trava eletromagnética liberada",
    # proteção de molde
    0x2A100109: "Desvio na detecção de temperatura 1 (não usado)",
    0x2A10010A: "Desvio na detecção de temperatura 2 (não usado)",
    0x2A10010B: "Falha na proteção do molde (não usado)",
    0x2A10010C: "Emergência da proteção do molde (não usado)",
    0x2A10010D: "Aguardando proteção do molde (não usado)",
    0x2A10010E: "Falha na proteção manual do molde",
    0x2A10010F: "Falha na proteção automática do molde",
    0x2A100110: "Proteção do molde: entrada de recuo da placa extratora anormal",
    0x2A100201: "Proteção do molde: entrada de liberação do robô anormal",
    0x2A100202: "Proteção do molde: entrada de alarme de proteção anormal",
    0x2A100203: "Proteção do molde: entrada de alarme de moldagem anormal",
    0x2A100204: "Aguardando proteção do molde: confirmação de recuo da placa extratora",
    0x2A100205: "Aguardando proteção do molde: confirmação de fim da abertura",
    0x2A100206: "Aguardando proteção do molde: recuo dos pinos",
    0x2A100207: "Aguardando proteção do molde: confirmação do inserto no lugar",
    0x2A100208: "Proteção do molde: entrada de inserto no lugar anormal",
    # lubrificação (bomba 1; 2 e 3 abaixo)
    0x30100101: "Nível de óleo da bomba de lubrificação anormal",
    0x30100102: "Falha na detecção de pressão da lubrificação",
    0x30100103: "Detecção de parada da lubrificação anormal",
    0x30100104: "Alívio de pressão da bomba de lubrificação anormal",
    # robô e produto
    0x31100101: "Falha do robô",
    0x31100102: "Emergência do robô",
    0x31100103: "Aguardando robô",
    0x31100104: "Monitoramento do molde anormal (não usado)",
    0x31100105: "Aguardando robô: avanço do extrator seguro",
    0x31100106: "Aguardando robô: recuo do extrator seguro",
    0x31100107: "Aguardando robô liberar o fechamento",
    0x31100108: "Aguardando robô liberar a abertura",
    0x31100109: "Aguardando início de ciclo do robô",
    0x3110010A: "Aguardando robô: área do molde segura",
    0x3110010B: "Aguardando conexão do robô",
    0x31100201: "Robô anormal: área do molde segura",
    0x31100202: "Robô anormal: liberação de abertura",
    0x31100203: "Robô anormal: liberação de fechamento",
    0x31100204: "Robô anormal: avanço do extrator seguro",
    0x31100205: "Robô anormal: recuo do extrator seguro",
    0x31100206: "Falha do robô: entrada sem mudança",
    0x32100101: "Falha na detecção da fotocélula",
    0x32100102: "Saída de peças cheia",
    # motores, servo e EtherCAT
    0x4100101: "Falha na partida do motor",
    0x4100102: "Sobrecarga do motor",
    0x4100103: "Motor não ligado",
    0x4100104: "Temperatura do motor muito alta",
    0x4100105: "Motor anormal",
    0x4100106: "EtherCAT: tempo esgotado no SDO, falha ao habilitar",
    0x4100107: "EtherCAT: tempo esgotado no SDO, falha ao desabilitar",
    0x4100108: "Sequência de fases da alimentação errada",
    0x4100109: "Sem dados de retorno",
    0x410010A: "Número de escravos EtherCAT não confere",
    0x410010B: "Configuração da rede EtherCAT errada",
    0x410010C: "Configuração do escravo errada",
    0x410010D: "Configuração do relógio dos escravos incompleta (não usado)",
    0x410010E: "Configurando relógio dos escravos (não usado)",
    0x410010F: "Aguardando partida dos servos",
    0x4100110: "Escravo EtherCAT desconectado",
    0x4100201: "Alarme de servo, verifique",
    0x4100202: "Alarme STO não pode ser apagado",
    0x4100203: "Sobrecarga no motor da bomba de óleo dos machos",
    0x4100204: "Tensão baixa no barramento do eixo de injeção",
    0x4100205: "Tensão baixa no barramento do eixo de abertura/fechamento",
    0x4100206: "Tensão baixa no barramento do eixo do extrator",
    0x4100207: "Tensão baixa no barramento do eixo de dosagem",
    0x4100208: "Tensão baixa no barramento do servo 5",
    0x4100209: "Tensão baixa no barramento do servo 6",
    0x410020A: "Servo não está pronto",
    0x410020C: "Alarme da bomba de óleo dos machos",
    0x410020D: "Falha na unidade retificadora",
    # temperatura
    0x5100101: "Tempo de pré-aquecimento não atingido",
    0x5100102: "Temperatura não está subindo",
    0x5100103: "Cancele o modo de manutenção de temperatura",
    0x5100104: "Contator do aquecimento anormal",
    0x5100105: "Temperatura alta na boca de alimentação",
    0x5100106: "Aquecimento desligado",
    0x5100107: "Temperatura alta no motor de injeção",
    0x5100108: "Temperatura alta no motor de abertura/fechamento",
    0x5100109: "Temperatura alta no motor do extrator",
    0x510010A: "Temperatura alta no motor de dosagem",
    0x510010B: "Temperatura alta no painel elétrico",
    0x510010C: "Otimização de temperatura em andamento",
    0x510010D: "Condições para otimização não atendidas",
    0x510010E: "Falha de comunicação com a placa de temperatura ambiente",
    0x510010F: "Setpoint de temperatura precisa ser maior que 150 °C",
    0x5100110: "Temperatura real muito alta",
    0x5100201: "Sem autoaprendizado de temperatura, aquecimento sincronizado inválido",
    0x5100202: "Falha na seleção do tipo de termopar (E502)",
    0x5100203: "Falha de comunicação com a placa E502",
    0x5100204: "Saída de controle de temperatura anormal",
    # I/O
    0x6100101: "Tensão H24V anormal ou fiação de saída anormal",
    0x6100102: "Fiação de saída anormal",
    0x6100103: "Entrada forçada ativa",
    # curso e encoders
    0x7100101: "Posição da injeção fora do curso mecânico",
    0x7100102: "Posição de abertura/fechamento fora do curso mecânico",
    0x7100103: "Posição do extrator fora do curso mecânico",
    0x7100104: "Dados de pulso do motor de injeção anormais",
    0x7100105: "Dados de pulso do motor da placa anormais",
    0x7100106: "Dados de pulso do motor do extrator anormais",
    0x7100107: "Dados de pulso do motor de dosagem anormais",
    0x7100108: "Sensor de força inválido (não usado)",
    0x7100109: "Desvio de posição do eixo de injeção",
    0x710010A: "Desvio de posição do eixo da placa",
    0x710010B: "Desvio de posição do eixo do extrator",
    # servo / zeragem
    0xA100102: "Modo do servo não confere com o controlador",
    0xA100103: "Fora do limite",
    0xA100104: "Pressione o botão de reset",
    0xA100105: "Direção do eixo de injeção errada, refaça a zeragem",
    0xA100106: "Direção do eixo de abertura/fechamento errada, refaça a zeragem",
    0xA100107: "Direção do eixo do extrator errada, refaça a zeragem",
    0xA100108: "Direção do eixo de dosagem errada, refaça a zeragem",
    0xA100109: "Servo do eixo de injeção trocado, refaça a zeragem",
    0xA10010A: "Servo do eixo de abertura/fechamento trocado, refaça a zeragem",
    0xA10010B: "Servo do eixo do extrator trocado, refaça a zeragem",
    0xA10010C: "Desvio grande no encoder da injeção após religar, refaça a zeragem",
    0xA10010D: "Desvio grande no encoder da abertura/fechamento após religar, refaça a zeragem",
    0xA10010E: "Desvio grande no encoder do extrator após religar, refaça a zeragem",
    0xA100201: "Iniciando a rede",
    0xA100202: "Reconfigurando o EtherCAT",
    # óleo
    0xC100101: "Falha na detecção do filtro de óleo",
    0xC100102: "Falha na detecção do nível de óleo",
    0xC100103: "Desvio na temperatura do óleo",
    # comunicação com os servos
    0xD100101: "Falha na leitura em lote de SDO",
    0xD100102: "Falha no envio de dados SDO",
    0xD100103: "Falha na leitura de dados SDO",
}

# Pressão das cavidades 1 a 8
for _n in range(1, 9):
    INO_ALARMS[0x2A100100 + _n] = f"Desvio de pressão na cavidade {_n}"

# Machos B a F (mesmos 6 alarmes do macho A)
for _i, _letra in enumerate("BCDEF", start=2):
    _base = 0x22000000 + (_i << 20) + 0x100
    INO_ALARMS[_base + 1] = f"Macho {_letra} não chegou à posição"
    INO_ALARMS[_base + 2] = f"Desvio de posição no movimento do macho {_letra}"
    INO_ALARMS[_base + 3] = f"Aguardando robô liberar entrada do macho {_letra}"
    INO_ALARMS[_base + 4] = f"Aguardando robô liberar saída do macho {_letra}"
    INO_ALARMS[_base + 5] = f"Robô anormal: liberação de entrada do macho {_letra}"
    INO_ALARMS[_base + 6] = f"Robô anormal: liberação de saída do macho {_letra}"

# Sopros 2 a 14 (0x232..0x23E)
for _n in range(2, 15):
    INO_ALARMS[0x23000000 + (_n << 20) + 0x101] = f"Aguardando fim do sopro {_n}"

# Bombas de lubrificação 2 e 3
for _n in (2, 3):
    _base = 0x30000000 + (_n << 20) + 0x100
    INO_ALARMS[_base + 1] = f"Nível de óleo da bomba de lubrificação {_n} anormal"
    INO_ALARMS[_base + 2] = f"Falha na detecção de pressão da lubrificação {_n}"
    INO_ALARMS[_base + 3] = f"Detecção de parada da lubrificação {_n} anormal"
    INO_ALARMS[_base + 4] = f"Alívio de pressão da bomba de lubrificação {_n} anormal"

# Tentativas de conexão da rede 1 a 8
for _n in range(1, 9):
    INO_ALARMS[0xA100208 + _n] = f"Iniciando a rede: tentativa {_n}"

ALARM_PREFIX_INO = "WarnIno"


def ino_alarm_tag(code: int) -> str:
    return f"{ALARM_PREFIX_INO}{code:X}"


def ino_alarm_label(tag: str) -> str | None:
    """Nome do alarme a partir da tag WarnIno<HEX>; None se a tag não for desse tipo."""
    if not tag.startswith(ALARM_PREFIX_INO):
        return None
    try:
        code = int(tag[len(ALARM_PREFIX_INO):], 16)
    except ValueError:
        return None
    return INO_ALARMS.get(code, f"Alarme do painel 0x{code:08X}")
