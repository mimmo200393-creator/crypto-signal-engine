"""
strategies/edge_lab/ote_sc.py
Edge Lab — Step 9: OTE Session Continuation (OTE-SC)

Strategia istituzionale basata su:
    1. Trend BULLISH o BEARISH (no NEUTRAL, no TRANSITION)
    2. OTE Touch: almeno High o Low della candela M15 entra nella zona 61.8%-78.6%
    3. Candela di conferma: Close > Open BUY (corpo >= 50% range), Close < Open SELL
    4. Entry:  Close M15 della candela di conferma
    5. SL:     estremo sessione di riferimento (Low per BUY, High per SELL)
    6. TP:     nearest liquidity target con Priority Score più alto
    7. EXPIRED: 96 candele M15 (24h operative)

Tradeability (Phase 1A — mai bloccante, solo flag informativi):
    - RR_TOO_LOW
    - STOP_TOO_WIDE

Consuma il market_context prodotto dal Market Context Engine (Step 8).
Non ricalcola nulla — legge tutto dal context.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

import pandas as pd

from strategies.edge_lab.session_engine import check_ote_touch
from strategies.edge_lab.liquidity_engine import (
    find_nearest_liquidity_target,
    find_session_sl_extreme,
)
from strategies.edge_lab.market_context_engine import get_direction_context

logger = logging.getLogger("edge_lab.ote_sc")

STRATEGY_NAME    = "OTE-SC"
STRATEGY_VERSION = "Phase1A"

# Spec OTE-SC
CONFIRMATION_BODY_PCT = 0.50
EXPIRY_BARS_M15       = 96
MIN_RR_FLAG           = 1.5    # sotto questa soglia → flag informativo
MIN_RR_BLOCK          = 1.0    # sotto questa soglia → segnale bloccato
MAX_SL_ATR_MULT       = 3.0
MIN_QUALITY_LABEL     = "MEDIUM"  # LOW → segnale bloccato


def _find_ote_touch_candle(
    df_m15: pd.DataFrame,
    ote_zone: dict,
    lookback: int = 5,
) -> Optional[int]:
    if ote_zone is None or len(df_m15) < 2:
        return None
    end   = len(df_m15) - 1
    start = max(0, end - lookback)
    for i in range(end - 1, start - 1, -1):
        candle = df_m15.iloc[i]
        if check_ote_touch(candle, ote_zone):
            return i
    return None


def _is_confirmation_candle(candle: pd.Series, direction: str) -> bool:
    o = float(candle["open"])
    h = float(candle["high"])
    l = float(candle["low"])
    c = float(candle["close"])
    body = abs(c - o)
    rng  = h - l
    if rng <= 0:
        return False
    body_pct = body / rng
    if direction == "BUY":
        return c > o and body_pct >= CONFIRMATION_BODY_PCT
    else:
        return c < o and body_pct >= CONFIRMATION_BODY_PCT


def _compute_rr_and_flags(
    entry: float,
    sl: float,
    tp: float,
    atr_m15: float,
) -> tuple[float, list[str]]:
    risk   = abs(entry - sl)
    reward = abs(tp - entry)
    rr     = reward / risk if risk > 0 else 0.0
    flags: list[str] = []
    if rr < MIN_RR_FLAG:
        flags.append(f"RR_TOO_LOW ({rr:.2f} < {MIN_RR_FLAG})")
    if atr_m15 > 0 and risk > MAX_SL_ATR_MULT * atr_m15:
        flags.append(f"STOP_TOO_WIDE ({risk:.4f} > {MAX_SL_ATR_MULT}x ATR={atr_m15:.4f})")
    return rr, flags


def generate_ote_sc_signal(
    market_ctx: dict,
    df_m15: pd.DataFrame,
    direction: str,
) -> dict:
    diag: dict = {
        "strategy":   STRATEGY_NAME,
        "direction":  direction,
        "conditions": {},
        "flags":      [],
        "rejection":  None,
    }

    def reject(reason: str) -> dict:
        diag["rejection"] = reason
        logger.info("OTE-SC [%s %s]: REJECT %s", market_ctx.get("asset","?"), direction, reason)
        return {"signal": None, "diagnostics": diag}

    # ── Gate 0: hard blocks dal Market Context Engine ────────
    dir_ctx = get_direction_context(market_ctx, direction)
    if not dir_ctx["is_tradeable"]:
        diag["conditions"]["market_tradeable"] = False
        return reject(f"MARKET_NOT_TRADEABLE ({', '.join(dir_ctx['block_reasons'])})")
    diag["conditions"]["market_tradeable"] = True

    # ── Gate 1: Trend BULLISH o BEARISH ─────────────────────
    trend_ok = dir_ctx["trend_ok"]
    diag["conditions"]["trend_aligned"] = trend_ok
    if not trend_ok:
        return reject(f"TREND_NOT_ALIGNED (combined={market_ctx['trend']['combined']})")

    # ── Gate 2: OTE zone disponibile ────────────────────────
    ote_zone = dir_ctx.get("ote_zone")
    if ote_zone is None:
        diag["conditions"]["ote_zone_available"] = False
        return reject("OTE_ZONE_UNAVAILABLE (no reference session levels)")
    diag["conditions"]["ote_zone_available"] = True
    diag["ote_zone"] = ote_zone

    # ── Gate 3: OTE Touch ───────────────────────────────────
    atr_m15 = (market_ctx.get("volatility") or {}).get("atr_m15", 0.0)

    touch_idx = _find_ote_touch_candle(df_m15, ote_zone, lookback=10)
    diag["conditions"]["ote_touch"] = touch_idx is not None
    if touch_idx is None:
        return reject("NO_OTE_TOUCH (nessuna candela M15 ha toccato la zona OTE)")
    diag["ote_touch_candle_idx"] = touch_idx

    # ── Gate 4: Candela di conferma ─────────────────────────
    confirmation_idx = None
    for i in range(touch_idx, len(df_m15) - 1):
        if _is_confirmation_candle(df_m15.iloc[i], direction):
            confirmation_idx = i
            break

    diag["conditions"]["confirmation_candle"] = confirmation_idx is not None
    if confirmation_idx is None:
        return reject(
            f"NO_CONFIRMATION_CANDLE "
            f"(nessuna candela {'bullish' if direction=='BUY' else 'bearish'} "
            f"con corpo>={CONFIRMATION_BODY_PCT*100:.0f}% dopo OTE touch)"
        )

    diag["confirmation_candle_idx"] = confirmation_idx
    conf_candle = df_m15.iloc[confirmation_idx]

    # Timestamp candela di conferma — usato per deduplicazione nel runner
    conf_ts_ms = int(conf_candle["timestamp"])
    diag["confirmation_candle_ts"] = conf_ts_ms

    # ── Entry ────────────────────────────────────────────────
    entry = float(conf_candle["close"])
    diag["entry"] = entry

    # ── Stop Loss: estremo sessione di riferimento ───────────
    liq_map = dir_ctx.get("liq_map")
    sl_from_session = find_session_sl_extreme(liq_map, direction) if liq_map else None

    if sl_from_session is None:
        if direction == "BUY":
            sl_from_session = ote_zone["ote_low"] - atr_m15 * 0.5
        else:
            sl_from_session = ote_zone["ote_high"] + atr_m15 * 0.5

    sl = float(sl_from_session)

    if direction == "BUY" and sl >= entry:
        return reject(f"SL_INVALID_BUY (sl={sl:.4f} >= entry={entry:.4f})")
    if direction == "SELL" and sl <= entry:
        return reject(f"SL_INVALID_SELL (sl={sl:.4f} <= entry={entry:.4f})")

    diag["sl"] = sl

    # ── Take Profit: nearest liquidity target ─────────────────
    tp_level = find_nearest_liquidity_target(liq_map, entry, direction) if liq_map else None
    if tp_level is None:
        return reject("NO_LIQUIDITY_TARGET (nessun target di liquidità disponibile)")

    tp = float(tp_level["price"])
    diag["tp"]          = tp
    diag["tp_label"]    = tp_level.get("label", "N/A")
    diag["tp_priority"] = tp_level.get("priority_label", "N/A")

    # ── RR e flag tradeability ───────────────────────────────
    rr, flags = _compute_rr_and_flags(entry, sl, tp, atr_m15)
    diag["rr"]    = rr
    diag["flags"] = flags

    if flags:
        logger.info(
            "OTE-SC [%s %s]: flags informativi (non bloccanti): %s",
            market_ctx.get("asset","?"), direction, flags
        )

    # ── Quality Score ─────────────────────────────────────────
    quality_score, quality_label = _compute_quality_score(dir_ctx, rr, flags)
    diag["quality_score"] = quality_score
    diag["quality_label"] = quality_label

    # ── Gate 5: RR minimo bloccante ─────────────────────────
    if rr < MIN_RR_BLOCK:
        return reject(f"RR_TOO_LOW_BLOCK (rr={rr:.2f} < min={MIN_RR_BLOCK})")

    # ── Gate 6: Quality minima ───────────────────────────────
    if quality_label == "LOW":
        return reject(f"QUALITY_TOO_LOW (score={quality_score}/LOW)")

    # ── Costruzione segnale ───────────────────────────────────
    asset     = market_ctx.get("asset", "UNKNOWN")
    signal_id = str(uuid.uuid4())
    conf_dt   = datetime.fromtimestamp(conf_ts_ms / 1000, tz=timezone.utc)

    signal = {
        "signal_id":        signal_id,
        "strategy_name":    STRATEGY_NAME,
        "strategy_version": STRATEGY_VERSION,
        "asset":            asset,
        "direction":        direction,
        "timestamp_setup":  conf_dt.isoformat(),

        # Prezzi
        "entry":     entry,
        "stop_loss": sl,
        "tp":        tp,
        "rr":        round(rr, 4),

        # OTE
        "ote_low":          ote_zone["ote_low"],
        "ote_high":         ote_zone["ote_high"],
        "ote_touch_idx":    touch_idx,
        "confirmation_idx": confirmation_idx,

        # ← NUOVO: timestamp candela conferma per deduplicazione
        "confirmation_candle_ts": conf_ts_ms,

        # Liquidity target
        "liquidity_target":          tp_level.get("label"),
        "liquidity_target_price":    tp,
        "liquidity_target_priority": tp_level.get("priority_label"),
        "liquidity_target_score":    tp_level.get("priority_score"),

        # SL source
        "sl_source": "session_extreme",

        # Contesto
        "session":          dir_ctx.get("session"),
        "ref_session":      dir_ctx.get("ref_session"),
        "trend_h4":         (market_ctx["trend"]["h4"] or {}).get("direction"),
        "trend_h1":         (market_ctx["trend"]["h1"] or {}).get("direction"),
        "trend_combined":   market_ctx["trend"]["combined"],
        "vol_regime_m15":   (market_ctx.get("volatility") or {}).get("regime_m15"),
        "sr_reaction":      dir_ctx.get("sr_reaction", False),
        "sr_score":         dir_ctx.get("sr_score", 0.0),

        # Quality
        "quality_score": quality_score,
        "quality_label": quality_label,

        # Flag informativi (non bloccanti)
        "tradeability_flags": flags,

        # Tracking
        "final_outcome": "OPEN",
        "expiry_bars":   EXPIRY_BARS_M15,
    }

    logger.info(
        "OTE-SC [%s %s]: SIGNAL entry=%.4f sl=%.4f tp=%.4f rr=%.2f "
        "quality=%d/%s session=%s ref=%s target=%s conf_ts=%d",
        asset, direction, entry, sl, tp, rr,
        quality_score, quality_label,
        dir_ctx.get("session"), dir_ctx.get("ref_session"),
        tp_level.get("label"), conf_ts_ms,
    )

    return {"signal": signal, "diagnostics": diag}


def _compute_quality_score(
    dir_ctx: dict,
    rr: float,
    flags: list[str],
) -> tuple[int, str]:
    score = 0
    score += 3  # trend allineato (prerequisito)

    if dir_ctx.get("sr_reaction", False):
        score += 2
    if dir_ctx.get("sr_score", 0.0) >= 0.5:
        score += 2
    if rr >= 3.0:
        score += 3
    elif rr >= 2.0:
        score += 2

    score -= len(flags)
    score = max(0, min(score, 10))

    if score >= 7:
        label = "HIGH"
    elif score >= 4:
        label = "MEDIUM"
    else:
        label = "LOW"

    return score, label


def is_signal_expired(signal: dict) -> bool:
    expiry    = signal.get("expiry_bars", EXPIRY_BARS_M15)
    bars_open = signal.get("bars_open", 0)
    return bars_open >= expiry
