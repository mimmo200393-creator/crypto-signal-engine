"""
daily_brief_runner.py
Script helper chiamato dal workflow GitHub Actions per il Daily Brief.
Sostituisce il python3 -c inline che causava problemi YAML.
"""
import os
import yaml
from storage import db
from core import daily_brief

config = yaml.safe_load(open("config.yaml"))
config["TELEGRAM_BOT_TOKEN"] = os.environ.get("TELEGRAM_BOT_TOKEN", "")
config["TELEGRAM_CHAT_ID"] = os.environ.get("TELEGRAM_CHAT_ID", "")
config["NTFY_TOPIC"] = os.environ.get("NTFY_TOPIC", "")

conn = db.get_connection(config["DB_PATH"])
db.init_db(conn)
daily_brief.send_daily_brief(conn, config)
