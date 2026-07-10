"""
candles_runner.py
Fetch incrementale candele H4/H1.

Sostituisce main.py nel workflow: scarica solo i dati di mercato
necessari a V4.1, V4.1 Phase 1 e Edge Lab, senza eseguire
nessuna strategia di trading.

Tabella target: candles_cache (stessa usata da V4.1 e Edge Lab)

── MULTI-PROVIDER (nuovo) ────────────────────────────────────────
Gli asset non arrivano piu' dallo stesso exchange:
    BTC_USDT  -> Crypto.com   (core.exchange)
    XAU_USD   -> Twelve Data  (core.exchange_twelvedata)
Il modulo core.data_source sceglie il provider e applica la cadenza
di fetch per non sforare il budget chiamate dei provider a consumo.

── ASSET DA CONFIG (fix) ─────────────────────────────────────────
Prima gli asset erano hardcoded qui, mentre i runner delle strategie
li leggevano da config.yaml: bastava dimenticarne uno per avere un
asset senza candele (o candele di un asset non piu' operativo).
Ora config.yaml e' l'unica fonte di verita': chiave ASSETS.
"""

import os
import logging
import yaml

from storage import db
from core import data_source

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("candles_runner")

# Fallback se ASSETS manca in config (retrocompatibilita')
DEFAULT_ASSETS = ["BTC_USDT"]
TIMEFRAMES = {"H4": "4h", "H1": "1h"}


def run(config: dict):
    conn = db.get_connection(config["DB_PATH"])
    db.init_db(conn)  # crea candles_cache se non esiste

    base_url      = config["EXCHANGE_BASE_URL"]
    max_per_call  = config["MAX_CANDLES_PER_CALL"]
    delay         = config["REQUEST_DELAY_SECONDS"]
    target        = config["BOOTSTRAP_TARGET_CANDLES"]

    assets = config.get("ASSETS", DEFAULT_ASSETS)
    logger.info("candles_runner: asset attivi = %s", ", ".join(assets))

    for asset in assets:
        # Provider giusto per questo asset (Crypto.com o Twelve Data)
        exchange = data_source.get_provider(asset, family="main")

        for tf_label, tf_code in TIMEFRAMES.items():
            existing = db.count_candles(conn, asset, tf_code)

            if existing < 50:
                # Bootstrap iniziale
                logger.info("Bootstrap %s %s...", asset, tf_code)
                try:
                    candles = exchange.bootstrap_history(
                        base_url, asset, tf_code, target, max_per_call, delay
                    )
                except Exception as e:
                    logger.error("Bootstrap %s %s fallito: %s", asset, tf_code, e)
                    continue
                db.upsert_candles(conn, asset, tf_code, candles)
                logger.info(
                    "Bootstrap completato %s %s: %d candele",
                    asset, tf_code, len(candles)
                )
            else:
                # Aggiornamento incrementale
                last_ts = db.get_latest_timestamp(conn, asset, tf_code)

                # Cadenza: sui provider a consumo evita chiamate inutili
                if not data_source.should_fetch(asset, tf_code, last_ts):
                    logger.info(
                        "Skip fetch %s %s (cadenza non raggiunta)", asset, tf_code
                    )
                    continue

                try:
                    new_candles = exchange.fetch_new_candles_since(
                        base_url, asset, tf_code, last_ts, max_per_call, delay
                    )
                except Exception as e:
                    logger.error("Update %s %s fallito: %s", asset, tf_code, e)
                    continue

                if new_candles:
                    db.upsert_candles(conn, asset, tf_code, new_candles)
                    logger.info(
                        "Update %s %s: +%d candele",
                        asset, tf_code, len(new_candles)
                    )
                else:
                    # Per XAU_USD e' normale: mercato chiuso (weekend/pausa)
                    logger.info("Nessuna nuova candela %s %s", asset, tf_code)

    conn.close()
    logger.info("candles_runner completato.")


if __name__ == "__main__":
    config = yaml.safe_load(open("config.yaml"))
    run(config)
