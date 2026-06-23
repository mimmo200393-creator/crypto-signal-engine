"""
candles_runner.py
Fetch incrementale candele H4/H1 per BTC_USDT e PAXG_USDT.

Sostituisce main.py nel workflow: scarica solo i dati di mercato
necessari a V4.1, V4.1 Phase 1 e Edge Lab, senza eseguire
nessuna strategia di trading.

Tabella target: candles_cache (stessa usata da V4.1 e Edge Lab)

Tempo atteso: ~3-5s vs 29s di main.py
"""

import os
import logging
import yaml

from storage import db
from core import exchange

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("candles_runner")

# Asset e timeframe necessari a V4.1 + Edge Lab
ASSETS     = ["BTC_USDT", "PAXG_USDT"]
TIMEFRAMES = {"H4": "4h", "H1": "1h"}


def run(config: dict):
    conn = db.get_connection(config["DB_PATH"])
    db.init_db(conn)  # crea candles_cache se non esiste

    base_url      = config["EXCHANGE_BASE_URL"]
    max_per_call  = config["MAX_CANDLES_PER_CALL"]
    delay         = config["REQUEST_DELAY_SECONDS"]
    target        = config["BOOTSTRAP_TARGET_CANDLES"]

    for asset in ASSETS:
        for tf_label, tf_code in TIMEFRAMES.items():
            existing = db.count_candles(conn, asset, tf_code)

            if existing < 50:
                # Bootstrap iniziale
                logger.info("Bootstrap %s %s...", asset, tf_code)
                candles = exchange.bootstrap_history(
                    base_url, asset, tf_code, target, max_per_call, delay
                )
                db.upsert_candles(conn, asset, tf_code, candles)
                logger.info(
                    "Bootstrap completato %s %s: %d candele",
                    asset, tf_code, len(candles)
                )
            else:
                # Aggiornamento incrementale
                last_ts = db.get_latest_timestamp(conn, asset, tf_code)
                new_candles = exchange.fetch_new_candles_since(
                    base_url, asset, tf_code, last_ts, max_per_call, delay
                )
                if new_candles:
                    db.upsert_candles(conn, asset, tf_code, new_candles)
                    logger.info(
                        "Update %s %s: +%d candele",
                        asset, tf_code, len(new_candles)
                    )
                else:
                    logger.info("Nessuna nuova candela %s %s", asset, tf_code)

    conn.close()
    logger.info("candles_runner completato.")


if __name__ == "__main__":
    config = yaml.safe_load(open("config.yaml"))
    run(config)
