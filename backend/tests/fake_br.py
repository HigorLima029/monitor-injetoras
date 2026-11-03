"""Servidor OPC-UA falso que imita o painel B&R da JM228 (Euromap 63, ::EM63Mapp)."""
from __future__ import annotations

from asyncua import Server, ua

# Valores copiados da leitura real da JM228 (29/10/2026), com o canhao aquecendo.
PEM63A = {
    "ActSwitStaBrlZn": ["1", "1", "1", "1", "0", "0", "0", "0"],
    "SetTmpBrlZn": [180.0, 180.0, 180.0, 180.0, 200.0, 25.0, 25.0, 30.0],
    "ActTmpBrlZn": [176.5, 179.0, 181.2, 150.0, 0.0, 0.0, 0.0, 0.0],
    "SetTmpBrlZnLdev": [20.0] * 8,
    "SetTmpBrlZnHdev": [20.0] * 8,
    "SetTmpOil": 60.0, "ActTmpOil": 41.0,
    "ActTimCyc": 31.4, "ActTimFill": 1.2, "ActTimPlst": 6.3, "ActStkCsh": 8.1, "ActStkPlst": 120.0,
    "ActPrsMachHydMax": 98.0, "myActTimCool": 12.0, "myActTimMldCls": 2.1,
    "myActTimMldOpn": 1.9, "myActTimEject": 0.7, "myActIPAddr": "10.86.20.253",
}
PEM63M = {"ActStsMach": "Production", "ActStsCyc": "NONE", "SetCntCyc": 0, "ActCntCyc": 9283,
          "ActCntPrtRej": 3, "ActDescJob": "iocfg", "ActCntMld": 4783}


async def start(port: int = 4852):
    server = Server()
    await server.init()
    server.set_endpoint(f"opc.tcp://127.0.0.1:{port}")
    await server.register_namespace("urn:B&R/plc/")
    ns = await server.register_namespace("urn:B&R/pv/")
    plc = await server.nodes.objects.add_object(ns, "PLC")
    mods = await plc.add_object(ns, "Modules")
    root = await mods.add_object(ua.NodeId("::", ns), "<Default>")
    em = await root.add_object(ua.NodeId("::EM63Mapp", ns), "EM63Mapp")
    nodes = {}
    for struct, fields in (("pEm63a", PEM63A), ("pEm63m", PEM63M)):
        parent = await em.add_object(ua.NodeId(f"::EM63Mapp:{struct}", ns), struct)
        for name, value in fields.items():
            nid = ua.NodeId(f"::EM63Mapp:{struct}.{name}", ns)
            nodes[f"{struct}.{name}"] = await parent.add_variable(nid, name, value)
    await server.start()
    return server, nodes
