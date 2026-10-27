"""Catálogo das variáveis OPC-UA do gateway Mega (CHL3200) que o painel usa.

Os nomes vêm do servidor OPC-UA do gateway (namespace 1, identificador string).
Unidades são presumidas a partir do nome; confirme com a Chen Hsong.
"""
from __future__ import annotations

import re

# (tag, rótulo, unidade, grupo)
KPI_TAGS: list[tuple[str, str, str, str]] = [
    ("DeviceOnOff", "Máquina ligada", "", "estado"),
    ("OperatorMode", "Modo de operação", "", "estado"),
    ("ProductionStatus", "Status de produção", "", "estado"),
    ("ProductionCount", "Peças produzidas", "pç", "producao"),
    ("ProductionGoodCount", "Peças boas", "pç", "producao"),
    ("BadProduction", "Peças ruins", "pç", "producao"),
    ("DefectCount", "Defeitos", "pç", "producao"),
    ("TargetProductionCount", "Meta de produção", "pç", "producao"),
    ("CycleMonitor", "Tempo de ciclo", "s", "ciclo"),
    ("ProductionCycle", "Ciclo de produção", "s", "ciclo"),
    ("InjectLastTime", "Tempo de injeção", "s", "ciclo"),
    ("HoldLastTime", "Tempo de recalque", "s", "ciclo"),
    ("PlastLastTime", "Tempo de dosagem", "s", "ciclo"),
    ("ColdLastTime", "Tempo de resfriamento", "s", "ciclo"),
    ("ClampLastTime", "Tempo de fechamento", "s", "ciclo"),
    ("OpenLastTime", "Tempo de abertura", "s", "ciclo"),
    ("EjectLastTime", "Tempo de extração", "s", "ciclo"),
    ("InjectMaxPress", "Pressão máx. de injeção", "bar", "qualidade"),
    ("InjectEndLastPos", "Colchão", "mm", "qualidade"),
    ("PlastEndLastPos", "Fim de dosagem", "mm", "qualidade"),
    ("InjectMaxSpeed", "Velocidade máx. de injeção", "%", "qualidade"),
    ("PowerConsume", "Consumo de energia", "kWh", "energia"),
    ("TempNozzleNow", "Bico", "°C", "temperatura"),
    ("TempZ1Now", "Zona 1", "°C", "temperatura"),
    ("TempZ2Now", "Zona 2", "°C", "temperatura"),
    ("TempZ3Now", "Zona 3", "°C", "temperatura"),
    ("TempZ4Now", "Zona 4", "°C", "temperatura"),
    ("TempZ5Now", "Zona 5", "°C", "temperatura"),
    ("TempOilNow", "Óleo", "°C", "temperatura"),
]

# Zonas do canhão: (rótulo, tag real, tag setpoint, tag mínimo, tag máximo)
BARREL_ZONES: list[tuple[str, str, str, str, str]] = [
    ("Bico", "TempNozzleNow", "TempNozzle", "MinTempNozzle", "MaxTempNozzle"),
    ("Zona 1", "TempZ1Now", "TempZ1", "MinTempZ1", "MaxTempZ1"),
    ("Zona 2", "TempZ2Now", "TempZ2", "MinTempZ2", "MaxTempZ2"),
    ("Zona 3", "TempZ3Now", "TempZ3", "MinTempZ3", "MaxTempZ3"),
    ("Zona 4", "TempZ4Now", "TempZ4", "MinTempZ4", "MaxTempZ4"),
    ("Zona 5", "TempZ5Now", "TempZ5", "MinTempZ5", "MaxTempZ5"),
]

SETPOINT_TAGS = sorted({t for z in BARREL_ZONES for t in z[2:]} | {"TempOil", "MaxTempOil"})

# Tags gravadas no histórico (SQLite) a cada leitura.
HISTORY_TAGS = [t[0] for t in KPI_TAGS] + SETPOINT_TAGS

ALARM_PREFIX = "Warn"

ALARM_LABELS: dict[str, str] = {
    "WarnMergencyStopped": "Emergência acionada",
    "WarnFrontSafetyDoorOpen": "Porta de segurança frontal aberta",
    "WarnRearSafetyDoorOpen": "Porta de segurança traseira aberta",
    "WarnSafetyDoorLimit": "Limite da porta de segurança",
    "WarnSafetyDoorLimitError": "Erro no fim de curso da porta",
    "WarnSafetyDoorLatchError": "Erro na trava da porta de segurança",
    "WarnDoorLatchError": "Erro na trava da porta",
    "WarnContCabinetDoorOpen": "Porta do painel elétrico aberta",
    "WarnNozzleGuardOpen": "Proteção do bico aberta",
    "WarnCycleTooLong": "Ciclo muito longo",
    "WarnBarrelTempNotReached": "Temperatura do canhão não atingida",
    "WarnBarrelTempTooHigh": "Temperatura do canhão alta",
    "WarnBarrelPre-heat": "Pré-aquecimento do canhão",
    "WarnBarrelPurging": "Purga do canhão",
    "WarnResinTemperatureLow": "Temperatura da resina baixa",
    "WarnOilTempHigh": "Temperatura do óleo alta",
    "WarnOilTempLow": "Temperatura do óleo baixa",
    "WarnOilLevelLow": "Nível de óleo baixo",
    "WarnOilFilterError": "Erro no filtro de óleo",
    "WarnCloggedOilFilter": "Filtro de óleo entupido",
    "WarnCloggedOilScreen": "Tela de óleo entupida",
    "WarnNoCoolingWater": "Sem água de refrigeração",
    "WarnLubricationOilLevelLow": "Nível de óleo de lubrificação baixo",
    "WarnLubricationPressureLow": "Pressão de lubrificação baixa",
    "WarnGreasePressureLow": "Pressão de graxa baixa",
    "WarnAirPressureLow": "Pressão de ar baixa",
    "WarnPumpNotStarted": "Bomba não ligada",
    "WarnPumpMotorOverload": "Sobrecarga do motor da bomba",
    "WarnServoControlAlarm": "Alarme do servo",
    "WarnMouldProtectionAlarm": "Proteção do molde acionada",
    "WarnMouldAdjError": "Erro no ajuste do molde",
    "WarnMouldAdjAboveLimit": "Ajuste do molde acima do limite",
    "WarnMouldAdjtBelowLimit": "Ajuste do molde abaixo do limite",
    "WarnMouldAdMotorOverload": "Sobrecarga do motor de ajuste do molde",
    "WarnClampingForceTooLow": "Força de fechamento baixa",
    "WarnClampingNotComp": "Fechamento não concluído",
    "WarnClampOpen/CloseError": "Erro de abertura/fechamento",
    "WarnLockingNotComplete": "Travamento não concluído",
    "WarnEjectorAlarm": "Alarme do extrator",
    "WarnEjectorPlateNotRet": "Placa extratora não retornou",
    "WarnCore-PullAlarm": "Alarme de macho",
    "WarnPlastNotComp": "Dosagem não concluída",
    "WarnShort-Shot/Over-Shot": "Injeção incompleta/excesso",
    "WarnBadPart": "Peça ruim",
    "WarnBadPartsMaxReached": "Limite de peças ruins atingido",
    "WarnProductCompleted": "Meta de produção concluída",
    "WarnProductDropNot": "Peça não caiu",
    "WarnProductEjectError": "Erro na extração da peça",
    "WarnProductSensorError": "Erro no sensor de peça",
    "WarnOutofMaterial": "Falta de material",
    "WarnMaterialChange": "Troca de material",
    "WarnBlockedNozzle": "Bico entupido",
    "WarnNozzleForwardError": "Erro no avanço do bico",
    "WarnRobotAlarm": "Alarme do robô",
    "WarnRobotError": "Erro do robô",
    "WarnRobotNotReturned": "Robô não retornou",
    "WarnRobotSafetyError": "Erro de segurança do robô",
    "WarnLowAirPresforRobot": "Pressão de ar baixa para o robô",
    "WarnTakeOutError": "Erro na retirada",
    "WarnPresTransducerError": "Erro no transdutor de pressão",
    "WarnPlastRPMSensorError": "Erro no sensor de rotação da dosagem",
    "WarnOut-of-Battery": "Bateria fraca",
    "WarnSafetyRelayNotReset": "Relé de segurança não rearmado",
    "WarnSafetyValveError": "Erro na válvula de segurança",
    "WarnInjectionSettingsError": "Erro nos parâmetros de injeção",
    "WarnBackPressureTooHigh": "Contrapressão alta",
    "WarnAccChargeAlarm": "Alarme de carga do acumulador",
    "WarnHydraulicClampError": "Erro no fechamento hidráulico",
}


def humanize(tag: str) -> str:
    name = tag[len(ALARM_PREFIX):] if tag.startswith(ALARM_PREFIX) else tag
    name = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", name).replace("-", " ").replace("/", " / ")
    return re.sub(r"\s+", " ", name).strip()


def alarm_label(tag: str) -> str:
    return ALARM_LABELS.get(tag, humanize(tag))


KPI_META = {t: {"label": label, "unit": unit, "group": group} for t, label, unit, group in KPI_TAGS}
