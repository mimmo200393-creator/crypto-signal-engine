"""
signal_engine.py
Orchestratore principale: bootstrap storico, loop di scansione,
valutazione setup, gestione trade aperti, alert Telegram.
"""

import logging
from datetime import datetime, timezone

from core import exchange, indicators, strategy, scoring, macro, trade_manager
from storage import db
from notifications import telegram_bot

logger = logging.getLogger("signal_engine")


def bootstrap_all(conn, config: dict):
    """
    Per ogni asset/timeframe nella watchlist, se la cache candele e'
    vuota o insufficiente, esegue il bootstrap storico.
    """
    base_url = config["EXCHANGE_BASE_URL"]
    target = config["BOOTSTRAP_TARGET_CANDLES"]
    max_per_call = config["MAX_CANDLES_PER_CALL"]
    delay = config["REQUEST_DELAY_SECONDS"]

    for asset in config["WATCHLIST"]:
        for tf_label, tf_code in config["TIMEFRAMES"].items():
            existing = db.count_candles(conn, asset, tf_code)
            if existing >= target:
                logger.info("Bootstrap skip per %s %s (%d candele in cache)", asset, tf_code, existing)
                continue

            logger.info("Bootstrap in corso: %s %s ...", asset, tf_code)
            try:
                candles = exchange.bootstrap_history(
                    base_url, asset, tf_code, target, max_per_call, delay
                )
                db.upsert_candles(conn, asset, tf_code, candles)
            except exchange.ExchangeError as e:
                logger.warning(
                    "Bootstrap fallito per %s %s, asset SKIPPATO (probabilmente non listato "
                    "sull'exchange): %s", asset, tf_code, e
                )
                continue


def update_candles(conn, asset: str, config: dict):
    """
    Aggiorna le candele H1 e H4 per `asset` con eventuali nuove candele
    disponibili dall'exchange.

    Ritorna dict {"h1_new": [...], "h4_new": [...]} con le nuove candele
    (liste vuote se nessuna nuova candela).
    """
    base_url = config["EXCHANGE_BASE_URL"]
    max_per_call = config["MAX_CANDLES_PER_CALL"]
    delay = config["REQUEST_DELAY_SECONDS"]

    result = {}
    for tf_label, tf_code in config["TIMEFRAMES"].items():
        last_ts = db.get_latest_timestamp(conn, asset, tf_code)
        if last_ts is None:
            # non dovrebbe succedere dopo bootstrap, ma per sicurezza:
            new_candles = exchange.bootstrap_history(
                base_url, asset, tf_code, config["BOOTSTRAP_TARGET_CANDLES"],
                max_per_call, delay
            )
        else:
            new_candles = exchange.fetch_new_candles_since(
                base_url, asset, tf_code, last_ts, max_per_call, delay
            )

        if new_candles:
            db.upsert_candles(conn, asset, tf_code, new_candles)

        result[tf_label] = new_candles

    return result


def run_scan_cycle(conn, config: dict):
    """
    Esegue un singolo ciclo di scansione su tutta la watchlist:
