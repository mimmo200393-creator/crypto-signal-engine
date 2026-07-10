"""
core/v3_runner.py
Orchestratore dell'Institutional Scanner Framework V3.2.

Punto di ingresso separato da main.py / signal_engine.py esistenti.
Ad ogni esecuzione, per ciascun asset in V3_ASSETS (PAXG_USDT, BTC_USDT):
    1. Aggiorna le candele D1/M30/M15 (v3_exchange)
    2. Carica H4/H1 dalle tabelle candles_cache esistenti (riuso dati gia' scaricati)
    3. Calcola indicatori (EMA/ATR) sui 5 timeframe
    4. Esegue la pipeline Trend->Pullback->Transition->Continuation->Execution
    5. Se generato un segnale valido, lo salva e notifica via Telegram e ntfy

PAXG_USDT e BTC_USDT sono valutati e tracciati in modo indipendente.
Non importa né modifica core/signal_engine.py.
"""

import logging
from datetime import datetime, timezone

from storage import db as core_db
from core import indicators
from core import data_source
from core import v3_db
from strategies import institutional_scanner_v3 as v3
from notifications import v3_telegram

logger = logging.getLogger("v3_runner")

V3_TIMEFRAMES = {"D1": "1D", "H4": "4h", "H1": "1h", "M30": "30m", "M15": "15m"}


def _update_v3_candles(conn, asset: str, config: dict):
    """
    Bootstrap o aggiornamento incrementale per D1/M30/M15 di un asset.

    Multi-provider:
        BTC_USDT -> Crypto.com
        XAU_USD  -> Twelve Data
    """
    base_url = config["EXCHANGE_BASE_URL"]
    max_per_call = config.get("MAX_CANDLES_PER_CALL", 300)
    request_delay = config.get("REQUEST_DELAY_SECONDS", 0.75)
    target_candles = config.get("BOOTSTRAP_TARGET_CANDLES", 300)

    exchange = data_source.get_provider(asset, family="v3")

    for tf_label in ("D1", "M30", "M15"):
        tf = V3_TIMEFRAMES[tf_label]
        existing_count = v3_db.count_v3_candles(conn, asset, tf)

        if existing_count < 50:
            logger.info("V3 bootstrap storico per %s %s...", asset, tf)
            try:
                candles = exchange.bootstrap_history(
                    base_url, asset, tf, target_candles, max_per_call, request_delay
                )
            except Exception as e:
                logger.error("V3 bootstrap %s %s fallito: %s", asset, tf, e)
                continue
            v3_db.upsert_v3_candles(conn, asset, tf, candles)
            logger.info("V3 bootstrap completato: %s %s (%d candele)", asset, tf, len(candles))
        else:
            last_ts = v3_db.get_v3_latest_timestamp(conn, asset, tf)

            if not data_source.should_fetch(asset, tf, last_ts):
                logger.info("V3 skip fetch %s %s (cadenza non raggiunta)", asset, tf)
                continue

            try:
                new_candles = exchange.fetch_new_candles_since(
                    base_url, asset, tf, last_ts, max_per_call, request_delay
                )
            except Exception as e:
                logger.error("V3 update %s %s fallito: %s", asset, tf, e)
                continue

            if new_candles:
                v3_db.upsert_v3_candles(conn, asset, tf, new_candles)
                logger.info("V3 update: +%d candele %s %s", len(new_candles), asset, tf)
            else:
                logger.info("V3 update: nessuna nuova candela %s %s", asset, tf)


def _prepare_dataframes(conn, asset: str, config: dict):
    """Carica H4/H1 dalle tabelle esistenti, D1/M30/M15 dalle tabelle v3 dedicate."""
    limit = config.get("BOOTSTRAP_TARGET_CANDLES", 300)

    df_h4 = core_db.get_candles_df(conn, asset, V3_TIMEFRAMES["H4"], limit=limit)
    df_h1 = core_db.get_candles_df(conn, asset, V3_TIMEFRAMES["H1"], limit=limit)
    df_d1 = v3_db.get_v3_candles_df(conn, asset, V3_TIMEFRAMES["D1"], limit=limit)
    df_m30 = v3_db.get_v3_candles_df(conn, asset, V3_TIMEFRAMES["M30"], limit=limit)
    df_m15 = v3_db.get_v3_candles_df(conn, asset, V3_TIMEFRAMES["M15"], limit=limit)

    ema_periods = config.get("EMA_PERIODS", [21, 50, 100, 200])
    atr_period = config.get("ATR_PERIOD", 14)

    for df in (df_h4, df_h1, df_d1, df_m30, df_m15):
        if len(df) > atr_period:
            indicators.add_atr(df, atr_period)

    if len(df_h4) > max(ema_periods):
        indicators.add_emas(df_h4, ema_periods)
    if len(df_h1) > max(ema_periods):
        indicators.add_emas(df_h1, ema_periods)
    if len(df_d1) > 200:
        indicators.add_emas(df_d1, [50, 200])

    return df_d1, df_h4, df_h1, df_m30, df_m15


def _run_for_asset(conn, asset: str, config: dict):
    logger.info("V3 Scanner: inizio ciclo per %s", asset)

    try:
        _update_v3_candles(conn, asset, config)
    except Exception as e:
        logger.error("V3 Scanner [%s]: errore durante update candele: %s", asset, e)
        return

    df_d1, df_h4, df_h1, df_m30, df_m15 = _prepare_dataframes(conn, asset, config)

    if len(df_h4) < 15 or len(df_h1) < 35 or len(df_m30) < 20 or len(df_m15) < 10:
        logger.warning(
            "V3 Scanner [%s]: dati insufficienti (d1=%d h4=%d h1=%d m30=%d m15=%d), skip.",
            asset, len(df_d1), len(df_h4), len(df_h1), len(df_m30), len(df_m15)
        )
        return

    market_data = {
        "asset": asset,
        "df_d1": df_d1, "df_h4": df_h4, "df_h1": df_h1,
        "df_m30": df_m30, "df_m15": df_m15,
        "timestamp": datetime.now(timezone.utc),
    }

    result = v3.generate_v3_signal(market_data)
    signal = result["signal"]
    diagnostics = result["diagnostics"]

    logger.info(
        "V3 Scanner [%s] diagnostics: h4_structure=%s daily_context=%s rejections=%s",
        asset, diagnostics.get("h4_structure"), diagnostics.get("daily_context"),
        diagnostics.get("rejections", [])
    )

    if signal is None:
        logger.info("V3 Scanner [%s]: nessun segnale generato in questo ciclo.", asset)
        return

    signal_id = v3_db.insert_v3_signal(conn, signal)
    logger.info(
        "V3 Scanner [%s]: SEGNALE generato [%s] score=%.0f/9 RR=%.2f (id=%s)",
        asset, signal["direction"], signal["signal_quality"], signal["rr"], signal_id
    )

    bot_token = config.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = config.get("TELEGRAM_CHAT_ID", "")
    ntfy_topic = config.get("NTFY_TOPIC", "")
    if bot_token and chat_id:
        sent = v3_telegram.send_v3_signal_alert(bot_token, chat_id, signal)
        logger.info("V3 Scanner [%s]: notifica Telegram inviata=%s", asset, sent)
    if ntfy_topic:
        ntfy_sent = v3_telegram.send_v3_signal_alert_ntfy(ntfy_topic, signal)
        logger.info("V3 Scanner [%s]: notifica ntfy inviata=%s", asset, ntfy_sent)


def run_v3_scan(config: dict):
    """Entry point principale, chiamato dal workflow GitHub Actions."""
    conn = core_db.get_connection(config["DB_PATH"])
    v3_db.init_v3_schema(conn, "storage/v3_schema.sql")

    assets = config.get("V3_SCANNER", {}).get("assets", v3.V3_ASSETS)

    logger.info("=== V3 Scanner: inizio ciclo completo (%s) ===", ", ".join(assets))

    for asset in assets:
        try:
            _run_for_asset(conn, asset, config)
        except Exception as e:
            logger.error("V3 Scanner [%s]: errore non gestito: %s", asset, e)

    conn.close()
    logger.info("=== V3 Scanner: fine ciclo completo ===")
