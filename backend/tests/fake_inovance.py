"""Servidor OPC-UA falso que imita o painel Inovance IT7000 (PLC > Modules > <Default> > App).

Nos NUMERICOS e nomes em ingles, como no painel real (192.168.10.118). Valores de uma
maquina produzindo, para o translate ter o que converter (no painel parado tudo vem 0).
"""
from __future__ import annotations

from asyncua import Server, ua

# browse name -> (nodeid numerico, valor inicial), copiando o padrao do painel real
APP_VARS = {
    "Alarm1": (4279238665, 0.0),
    "Alarm2": (4279238666, 0.0),
    "Operate State": (1441794, 3.0),           # != 0 -> produzindo
    "Temp1 Real": (85327875, 210.0),           # bico
    "Temp2 Real": (85327876, 200.0),           # zona 1
    "Temp3 Real": (85327877, 190.0),
    "Temp4 Real": (85327878, 180.0),
    "Temp5 Real": (85327879, 170.0),
    "Temp6 Real": (85327880, 160.0),           # zona 5
    "Temp7 Real": (85327881, 50.0),            # nao mapeada (so leitura crua)
    "Temp1 Set": (85131264, 215.0),
    "Temp2 Set": (85131265, 205.0),
    "Temp3 Set": (85131266, 195.0),
    "Temp4 Set": (85131267, 185.0),
    "Temp5 Set": (85131268, 175.0),
    "Temp6 Set": (85131269, 165.0),
    "Temp7 Set": (85131270, 55.0),
    "CycTime Real": (1441832, 28.5),
    "Inj Tm Real": (269877250, 1.5),
    "Charge Tm Real": (269877258, 6.0),
    "Charge Pos Real": (269877261, 115.0),
    "Close Tm Real": (538312711, 2.0),
    "Open Tm Real": (538312718, 1.8),
    "Eject Tm Real": (555089922, 0.6),
    "Max InjPrs Real": (269877268, 120.0),
    "Inject Max Speed": (269877324, 95.0),
    "Inject Min Pos": (269877274, 8.0),
    "OpenCnt Hi Real": (538312706, 1.0),       # 1*10000
    "OpenCnt Low Real": (538312707, 2345.0),   # + 2345 = 12345
}


async def start(port: int = 4853):
    server = Server()
    await server.init()
    server.set_endpoint(f"opc.tcp://127.0.0.1:{port}")
    ns = await server.register_namespace("urn:Inovance/InoTouchPad/")
    plc = await server.nodes.objects.add_object(ua.NodeId(710790927, ns), "PLC")
    mods = await plc.add_object(ua.NodeId(2297884803, ns), "Modules")
    root = await mods.add_object(ua.NodeId(1659256805, ns), "<Default>")
    app = await root.add_object(ua.NodeId(700000000, ns), "App")
    nodes = {}
    for name, (nid, value) in APP_VARS.items():
        nodes[name] = await app.add_variable(ua.NodeId(nid, ns), name, value)
        await nodes[name].set_writable()  # so o servidor de teste altera os valores
    await server.start()
    return server, nodes
