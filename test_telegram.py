"""
test_telegram.py
Test rapido di invio messaggio Telegram.
Esegui: python3 test_telegram.py
"""

import yaml
from notifications import telegram_bot

config = yaml.safe_load(open("config.yaml"))

ok = telegram_bot.send_message(
    config["TELEGRAM_BOT_TOKEN"],
    config["TELEGRAM_CHAT_ID"],
    "🚀 Crypto Signal Engine V1.0 - Test di connessione riuscito!"
)

print("Invio riuscito:", ok)
