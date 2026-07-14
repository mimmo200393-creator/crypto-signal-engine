"""
market_radar_scanner_runner.py
Entry point per il Market Radar V1 — chiamato dal workflow GitHub Actions.

Modellato sul wrapper edge_lab_runner.py della root: carica config.yaml,
inietta le env var, e chiama run_radar_scan().

MODALITA' V1: sola registrazione. Il radar scrive le Entry Zone e le loro
metriche (MAE/MFE, transizioni) nella tabella radar_zones di signals.db.
Nessun BUY/SELL. La validazione avviene dalla dashboard Radar Lab (beta).
"""
import os
import logging
import yaml
from core import market_radar_runner

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

config = yaml.safe_load(open("config.yaml"))
config["TELEGRAM_BOT_TOKEN"] = os.environ.get("TELEGRAM_BOT_TOKEN", "")
config["TELEGRAM_CHAT_ID"]   = os.environ.get("TELEGRAM_CHAT_ID", "")
config["NTFY_TOPIC"]         = os.environ.get("NTFY_TOPIC", "")

market_radar_runner.run_radar_scan(config)
