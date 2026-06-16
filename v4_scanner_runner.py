"""
v4_scanner_runner.py
Script helper chiamato dal workflow GitHub Actions per
l'Institutional Scanner Framework V4.0 Daily Edition.

Isolato da main.py, v3_scanner_runner.py esistenti.
Deve essere eseguito DOPO v3_scanner_runner.py nello stesso ciclo,
perche' riusa le candele D1/M30/M15 scaricate da quest'ultimo
(stessa tabella v3_candles_cache, evitando fetch duplicati).
"""
import os
import yaml
from core import v4_runner

config = yaml.safe_load(open("config.yaml"))
config["TELEGRAM_BOT_TOKEN"] = os.environ.get("TELEGRAM_BOT_TOKEN", "")
config["TELEGRAM_CHAT_ID"] = os.environ.get("TELEGRAM_CHAT_ID", "")
config["NTFY_TOPIC"] = os.environ.get("NTFY_TOPIC", "")

if config.get("V4_SCANNER", {}).get("enabled", False):
    v4_runner.run_v4_scan(config)
