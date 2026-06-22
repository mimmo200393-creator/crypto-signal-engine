"""
edge_lab_runner.py
Entry point per Institutional Edge Lab — OTE-SC Phase 1A.
Chiamato dal workflow GitHub Actions (scan.yml).
"""

import os
import logging
import yaml
from core import edge_lab_runner

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

config = yaml.safe_load(open("config.yaml"))
config["TELEGRAM_BOT_TOKEN"] = os.environ.get("TELEGRAM_BOT_TOKEN", "")
config["TELEGRAM_CHAT_ID"]   = os.environ.get("TELEGRAM_CHAT_ID", "")
config["NTFY_TOPIC"]         = os.environ.get("NTFY_TOPIC", "")

edge_lab_runner.run_edge_lab_scan(config)
