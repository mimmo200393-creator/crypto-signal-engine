"""
ote_scanner_runner.py
Entry point per OTE Fase A — chiamato dal workflow GitHub Actions.
"""
import os
import logging
import yaml
from core import ote_runner

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

config = yaml.safe_load(open("config.yaml"))
config["TELEGRAM_BOT_TOKEN"] = os.environ.get("TELEGRAM_BOT_TOKEN", "")
config["TELEGRAM_CHAT_ID"] = os.environ.get("TELEGRAM_CHAT_ID", "")
config["NTFY_TOPIC"] = os.environ.get("NTFY_TOPIC", "")

ote_runner.run_ote_scan(config)
