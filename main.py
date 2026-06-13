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

