"""
v41_scanner_runner.py
Script helper chiamato dal workflow GitHub Actions per
l'Institutional Scanner Framework V4.1 Intraday Wave Edition.

Isolato da main.py, v3_scanner_runner.py, v4_scanner_runner.py.
Deve essere eseguito DOPO v3_scanner_runner.py nello stesso ciclo,
perche' riusa le candele H1/M15 scaricate da quest'ultimo
(stessa tabella candles_cache/v3_candles_cache, evitando fetch
duplicati verso l'exchange).
"""
import os
import logging
import yaml
from core import v41_runner

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

config = yaml.safe_load(open("config.yaml"))
config["TELEGRAM_BOT_TOKEN"] = os.environ.get("TELEGRAM_BOT_TOKEN", "")
config["TELEGRAM_CHAT_ID"] = os.environ.get("TELEGRAM_CHAT_ID", "")
config["NTFY_TOPIC"] = os.environ.get("NTFY_TOPIC", "")

if config.get("V41_SCANNER", {}).get("enabled", False):
    v41_runner.run_v41_scan(config)
