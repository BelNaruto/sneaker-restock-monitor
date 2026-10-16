"""Sneaker Restock Monitor — new drops, restocks and price drops → Discord.

Watches KicksCrew collections (e.g. nike, jordan, yeezy) and/or keyword searches and posts
rich Discord embeds with the image, in-stock sizes and the price of every size.

Data: KicksCrew Sneakers Data API — https://rapidapi.com/belchiorarkad-FqvHs2EDOtP/api/kickscrew-sneakers-data
Support (fastest): Telegram @pintoflow

    pip install -r requirements.txt
    cp config.example.yaml config.yaml   # edit it
    RAPIDAPI_KEY=xxx DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/... python monitor.py
    python monitor.py --once             # single pass (cron / GitHub Actions)
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

import requests
import yaml

HOST = "kickscrew-sneakers-data.p.rapidapi.com"
BASE = f"https://{HOST}"
STATE_FILE = Path(os.getenv("STATE_FILE", "state.json"))
log = logging.getLogger("monitor")

COLORS = {"new": 0x3B82F6, "restock": 0x22C55E, "price_drop": 0xFF5A1F}
LABELS = {"new": "🆕 NEW PRODUCT", "restock": "✅ RESTOCK", "price_drop": "📉 PRICE DROP"}


@dataclass
class Product:
    id: str
    title: str
    vendor: str
    handle: str
    image: str
    sizes: dict[str, tuple[float, bool]] = field(default_factory=dict)  # "US 10" -> (price, in_stock)

    @property
    def url(self) -> str:
        return f"https://www.kickscrew.com/products/{self.handle}"

    @property
    def sku(self) -> str:
        m = re.search(r"[A-Z0-9]{5,7}-[0-9]{3}$", self.title)
        return m.group(0) if m else "—"

    def in_stock(self) -> dict[str, float]:
        return {s: p for s, (p, ok) in self.sizes.items() if ok and p > 0}

    def min_price(self) -> float | None:
        prices = list(self.in_stock().values())
        return min(prices) if prices else None


def us_size(label: str) -> str:
    m = re.search(r"US\s*([\d.]+[YCK]?)", label or "")
    return m.group(1) if m else (label or "?")


class Api:
    def __init__(self, key: str):
        self.s = requests.Session()
        self.s.headers.update({"x-rapidapi-key": key, "x-rapidapi-host": HOST})

    def get(self, path: str, **params) -> dict:
        for attempt in range(3):
            r = self.s.get(BASE + path, params=params, timeout=60)
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(2 * (attempt + 1))
                continue
            r.raise_for_status()
            return r.json()
        r.raise_for_status()
        return {}

    def collection(self, name: str, pages: int, per_page: int = 24) -> list[Product]:
        out = []
        for page in range(1, pages + 1):
            data = self.get("/product/bycollection/v2", collection=name, page=page, perPage=per_page, currency="USD")["data"]
            for p in data.get("products", []):
                prod = Product(str(p["id"]), p["title"], p.get("vendor", ""), p["handle"],
                               (p.get("images") or [{}])[0].get("src", ""))
                for v in p.get("variants", []):
                    prod.sizes[us_size(v.get("option2", ""))] = (float(v.get("price") or 0), bool(v.get("available")))
                out.append(prod)
            if not data.get("products"):
                break
        return out

    def keyword(self, query: str, limit: int = 8) -> list[Product]:
        res = self.get("/search/v2", query=query, hitsPerPage=limit, page=1, region="US", currency="USD")["data"]
        out = []
        for p in res.get("products", []):
            d = self.get("/product/description/byslugs", handle=p["handle"], country="US").get("data") or {}
            prod = Product(str(p["id"]), p["title"], p.get("vendor", ""), p["handle"], p.get("image_url", ""))
            for e in (d.get("variants") or {}).get("edges", []):
                n = e["node"]
                size = next((o["value"] for o in n["selectedOptions"] if o["name"] == "Size"), n.get("title", ""))
                prod.sizes[us_size(size)] = (float(n["price"]["amount"]), bool(n["availableForSale"]))
            out.append(prod)
        return out


def detect(prod: Product, prev: dict | None, cfg: dict) -> list[tuple[str, str]]:
    """Return [(event, detail)] by comparing with the previous snapshot."""
    wanted = set(map(str, cfg.get("sizes") or []))
    stock = {s: p for s, p in prod.in_stock().items() if not wanted or s in wanted}
    if prev is None:
        return [("new", f"{len(stock)} sizes in stock")] if cfg.get("notify_new", True) and stock else []
    events = []
    before = {s for s, (p, ok) in prev["sizes"].items() if ok and p > 0}
    back = sorted(set(stock) - before, key=lambda x: float(re.sub(r"[^\d.]", "", x) or 0))
    if back:
        events.append(("restock", "Back in stock: " + ", ".join(f"US {s}" for s in back)))
    old_min, new_min = prev.get("min"), min(stock.values()) if stock else None
    pct = float(cfg.get("price_drop_percent", 5))
    if old_min and new_min and new_min <= old_min * (1 - pct / 100):
        events.append(("price_drop", f"From ${old_min:,.2f} → **${new_min:,.2f}** (-{(1 - new_min / old_min) * 100:.0f}%)"))
    return events


def embed(prod: Product, event: str, detail: str) -> dict:
    stock = sorted(prod.in_stock().items(), key=lambda kv: float(re.sub(r"[^\d.]", "", kv[0]) or 0))
    cheapest = min(stock, key=lambda kv: kv[1]) if stock else None
    lines = [f"`US {s:<5}` ${p:,.2f}{'  ⭐' if cheapest and s == cheapest[0] else ''}" for s, p in stock[:24]]
    half = (len(lines) + 1) // 2
    fields = [
        {"name": "Brand", "value": prod.vendor or "—", "inline": True},
        {"name": "SKU", "value": prod.sku, "inline": True},
        {"name": "Lowest price", "value": f"${cheapest[1]:,.2f} (US {cheapest[0]})" if cheapest else "—", "inline": True},
    ]
    if lines:
        fields.append({"name": "Sizes & prices", "value": "\n".join(lines[:half]), "inline": True})
        if lines[half:]:
            fields.append({"name": "​", "value": "\n".join(lines[half:]), "inline": True})
    return {
        "author": {"name": LABELS[event]},
        "title": prod.title,
        "url": prod.url,
        "description": detail,
        "color": COLORS[event],
        "thumbnail": {"url": prod.image},
        "fields": fields,
        "footer": {"text": "Sneaker Restock Monitor · KicksCrew Sneakers Data API"},
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def notify(webhook: str | None, embeds: list[dict]) -> None:
    for e in embeds:
        log.info("%s | %s | %s", e["author"]["name"], e["title"], e["description"])
    if not webhook:
        return
    for i in range(0, len(embeds), 10):  # Discord: max 10 embeds per message
        r = requests.post(webhook, json={"username": "Restock Monitor", "embeds": embeds[i:i + 10]}, timeout=30)
        if r.status_code == 429:
            time.sleep(float(r.json().get("retry_after", 2)))
            requests.post(webhook, json={"embeds": embeds[i:i + 10]}, timeout=30)


def run_once(api: Api, cfg: dict, state: dict) -> dict:
    products: list[Product] = []
    for c in cfg.get("collections", []):
        products += api.collection(c, int(cfg.get("pages_per_collection", 2)))
    for q in cfg.get("keywords", []):
        products += api.keyword(q)

    keywords_filter = [k.lower() for k in cfg.get("title_filter", [])]
    embeds, first_run = [], not state
    for prod in products:
        if keywords_filter and not any(k in prod.title.lower() for k in keywords_filter):
            continue
        prev = state.get(prod.id)
        if not (first_run and cfg.get("silent_first_run", True)):
            for ev, detail in detect(prod, prev, cfg):
                embeds.append(embed(prod, ev, detail))
        state[prod.id] = {"title": prod.title, "min": prod.min_price(),
                          "sizes": {s: list(v) for s, v in prod.sizes.items()}, "seen": time.time()}
    notify(os.getenv("DISCORD_WEBHOOK_URL") or cfg.get("discord_webhook"), embeds)
    log.info("checked %d products, %d alerts", len(products), len(embeds))
    return state


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    cfg = yaml.safe_load(open(args.config, encoding="utf-8"))
    api = Api(os.environ["RAPIDAPI_KEY"])
    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    while True:
        try:
            state = run_once(api, cfg, state)
            STATE_FILE.write_text(json.dumps(state))
        except Exception as exc:  # keep the monitor alive
            log.exception("cycle failed: %s", exc)
        if args.once:
            break
        time.sleep(int(cfg.get("interval_seconds", 300)))


if __name__ == "__main__":
    main()
