"""
core/signal_engine.py  (V2.1)
Orchestratore multi-strategia.

Pipeline per ogni asset/candela H1:
    1. Carica strategie abilitate (registry)
    2. Esegue scanner -> genera segnali candidati
    3. Calcola Raw Score (fatto dalla strategia)
    4. Applica Market Regime Detector
    5. Calcola Final Score = Raw Score + Regime Bonus/Penalty
    6. Applica Correlation Engine
    7. Salva nel database
    8. Invia notifiche Telegram/ntfy per segnali approvati
"""

import json
import logging
import requests
from datetime import datetime, timezone
from typing import List, Dict

from core import exchange, indicators, macro, trade_manager, market_regime
from core.correlation_engine import apply_correlation_filter
from core.strategy_registry import StrategyRegistry
from strategies.base import Signal
from storage import db
from notifications import telegram_bot, ntfy_bot

logger = logging.getLogger("signal_engine")

DEFAULT_NOTIFY_THRESHOLD = 9


def bootstrap_all(conn, config: dict):
    base_url = config["EXCHANGE_BASE_URL"]
    target = config["BOOTSTRAP_TARGET_CANDLES"]
    max_per_call = config["MAX_CANDLES_PER_CALL"]
    delay = config["REQUEST_DELAY_SECONDS"]

    for asset in config["WATCHLIST"]:
        for tf_label, tf_code in config["TIMEFRAMES"].items():
            existing = db.count_candles(conn, asset, tf_code)
            if existing >= target:
                logger.info("Bootstrap skip: %s %s (%d candele)", asset, tf_code, existing)
                continue
            logger.info("Bootstrap: %s %s ...", asset, tf_code)
            try:
                candles = exchange.bootstrap_history(
                    base_url, asset, tf_code, target, max_per_call, delay
                )
                db.upsert_candles(conn, asset, tf_code, candles)
            except (exchange.ExchangeError, requests.exceptions.HTTPError) as e:
                logger.warning("Bootstrap fallito %s %s: %s", asset, tf_code, e)


def update_candles(conn, asset: str, config: dict) -> dict:
    base_url = config["EXCHANGE_BASE_URL"]
    max_per_call = config["MAX_CANDLES_PER_CALL"]
    delay = config["REQUEST_DELAY_SECONDS"]

    result = {}
    for tf_label, tf_code in config["TIMEFRAMES"].items():
        last_ts = db.get_latest_timestamp(conn, asset, tf_code)
        if last_ts is None:
            new_candles = exchange.bootstrap_history(
                base_url, asset, tf_code,
                config["BOOTSTRAP_TARGET_CANDLES"], max_per_call, delay
            )
        else:
            new_candles = exchange.fetch_new_candles_since(
                base_url, asset, tf_code, last_ts, max_per_call, delay
            )
        if new_candles:
            db.upsert_candles(conn, asset, tf_code, new_candles)
        result[tf_label] = new_candles
    return result


def run_scan_cycle(conn, config: dict, registry: StrategyRegistry):
    expiry_bars = config["TRADE_EXPIRY_BARS"]
    notify_threshold = config.get("NOTIFY_FINAL_SCORE_THRESHOLD", DEFAULT_NOTIFY_THRESHOLD)
    cooldown_hours = config["COOLDOWN_HOURS"]
    macro_provider = macro.get_provider(config)
    macro_window = config["MACRO_WINDOW_MINUTES"]
    corr_threshold = config.get("CORRELATION_THRESHOLD", 0.80)
    corr_lookback = config.get("CORRELATION_LOOKBACK", 100)
    limit = config["BOOTSTRAP_TARGET_CANDLES"]

    for asset in config["WATCHLIST"]:
        try:
            updates = update_candles(conn, asset, config)
        except (exchange.ExchangeError, requests.exceptions.HTTPError) as e:
            logger.warning("Update fallito %s: %s", asset, e)
            continue

        new_h1_candles = updates.get("H1", [])
        new_h4_candles = updates.get("H4", [])

        logger.info("Check %s: +%d H1, +%d H4",
                    asset, len(new_h1_candles), len(new_h4_candles))

        if not new_h1_candles:
            continue

        for new_candle in new_h1_candles:
            # Trade management V1 (invariato)
            trade_manager.update_open_trades_for_asset(conn, asset, new_candle, expiry_bars)

            df_h1 = db.get_candles_df(conn, asset, config["TIMEFRAMES"]["H1"], limit=limit)
            df_h4 = db.get_candles_df(conn, asset, config["TIMEFRAMES"]["H4"], limit=limit)

            min_rows = max(config["EMA_PERIODS"]) + config["PIVOT_LOOKBACK"] * 2 + 60
            if len(df_h1) < min_rows or len(df_h4) < max(config["EMA_PERIODS"]):
                logger.info("Dati insufficienti per %s", asset)
                continue

            indicators.compute_all_indicators(df_h1, df_h4, config)

            current_ts_ms = int(new_candle["timestamp"])
            current_dt = datetime.fromtimestamp(current_ts_ms / 1000, tz=timezone.utc)
            macro_event = macro_provider.get_active_event(current_dt, macro_window)

            last_h1 = df_h1.iloc[-1]
            last_h4 = df_h4.iloc[-1]
            logger.info(
                "%s | close=%.4f | H1 EMA21=%.4f EMA50=%.4f | "
                "H4 EMA50=%.4f EMA100=%.4f EMA200=%.4f | ATR=%.4f",
                asset, last_h1["close"],
                last_h1["ema_21"], last_h1["ema_50"],
                last_h4["ema_50"], last_h4["ema_100"], last_h4["ema_200"],
                last_h1["atr"]
            )

            # Market Regime
            regime = market_regime.detect_regime(df_h1, df_h4)

            market_snapshot = {
                "close": float(last_h1["close"]),
