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


def load_criteria() -> dict:
    if CRITERIA_FILE.exists():
        return json.loads(CRITERIA_FILE.read_text())
    return {"max_price": 1900, "min_ram_gb": 24, "min_bandwidth": 250,
            "blocked_chips": ["M3 Pro"]}


def matches(l: Listing, c: dict) -> bool:
    if l.price > c["max_price"]:
        return False
    if l.ram_gb < c["min_ram_gb"]:
        return False
    if l.bandwidth < c["min_bandwidth"]:
        return False
    if l.chip in c.get("blocked_chips", []):
        return False
    return True


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
            raise
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

    if novos or baixou or args.force_notify:
        partes = ["<b>Monitor Apple Refurb</b>"]
        for l in novos:
            partes.append(f'🆕 <a href="{esc(l.url)}">{esc(l.chip)} {l.ram_gb}GB</a> — '
                          f"US$ {l.price:,.0f} · {l.bandwidth} GB/s · score {l.score()}")
        for l in baixou:
            partes.append(f'📉 <a href="{esc(l.url)}">{esc(l.chip)} {l.ram_gb}GB</a> — '
                          f"US$ {seen[l.part]:,.0f} → <b>US$ {l.price:,.0f}</b>")
        if args.force_notify and not novos and not baixou:
            # Execucao manual sem novidade: manda o retrato de agora, para
            # confirmar que a ligacao com o Telegram esta viva.
            partes.append(f"<i>Sem novidade. {len(listings)} produtos no catalogo, "
                          f"{len(hits)} passam nos criterios.</i>")
            for l in hits[:5]:
                partes.append(f'• <a href="{esc(l.url)}">{esc(l.chip)} {l.ram_gb}GB</a> — '
                              f"US$ {l.price:,.0f} · {l.bandwidth} GB/s · score {l.score()}")
            if not hits:
                # Criterio nenhum e sabio o bastante para ser a unica porta.
                # Sem isto, um estoque que rodou vira silencio e o silencio
                # parece "nada mudou" quando na verdade era "olha isto aqui".
                partes.append("<i>Nenhum item passa nos criterios. "
                              "Melhores abaixo do teto de preco:</i>")
                for l in quase[:5]:
                    partes.append(f'• <a href="{esc(l.url)}">{esc(l.chip)} {l.ram_gb}GB '
                                  f"{l.ssd_gb}GB</a> — US$ {l.price:,.0f} · "
                                  f"{l.bandwidth} GB/s · score {l.score()}")
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
