"""
core/v41_runner.py
Orchestratore di Institutional Scanner Framework V4.1 Intraday Wave Edition.

Riusa l'infrastruttura dati di v3_db (candele H4/H1 da candles_cache
esistente, M15 da v3_candles_cache, gia' aggiornate da v3_runner nello
stesso ciclo di scan) per evitare fetch duplicati verso l'exchange.
La logica di generazione segnali e il tracking sono completamente
separati da V3.2 e V4.0 (v41_signals e' una tabella isolata).

Punto di ingresso indipendente da main.py, v3_runner.py, v4_runner.py.
"""

import logging
from datetime import datetime, timezone

from storage import db as core_db
from core import indicators, macro
from core import v3_db
from core import v41_db
from strategies import institutional_scanner_v41 as v41
from notifications import v41_telegram

logger = logging.getLogger("v41_runner")

V41_TIMEFRAMES = {"H4": "4h", "H1": "1h", "M15": "15m"}


def _prepare_dataframes(conn, asset: str, config: dict):
    """
    Carica H4/H1 dalle tabelle candles_cache esistenti, M15 dalla
    tabella v3_candles_cache (condivisa con V3.2/V4.0, niente fetch
    duplicato: si assume che v3_runner abbia gia' aggiornato i dati
    in questo stesso ciclo di scan).
    """
    limit = config.get("BOOTSTRAP_TARGET_CANDLES", 300)

    df_h4 = core_db.get_candles_df(conn, asset, V41_TIMEFRAMES["H4"], limit=limit)
    df_h1 = core_db.get_candles_df(conn, asset, V41_TIMEFRAMES["H1"], limit=limit)
    df_m15 = v3_db.get_v3_candles_df(conn, asset, V41_TIMEFRAMES["M15"], limit=limit)
    df_d1 = v3_db.get_v3_candles_df(conn, asset, "1D", limit=30)

    ema_periods = config.get("EMA_PERIODS", [21, 50, 100, 200])
    atr_period = config.get("ATR_PERIOD", 14)

    for df in (df_h4, df_h1, df_m15):
        if len(df) > atr_period:
            indicators.add_atr(df, atr_period)

    if len(df_h4) > max(ema_periods):
        indicators.add_emas(df_h4, ema_periods)
    if len(df_h1) > max(ema_periods):
        indicators.add_emas(df_h1, ema_periods)

    return df_h4, df_h1, df_m15, df_d1


def _check_watchlist(conn, asset: str, df_h4, df_d1, df_m15, now: datetime, config: dict):
    """
    Valuta i livelli di liquidità in prossimità del prezzo attuale e
    genera un Watchlist Alert solo sulla transizione fuori -> dentro
    fascia per ciascun livello, per evitare notifiche ripetute mentre
    il prezzo resta nella stessa zona.
    """
    if len(df_m15) < 1:
        return

    current_price = float(df_m15.iloc[-1]["close"])
    liquidity_map = v41.build_liquidity_map(df_h4, df_d1)
    proximities = v41.find_watchlist_proximities(liquidity_map, current_price)

    proximities_by_label = {p["label"]: p for p in proximities}
    timestamp_str = now.isoformat()

    # Livelli ora in prossimità: controllo la transizione
    for label, proximity in proximities_by_label.items():
        was_inside = v41_db.get_watchlist_state(conn, asset, label)
        if not was_inside:
            alert_id = v41_db.insert_watchlist_alert(conn, asset, proximity, timestamp_str)
            logger.info(
                "V4.1 Scanner [%s]: WATCHLIST ALERT [%s] livello=%s distanza=%.3f%% potential=%s (id=%s)",
                asset, asset, label, proximity["distance_pct"] * 100,
                proximity["potential_direction"], alert_id
            )
            bot_token = config.get("TELEGRAM_BOT_TOKEN", "")
            chat_id = config.get("TELEGRAM_CHAT_ID", "")
            if bot_token and chat_id:
                sent = v41_telegram.send_v41_watchlist_alert(bot_token, chat_id, asset, proximity)
                logger.info("V4.1 Scanner [%s]: notifica Watchlist Telegram inviata=%s", asset, sent)
        v41_db.set_watchlist_state(conn, asset, label, True, timestamp_str)

    # Livelli che NON sono più in prossimità: reset dello stato a False,
    # cosi' un futuro rientro genera un nuovo Watchlist Alert
    all_level_labels = {lv["label"] for lv in liquidity_map["levels"]}
    for label in all_level_labels:
        if label not in proximities_by_label:
            was_inside = v41_db.get_watchlist_state(conn, asset, label)
            if was_inside:
                v41_db.set_watchlist_state(conn, asset, label, False, timestamp_str)


def _run_for_asset(conn, asset: str, config: dict, macro_provider, now: datetime):
    logger.info("V4.1 Scanner: inizio ciclo per %s", asset)

    df_h4, df_h1, df_m15, df_d1 = _prepare_dataframes(conn, asset, config)

    if len(df_h4) < 15 or len(df_h1) < 20 or len(df_m15) < 25:
        logger.warning(
            "V4.1 Scanner [%s]: dati insufficienti (h4=%d h1=%d m15=%d), skip. "
            "(I dati vengono scaricati da V3 Scanner: eseguirlo prima nello stesso ciclo)",
            asset, len(df_h4), len(df_h1), len(df_m15)
        )
        return

    # Watchlist Alert: valutato indipendentemente dal trade alert,
    # serve a preparare il trader anche quando il trigger operativo
    # (BOS/CHOCH) non è ancora scattato.
    try:
        _check_watchlist(conn, asset, df_h4, df_d1, df_m15, now, config)
    except Exception as e:
        logger.error("V4.1 Scanner [%s]: errore Watchlist non gestito: %s", asset, e)

    market_data = {
        "asset": asset,
        "df_h4": df_h4, "df_h1": df_h1, "df_m15": df_m15, "df_d1": df_d1,
        "timestamp": now,
        "macro_provider": macro_provider,
    }

    result = v41.generate_v41_signal(market_data)
    signal = result["signal"]
    diagnostics = result["diagnostics"]

    logger.info(
        "V4.1 Scanner [%s] diagnostics: trigger_found=%s trigger_types=%s rejections=%s",
        asset, diagnostics.get("trigger_found"), diagnostics.get("trigger_types"),
        diagnostics.get("rejections", [])
    )

    if signal is None:
        logger.info("V4.1 Scanner [%s]: nessun alert in questo ciclo.", asset)
        return

    signal_id = v41_db.insert_v41_signal(conn, signal)
    logger.info(
        "V4.1 Scanner [%s]: ALERT generato [%s] trigger=%s quality=%d/12 (%s) (id=%s)",
        asset, signal["direction"], signal["trigger_types"],
        signal["quality_score"], signal["quality_label"], signal_id
    )

    bot_token = config.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = config.get("TELEGRAM_CHAT_ID", "")
    if bot_token and chat_id:
        sent = v41_telegram.send_v41_signal_alert(bot_token, chat_id, signal)
        logger.info("V4.1 Scanner [%s]: notifica Telegram inviata=%s", asset, sent)


def run_v41_scan(config: dict):
    """Entry point principale, chiamato dal workflow GitHub Actions."""
    conn = core_db.get_connection(config["DB_PATH"])
    v41_db.init_v41_schema(conn, "storage/v41_schema.sql")

    macro_provider = macro.get_provider(config)
    now = datetime.now(timezone.utc)

    logger.info("=== V4.1 Scanner Intraday Wave: inizio ciclo (PAXG_USDT + BTC_USDT) ===")

    assets = config.get("V41_SCANNER", {}).get("assets", v41.V41_ASSETS)

    for asset in assets:
        try:
            _run_for_asset(conn, asset, config, macro_provider, now)
        except Exception as e:
            logger.error("V4.1 Scanner [%s]: errore non gestito: %s", asset, e)

    conn.close()
    logger.info("=== V4.1 Scanner Intraday Wave: fine ciclo ===")
