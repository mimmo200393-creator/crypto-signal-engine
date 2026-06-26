"""
strategies/edge_lab/market_context_engine.py
Edge Lab — Step 8: Market Context Engine (orchestratore Layer 1)

Unisce Step 2-7 in un unico snapshot di contesto di mercato per asset.
Chiamato dal runner Edge Lab ad ogni scan, prima di qualsiasi strategia.

Pipeline per asset:
    1. Trend Engine      (Step 2) → trend H4 + H1
    2. Session Engine    (Step 3) → sessione corrente + livelli sessione
    3. Fibonacci Engine  (Step 7) → livelli Fib + zona OTE
    4. Liquidity Engine  (Step 4) → mappa liquidità con livelli sessione
    5. S/R Engine        (Step 5) → zone S/R multi-timeframe
    6. Volatility Engine (Step 6) → regime ATR + kill zone
    7. Macro Engine      (Step 6) → evento macro + blackout

Output: dict `market_context` salvato in `market_context_snapshots`.

Il market_context viene passato direttamente alle strategie Layer 2
(OTE-SC, Step 9) che lo consumano senza ricalcolare nulla.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

import pandas as pd

from strategies.edge_lab.trend_engine import (
    compute_trend_h4,
    compute_trend_h1,
    combine_trends,
    compute_ema_slope,
)
from strategies.edge_lab.session_engine import build_session_context
from strategies.edge_lab.fibonacci_engine import (
    compute_fibonacci_levels,
    format_fibonacci_summary,
)
from strategies.edge_lab.liquidity_engine import (
    build_el_liquidity_map,
    format_el_liquidity_map_summary,
)
from strategies.edge_lab.sr_engine import (
    build_sr_map,
    get_sr_score,
    check_sr_reaction,
    format_sr_map_summary,
)
from strategies.edge_lab.volatility_macro_engine import (
    compute_volatility_context,
    get_macro_context,
    format_volatility_summary,
    format_macro_summary,
)
from core.macro import MacroEventProvider

logger = logging.getLogger("edge_lab.market_context")


# ============================================================
# Entry point principale
# ============================================================

def build_market_context(
    asset: str,
    df_h4: pd.DataFrame,
    df_h1: pd.DataFrame,
    df_m15: pd.DataFrame,
    df_d1: pd.DataFrame,
    now: datetime,
    macro_provider: Optional[MacroEventProvider] = None,
    config: Optional[dict] = None,
) -> dict:
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    ema_periods = config.get("EMA_PERIODS", [21, 50, 100, 200]) if config else [21, 50, 100, 200]
    momentum_lookback = 5

    current_price = float(df_m15.iloc[-1]["close"]) if len(df_m15) > 0 else 0.0

    ctx: dict = {
        "asset":         asset,
        "timestamp":     now.isoformat(),
        "current_price": current_price,
    }

    # ── Step 2: Trend Engine ─────────────────────────────────
    trend_h4 = _safe(compute_trend_h4,  df_h4, ema_periods, momentum_lookback)
    trend_h1 = _safe(compute_trend_h1,  df_h1, ema_periods, momentum_lookback)

    if trend_h4 and trend_h1:
        combined = combine_trends(trend_h4["direction"], trend_h1["direction"])
    else:
        combined = "NEUTRAL"

    ctx["trend"] = {
        "h4":      trend_h4,
        "h1":      trend_h1,
        "combined": combined,
    }

    logger.info(
        "%s | Trend: H4=%s H1=%s combined=%s",
        asset,
        trend_h4["direction"] if trend_h4 else "N/A",
        trend_h1["direction"] if trend_h1 else "N/A",
        combined,
    )

    # ── Step 3: Session Engine ───────────────────────────────
    session_ctx = _safe(build_session_context, df_m15, now)
    ctx["session"] = session_ctx

    current_session   = session_ctx["current_session"]   if session_ctx else "UNKNOWN"
    reference_session = session_ctx["reference_session"] if session_ctx else "UNKNOWN"
    reference_levels  = session_ctx["reference_levels"]  if session_ctx else None

    logger.info(
        "%s | Session: current=%s ref=%s ref_levels=%s",
        asset, current_session, reference_session,
        f"H={reference_levels['high']:.4f} L={reference_levels['low']:.4f}"
        if reference_levels else "N/A",
    )

    # ── Step 7: Fibonacci Engine ─────────────────────────────
    fib_buy  = _safe(compute_fibonacci_levels, reference_levels, "BUY")
    fib_sell = _safe(compute_fibonacci_levels, reference_levels, "SELL")

    ctx["fibonacci"] = {
        "BUY":  fib_buy,
        "SELL": fib_sell,
        "reference_levels": reference_levels,
    }

    if fib_buy:
        logger.info("%s | %s", asset, format_fibonacci_summary(fib_buy, current_price))
    if fib_sell:
        logger.info("%s | %s", asset, format_fibonacci_summary(fib_sell, current_price))

    # ── Step 4: Liquidity Engine ─────────────────────────────
    liq_map = _safe(build_el_liquidity_map, df_h4, df_d1, df_m15, current_price, now)
    ctx["liquidity"] = liq_map

    if liq_map:
        logger.info("%s | %s", asset, format_el_liquidity_map_summary(liq_map, asset))

    # ── Step 5: S/R Engine ───────────────────────────────────
    sr_map = _safe(build_sr_map, df_h4, df_h1, df_m15)
    ctx["sr"] = sr_map

    if sr_map and current_price > 0:
        sr_score_buy  = get_sr_score(sr_map, current_price, "BUY")
        sr_score_sell = get_sr_score(sr_map, current_price, "SELL")
        sr_reaction_buy  = check_sr_reaction(sr_map, current_price, "BUY")
        sr_reaction_sell = check_sr_reaction(sr_map, current_price, "SELL")
        ctx["sr_scores"] = {
            "score_buy":    sr_score_buy,
            "score_sell":   sr_score_sell,
            "reaction_buy":  sr_reaction_buy,
            "reaction_sell": sr_reaction_sell,
        }
        logger.info("%s | %s", asset, format_sr_map_summary(sr_map, current_price))
    else:
        ctx["sr_scores"] = {
            "score_buy": 0.0, "score_sell": 0.0,
            "reaction_buy": False, "reaction_sell": False,
        }

    # ── Step 6: Volatility + Macro Engine ───────────────────
    vol_ctx = _safe(
        compute_volatility_context, df_m15, df_h1, now, current_price
    )
    ctx["volatility"] = vol_ctx

    macro_ctx = get_macro_context(macro_provider, now) if macro_provider else {
        "event": None, "is_blackout": False,
        "minutes_to_event": None, "macro_risk": "LOW",
    }
    ctx["macro"] = macro_ctx

    if vol_ctx:
        logger.info("%s | %s", asset, format_volatility_summary(vol_ctx))
    logger.info("%s | %s", asset, format_macro_summary(macro_ctx))

    # ── Tradeability summary ─────────────────────────────────
    # Blocchi hard (indipendenti dalla direzione):
    # NEUTRAL  → bloccato (nessuna direzione definita)
    # TRANSITION → NON bloccato (dati: H4 BEARISH + H1 NEUTRAL/TRANSITION
    #              performa meglio di H4+H1 allineati — è un pullback)
    hard_blocks = []
    if vol_ctx and not vol_ctx["is_tradeable"]:
        hard_blocks.append(vol_ctx["block_reason"])
    if macro_ctx["is_blackout"]:
        ev = macro_ctx["event"] or {}
        hard_blocks.append(f"MACRO_BLACKOUT_{ev.get('type','?')}")
    if combined == "NEUTRAL":
        hard_blocks.append("TREND_NEUTRAL")
    if current_session == "OVERLAP":
        hard_blocks.append("SESSION_OVERLAP_EXCLUDED")

    ctx["is_tradeable"]  = len(hard_blocks) == 0
    ctx["block_reasons"] = hard_blocks

    logger.info(
        "%s | Tradeable=%s blocks=%s",
        asset, ctx["is_tradeable"], hard_blocks or "none",
    )

    return ctx


# ============================================================
# Helper per direzione specifica (usato da OTE-SC)
# ============================================================

def get_direction_context(market_ctx: dict, direction: str) -> dict:
    trend = market_ctx.get("trend", {})
    combined = trend.get("combined", "NEUTRAL")

    if direction == "BUY":
        trend_ok    = combined == "BULLISH"
        sr_score    = market_ctx.get("sr_scores", {}).get("score_buy", 0.0)
        sr_reaction = market_ctx.get("sr_scores", {}).get("reaction_buy", False)
    else:
        trend_ok    = combined == "BEARISH"
        sr_score    = market_ctx.get("sr_scores", {}).get("score_sell", 0.0)
        sr_reaction = market_ctx.get("sr_scores", {}).get("reaction_sell", False)

    fib      = (market_ctx.get("fibonacci") or {}).get(direction)
    sess_ctx = market_ctx.get("session") or {}

    ote_zone = None
    if fib:
        ote_zone = {
            "ote_low":  fib["ote_low"],
            "ote_high": fib["ote_high"],
            "fib_618":  fib["fib_618"],
            "fib_786":  fib["fib_786"],
        }

    return {
        "trend_ok":      trend_ok,
        "fib":           fib,
        "ote_zone":      ote_zone,
        "sr_score":      sr_score,
        "sr_reaction":   sr_reaction,
        "liq_map":       market_ctx.get("liquidity"),
        "session":       sess_ctx.get("current_session", "UNKNOWN"),
        "ref_session":   sess_ctx.get("reference_session", "UNKNOWN"),
        "ref_levels":    sess_ctx.get("reference_levels"),
        "is_tradeable":  market_ctx.get("is_tradeable", False),
        "block_reasons": market_ctx.get("block_reasons", []),
    }


# ============================================================
# Serializzazione per DB (market_context_snapshots)
# ============================================================

def serialize_for_db(market_ctx: dict) -> dict:
    import json

    trend    = market_ctx.get("trend") or {}
    trend_h4 = trend.get("h4") or {}
    trend_h1 = trend.get("h1") or {}
    sess     = market_ctx.get("session") or {}
    vol      = market_ctx.get("volatility") or {}
    macro    = market_ctx.get("macro") or {}
    liq      = market_ctx.get("liquidity") or {}
    sr_sc    = market_ctx.get("sr_scores") or {}
    fib_buy  = (market_ctx.get("fibonacci") or {}).get("BUY") or {}

    nearest_above = (liq.get("nearest_above") or {})
    nearest_below = (liq.get("nearest_below") or {})

    return {
        "asset":               market_ctx["asset"],
        "timestamp_snapshot":  market_ctx["timestamp"],
        "current_price":       market_ctx["current_price"],

        "trend_h4":            trend_h4.get("direction"),
        "trend_h1":            trend_h1.get("direction"),
        "trend_combined":      trend.get("combined"),
        "ema_slope_h4":        trend_h4.get("ema_slope"),
        "ema_slope_h1":        trend_h1.get("ema_slope"),

        "current_session":     sess.get("current_session"),
        "reference_session":   sess.get("reference_session"),
        "ref_high":            (sess.get("reference_levels") or {}).get("high"),
        "ref_low":             (sess.get("reference_levels") or {}).get("low"),
        "ref_range":           (sess.get("reference_levels") or {}).get("range"),

        "ote_low":             fib_buy.get("ote_low"),
        "ote_high":            fib_buy.get("ote_high"),
        "fib_618":             fib_buy.get("fib_618"),
        "fib_786":             fib_buy.get("fib_786"),

        "nearest_above_label": nearest_above.get("label"),
        "nearest_above_price": nearest_above.get("price"),
        "nearest_below_label": nearest_below.get("label"),
        "nearest_below_price": nearest_below.get("price"),

        "sr_score_buy":        sr_sc.get("score_buy", 0.0),
        "sr_score_sell":       sr_sc.get("score_sell", 0.0),
        "sr_reaction_buy":     sr_sc.get("reaction_buy", False),
        "sr_reaction_sell":    sr_sc.get("reaction_sell", False),

        "vol_regime_m15":      vol.get("regime_m15"),
        "vol_regime_h1":       vol.get("regime_h1"),
        "atr_m15":             vol.get("atr_m15"),
        "atr_h1":              vol.get("atr_h1"),
        "is_high_vol_window":  vol.get("is_high_vol_window", False),

        "macro_risk":          macro.get("macro_risk", "LOW"),
        "macro_is_blackout":   macro.get("is_blackout", False),
        "macro_event_type":    (macro.get("event") or {}).get("type"),
        "macro_minutes_to_event": macro.get("minutes_to_event"),

        "is_tradeable":        market_ctx.get("is_tradeable", False),
        "block_reasons":       json.dumps(market_ctx.get("block_reasons", [])),
    }


# ============================================================
# Utility
# ============================================================

def _safe(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Exception as e:
        logger.warning("market_context._safe: %s → %s", fn.__name__, e)
        return None
