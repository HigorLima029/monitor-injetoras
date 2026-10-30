"""Coletor OPC-UA (somente leitura) para gateways Mega CHL3200 e paineis B&R.

Conecta num servidor OPC-UA, lista todas as variaveis e grava em CSV:
  - "Exportar variaveis": um CSV com NodeId, caminho, tipo e valor atual de cada variavel.
  - "Gravar valores": um CSV com uma linha a cada intervalo, uma coluna por variavel.

Nunca escreve na maquina: so usa browse e leitura de valores.

Uso com janela:  coletor_opcua            (ou python coletor_opcua.py)
Uso no terminal: coletor_opcua --url opc.tcp://192.168.0.8:4840 --exportar
                 coletor_opcua --url opc.tcp://192.168.0.8:4840 --gravar 60 --intervalo 5 --filtro Temp
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import datetime as dt
import json
import os
import sys
import threading
from pathlib import Path

from asyncua import Client, ua

VERSAO = "1.0"
LIMITE_VARIAVEIS = 20000
PROFUNDIDADE_MAX = 12
BLOCO_LEITURA = 200


def pasta_saida() -> Path:
    base = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent
    pasta = base / "coletas"
    pasta.mkdir(exist_ok=True)
    return pasta


def agora_txt() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")


def arquivo_novo(prefixo: str) -> Path:
    base = pasta_saida() / f"{prefixo}_{agora_txt()}"
    arquivo, n = base.with_suffix(".csv"), 2
    while arquivo.exists():  # duas coletas no mesmo segundo nao se sobrescrevem
        arquivo, n = Path(f"{base}_{n}.csv"), n + 1
    return arquivo


def texto_valor(valor) -> str:
    if valor is None:
        return ""
    if isinstance(valor, (list, tuple)):
        return json.dumps([texto_valor(v) if not isinstance(v, (int, float, bool)) else v for v in valor], ensure_ascii=False)
    if isinstance(valor, ua.LocalizedText):
        return valor.Text or ""
    if isinstance(valor, (dt.datetime,)):
        return valor.isoformat()
    if isinstance(valor, float):
        return repr(round(valor, 6))
    return str(valor)


class Coletor:
    def __init__(self, url: str, log=print):
        self.url = url
        self.log = log
        self.variaveis: list[dict] = []

    def _cliente(self) -> Client:
        cliente = Client(self.url, timeout=30, watchdog_intervall=30)
        return cliente

    async def _navegar(self, cliente: Client) -> list[dict]:
        server_id = ua.NodeId(ua.ObjectIds.Server)
        vistos: set = set()
        achadas: list[dict] = []
        fila = [(cliente.nodes.objects, "", 0)]
        while fila and len(achadas) < LIMITE_VARIAVEIS:
            no, caminho, nivel = fila.pop(0)
            try:
                filhos = await no.get_children()
            except Exception:
                continue
            for filho in filhos:
                if filho.nodeid == server_id or filho.nodeid in vistos:
                    continue
                vistos.add(filho.nodeid)
                try:
                    nome = (await filho.read_browse_name()).Name
                    classe = await filho.read_node_class()
                except Exception:
                    continue
                caminho_filho = f"{caminho}/{nome}" if caminho else nome
                if classe == ua.NodeClass.Variable:
                    achadas.append({"no": filho, "nodeid": filho.nodeid.to_string(), "caminho": caminho_filho, "nome": nome})
                    if nivel < PROFUNDIDADE_MAX:
                        fila.append((filho, caminho_filho, nivel + 1))  # B&R: structs tem campos filhos
                elif classe == ua.NodeClass.Object and nivel < PROFUNDIDADE_MAX:
                    fila.append((filho, caminho_filho, nivel + 1))
        return achadas

    async def _ler(self, cliente: Client, variaveis: list[dict]) -> list:
        valores = []
        for i in range(0, len(variaveis), BLOCO_LEITURA):
            bloco = variaveis[i:i + BLOCO_LEITURA]
            try:
                lidos = await cliente.read_values([v["no"] for v in bloco])
            except Exception:
                lidos = []
                for v in bloco:  # um valor ruim nao derruba o bloco inteiro
                    try:
                        lidos.append(await v["no"].read_value())
                    except Exception:
                        lidos.append(None)
            valores.extend(lidos)
        return valores

    def _filtrar(self, variaveis: list[dict], filtro: str) -> list[dict]:
        if not filtro:
            return variaveis
        termos = [t.strip().lower() for t in filtro.split(",") if t.strip()]
        return [v for v in variaveis if any(t in v["caminho"].lower() for t in termos)]

    async def testar(self) -> str:
        async with self._cliente() as cliente:
            nome = await cliente.nodes.server.get_child(["0:ServerStatus", "0:BuildInfo", "0:ProductName"])
            produto = await nome.read_value()
            return f"Conectado: {produto}"

    async def exportar(self, filtro: str = "") -> Path:
        self.log(f"Conectando em {self.url} ...")
        async with self._cliente() as cliente:
            self.log("Conectado. Procurando variaveis (pode levar 1 a 2 minutos)...")
            variaveis = self._filtrar(await self._navegar(cliente), filtro)
            self.log(f"{len(variaveis)} variaveis encontradas. Lendo valores...")
            valores = await self._ler(cliente, variaveis)
        arquivo = arquivo_novo("variaveis")
        with open(arquivo, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f, delimiter=";")
            w.writerow(["NodeId", "Caminho", "Nome", "Tipo", "Valor"])
            for v, valor in zip(variaveis, valores):
                w.writerow([v["nodeid"], v["caminho"], v["nome"], type(valor).__name__ if valor is not None else "", texto_valor(valor)])
        self.log(f"Pronto: {arquivo}")
        return arquivo

    async def gravar(self, minutos: float, intervalo: float, filtro: str = "", parar: threading.Event | None = None) -> Path:
        parar = parar or threading.Event()
        self.log(f"Conectando em {self.url} ...")
        async with self._cliente() as cliente:
            variaveis = self._filtrar(await self._navegar(cliente), filtro)
            if not variaveis:
                raise RuntimeError("Nenhuma variavel encontrada (confira o filtro).")
            arquivo = arquivo_novo("gravacao")
            fim = asyncio.get_running_loop().time() + minutos * 60
            linhas = 0
            self.log(f"Gravando {len(variaveis)} variaveis a cada {intervalo:g} s por {minutos:g} min em {arquivo.name}")
            with open(arquivo, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f, delimiter=";")
                w.writerow(["DataHora"] + [v["caminho"] for v in variaveis])
                while not parar.is_set() and asyncio.get_running_loop().time() < fim:
                    inicio = asyncio.get_running_loop().time()
                    valores = await self._ler(cliente, variaveis)
                    w.writerow([dt.datetime.now().isoformat(timespec="seconds")] + [texto_valor(x) for x in valores])
                    f.flush()
                    linhas += 1
                    if linhas % 10 == 0:
                        self.log(f"{linhas} leituras gravadas")
                    espera = intervalo - (asyncio.get_running_loop().time() - inicio)
                    while espera > 0 and not parar.is_set():
                        await asyncio.sleep(min(0.5, espera))
                        espera -= 0.5
        self.log(f"Pronto: {linhas} leituras em {arquivo}")
        return arquivo


def abrir_pasta(caminho: Path) -> None:
    try:
        if os.name == "nt":
            os.startfile(caminho)  # type: ignore[attr-defined]
    except Exception:
        pass


def janela() -> None:
    import tkinter as tk
    from tkinter import ttk

    raiz = tk.Tk()
    raiz.title(f"Coletor OPC-UA (somente leitura) v{VERSAO}")
    raiz.geometry("720x520")
    quadro = ttk.Frame(raiz, padding=12)
    quadro.pack(fill="both", expand=True)

    campos = {}
    for linha, (rotulo, chave, padrao) in enumerate([
        ("Endereco OPC-UA", "url", "opc.tcp://192.168.0.8:4840"),
        ("Filtro (opcional, ex.: Temp,Production)", "filtro", ""),
        ("Gravar por (minutos)", "minutos", "60"),
        ("Intervalo (segundos)", "intervalo", "5"),
    ]):
        ttk.Label(quadro, text=rotulo).grid(row=linha, column=0, sticky="w", pady=3)
        var = tk.StringVar(value=padrao)
        ttk.Entry(quadro, textvariable=var, width=48).grid(row=linha, column=1, sticky="we", pady=3)
        campos[chave] = var
    quadro.columnconfigure(1, weight=1)

    botoes = ttk.Frame(quadro)
    botoes.grid(row=4, column=0, columnspan=2, pady=10, sticky="w")
    saida = tk.Text(quadro, height=18, wrap="word")
    saida.grid(row=5, column=0, columnspan=2, sticky="nsew")
    quadro.rowconfigure(5, weight=1)
    ttk.Label(quadro, text=f"Arquivos salvos em: {pasta_saida()}").grid(row=6, column=0, columnspan=2, sticky="w", pady=(6, 0))

    parar = threading.Event()

    def log(msg: str) -> None:
        raiz.after(0, lambda: (saida.insert("end", msg + "\n"), saida.see("end")))

    def rodar(acao) -> None:
        def alvo():
            try:
                resultado = asyncio.run(acao())
                if isinstance(resultado, Path):
                    abrir_pasta(resultado.parent)
                elif resultado:
                    log(str(resultado))
            except Exception as erro:  # mostra o erro na janela, sem fechar o programa
                log(f"ERRO: {type(erro).__name__}: {erro}")
                log("Confira: o PC esta na mesma rede do gateway? O endereco e a porta 4840 estao certos?")
        threading.Thread(target=alvo, daemon=True).start()

    def coletor() -> Coletor:
        return Coletor(campos["url"].get().strip(), log)

    def numero(chave: str, padrao: float) -> float:
        try:
            return float(campos[chave].get().replace(",", "."))
        except ValueError:
            return padrao

    def gravar() -> None:
        parar.clear()
        rodar(lambda: coletor().gravar(numero("minutos", 60), max(1.0, numero("intervalo", 5)), campos["filtro"].get(), parar))

    ttk.Button(botoes, text="Testar conexao", command=lambda: rodar(lambda: coletor().testar())).pack(side="left", padx=4)
    ttk.Button(botoes, text="Exportar variaveis (CSV)", command=lambda: rodar(lambda: coletor().exportar(campos["filtro"].get()))).pack(side="left", padx=4)
    ttk.Button(botoes, text="Gravar valores (CSV)", command=gravar).pack(side="left", padx=4)
    ttk.Button(botoes, text="Parar gravacao", command=parar.set).pack(side="left", padx=4)

    log("Somente leitura: este programa nunca altera a maquina.")
    log("1) Ajuste o endereco  2) Testar conexao  3) Exportar ou Gravar")
    raiz.mainloop()


def main() -> None:
    p = argparse.ArgumentParser(description="Coletor OPC-UA somente leitura")
    p.add_argument("--url", help="ex.: opc.tcp://192.168.0.8:4840")
    p.add_argument("--exportar", action="store_true", help="exporta todas as variaveis com valor atual")
    p.add_argument("--gravar", type=float, metavar="MIN", help="grava valores durante MIN minutos")
    p.add_argument("--intervalo", type=float, default=5, help="segundos entre leituras (padrao 5)")
    p.add_argument("--filtro", default="", help="so variaveis cujo caminho contem estes termos (separados por virgula)")
    a = p.parse_args()
    if not a.url:
        janela()
        return
    c = Coletor(a.url)
    if a.gravar:
        asyncio.run(c.gravar(a.gravar, max(1.0, a.intervalo), a.filtro))
    elif a.exportar:
        asyncio.run(c.exportar(a.filtro))
    else:
        print(asyncio.run(c.testar()))


if __name__ == "__main__":
    main()
