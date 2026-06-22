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
CONFIRMATION_BODY_PCT = 0.50   # corpo >= 50% del range candela
EXPIRY_BARS_M15       = 96     # 24h operative in candele M15
MIN_RR_FLAG           = 1.5    # sotto questa soglia → flag RR_TOO_LOW (non bloccante)
MAX_SL_ATR_MULT       = 3.0    # SL > 3x ATR M15 → flag STOP_TOO_WIDE (non bloccante)


# ============================================================
# Check candela OTE touch
# ============================================================

def _find_ote_touch_candle(
    df_m15: pd.DataFrame,
    ote_zone: dict,
    lookback: int = 5,
) -> Optional[int]:
    """
    Cerca la candela M15 più recente che ha toccato la zona OTE
    nelle ultime `lookback` candele (esclusa l'ultima non ancora chiusa).

    Ritorna l'indice nel DataFrame oppure None.
    """
    if ote_zone is None or len(df_m15) < 2:
        return None

    # Esamina le ultime `lookback` candele chiuse (escludi l'ultima aperta)
    end = len(df_m15) - 1
    start = max(0, end - lookback)

    for i in range(end - 1, start - 1, -1):
        candle = df_m15.iloc[i]
        if check_ote_touch(candle, ote_zone):
            return i

    return None


# ============================================================
# Check candela di conferma
# ============================================================

def _is_confirmation_candle(candle: pd.Series, direction: str) -> bool:
    """
    BUY:  Close > Open AND corpo >= 50% del range (High - Low)
    SELL: Close < Open AND corpo >= 50% del range

    corpo = abs(Close - Open)
    range = High - Low
    """
    o = float(candle["open"])
    h = float(candle["high"])
    l = float(candle["low"])
    c = float(candle["close"])

    body  = abs(c - o)
    rng   = h - l

    if rng <= 0:
        return False

    body_pct = body / rng

    if direction == "BUY":
        return c > o and body_pct >= CONFIRMATION_BODY_PCT
    else:
        return c < o and body_pct >= CONFIRMATION_BODY_PCT


# ============================================================
# Calcolo RR e flag tradeability
# ============================================================

def _compute_rr_and_flags(
    entry: float,
    sl: float,
    tp: float,
    atr_m15: float,
) -> tuple[float, list[str]]:
    """
    Calcola R/R e raccoglie flag informativi (non bloccanti in Phase 1A).
    """
    risk   = abs(entry - sl)
    reward = abs(tp - entry)
    rr     = reward / risk if risk > 0 else 0.0

    flags: list[str] = []

    if rr < MIN_RR_FLAG:
        flags.append(f"RR_TOO_LOW ({rr:.2f} < {MIN_RR_FLAG})")

    if atr_m15 > 0 and risk > MAX_SL_ATR_MULT * atr_m15:
        flags.append(f"STOP_TOO_WIDE ({risk:.4f} > {MAX_SL_ATR_MULT}x ATR={atr_m15:.4f})")

    return rr, flags


# ============================================================
# Entry point principale
# ============================================================

def generate_ote_sc_signal(
    market_ctx: dict,
    df_m15: pd.DataFrame,
    direction: str,
) -> dict:
    """
    Genera un segnale OTE-SC per la direzione specificata.

    Args:
        market_ctx: output di build_market_context() (Step 8)
        df_m15:     candele M15 aggiornate
        direction:  "BUY" | "SELL"

    Returns:
        {
            "signal":      dict | None,   # segnale completo o None
            "diagnostics": dict,          # dettaglio condizioni valutate
        }
    """
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
    # La candela di conferma è quella immediatamente DOPO il touch,
    # oppure la stessa candela di touch se ha già il corpo richiesto.
    # Cerchiamo dalla touch_idx in poi (inclusa) verso la più recente.
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

    # ── Entry ────────────────────────────────────────────────
    entry = float(conf_candle["close"])
    diag["entry"] = entry

    # ── Stop Loss: estremo sessione di riferimento ───────────
    liq_map = dir_ctx.get("liq_map")
    sl_from_session = find_session_sl_extreme(liq_map, direction) if liq_map else None

    if sl_from_session is None:
        # Fallback: estremo OTE zone + buffer ATR
        if direction == "BUY":
            sl_from_session = ote_zone["ote_low"] - atr_m15 * 0.5
        else:
            sl_from_session = ote_zone["ote_high"] + atr_m15 * 0.5

    sl = float(sl_from_session)

    # Validità SL: deve essere dall'altro lato dell'entry
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
    diag["tp"] = tp
    diag["tp_label"] = tp_level.get("label", "N/A")
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

    # ── Costruzione segnale ───────────────────────────────────
    asset     = market_ctx.get("asset", "UNKNOWN")
    now       = datetime.fromisoformat(market_ctx["timestamp"])
    signal_id = str(uuid.uuid4())

    conf_ts_ms = int(conf_candle["timestamp"])
    conf_dt    = datetime.fromtimestamp(conf_ts_ms / 1000, tz=timezone.utc)

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
        "quality=%d/%s session=%s ref=%s target=%s",
        asset, direction, entry, sl, tp, rr,
        quality_score, quality_label,
        dir_ctx.get("session"), dir_ctx.get("ref_session"),
        tp_level.get("label"),
    )

    return {"signal": signal, "diagnostics": diag}


# ============================================================
# Quality Score
# ============================================================

def _compute_quality_score(
    dir_ctx: dict,
    rr: float,
    flags: list[str],
) -> tuple[int, str]:
    """
    Quality Score OTE-SC [0-10]:
        +3  trend H4 allineato (incluso nel dir_ctx.trend_ok)
        +2  SR reaction alla zona OTE
        +2  SR score H4 (score_buy/sell > 0.5)
        +2  RR >= 2.0
        +1  RR >= 3.0 (bonus aggiuntivo)
        -1  per ogni flag tradeability (RR_TOO_LOW, STOP_TOO_WIDE)

    Label:
        HIGH   >= 7
        MEDIUM >= 4
        LOW    < 4
    """
    score = 0

    # Trend allineato è prerequisito (già verificato come gate)
    score += 3

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


# ============================================================
# Trade monitoring (EXPIRED check — usato dal runner)
# ============================================================

def is_signal_expired(signal: dict) -> bool:
    """
    Ritorna True se il segnale ha superato EXPIRY_BARS_M15 candele M15.
    Legge bars_open dal dict segnale (aggiornato dal trade tracker).
    """
    expiry    = signal.get("expiry_bars", EXPIRY_BARS_M15)
    bars_open = signal.get("bars_open", 0)
    return bars_open >= expiry
