"""Verificação de bancada: duas Beckhoffs (TwinCAT 2) lidas no mesmo processo.

Abre duas conexões pyads, uma para cada PLC, e lê a cada 2 s durante 60 s
as variáveis .CYCLETIME_MQTT e .AAAPFWTEMPTOHMI[1].ACTUALTEMP de cada uma.
Imprime ok/erro por leitura. Só leitura (read_by_name); nenhuma escrita.

Uso (com o Monitor parado):
    .venv/bin/python scripts/teste_duas_beckhoff.py \
        --a 5.79.97.240.1.1@10.86.20.186 --b 5.112.28.141.1.1@192.168.0.111

Requer ADS_LOCAL_NET_ID definido no .env (raiz do projeto) ou no ambiente.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime
from pathlib import Path

# Garante que `app.config` seja importável quando o script roda de backend/.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app.config  # noqa: E402,F401  (carrega o .env da raiz para os.environ)

try:
    import pyads
except ImportError:  # pragma: no cover
    pyads = None

DURACAO_S = 60
INTERVALO_S = 2
PORTA_ADS_PADRAO = 801  # runtime PLC do TwinCAT 2 (mesmo default de ads_source.py)
VARIAVEIS = (".CYCLETIME_MQTT", ".AAAPFWTEMPTOHMI[1].ACTUALTEMP")


def ler_netid_ip(texto: str) -> tuple[str, str]:
    """'5.79.97.240.1.1@10.86.20.186' -> ('5.79.97.240.1.1', '10.86.20.186')."""
    if "@" not in texto:
        raise argparse.ArgumentTypeError(
            f"formato esperado NETID@IP, recebido: {texto!r}"
        )
    net_id, ip = texto.split("@", 1)
    return net_id.strip(), ip.strip()


def abrir(rotulo: str, net_id: str, ip: str, porta: int):
    conexao = pyads.Connection(net_id, porta, ip)
    conexao.open()
    print(f"[{rotulo}] conexão aberta: {net_id} em {ip}:{porta}")
    return conexao


def ler(conexao, variavel: str):
    """Retorna (True, valor) ou (False, mensagem de erro). Nunca escreve no PLC."""
    try:
        valor = conexao.read_by_name(variavel, pyads.PLCTYPE_LREAL)
        return True, valor
    except Exception as exc:  # noqa: BLE001  (queremos registrar qualquer falha)
        return False, str(exc)


def fechar(conexao) -> None:
    if conexao is None:
        return
    try:
        conexao.close()
    except Exception:  # noqa: BLE001
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Lê duas Beckhoffs no mesmo processo por 60 s (a cada 2 s), só leitura. "
            "Requer ADS_LOCAL_NET_ID no .env."
        ),
    )
    parser.add_argument(
        "--a", required=True, type=ler_netid_ip, metavar="NETID@IP",
        help="primeira Beckhoff, ex.: 5.79.97.240.1.1@10.86.20.186",
    )
    parser.add_argument(
        "--b", required=True, type=ler_netid_ip, metavar="NETID@IP",
        help="segunda Beckhoff, ex.: 5.112.28.141.1.1@192.168.0.111",
    )
    parser.add_argument(
        "--porta", type=int, default=PORTA_ADS_PADRAO,
        help=f"porta ADS do runtime PLC (padrão {PORTA_ADS_PADRAO}, TwinCAT 2)",
    )
    args = parser.parse_args(argv)

    if pyads is None:
        print("pyads não está instalado no ambiente (use backend/.venv).", file=sys.stderr)
        return 2

    local_net_id = os.environ.get("ADS_LOCAL_NET_ID", "").strip()
    if not local_net_id:
        print("ADS_LOCAL_NET_ID não está definido (.env ou ambiente).", file=sys.stderr)
        return 2

    rotulos = {"A": args.a, "B": args.b}
    conexoes: dict[str, object] = {}
    try:
        pyads.set_local_address(local_net_id)
        for rotulo, (net_id, ip) in rotulos.items():
            try:
                conexoes[rotulo] = abrir(rotulo, net_id, ip, args.porta)
            except Exception as exc:  # noqa: BLE001
                print(f"Falha ao abrir conexão [{rotulo}] {net_id}@{ip}: {exc}", file=sys.stderr)
                return 1

        print(f"Lendo por {DURACAO_S} s, a cada {INTERVALO_S} s. Ctrl+C para interromper.")
        inicio = time.monotonic()
        while time.monotonic() - inicio < DURACAO_S:
            agora = datetime.now().strftime("%H:%M:%S")
            for rotulo, conexao in conexoes.items():
                for variavel in VARIAVEIS:
                    ok, resultado = ler(conexao, variavel)
                    if ok:
                        print(f"{agora} [{rotulo}] {variavel} = {resultado} -> ok")
                    else:
                        print(f"{agora} [{rotulo}] {variavel} -> erro: {resultado}")
            time.sleep(INTERVALO_S)
        print("Fim do teste.")
        return 0
    except KeyboardInterrupt:
        print("Interrompido pelo usuário.")
        return 130
    except Exception as exc:  # noqa: BLE001
        print(f"Falha inesperada: {exc}", file=sys.stderr)
        return 1
    finally:
        for conexao in conexoes.values():
            fechar(conexao)


if __name__ == "__main__":
    sys.exit(main())
