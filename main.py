"""
main.py
Entry point del Crypto Signal Engine V1.0 (FROZEN).

Modalita' di esecuzione:
- Default: ESEGUE UN SINGOLO CICLO DI SCANSIONE e termina.
  Pensato per essere richiamato periodicamente da uno scheduler esterno
  (es. GitHub Actions cron, ogni SCAN_INTERVAL_MINUTES).
- Con --loop: esegue un loop continuo locale (utile per test/debug locali,
  NON usato in produzione su GitHub Actions).

Variabili d'ambiente (override su config.yaml, per evitare di committare
secrets nel repo):
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


def load_config(path="config.yaml") -> dict:
    with open(path, "r") as f:
        config = yaml.safe_load(f)

    # Override da variabili d'ambiente (per GitHub Actions Secrets)
    env_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    env_chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if env_token:
        config["TELEGRAM_BOT_TOKEN"] = env_token
    if env_chat_id:
        config["TELEGRAM_CHAT_ID"] = env_chat_id

    env_ntfy = os.environ.get("NTFY_TOPIC")
    if env_ntfy:
        config["NTFY_TOPIC"] = env_ntfy

    # Override TEMPORANEO soglia Telegram per test end-to-end con dati reali
    # (non modifica config.yaml, vale solo per questa esecuzione)
    env_threshold = os.environ.get("TELEGRAM_SCORE_THRESHOLD_OVERRIDE")
    if env_threshold:
        config["TELEGRAM_SCORE_THRESHOLD"] = int(env_threshold)
        config["DB_SCORE_THRESHOLD"] = min(config["DB_SCORE_THRESHOLD"], int(env_threshold))

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


def run_single_cycle(conn, config: dict, logger):
    logger.info("--- Inizio ciclo di scansione ---")
    signal_engine.run_scan_cycle(conn, config)
    logger.info("--- Fine ciclo di scansione ---")


def run_test_alert(config: dict, logger):
    """
    Invia un alert di TEST con dati finti (Telegram + ntfy), per
    verificare che formato/invio funzionino end-to-end. Non scrive
    nulla nel database.
    """
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
    label = "🔥 High Quality Setup (TEST)"

    sent = telegram_bot.send_alert(
        config["TELEGRAM_BOT_TOKEN"], config["TELEGRAM_CHAT_ID"],
        fake_setup, score, label
    )
    logger.info("Test alert Telegram inviato: %s", sent)

    sent_ntfy = ntfy_bot.send_alert(
        config.get("NTFY_TOPIC"), fake_setup, score, label
    )
    logger.info("Test alert ntfy inviato: %s", sent_ntfy)

    if not sent:
        sys.exit(1)


def main():
    config = load_config()
    setup_logging(config)
    logger = logging.getLogger("main")

    logger.info(
        "Avvio %s %s | watchlist=%d asset | scan_interval=%dmin",
        config["STRATEGY_NAME"], config["STRATEGY_VERSION"],
        len(config["WATCHLIST"]), config["SCAN_INTERVAL_MINUTES"]
    )

    conn = db.get_connection(config["DB_PATH"])
    db.init_db(conn)

    if os.environ.get("SEND_TEST_ALERT") == "true":
        logger.info("Modalita' TEST ALERT attiva: invio messaggio di test e termino.")
        run_test_alert(config, logger)
        return

    logger.info("Verifica bootstrap storico (solo se necessario)...")
    signal_engine.bootstrap_all(conn, config)
    logger.info("Bootstrap completato.")

    loop_mode = "--loop" in sys.argv

    if not loop_mode:
        # Modalita' one-shot: un ciclo e termina (GitHub Actions cron)
        try:
            run_single_cycle(conn, config, logger)
        except Exception:
            logger.exception("Errore durante il ciclo di scansione")
            sys.exit(1)
        return

    # Modalita' loop continuo (solo per test/debug locali)
    interval_seconds = config["SCAN_INTERVAL_MINUTES"] * 60
    while True:
        try:
            run_single_cycle(conn, config, logger)
        except Exception:
            logger.exception("Errore durante il ciclo di scansione")
        time.sleep(interval_seconds)


if __name__ == "__main__":
    main()
