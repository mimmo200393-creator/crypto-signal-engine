"""
v41p1_scanner_runner.py
Entry point per Institutional Scanner V4.1 Phase 1
Money Flow & Intraday Edge Validation.
Eseguito dal workflow GitHub Actions in parallelo con V4.1.
"""
import os
import logging
import yaml
from core import v41p1_runner

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

config = yaml.safe_load(open("config.yaml"))
config["TELEGRAM_BOT_TOKEN"] = os.environ.get("TELEGRAM_BOT_TOKEN", "")
config["TELEGRAM_CHAT_ID"]   = os.environ.get("TELEGRAM_CHAT_ID", "")
config["NTFY_TOPIC"]         = os.environ.get("NTFY_TOPIC", "")

if config.get("V41P1_SCANNER", {}).get("enabled", False):
    v41p1_runner.run_v41p1_scan(config)
