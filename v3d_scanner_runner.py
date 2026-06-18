"""
v3d_scanner_runner.py
Script helper chiamato dal workflow GitHub Actions per
l'Institutional Scanner Framework V3 Dynamic.

Versione sperimentale con H4 come quality factor.
"""
import os
import logging
import yaml
from core import v3d_runner

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

config = yaml.safe_load(open("config.yaml"))
config["TELEGRAM_BOT_TOKEN"] = os.environ.get("TELEGRAM_BOT_TOKEN", "")
config["TELEGRAM_CHAT_ID"] = os.environ.get("TELEGRAM_CHAT_ID", "")
config["NTFY_TOPIC"] = os.environ.get("NTFY_TOPIC", "")

if config.get("V3D_SCANNER", {}).get("enabled", False):
    v3d_runner.run_v3d_scan(config)
