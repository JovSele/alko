"""
Žufánek Drop Monitor
Sleduje dostupnosť produktov na zufanek.cz a posiela Telegram alerty.
"""

import asyncio
import hashlib
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from telegram import Bot
from telegram.constants import ParseMode

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Konfigurácia ──────────────────────────────────────────────────────────────

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")  # Discord webhook cez Telegram bridge, alebo priamy chat ID

STATE_FILE = Path("state.json")
CHECK_INTERVAL_SECONDS = 30  # Ako často kontrolujeme (v sekundách)

# Zoznam URL stránok, ktoré monitorujeme
# Môžeš pridať ľubovoľné Žufánek produktové stránky
TARGETS = [
    {
        "name": "Žufánek — Všetky produkty",
        "url": "https://www.lepsinalada.cz/znacka/zufanek/",
        "type": "category",
    },
    {
        "name": "Žufánek — Limitované edície",
        "url": "https://www.lepsinalada.cz/limitovane-edice/",
        "type": "category",
    },
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "cs-CZ,cs;q=0.9,sk-SK;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


# ── Dátové štruktúry ──────────────────────────────────────────────────────────

@dataclass
class Product:
    name: str
    url: str
    price: Optional[str]
    available: bool
    hash: str  # hash celého produktového bloku — zachytí akúkoľvek zmenu


@dataclass
class TargetState:
    url: str
    products: dict[str, Product] = field(default_factory=dict)
    last_check: Optional[str] = None
    error_count: int = 0


# ── Parsovanie ────────────────────────────────────────────────────────────────

def parse_category(html: str, base_url: str) -> list[Product]:
    """
    Parsuje kategóriu produktov na lepsinalada.cz (Shoptet).
    Selektory overené na živom HTML 2026-05.
    """
    soup = BeautifulSoup(html, "lxml")
    products = []

    # Shoptet štruktúra: div.product > div.p[data-testid="productItem"]
    for card in soup.select("div.product div.p[data-testid='productItem']"):
        try:
            # Názov — <span data-micro="name">
            name_el = card.select_one("[data-micro='name'], [data-testid='productCardName']")
            name = name_el.get_text(strip=True) if name_el else "Neznámy produkt"

            # URL — <a class="name" href="...">
            link_el = card.select_one("a.name[href], a.image[href]")
            url = link_el["href"] if link_el else base_url
            if url.startswith("/"):
                url = "https://www.lepsinalada.cz" + url

            # Cena — data-micro-price atribút je najpresnejší
            offer_el = card.select_one("[data-micro='offer'][data-micro-price]")
            if offer_el:
                price = offer_el.get("data-micro-price", "") + " Kč"
            else:
                price_el = card.select_one(".price, .p-price, [class*='price']")
                price = price_el.get_text(strip=True) if price_el else None

            # Dostupnosť — Shoptet používa .availability span s textom a farbou
            raw_html = str(card)
            availability_el = card.select_one(".availability span, [data-micro-availability]")

            if availability_el:
                avail_text = availability_el.get_text(strip=True).lower()
                avail_schema = availability_el.get("data-micro-availability", "")
                available = (
                    "skladem" in avail_text
                    or "InStock" in avail_schema
                    or "https://schema.org/InStock" in avail_schema
                )
            else:
                # Fallback — hľadaj textové signály
                unavailable_signals = ["vyprodáno", "vypredané", "out of stock", "není skladem", "sold out"]
                available = not any(sig in raw_html.lower() for sig in unavailable_signals)
                buy_signals = ["přidat do košíku", "do košíka", "koupit"]
                if any(sig in raw_html.lower() for sig in buy_signals):
                    available = True

            # Hash — len relevantná časť (vynechaj rating ktorý sa mení)
            # Sledujeme: názov + URL + cena + dostupnosť
            stable_content = f"{name}|{url}|{price}|{available}"
            product_hash = hashlib.md5(stable_content.encode()).hexdigest()

            products.append(Product(
                name=name,
                url=url,
                price=price,
                available=available,
                hash=product_hash,
            ))

        except Exception as e:
            log.warning(f"Chyba pri parsovaní produktu: {e}")
            continue

    return products


def parse_product_page(html: str, url: str, name: str) -> Optional[Product]:
    """Parsuje jednotlivú produktovú stránku."""
    soup = BeautifulSoup(html, "lxml")
    raw = html.lower()

    unavailable_signals = [
        "vyprodáno", "vypredané", "out of stock",
        "nedostupné", "není skladem", "sold out",
    ]
    available = not any(sig in raw for sig in unavailable_signals)

    buy_signals = ["přidat do košíku", "do košíka", "koupit", "kúpiť"]
    if any(sig in raw for sig in buy_signals):
        available = True

    price_el = soup.select_one(".price, .product-price, [itemprop='price']")
    price = price_el.get_text(strip=True) if price_el else None

    return Product(
        name=name,
        url=url,
        price=price,
        available=available,
        hash=hashlib.md5(html.encode()).hexdigest(),
    )


# ── State management ──────────────────────────────────────────────────────────

def load_state() -> dict[str, TargetState]:
    if not STATE_FILE.exists():
        return {}
    try:
        raw = json.loads(STATE_FILE.read_text())
        result = {}
        for url, data in raw.items():
            products = {
                k: Product(**v)
                for k, v in data.get("products", {}).items()
            }
            result[url] = TargetState(
                url=url,
                products=products,
                last_check=data.get("last_check"),
                error_count=data.get("error_count", 0),
            )
        return result
    except Exception as e:
        log.error(f"Chyba pri načítaní stavu: {e}")
        return {}


def save_state(state: dict[str, TargetState]) -> None:
    data = {}
    for url, ts in state.items():
        data[url] = {
            "products": {
                k: {
                    "name": p.name,
                    "url": p.url,
                    "price": p.price,
                    "available": p.available,
                    "hash": p.hash,
                }
                for k, p in ts.products.items()
            },
            "last_check": ts.last_check,
            "error_count": ts.error_count,
        }
    STATE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2))


# ── Telegram notifikácie ──────────────────────────────────────────────────────

async def send_alert(bot: Bot, event: str, product: Product, target_name: str) -> None:
    """Pošle Telegram správu pri detekcii zmeny."""

    if event == "new_available":
        emoji = "🚨"
        title = "NOVÝ PRODUKT — DOSTUPNÝ"
    elif event == "restock":
        emoji = "✅"
        title = "RESTOCK DETECTED"
    elif event == "new_unavailable":
        emoji = "📦"
        title = "Nový produkt (nedostupný)"
    elif event == "out_of_stock":
        emoji = "❌"
        title = "Vypredané"
    else:
        emoji = "🔄"
        title = "Zmena"

    price_line = f"💰 *Cena:* {product.price}" if product.price else ""

    # Direktný link na košík — ak Žufánek podporuje query param
    # Inak fallback na produktovú stránku
    cart_url = product.url  # uprav na ?add-to-cart=ID ak vieš ID

    msg = (
        f"{emoji} <b>{title}</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🏷️ <b>{product.name}</b>\n"
        f"📍 <i>{target_name}</i>\n"
        f"{price_line}\n"
        f"⏰ {datetime.now().strftime('%H:%M:%S')}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f'<a href="{cart_url}">👉 KÚPIŤ TERAZ</a>'
    )

    try:
        await bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=msg,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=False,
        )
        log.info(f"Alert odoslaný: {event} — {product.name}")
    except Exception as e:
        log.error(f"Chyba pri odosielaní Telegram správy: {e}")


# ── Hlavná logika ─────────────────────────────────────────────────────────────

async def check_target(
    client: httpx.AsyncClient,
    bot: Bot,
    target: dict,
    state: dict[str, TargetState],
) -> None:
    url = target["url"]
    name = target["name"]

    if url not in state:
        state[url] = TargetState(url=url)

    ts = state[url]

    try:
        resp = await client.get(url, headers=HEADERS, timeout=15.0, follow_redirects=True)
        resp.raise_for_status()
        ts.error_count = 0
    except Exception as e:
        ts.error_count += 1
        log.warning(f"[{name}] Fetch error #{ts.error_count}: {e}")
        return

    ts.last_check = datetime.now().isoformat()

    # Parsovanie podľa typu targetu
    if target["type"] == "category":
        current_products = {
            p.url: p
            for p in parse_category(resp.text, url)
        }
    else:
        parsed = parse_product_page(resp.text, url, name)
        current_products = {url: parsed} if parsed else {}

    if not current_products:
        log.warning(f"[{name}] Žiadne produkty nenájdené — skontroluj CSS selektory")

    prev_products = ts.products

    # Prvý beh — len uložíme stav, neposielame alerty (aby sme nefloodili)
    first_run = not prev_products

    for prod_url, product in current_products.items():
        prev = prev_products.get(prod_url)

        if first_run:
            log.info(f"[{name}] Inicializácia: {product.name} — {'✅' if product.available else '❌'}")
            continue

        if prev is None:
            # Nový produkt
            event = "new_available" if product.available else "new_unavailable"
            log.info(f"[{name}] Nový produkt: {product.name}")
            await send_alert(bot, event, product, name)

        elif prev.hash != product.hash:
            # Zmena na produkte
            if not prev.available and product.available:
                log.info(f"[{name}] RESTOCK: {product.name}")
                await send_alert(bot, "restock", product, name)
            elif prev.available and not product.available:
                log.info(f"[{name}] Vypredané: {product.name}")
                # await send_alert(bot, "out_of_stock", product, name)
                # ^ Odkomentuj ak chceš aj "vypredané" notifikácie

    ts.products = current_products
    save_state(state)


async def run_checks(
    client: httpx.AsyncClient,
    bot: Bot,
    state: dict[str, TargetState],
) -> None:
    log.info(f"Spúšťam kontrolu {len(TARGETS)} targetov...")
    tasks = [check_target(client, bot, t, state) for t in TARGETS]
    await asyncio.gather(*tasks)


# ── Entry point ───────────────────────────────────────────────────────────────

async def main() -> None:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        raise ValueError(
            "Chýba TELEGRAM_TOKEN alebo TELEGRAM_CHAT_ID v .env súbore"
        )

    state = load_state()
    bot = Bot(token=TELEGRAM_TOKEN)
    scheduler = AsyncIOScheduler()

    async with httpx.AsyncClient() as client:
        # Prvý beh ihneď
        await run_checks(client, bot, state)

        # Pravidelné kontroly
        scheduler.add_job(
            run_checks,
            "interval",
            seconds=CHECK_INTERVAL_SECONDS,
            args=[client, bot, state],
            max_instances=1,  # nikdy nespúšťaj 2 súbežné kontroly
        )
        scheduler.start()

        log.info(f"Monitor beží. Interval: {CHECK_INTERVAL_SECONDS}s. Ctrl+C na zastavenie.")
        try:
            while True:
                await asyncio.sleep(1)
        except (KeyboardInterrupt, SystemExit):
            scheduler.shutdown()
            log.info("Monitor zastavený.")


if __name__ == "__main__":
    asyncio.run(main())