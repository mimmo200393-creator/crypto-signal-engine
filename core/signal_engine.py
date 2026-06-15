"""
core/signal_engine.py  (V2.1 — stabilizzazione)

Fix applicati:
    FIX 1: Processa SOLO l'ultima candela H1 per ciclo.
            Le candele accumulate (gap tra scan) vengono aggiornate
            nel DB ma non valutate — un segnale su una candela vecchia
            non ha senso operativo.
    FIX 2: Diagnostica dettagliata per ogni "no signal".
            Ogni strategia restituisce un dict con il motivo esatto
            del rifiuto invece di un semplice None.
"""

import logging
import requests
from datetime import datetime, timezone
from typing import List, Dict

from core import exchange, indicators, macro, trade_manager, market_regime
from core.trade_manager_v2 import update_open_signals_for_asset
from core.correlation_engine import apply_correlation_filter
from core.strategy_registry import StrategyRegistry
from strategies.base import Signal
from storage import db
from notifications import telegram_bot, ntfy_bot

logger = logging.getLogger("signal_engine")

DEFAULT_NOTIFY_THRESHOLD = 8


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
    """
    Pipeline V2.1 stabilizzata.

    FIX 1 — Una sola candela per ciclo:
        Se il sistema ha accumulato N candele H1 (gap tra scan),
        aggiorna il trade manager per tutte, ma valuta i segnali
        SOLO sull'ultima. Un segnale su una candela di 3 ore fa
        non ha valore operativo.

    FIX 2 — Diagnostica dettagliata:
        Ogni strategia espone generate_signal_with_diagnostics()
        che restituisce (signal_or_none, diagnostics_dict).
        Il log mostra esattamente quale condizione ha fallito.
    """
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

        # FIX 1: aggiorna trade manager per TUTTE le candele accumulate
        # ma valuta segnali SOLO sull'ultima
        for candle in new_h1_candles:
            trade_manager.update_open_trades_for_asset(conn, asset, candle, expiry_bars)
            update_open_signals_for_asset(conn, asset, candle, expiry_bars)

        if len(new_h1_candles) > 1:
            logger.info(
                "%s: %d candele accumulate — trade manager aggiornato su tutte, "
                "valutazione segnali SOLO sull'ultima (FIX 1)",
                asset, len(new_h1_candles)
            )

        # Valuta segnali SOLO sull'ultima candela H1
        last_candle = new_h1_candles[-1]
        current_ts_ms = int(last_candle["timestamp"])
        current_dt = datetime.fromtimestamp(current_ts_ms / 1000, tz=timezone.utc)

        # Carica dati aggiornati
        df_h1 = db.get_candles_df(conn, asset, config["TIMEFRAMES"]["H1"], limit=limit)
        df_h4 = db.get_candles_df(conn, asset, config["TIMEFRAMES"]["H4"], limit=limit)

        min_rows = max(config["EMA_PERIODS"]) + config["PIVOT_LOOKBACK"] * 2 + 60
        if len(df_h1) < min_rows or len(df_h4) < max(config["EMA_PERIODS"]):
            logger.info("Dati insufficienti per %s", asset)
            continue

        indicators.compute_all_indicators(df_h1, df_h4, config)

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
        logger.info("%s | Regime: %s", asset, regime)

        market_snapshot = {
            "close": float(last_h1["close"]),
            "ema_21_h1": float(last_h1["ema_21"]),
            "ema_50_h1": float(last_h1["ema_50"]),
            "ema_50_h4": float(last_h4["ema_50"]),
            "ema_100_h4": float(last_h4["ema_100"]),
            "ema_200_h4": float(last_h4["ema_200"]),
            "atr_h1": float(last_h1["atr"]),
            "market_regime": regime,
        }

        # Scanner — FIX 2: diagnostica dettagliata
        candidate_signals: List[Signal] = []

        for strategy in registry.active_strategies:
            for direction in ["LONG", "SHORT"]:

                # Cooldown check
                last_ts_str = db.get_last_signal_timestamp(
                    conn, asset, direction, strategy.name
                )
                if last_ts_str:
                    try:
                        last_ts_ms_val = int(
                            datetime.fromisoformat(last_ts_str).timestamp() * 1000
                        )
                        elapsed_h = (current_ts_ms - last_ts_ms_val) / (1000 * 3600)
                        if elapsed_h < cooldown_hours:
                            logger.debug(
                                "%s %s %s: COOLDOWN (%.1fh < %dh)",
                                strategy.name, asset, direction,
                                elapsed_h, cooldown_hours
                            )
                            continue
                    except Exception:
                        pass

                # Segnale già aperto
                if db.has_open_signal(conn, asset, direction, strategy.name):
                    logger.debug(
                        "%s %s %s: SKIP (segnale OPEN già presente)",
                        strategy.name, asset, direction
                    )
                    continue

                market_data = {
                    "asset": asset,
                    "direction": direction,
                    "df_h1": df_h1.copy(),
                    "df_h4": df_h4.copy(),
                    "config": config,
                }

                # FIX 2: usa generate_signal_with_diagnostics se disponibile
                if hasattr(strategy, "generate_signal_with_diagnostics"):
                    signal, diag = strategy.generate_signal_with_diagnostics(market_data)
                    if signal is None:
                        _log_diagnostics(strategy.name, asset, direction, diag)
                        continue
                else:
                    signal = strategy.generate_signal(market_data)
                    if signal is None:
                        logger.info("%s %s %s: no signal", strategy.name, asset, direction)
                        continue

                # Regime bonus
                regime_bonus = market_regime.get_regime_bonus(strategy.name, regime)
                signal.final_score = signal.raw_score + regime_bonus
                signal.market_regime = regime
                signal.additional_context["macro_event"] = macro_event

                candidate_signals.append(signal)
                logger.info(
                    "✅ CANDIDATO: %s %s %s | raw=%.0f final=%.0f regime=%s",
                    strategy.name, asset, direction,
                    signal.raw_score, signal.final_score, regime
                )

        if not candidate_signals:
            continue

        candles_cache = {
            asset: db.get_candles_df(
                conn, asset, config["TIMEFRAMES"]["H1"],
                limit=corr_lookback + 5
            )
        }

        # Correlation Engine
        filtered_signals = apply_correlation_filter(
            candidate_signals, candles_cache,
            threshold=corr_threshold,
            lookback=corr_lookback,
        )

        # Salva e notifica
        for signal in filtered_signals:
            if signal.trade_status == "REJECTED":
                db.insert_signal(conn, signal, market_snapshot)
                logger.info("REJECTED (correlation): %s %s %s",
                            signal.strategy_name, signal.asset, signal.direction)
                continue

            signal.trade_status = "APPROVED"
            signal_id = db.insert_signal(conn, signal, market_snapshot)

            send_telegram = signal.additional_context.get("send_telegram", False)
            final_ok = signal.final_score >= notify_threshold

            if send_telegram and final_ok:
                db.update_signal_status(conn, signal_id, "OPEN")

                # Usa formato dedicato per Zone + Confirmation
                if signal.strategy_name == "Zone + Confirmation":
                    sent_tg = telegram_bot.send_zone_signal_alert(
                        config["TELEGRAM_BOT_TOKEN"],
                        config["TELEGRAM_CHAT_ID"],
                        signal
                    )
                else:
                    sent_tg = telegram_bot.send_signal_alert(
                        config["TELEGRAM_BOT_TOKEN"],
                        config["TELEGRAM_CHAT_ID"],
                        signal
                    )
                ntfy_bot.send_signal_alert(config.get("NTFY_TOPIC"), signal)

                logger.info(
                    "📨 NOTIFIED: %s %s %s | raw=%.0f final=%.0f | tg=%s",
                    signal.strategy_name, signal.asset, signal.direction,
                    signal.raw_score, signal.final_score, sent_tg
                )
            else:
                db.update_signal_status(conn, signal_id, "OPEN")
                logger.info(
                    "💾 OPEN (no notify): %s %s %s | raw=%.0f final=%.0f",
                    signal.strategy_name, signal.asset, signal.direction,
                    signal.raw_score, signal.final_score
                )


def _log_diagnostics(strategy_name: str, asset: str, direction: str, diag: dict):
    """
    Logga la diagnostica dettagliata di un segnale rifiutato.
    """
    if not diag:
        logger.info("%s %s %s: no signal", strategy_name, asset, direction)
        return

    lines = [f"{strategy_name} {asset} {direction}:"]
    for condition, passed in diag.get("conditions", {}).items():
        icon = "✓" if passed is True else ("✗" if passed is False else f"→ {passed}")
        lines.append(f"  {icon} {condition}")

    score = diag.get("raw_score")
    reason = diag.get("rejection_reason", "CONDITIONS_NOT_MET")

    if score is not None:
        lines.append(f"  Raw Score: {score:.0f}/10")
    lines.append(f"  Rejected: {reason}")

    logger.info("\n".join(lines))
