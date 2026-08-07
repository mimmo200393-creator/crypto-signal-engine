"""
lh_digest_runner.py
Entry point per il riepilogo serale delle Restart Zone -- "Overnight
Trading Plan". Chiamato dal workflow GitHub Actions (scan.yml) alle 19:00 UTC.

Stesso pattern di daily_brief_runner.py.
"""
import os
import yaml
from core.lh_runner import send_zone_digest

config = yaml.safe_load(open("config.yaml"))
config["TELEGRAM_BOT_TOKEN"] = os.environ.get("TELEGRAM_BOT_TOKEN", "")
config["TELEGRAM_CHAT_ID"]   = os.environ.get("TELEGRAM_CHAT_ID", "")
config["NTFY_TOPIC"]         = os.environ.get("NTFY_TOPIC", "")

send_zone_digest(config)
