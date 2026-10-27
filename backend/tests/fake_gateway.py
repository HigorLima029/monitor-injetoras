"""Servidor OPC-UA falso que imita o gateway Mega (variáveis em ns=1, id string).

Uso manual:  python -m tests.fake_gateway  (escuta em opc.tcp://127.0.0.1:4841)
"""
from __future__ import annotations

import asyncio

from asyncua import Server, ua

TAGS = {
    "ProductionCount": 100.0, "ProductionGoodCount": 98.0, "BadProduction": 2.0,
    "CycleMonitor": 24.5, "TempZ1": 270.0, "TempZ1Now": 268.4, "TempNozzle": 230.0,
    "TempNozzleNow": 229.1, "InjectMaxPress": 95.0, "Inject1Pres": 110.0,
    "WarnFrontSafetyDoorOpen": 0.0, "WarnCycleTooLong": 0.0, "WiFiSsid": "Higorp",
}


async def start(port: int = 4841) -> tuple[Server, dict]:
    server = Server()
    await server.init()
    server.set_endpoint(f"opc.tcp://127.0.0.1:{port}")
    folder = await server.nodes.objects.add_folder(ua.NodeId("MegaGateway", 1), "MegaGateway")
    nodes = {}
    for name, value in TAGS.items():
        nodes[name] = await folder.add_variable(ua.NodeId(name, 1), name, value)
    await server.start()
    return server, nodes


async def _main() -> None:
    server, _ = await start()
    print("gateway falso em opc.tcp://127.0.0.1:4841 (Ctrl+C para sair)")
    try:
        while True:
            await asyncio.sleep(1)
    finally:
        await server.stop()


if __name__ == "__main__":
    asyncio.run(_main())
