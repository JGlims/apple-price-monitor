#!/usr/bin/env python3
"""Testes do monitor. Sem dependencia externa: python3 testes/teste_parse.py

Dois grupos:

1. parse_listing() contra uma amostra REAL da pagina da Apple, salva em
   testes/amostra_apple.json. Se a Apple mudar os nomes dos campos, o
   codigo novo vai falhar aqui antes de ir para producao, em vez de
   passar a devolver zeros em silencio.

2. conferir_saude() contra catalogos corrompidos de proposito. Um alarme
   que nunca foi visto tocar nao e um alarme.
"""
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import monitor as m  # noqa: E402

RAIZ = pathlib.Path(__file__).parent
falhas: list[str] = []


def checa(cond: bool, descricao: str) -> None:
    print(("  ok   " if cond else "  FALHA") + f"  {descricao}")
    if not cond:
        falhas.append(descricao)


# ---------------------------------------------------------------
print("\n[1] parse_listing() contra a amostra real da Apple")
amostra = json.loads((RAIZ / "amostra_apple.json").read_text())
lidos = [m.parse_listing(r) for r in amostra]
ok = [l for l in lidos if l]

checa(len(ok) == len(amostra), f"os {len(amostra)} produtos da amostra viram Listing")
checa(all(l.price > 0 for l in ok), "todo produto tem preco positivo")
checa(all(l.ssd_gb > 0 for l in ok), "todo produto tem SSD")
checa(all(l.chip in m.BANDWIDTH for l in ok), "todo chip e reconhecido pela tabela de banda")
checa(all(l.part for l in ok), "todo produto tem part number")
checa(all(l.url.startswith("https://www.apple.com/") for l in ok), "toda URL e absoluta")

# Valores exatos de um item conhecido: pega mudanca de unidade ou de campo.
alvo = next((l for l in ok if l.ram_gb == 24 and l.ssd_gb == 1024), None)
checa(alvo is not None, "a amostra tem o M5 24GB/1TB de referencia")
if alvo:
    checa(alvo.chip == "M5", f"chip lido como M5 (veio {alvo.chip!r})")
    checa(alvo.bandwidth == 153, f"banda do M5 = 153 GB/s (veio {alvo.bandwidth})")
    checa(1000 < alvo.price < 2500, f"preco plausivel (veio {alvo.price})")

# 1tb tem que virar 1024, nao 1.
checa(m._size_to_gb("2tb") == 2048, "'2tb' vira 2048 GB")
checa(m._size_to_gb("16gb") == 16, "'16gb' vira 16 GB")
checa(m._size_to_gb(None) == 0, "campo ausente vira 0, nao explode")

# ---------------------------------------------------------------
print("\n[2] conferir_saude() em catalogo saudavel")
import dataclasses  # noqa: E402

# A amostra tem 8 itens e o piso de saude e 20, entao replico para simular
# um catalogo de tamanho real. Part numbers unicos, senao viram um item so.
sadios = [dataclasses.replace(l, part=f"{l.part}-{i}")
          for i, l in enumerate(ok * 5) if l.ram_gb]
checa(len(sadios) >= m.SAUDE["min_produtos"], f"o catalogo de teste tem {len(sadios)} itens")
checa(m.conferir_saude(sadios, 400, {}) == [], "catalogo bom nao gera alerta")

print("\n[3] conferir_saude() em catalogos quebrados")

def variar(**campos):
    return [dataclasses.replace(l, **campos) for l in sadios]

checa(m.conferir_saude([], 400, {}) != [],
      "extraiu registros mas nenhum virou produto -> alerta")
checa(m.conferir_saude(ok[:3], 400, {}) != [],
      "catalogo minusculo -> alerta")
checa(m.conferir_saude(variar(price=0.0), 400, {}) != [],
      "preco zerado em todos -> alerta")
checa(m.conferir_saude(variar(ssd_gb=0), 400, {}) != [],
      "SSD zerado em todos -> alerta")
checa(m.conferir_saude(variar(chip="M9 Ultra"), 400, {}) != [],
      "chip desconhecido em todos -> alerta")
checa(m.conferir_saude(variar(ram_gb=0), 400, {}) == [],
      "RAM ausente NAO alerta (a Apple nao publica nos 16 polegadas)")
checa(m.conferir_saude(sadios, len(amostra), {f"p{i}": 1.0 for i in range(500)}) != [],
      "catalogo encolheu de 500 para poucos -> alerta")

# ---------------------------------------------------------------
print("\n[4] custo em real")
c = m.load_criteria()
checa(m.custo_brasil(1000, c, 5.0) > 1000 * 5.0, "pousado e sempre maior que a conversao pura")
checa(m.reais(1234567) == "1.234.567", "separador de milhar brasileiro")

# ---------------------------------------------------------------
print()
if falhas:
    print(f"{len(falhas)} FALHA(S):")
    for f in falhas:
        print(f"  - {f}")
    sys.exit(1)
print("todos os testes passaram")
