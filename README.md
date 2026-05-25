# alko
# Žufánek Drop Monitor

Monitoruje dropy a restocky na zufanek.cz. Posiela okamžité Telegram/Discord alerty.

## Setup (5 minút)

### 1. Prostredie
```bash
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Telegram Bot
1. Otvor Telegram, nájdi `@BotFather`
2. Pošli `/newbot`, daj mu meno
3. Skopíruj token
4. Nájdi `@userinfobot` — pošli mu správu, dá ti tvoje chat ID

### 3. Konfigurácia
```bash
cp .env.example .env
# Vyplň TELEGRAM_TOKEN a TELEGRAM_CHAT_ID
```

### 4. Spustenie
```bash
python monitor.py
```

## Produkčné nasadenie (VPS)

Pre spoľahlivý beh 24/7 nasaď na lacný VPS (Hetzner CX11 = ~4€/mesiac):

```bash
# systemd service
sudo nano /etc/systemd/system/zufanek-monitor.service
```

```ini
[Unit]
Description=Žufánek Drop Monitor
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/zufanek-monitor
ExecStart=/home/ubuntu/zufanek-monitor/venv/bin/python monitor.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable zufanek-monitor
sudo systemctl start zufanek-monitor
sudo journalctl -u zufanek-monitor -f  # logy
```

## Pridanie nových targetov

V `monitor.py` uprav `TARGETS`:

```python
TARGETS = [
    {
        "name": "Žufánek — Limitované edície",
        "url": "https://www.zufanek.cz/shop/limitovane-edice/",
        "type": "category",
    },
    {
        "name": "Konkrétny produkt",
        "url": "https://www.zufanek.cz/shop/nejaky-produkt/",
        "type": "product",
    },
    # Ľubovoľný iný Shoptet/custom eshop:
    {
        "name": "Veselý Drak — Pokémon",
        "url": "https://www.veselydrak.cz/kategorie/pokemon/",
        "type": "category",
    },
]
```

## Discord namiesto Telegramu

V `monitor.py` nahraď import:
```python
from discord_webhook import send_discord_alert
```
A v `check_target` nahraď `send_alert(...)` za `send_discord_alert(webhook_url, ...)`.

## Dôležité: CSS selektory

Pri prvom spustení skontroluj logy — ak vidíš `"Žiadne produkty nenájdené"`,
otvor DevTools na cieľovom webe a uprav selektory v `parse_category()`.

Žufánek aktuálne používa vlastný shop systém. Správne selektory nájdeš
v HTML zdrojovom kóde stránky (Ctrl+U).

## Interval kontroly

Predvolené: 30 sekúnd. Uprav `CHECK_INTERVAL_SECONDS` v `monitor.py`.

⚠️ Pod 15 sekúnd neodporúčam bez rotácie User-Agentov — zbytočne upozorňuješ server.