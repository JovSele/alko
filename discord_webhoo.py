"""
Discord Webhook sender — náhrada za Telegram
Pre Cook Group adminov, ktorí chcú alerty priamo do Discord kanála.

Použitie: importuj send_discord_alert namiesto send_alert v monitor.py
"""

import httpx
from datetime import datetime
from typing import Optional


async def send_discord_alert(
    webhook_url: str,
    event: str,
    product_name: str,
    product_url: str,
    price: Optional[str],
    target_name: str,
) -> None:
    """
    Pošle Discord embed správu cez webhook.
    webhook_url získaš v Discord: Nastavenia kanála → Integrácie → Webhooky
    """

    color_map = {
        "new_available": 0x00FF7F,   # zelená
        "restock": 0x00BFFF,         # modrá
        "new_unavailable": 0x808080, # šedá
        "out_of_stock": 0xFF4444,    # červená
    }

    title_map = {
        "new_available": "🚨 NOVÝ PRODUKT — DOSTUPNÝ",
        "restock": "✅ RESTOCK DETECTED",
        "new_unavailable": "📦 Nový produkt (nedostupný)",
        "out_of_stock": "❌ Vypredané",
    }

    fields = [
        {"name": "Zdroj", "value": target_name, "inline": True},
        {"name": "Čas", "value": datetime.now().strftime("%H:%M:%S"), "inline": True},
    ]
    if price:
        fields.append({"name": "Cena", "value": price, "inline": True})

    payload = {
        "embeds": [
            {
                "title": title_map.get(event, "🔄 Zmena"),
                "description": f"**[{product_name}]({product_url})**",
                "color": color_map.get(event, 0xFFFFFF),
                "fields": fields,
                "footer": {"text": "Žufánek Monitor"},
                "timestamp": datetime.utcnow().isoformat(),
            }
        ]
    }

    async with httpx.AsyncClient() as client:
        resp = await client.post(webhook_url, json=payload, timeout=10.0)
        resp.raise_for_status()