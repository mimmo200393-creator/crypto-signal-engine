"""
lh_scanner_runner.py
Entry point per Liquidity Hunter v1.0
Eseguito dal workflow GitHub Actions.
"""
import os
import logging
import yaml
from core import lh_runner

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

config = yaml.safe_load(open("config.yaml"))
config["TELEGRAM_BOT_TOKEN"] = os.environ.get("TELEGRAM_BOT_TOKEN", "")
config["TELEGRAM_CHAT_ID"]   = os.environ.get("TELEGRAM_CHAT_ID", "")
config["NTFY_TOPIC"]         = os.environ.get("NTFY_TOPIC", "")

lh_runner.run_lh_scan(config)
