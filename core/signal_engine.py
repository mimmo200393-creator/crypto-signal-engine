"""
signal_engine.py
Orchestratore principale: bootstrap storico, loop di scansione,
valutazione setup, gestione trade aperti, alert Telegram.
"""

import logging
import requests
from datetime import datetime, timezone

from core import exchange, indicators, strategy, scoring, macro, trade_manager
from storage import db
from notifications import telegram_bot, ntfy_bot

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
            except (exchange.ExchangeError, requests.exceptions.HTTPError) as e:
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
      1. Aggiorna le candele.
      2. Per ogni asset con una nuova candela H1 chiusa:
         - aggiorna i trade ACTIVE (trade_manager)
         - valuta nuovi setup LONG/SHORT
         - salva su DB / invia alert Telegram secondo lo scoring
    """
    cooldown_hours = config["COOLDOWN_HOURS"]
    expiry_bars = config["TRADE_EXPIRY_BARS"]
    db_threshold = config["DB_SCORE_THRESHOLD"]
    telegram_threshold = config["TELEGRAM_SCORE_THRESHOLD"]
    strategy_name = config["STRATEGY_NAME"]
    strategy_version = config["STRATEGY_VERSION"]
    macro_provider = macro.get_provider(config)
    macro_window = config["MACRO_WINDOW_MINUTES"]

    for asset in config["WATCHLIST"]:
        try:
            updates = update_candles(conn, asset, config)
        except (exchange.ExchangeError, requests.exceptions.HTTPError) as e:
            logger.warning("Update candele fallito per %s, asset SKIPPATO: %s", asset, e)
            continue

        new_h1_candles = updates.get("H1", [])
        new_h4_candles = updates.get("H4", [])

        logger.info(
            "Check %s: +%d candele H1, +%d candele H4",
            asset, len(new_h1_candles), len(new_h4_candles)
        )

        if not new_h1_candles:
            continue  # nessuna nuova candela H1 chiusa -> niente da fare per questo asset

        logger.info("Nuova candela H1 per %s: %d nuove candele", asset, len(new_h1_candles))

        for new_candle in new_h1_candles:
            # --- 1. Gestione trade aperti (sempre, ad ogni nuova candela H1) ---
            trade_manager.update_open_trades_for_asset(conn, asset, new_candle, expiry_bars)

            # --- 2. Carica dati indicatori aggiornati ---
            df_h1 = db.get_candles_df(conn, asset, config["TIMEFRAMES"]["H1"], limit=config["BOOTSTRAP_TARGET_CANDLES"])
            df_h4 = db.get_candles_df(conn, asset, config["TIMEFRAMES"]["H4"], limit=config["BOOTSTRAP_TARGET_CANDLES"])

            if len(df_h1) < max(config["EMA_PERIODS"]) + config["PIVOT_LOOKBACK"] * 2 + 2:
                logger.info("Dati H1 insufficienti per %s, skip valutazione setup.", asset)
                continue
            if len(df_h4) < max(config["EMA_PERIODS"]):
                logger.info("Dati H4 insufficienti per %s, skip valutazione setup.", asset)
                continue

            indicators.compute_all_indicators(df_h1, df_h4, config)

            last_h1 = df_h1.iloc[-1]
            last_h4 = df_h4.iloc[-1]
            logger.info(
                "%s | close=%.4f | H1 EMA21=%.4f EMA50=%.4f | H4 EMA50=%.4f EMA100=%.4f EMA200=%.4f | ATR=%.4f",
                asset, last_h1["close"], last_h1["ema_21"], last_h1["ema_50"],
                last_h4["ema_50"], last_h4["ema_100"], last_h4["ema_200"], last_h1["atr"]
            )

            current_ts_ms = int(new_candle["timestamp"])
            current_dt = datetime.fromtimestamp(current_ts_ms / 1000, tz=timezone.utc)

            # --- 3. Valutazione setup per entrambe le direzioni ---
            for direction, evaluator in [("LONG", strategy.evaluate_long), ("SHORT", strategy.evaluate_short)]:

                # cooldown check
                in_cooldown = trade_manager.check_cooldown(
                    conn, asset, direction, "Pullback EMA Trend",
                    current_ts_ms, cooldown_hours
                )
                if in_cooldown:
                    continue

                # un solo ACTIVE per asset+direzione
                if db.has_active_trade(conn, asset, direction):
                    continue

                setup = evaluator(df_h1, df_h4, config)
                if setup is None:
                    logger.info("%s %s: setup non valido (condizioni non soddisfatte)", asset, direction)
                    continue

                setup["asset"] = asset

                score = scoring.compute_score(setup)
                classification = scoring.classify_score(score, db_threshold, telegram_threshold)

                if not classification["save_to_db"]:
                    continue

                # --- Contesto macro (solo informativo) ---
                macro_event = macro_provider.get_active_event(current_dt, macro_window)
                setup["macro_event"] = macro_event

                trade_record = {
                    "strategy_name": strategy_name,
                    "strategy_version": strategy_version,
                    "timestamp_alert": None,
                    "timestamp_setup": current_ts_ms,
                    "asset": asset,
                    "setup": setup["setup"],
                    "direzione": direction,
                    "entry": setup["entry"],
                    "stop_loss": setup["stop_loss"],
                    "take_profit": setup["take_profit"],
                    "rr": setup["rr"],
                    "score": score,
                    "stato": "ACTIVE",
                    "atr_h1": setup["atr_h1"],
                    "support_level": setup["support_level"],
                    "resistance_level": setup["resistance_level"],
                    "trigger_type": setup["trigger_type"],
                    "macro_event_active": bool(macro_event),
                    "macro_event_type": macro_event["type"] if macro_event else None,
                    "macro_event_minutes_to_release": macro_event["minutes_to_release"] if macro_event else None,
                    "bars_open": 0,
                }

                trade_id = db.insert_trade(conn, trade_record)
                logger.info(
                    "Nuovo trade #%d salvato: %s %s score=%d rr=%.2f",
                    trade_id, asset, direction, score, setup["rr"]
                )

                if classification["send_telegram"]:
                    sent = telegram_bot.send_alert(
                        config["TELEGRAM_BOT_TOKEN"], config["TELEGRAM_CHAT_ID"],
                        setup, score, classification["label"]
                    )
                    if sent:
                        db.update_trade_alert_timestamp(
                            conn, trade_id, datetime.now(timezone.utc).isoformat()
                        )

                    # Canale aggiuntivo ntfy.sh (stessa soglia di Telegram)
                    ntfy_bot.send_alert(
                        config.get("NTFY_TOPIC"), setup, score, classification["label"]
                    )
