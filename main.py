"""
main.py  (V2.1)
Entry point del Crypto Signal Engine Institutional Adaptive Framework V2.1.

Modalita':
- Default:  one-shot (GitHub Actions cron)
- --loop:   loop continuo (debug locale)

Secrets da env (GitHub Actions Secrets):
    TELEGRAM_BOT_TOKEN
    TELEGRAM_CHAT_ID
    NTFY_TOPIC
"""

import os
import sys
import logging
import time
import yaml

from storage import db
from core import signal_engine
from core.strategy_registry import build_registry


def load_config(path="config.yaml") -> dict:
    with open(path, "r") as f:
        config = yaml.safe_load(f)

    for env_key, cfg_key in [
        ("TELEGRAM_BOT_TOKEN", "TELEGRAM_BOT_TOKEN"),
        ("TELEGRAM_CHAT_ID",   "TELEGRAM_CHAT_ID"),
        ("NTFY_TOPIC",         "NTFY_TOPIC"),
    ]:
        val = os.environ.get(env_key)
        if val:
            config[cfg_key] = val

    env_threshold = os.environ.get("TELEGRAM_SCORE_THRESHOLD_OVERRIDE")
    if env_threshold:
        config["NOTIFY_FINAL_SCORE_THRESHOLD"] = int(env_threshold)
        config["TELEGRAM_SCORE_THRESHOLD"] = int(env_threshold)
        config["DB_SCORE_THRESHOLD"] = min(config.get("DB_SCORE_THRESHOLD", 8), int(env_threshold))

    return config


def setup_logging(config: dict):
    log_file = config.get("LOG_FILE", "logs/engine.log")
    os.makedirs(os.path.dirname(log_file), exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(log_file),
            logging.StreamHandler(),
        ],
    )


def run_test_alert(config: dict, logger):
    from notifications import telegram_bot, ntfy_bot

    fake_setup = {
        "asset": "BTC_USDT",
        "setup": "Pullback EMA Trend",
        "direzione": "LONG",
        "entry": 67450.23,
        "stop_loss": 66280.15,
        "take_profit": 69890.50,
        "rr": 2.08,
        "pullback_ema50": True,
        "pullback_ema21": False,
        "trend_h4_ok": True,
        "trend_h1_ok": True,
        "sr_level_present": True,
        "macro_event": None,
    }
    score = 9
    label = "🔥 High Quality Setup (TEST V2.1)"

    sent_tg = telegram_bot.send_alert(
        config["TELEGRAM_BOT_TOKEN"], config["TELEGRAM_CHAT_ID"],
        fake_setup, score, label
    )
    logger.info("Test alert Telegram: %s", sent_tg)

    sent_ntfy = ntfy_bot.send_alert(
        config.get("NTFY_TOPIC"), fake_setup, score, label
    )
    logger.info("Test alert ntfy: %s", sent_ntfy)

    if not sent_tg:
        sys.exit(1)


def main():
    config = load_config()
    setup_logging(config)
    logger = logging.getLogger("main")

    logger.info(
        "Avvio %s %s | strategie=%s | watchlist=%d asset",
        config.get("FRAMEWORK_NAME", "CryptoSignalEngine"),
        config.get("FRAMEWORK_VERSION", "V2.1"),
        [k for k, v in config.get("STRATEGIES", {}).items() if v.get("enabled")],
        len(config["WATCHLIST"]),
    )

    conn = db.get_connection(config["DB_PATH"])
    db.init_db(conn)

    if os.environ.get("SEND_TEST_ALERT") == "true":
        logger.info("TEST ALERT MODE")
        run_test_alert(config, logger)
        return

    registry = build_registry(config)

    logger.info("Bootstrap storico...")
    signal_engine.bootstrap_all(conn, config)
    logger.info("Bootstrap completato.")

    loop_mode = "--loop" in sys.argv

    if not loop_mode:
        try:
            logger.info("--- Inizio ciclo di scansione ---")
            signal_engine.run_scan_cycle(conn, config, registry)
            logger.info("--- Fine ciclo di scansione ---")
        except Exception:
            logger.exception("Errore nel ciclo di scansione")
            sys.exit(1)
        return

    interval = config["SCAN_INTERVAL_MINUTES"] * 60
    while True:
        try:
            logger.info("--- Inizio ciclo di scansione ---")
            signal_engine.run_scan_cycle(conn, config, registry)
            logger.info("--- Fine ciclo di scansione ---")
        except Exception:
            logger.exception("Errore nel ciclo di scansione")
        time.sleep(interval)


if __name__ == "__main__":
    main()
