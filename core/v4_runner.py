"""
core/v4_runner.py
Orchestratore di Institutional Scanner Framework V4.0 Daily Edition.

Riusa l'infrastruttura dati di v3_exchange/v3_db (stesse candele
D1/M30/M15 per PAXG_USDT e BTC_USDT, gia' aggiornate da v3_runner
nello stesso ciclo di scan) per evitare fetch duplicati verso
l'exchange. La logica di generazione segnali e il tracking sono
invece completamente separati da V3.2 (v4_signals vs v3_signals).

Punto di ingresso indipendente da main.py, signal_engine.py e v3_runner.py.
"""

import logging
from datetime import datetime, timezone

from storage import db as core_db
from core import indicators
from core import v3_db
from core import v4_db
from strategies import institutional_scanner_v4 as v4
from notifications import v4_telegram

logger = logging.getLogger("v4_runner")

V4_TIMEFRAMES = {"D1": "1D", "H4": "4h", "H1": "1h", "M30": "30m", "M15": "15m"}


def _prepare_dataframes(conn, asset: str, config: dict):
    """
    Carica H4/H1 dalle tabelle candles_cache esistenti, D1/M30/M15
    dalle tabelle v3_candles_cache (condivise con V3.2, niente fetch
    duplicato: si assume che v3_runner abbia gia' aggiornato i dati
    in questo stesso ciclo di scan).
    """
    limit = config.get("BOOTSTRAP_TARGET_CANDLES", 300)

    df_h4 = core_db.get_candles_df(conn, asset, V4_TIMEFRAMES["H4"], limit=limit)
    df_h1 = core_db.get_candles_df(conn, asset, V4_TIMEFRAMES["H1"], limit=limit)
    df_d1 = v3_db.get_v3_candles_df(conn, asset, V4_TIMEFRAMES["D1"], limit=limit)
    df_m30 = v3_db.get_v3_candles_df(conn, asset, V4_TIMEFRAMES["M30"], limit=limit)
    df_m15 = v3_db.get_v3_candles_df(conn, asset, V4_TIMEFRAMES["M15"], limit=limit)

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
    logger.info("V4 Scanner: inizio ciclo per %s", asset)

    df_d1, df_h4, df_h1, df_m30, df_m15 = _prepare_dataframes(conn, asset, config)

    if len(df_h4) < 15 or len(df_h1) < 35 or len(df_m30) < 20 or len(df_m15) < 10:
        logger.warning(
            "V4 Scanner [%s]: dati insufficienti (d1=%d h4=%d h1=%d m30=%d m15=%d), skip. "
            "(I dati vengono scaricati da V3 Scanner: eseguirlo prima nello stesso ciclo)",
            asset, len(df_d1), len(df_h4), len(df_h1), len(df_m30), len(df_m15)
        )
        return

    market_data = {
        "asset": asset,
        "df_d1": df_d1, "df_h4": df_h4, "df_h1": df_h1,
        "df_m30": df_m30, "df_m15": df_m15,
        "timestamp": datetime.now(timezone.utc),
    }

    result = v4.generate_v4_signal(market_data)
    signal = result["signal"]
    diagnostics = result["diagnostics"]

    logger.info(
        "V4 Scanner [%s] diagnostics: h4_structure=%s rejections=%s",
        asset, diagnostics.get("h4_structure"), diagnostics.get("rejections", [])
    )

    if signal is None:
        logger.info("V4 Scanner [%s]: nessun segnale generato in questo ciclo.", asset)
        return

    signal_id = v4_db.insert_v4_signal(conn, signal)
    logger.info(
        "V4 Scanner [%s]: SEGNALE generato [%s] quality=%.0f/5 (%s) RR=%.2f (id=%s)",
        asset, signal["direction"], signal["signal_quality"],
        signal.get("quality_label", "?"), signal["rr"], signal_id
    )

    bot_token = config.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = config.get("TELEGRAM_CHAT_ID", "")
    ntfy_topic = config.get("NTFY_TOPIC", "")
    if bot_token and chat_id:
        sent = v4_telegram.send_v4_signal_alert(bot_token, chat_id, signal)
        logger.info("V4 Scanner [%s]: notifica Telegram inviata=%s", asset, sent)
    if ntfy_topic:
        ntfy_sent = v4_telegram.send_v4_signal_alert_ntfy(ntfy_topic, signal)
        logger.info("V4 Scanner [%s]: notifica ntfy inviata=%s", asset, ntfy_sent)


def run_v4_scan(config: dict):
    """Entry point principale, chiamato dal workflow GitHub Actions."""
    conn = core_db.get_connection(config["DB_PATH"])
    v4_db.init_v4_schema(conn, "storage/v4_schema.sql")

    logger.info("=== V4 Scanner Daily Edition: inizio ciclo (PAXG_USDT + BTC_USDT) ===")

    assets = config.get("V4_SCANNER", {}).get("assets", v4.V4_ASSETS)

    for asset in assets:
        try:
            _run_for_asset(conn, asset, config)
        except Exception as e:
            logger.error("V4 Scanner [%s]: errore non gestito: %s", asset, e)

    conn.close()
    logger.info("=== V4 Scanner Daily Edition: fine ciclo ===")
