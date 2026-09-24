#!/usr/bin/env python3
"""
Monitor de MacBooks recondicionados da Apple US.

Le o inventario ao vivo da loja de refurbished, filtra pelos criterios
definidos em criteria.yaml, compara com o estado da execucao anterior
e notifica por Telegram quando aparece algo novo que interessa.

Uso local:
    python3 monitor.py            # roda e imprime
    python3 monitor.py --dry-run  # nao notifica, nao salva estado
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, asdict
from pathlib import Path

ROOT = Path(__file__).parent
STATE_FILE = ROOT / "state.json"
CRITERIA_FILE = ROOT / "criteria.json"

# Uma URL basta: a pagina embute o catalogo Mac inteiro, nao so a secao aberta.
FEED_URLS = ["https://www.apple.com/shop/refurbished/mac/14-inch-macbook-pro"]

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

# Largura de banda de memoria por chip, em GB/s.
# E o numero que decide a velocidade de geracao de tokens num LLM local.
BANDWIDTH = {
    "M1": 68, "M1 Pro": 205, "M1 Max": 410,
    "M2": 102, "M2 Pro": 205, "M2 Max": 410,
    "M3": 102, "M3 Pro": 150, "M3 Max": 400,
    "M4": 120, "M4 Pro": 273, "M4 Max": 546,
    "M5": 153, "M5 Pro": 307, "M5 Max": 614,
}


@dataclass(frozen=True)
class Listing:
    part: str
    title: str
    chip: str
    ram_gb: int
    ssd_gb: int
    price: float
    url: str

    @property
    def bandwidth(self) -> int:
        return BANDWIDTH.get(self.chip, 0)

    def score(self) -> float:
        """Quanto de memoria e de banda voce leva por mil dolares."""
        if self.price <= 0:
            return 0.0
        return round((self.ram_gb + self.bandwidth / 10) / (self.price / 1000), 1)

    def line(self) -> str:
        return (f"{self.chip} · {self.ram_gb}GB · {self.ssd_gb}GB SSD · "
                f"US$ {self.price:,.0f} · {self.bandwidth} GB/s · score {self.score()}")


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                               "Accept-Language": "en-US,en;q=0.9"})
    with urllib.request.urlopen(req, timeout=45) as r:
        return r.read().decode("utf-8", errors="replace")


def extract_products(html: str) -> list[dict]:
    """A pagina embute o inventario como JSON. O nome da variavel ja mudou
    algumas vezes, entao tentamos alguns padroes antes de desistir."""
    patterns = [
        r"window\.REFURB_GRID_BOOTSTRAP\s*=\s*(\{.*?\});",
        r'"tiles"\s*:\s*(\[.*?\])\s*,\s*"',
        r"window\.__INITIAL_STATE__\s*=\s*(\{.*?\});",
    ]
    for pat in patterns:
        m = re.search(pat, html, re.DOTALL)
        if not m:
            continue
        try:
            blob = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        return list(_walk_for_products(blob))
    return []


def _walk_for_products(node, _depth=0):
    """Percorre o JSON procurando dicts que pareçam produto."""
    if _depth > 12:
        return
    if isinstance(node, dict):
        if "partNumber" in node and ("price" in node or "currentPrice" in node):
            yield node
        for v in node.values():
            yield from _walk_for_products(v, _depth + 1)
    elif isinstance(node, list):
        for v in node:
            yield from _walk_for_products(v, _depth + 1)


def _size_to_gb(raw: str) -> int:
    """'16gb' -> 16 · '2tb' -> 2048"""
    m = re.match(r"(\d+)\s*(gb|tb)", str(raw or "").lower())
    if not m:
        return 0
    return int(m.group(1)) * (1024 if m.group(2) == "tb" else 1)


def _ram_from_text(text: str) -> int:
    """Fallback quando tsMemorySize nao vem preenchido."""
    m = re.search(r"(\d+)\s*GB\s+(?:of\s+)?(?:unified\s+)?memory", text, re.I)
    return int(m.group(1)) if m else 0


def parse_listing(raw: dict) -> Listing | None:
    title = str(raw.get("title") or "")
    part = str(raw.get("partNumber") or "")
    url_path = str(raw.get("productDetailsUrl") or "")
    if not part or not title:
        return None

    dims = (raw.get("filters") or {}).get("dimensions") or {}
    if dims.get("refurbClearModel") not in {"macbookpro", "macbookair"}:
        return None

    # Item com desconto guarda o preco atual em previousPrice.raw_amount
    # (o nome engana: o "Was" fica na string irma). Sem desconto, em currentPrice.
    pn = raw.get("price") or {}
    amount = ((pn.get("previousPrice") or {}).get("raw_amount")
              or (pn.get("currentPrice") or {}).get("raw_amount"))
    try:
        price = float(amount or 0)
    except (TypeError, ValueError):
        return None
    if price <= 0:
        return None

    # O chip aparece no titulo e, com mais confiabilidade, na URL do produto.
    haystack = f"{title} {urllib.parse.unquote(url_path)}"
    chip_m = re.search(r"\bM(\d)\s*(Pro|Max|Ultra)?\b", haystack, re.I)
    chip = ""
    if chip_m:
        suffix = chip_m.group(2)
        chip = f"M{chip_m.group(1)}" + (f" {suffix.title()}" if suffix else "")

    return Listing(
        part=part,
        title=title,
        chip=chip,
        ram_gb=_size_to_gb(dims.get("tsMemorySize")) or _ram_from_text(haystack),
        ssd_gb=_size_to_gb(dims.get("dimensionCapacity")),
        price=price,
        url="https://www.apple.com" + url_path.split("?")[0],
    )


DEFAULTS = {
    "max_price": 1900, "min_ram_gb": 24, "min_bandwidth": 120,
    "blocked_chips": ["M3 Pro"],
    "stretch_price": 2300, "stretch_min_bandwidth": 250,
    # Orange County (regiao da Disney) = 6,5%. Osceola = 7,5%.
    "imposto_vendas_eua": 0.065,
    "iof": 0.035,               # confirmado para 2026, Decreto 6.306/2007
    "isencao_usd": 1000,        # isencao aerea, por pessoa
    "imposto_importacao": 0.50, # sobre o excedente
}


# Duas fontes: a primeira e brasileira e atualiza de minuto em minuto,
# a segunda existe para o dia em que a primeira cair. Ordem importa.
COTACAO_FONTES = [
    ("https://economia.awesomeapi.com.br/json/last/USD-BRL",
     lambda d: float(d["USDBRL"]["ask"])),
    ("https://open.er-api.com/v6/latest/USD",
     lambda d: float(d["rates"]["BRL"])),
]


def cotacao() -> float | None:
    """Dolar de hoje, ou None se nenhuma fonte responder.

    Preco em real e conveniencia, nao o trabalho do monitor: se a API
    cair, a mensagem sai so em dolar em vez de nao sair.
    """
    for url, extrai in COTACAO_FONTES:
        try:
            with urllib.request.urlopen(url, timeout=15) as r:
                v = extrai(json.loads(r.read()))
        except Exception:  # noqa: BLE001
            continue
        # Se a API mudar de formato e devolver lixo, e melhor nao mostrar
        # nada do que mostrar um numero errado com cara de certo.
        if 3.0 < v < 20.0:
            return v
    return None


def custo_brasil(usd: float, c: dict, cambio: float) -> float:
    """Quanto o notebook custa de verdade, pousado no Brasil.

    A conversao pura engana: US$ 1.699 nao vira R$ 8.800 no seu bolso.
    Entre a etiqueta e o Brasil entram tres coisas, nesta ordem:

      1. imposto de venda da Florida, cobrado no caixa
      2. IOF, sobre o que passou no cartao (nao sobre o imposto de
         importacao, que voce paga em reais na chegada)
      3. imposto de importacao: a isencao aerea cobre US$ 1.000 por
         pessoa e o excedente paga 50%. Notebook nao entra em
         "bens de uso pessoal" -- e declaravel.
    """
    compra_usd = usd * (1 + c["imposto_vendas_eua"])
    brl_compra = compra_usd * cambio * (1 + c["iof"])
    excedente = max(0.0, compra_usd - c["isencao_usd"])
    brl_importacao = excedente * c["imposto_importacao"] * cambio
    return brl_compra + brl_importacao


def reais(v: float) -> str:
    """1234.5 -> '1.234'. O separador brasileiro, nao o americano."""
    return f"{v:,.0f}".replace(",", ".")


def linha_item(l, criteria: dict, cambio: float | None, prefixo: str) -> str:
    """Uma linha de produto. Em dolar sempre; em real quando ha cotacao."""
    precos = f"US$ {l.price:,.0f}".replace(",", ".")
    if cambio:
        precos += (f" \u00b7 R$ {reais(l.price * cambio)}"
                   f" \u00b7 pousado <b>R$ {reais(custo_brasil(l.price, criteria, cambio))}</b>")
    return (f'{prefixo} <a href="{esc(l.url)}">{esc(l.chip)} {l.ram_gb}GB {l.ssd_gb}GB</a>'
            f" \u2014 {precos} \u00b7 {l.bandwidth} GB/s \u00b7 score {l.score()}")


def load_criteria() -> dict:
    c = dict(DEFAULTS)
    if CRITERIA_FILE.exists():
        c.update(json.loads(CRITERIA_FILE.read_text()))
    return c


def why(l: Listing, c: dict) -> str | None:
    """Por que este item merece um aviso — ou None se nao merece.

    Duas regras, porque sao dois desejos diferentes. A primeira e
    "cabe no orcamento e presta". A segunda e "um chip Pro entrou no
    alcance", que vale um esticao e por isso tem teto proprio.
    Uma regra so, com piso de banda alto E teto de preco baixo,
    nao descrevia nenhuma maquina que existe: era a razao de o
    monitor rodar por tres semanas sem nunca falar.
    """
    if l.chip in c.get("blocked_chips", []):
        return None
    if l.ram_gb < c["min_ram_gb"]:
        return None
    if l.price <= c["max_price"] and l.bandwidth >= c["min_bandwidth"]:
        return "cabe no orcamento"
    if l.price <= c["stretch_price"] and l.bandwidth >= c["stretch_min_bandwidth"]:
        return "chip Pro no alcance"
    return None


def matches(l: Listing, c: dict) -> bool:
    return why(l, c) is not None


def esc(s: object) -> str:
    """Escapa o que vai dentro de uma tag HTML do Telegram."""
    return html.escape(str(s), quote=False)


def _post(token: str, chat: str, text: str, parse_mode: str | None) -> None:
    body: dict = {"chat_id": chat, "text": text, "disable_web_page_preview": False}
    if parse_mode:
        body["parse_mode"] = parse_mode
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            r.read()
    except urllib.error.HTTPError as e:
        # O Telegram diz exatamente o que esta errado no corpo da resposta.
        # Engolir isso foi o que transformou um erro obvio em uma cacada.
        try:
            detail = json.loads(e.read().decode()).get("description", "")
        except Exception:  # noqa: BLE001
            detail = ""
        raise RuntimeError(f"HTTP {e.code} do Telegram: {detail or 'sem descricao'}") from None


def _mascara(chat: str) -> str:
    """Mostra o bastante para reconhecer o chat_id sem imprimi-lo inteiro."""
    return f"{len(chat)} digitos terminando em {chat[-3:]}" if len(chat) > 3 else "curto demais"


def _quem_sou(token: str) -> str:
    """Pergunta ao Telegram de quem e este token. Nao expoe o segredo."""
    try:
        with urllib.request.urlopen(
                f"https://api.telegram.org/bot{token}/getMe", timeout=15) as r:
            u = json.loads(r.read()).get("result", {}).get("username")
            return f"token do bot @{u}" if u else "token aceito, bot sem username"
    except Exception:  # noqa: BLE001
        return "token RECUSADO pelo Telegram (revogado ou errado)"


def notify(text: str) -> None:
    token = (os.environ.get("TELEGRAM_TOKEN") or "").strip()
    # O chat_id costuma vir do secret com espaco, aspas ou quebra de linha coladas.
    chat = (os.environ.get("TELEGRAM_CHAT_ID") or "").strip().strip('"\'')
    if not token or not chat:
        raise RuntimeError(
            "TELEGRAM_TOKEN e/ou TELEGRAM_CHAT_ID ausentes. No GitHub: "
            "Settings > Secrets and variables > Actions. Local: exporte as variaveis.")
    try:
        _post(token, chat, text, "HTML")
    except RuntimeError as e:
        if "parse" not in str(e).lower():
            # "400 Bad Request" nao diz de quem e o token nem para onde ia
            # a mensagem. Ja perdi uma sessao inteira por isso: o Telegram
            # aceitava o envio e a mensagem chegava no chat de OUTRO bot.
            # getMe nao expoe o segredo e responde a pergunta certa.
            raise RuntimeError(f"{e} | {_quem_sou(token)} | chat_id {_mascara(chat)}") from None
        # Formatacao quebrada nao deve custar a mensagem: manda sem marcacao.
        print(f"[aviso] {e} — reenviando sem formatacao", file=sys.stderr)
        _post(token, chat, re.sub(r"<[^>]+>", "", text), None)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="nao notifica e nao grava o estado")
    ap.add_argument("--force-notify", action="store_true",
                    help="manda o resumo mesmo sem novidade (util para testar a ligacao)")
    ap.add_argument("--selftest", action="store_true",
                    help="so testa a ligacao com o Telegram e sai")
    ap.add_argument("--digest", action="store_true",
                    help="manda o retrato do mercado mesmo sem novidade (batimento semanal)")
    args = ap.parse_args()

    if args.selftest:
        # Testa a fiacao sozinha, sem depender do scraper nem do formato da mensagem.
        try:
            notify("Selftest do monitor: a ligacao com o Telegram esta viva.")
        except Exception as e:  # noqa: BLE001
            print(f"[ERRO] selftest falhou: {e}", file=sys.stderr)
            return 3
        print("[ok] selftest passou — mensagem enviada")
        return 0

    criteria = load_criteria()
    seen: dict[str, float] = {}
    if STATE_FILE.exists():
        seen = json.loads(STATE_FILE.read_text())

    listings: dict[str, Listing] = {}
    for url in FEED_URLS:
        try:
            raw_products = extract_products(fetch(url))
        except Exception as e:  # noqa: BLE001
            print(f"[erro] {url}: {e}", file=sys.stderr)
            continue
        for raw in raw_products:
            l = parse_listing(raw)
            if l:
                listings[l.part] = l

    if not listings:
        print("[erro] nenhum produto extraído — o formato da página provavelmente mudou.",
              file=sys.stderr)
        return 2

    hits = sorted((l for l in listings.values() if matches(l, criteria)),
                  key=lambda x: -x.score())

    # "Novo" = nunca visto no catalogo, nao apenas "novo entre os que passam".
    # Sem isso, afrouxar um criterio faria tudo parecer novidade de uma vez.
    # Rede de seguranca: o que cabe no bolso, ainda que falhe nos outros filtros.
    quase = sorted((l for l in listings.values()
                    if l.price <= criteria["max_price"] and l not in hits),
                   key=lambda x: -x.score())

    novos = [l for l in hits if l.part not in seen]
    baixou = [l for l in hits if l.part in seen and l.price < seen[l.part] - 0.01]

    print(f"{len(listings)} produtos lidos · {len(hits)} passam nos critérios · "
          f"{len(novos)} novos · {len(baixou)} baixaram de preço")
    for l in hits:
        print("  " + l.line())

    if novos or baixou or args.force_notify or args.digest:
        cambio = cotacao()
        cab = "<b>Monitor Apple Refurb</b>"
        if cambio:
            cab += "  <i>(US$ 1 = R$ " + f"{cambio:.2f}".replace(".", ",") + ")</i>"
        partes = [cab]
        for l in novos:
            partes.append(linha_item(l, criteria, cambio, "\U0001F195")
                          + f" · <i>{esc(why(l, criteria))}</i>")
        for l in baixou:
            antes = f"US$ {seen[l.part]:,.0f}".replace(",", ".")
            partes.append(linha_item(l, criteria, cambio, "\U0001F4C9")
                          + f" · <i>era {antes}</i>")
        if (args.force_notify or args.digest) and not novos and not baixou:
            # Execucao manual sem novidade: manda o retrato de agora, para
            # confirmar que a ligacao com o Telegram esta viva.
            partes.append(f"<i>Sem novidade. {len(listings)} produtos no catalogo, "
                          f"{len(hits)} passam nos criterios.</i>")
            for l in hits[:5]:
                partes.append(linha_item(l, criteria, cambio, "\u2022"))
            if not hits:
                # Criterio nenhum e sabio o bastante para ser a unica porta.
                # Sem isto, um estoque que rodou vira silencio e o silencio
                # parece "nada mudou" quando na verdade era "olha isto aqui".
                partes.append("<i>Nenhum item passa nos criterios. "
                              "Melhores abaixo do teto de preco:</i>")
                for l in quase[:5]:
                    partes.append(linha_item(l, criteria, cambio, "\u2022"))
        if cambio:
            # Sem esta linha o "pousado" vira um numero magico. Com ela,
            # da para conferir a conta e mudar as premissas no criteria.json.
            pct = lambda x: f"{x * 100:g}".replace(".", ",")
            partes.append(
                f"\n<i>Pousado = etiqueta + {pct(criteria['imposto_vendas_eua'])}% de imposto "
                f"na Florida + {pct(criteria['iof'])}% de IOF + "
                f"{pct(criteria['imposto_importacao'])}% sobre o que passa de "
                f"US$ {reais(criteria['isencao_usd'])} de isencao. "
                "Estimativa, nao promessa.</i>")

        if not args.dry_run:
            try:
                notify("\n".join(partes))
                print("[ok] notificacao enviada no Telegram")
            except Exception as e:  # noqa: BLE001
                print(f"[ERRO] falha ao notificar: {e}", file=sys.stderr)
                return 3

    if not args.dry_run:
        STATE_FILE.write_text(json.dumps(
            {l.part: l.price for l in listings.values()}, indent=2, sort_keys=True))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
