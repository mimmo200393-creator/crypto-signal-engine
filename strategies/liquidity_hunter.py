"""
strategies/liquidity_hunter.py
Liquidity Hunter v1.0

Strategia basata sulla ricerca di liquidità — completamente indipendente da V4.1.

Pipeline:
    1. Liquidity Pool identificata dalla Money Flow Map
    2. Prezzo raggiunge la zona (proximity <= LIQUIDITY_PROXIMITY_PCT)
    3. Sweep confermato nelle ultime LIQUIDITY_SWEEP_LOOKBACK candele M15
       (high/low supera il livello + close rientra dentro)
    4. BOS o CHOCH M15 nella direzione opposta allo sweep
    5. Entry alla chiusura del trigger

Gestione trade:
    SL: dietro il massimo/minimo dello sweep
    TP: prossima Liquidity Pool nella direzione del movimento

Asset: BTC_USDT, PAXG_USDT
Trigger: M15
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

import pandas as pd

from strategies.institutional_scanner_v3 import (
    find_pivots,
    M15_BOS_LOOKBACK,
)

logger = logging.getLogger("liquidity_hunter")

STRATEGY_NAME    = "LH"
STRATEGY_VERSION = "v1.0"

EXPIRY_BARS_M15         = 96     # 24h operative
LIQUIDITY_PROXIMITY_PCT = 0.003  # 0.30%
LIQUIDITY_SWEEP_LOOKBACK = 4     # candele M15
SWEEP_PENETRATION_MIN_PCT = 0.0003  # penetrazione minima oltre il livello

# Quality Score bonus
SCORE_HIGH_PRIORITY_LEVEL = 2   # livello CRITICAL o HIGH nella MFM
SCORE_REJECTION_CANDLE    = 1   # candela di rifiuto presente (body < 30% range)
SCORE_SWEEP_STRONG        = 1   # sweep con penetrazione > 2x minima


# ============================================================
# Step 1: Identifica Liquidity Pool attive
# ============================================================

def find_active_liquidity_pools(
    mfm: dict,
    current_price: float,
) -> list[dict]:
    """
    Ritorna i livelli della MFM entro LIQUIDITY_PROXIMITY_PCT dal prezzo.
    Ordina per priority_score decrescente.
    """
    if not mfm or not mfm.get("levels"):
        return []

    active = []
    for lv in mfm["levels"]:
        price = lv["price"]
        if price <= 0:
            continue
        dist_pct = abs(price - current_price) / current_price
        if dist_pct <= LIQUIDITY_PROXIMITY_PCT:
            active.append({**lv, "distance_pct": dist_pct})

    return sorted(active, key=lambda lv: lv["priority_score"], reverse=True)


# ============================================================
# Step 2: Verifica Liquidity Sweep
# ============================================================

def check_liquidity_sweep(
    df_m15: pd.DataFrame,
    level: dict,
) -> Optional[dict]:
    """
    Cerca lo sweep di un livello di liquidità nelle ultime
    LIQUIDITY_SWEEP_LOOKBACK candele M15.

    Sweep BEARISH (livello HIGH sweepato):
        - High della candela supera il livello (+ penetrazione minima)
        - Close richiude SOTTO il livello

    Sweep BULLISH (livello LOW sweepato):
        - Low della candela scende sotto il livello (+ penetrazione minima)
        - Close richiude SOPRA il livello

    Returns:
        dict con direction, peak_price, candle_idx oppure None
    """
    if len(df_m15) < LIQUIDITY_SWEEP_LOOKBACK + 2:
        return None

    lvl_price = level["price"]
    kind      = level["kind"]  # "high" o "low"
    tol       = lvl_price * SWEEP_PENETRATION_MIN_PCT

    end   = len(df_m15) - 1
    start = max(1, end - LIQUIDITY_SWEEP_LOOKBACK)

    for i in range(end - 1, start - 1, -1):
        candle  = df_m15.iloc[i]
        c_high  = float(candle["high"])
        c_low   = float(candle["low"])
        c_close = float(candle["close"])

        if kind == "high":
            # Sweep bearish: high supera il livello, close rientra sotto
            penetration = c_high - lvl_price
            if penetration >= tol and c_close < lvl_price:
                return {
                    "direction":   "BEARISH",
                    "peak_price":  c_high,
                    "candle_idx":  i,
                    "penetration": penetration,
                    "level_price": lvl_price,
                    "level_label": level["label"],
                }

        else:  # kind == "low"
            # Sweep bullish: low scende sotto il livello, close rientra sopra
            penetration = lvl_price - c_low
            if penetration >= tol and c_close > lvl_price:
                return {
                    "direction":   "BULLISH",
                    "peak_price":  c_low,
                    "candle_idx":  i,
                    "penetration": penetration,
                    "level_price": lvl_price,
                    "level_label": level["label"],
                }

    return None


# ============================================================
# Step 3: BOS o CHOCH dopo lo sweep
# ============================================================

def find_post_sweep_trigger(
    df_m15: pd.DataFrame,
    sweep: dict,
    direction: str,
) -> Optional[dict]:
    """
    Cerca BOS o CHOCH M15 nella direzione del movimento post-sweep,
    DOPO la candela di sweep.

    direction: "BUY" (dopo sweep bearish) o "SELL" (dopo sweep bullish)

    Returns:
        dict con trigger_type, candle_idx, entry oppure None
    """
    sweep_idx = sweep["candle_idx"]

    # Cerca dal candle dopo lo sweep fino all'ultima chiusa
    search_start = sweep_idx + 1
    search_end   = len(df_m15) - 1  # escludi ultima (potenzialmente aperta)

    if search_start >= search_end:
        return None

    # BOS: Close supera il pivot strutturale nella direzione
    # CHOCH: Close inverte la struttura precedente

    # Pivot reference: massimo/minimo prima dello sweep
    pre_sweep = df_m15.iloc[:sweep_idx]
    if len(pre_sweep) < M15_BOS_LOOKBACK * 2 + 1:
        return None

    pivots = find_pivots(pre_sweep, M15_BOS_LOOKBACK)

    if direction == "BUY":
        # Cerco BOS bullish: close supera un pivot high recente
        highs = sorted(pivots["pivot_highs"], key=lambda p: p[2])
        if not highs:
            return None
        ref_high = highs[-1][1]  # ultimo pivot high

        for i in range(search_start, search_end):
            c_close = float(df_m15.iloc[i]["close"])
            if c_close > ref_high:
                return {
                    "trigger_type": "BOS",
                    "candle_idx":   i,
                    "entry":        c_close,
                    "ref_level":    ref_high,
                }

        # CHOCH: cerco close che supera swing high post-sweep
        post_sweep = df_m15.iloc[sweep_idx:search_end]
        if len(post_sweep) >= 3:
            swing_high = float(post_sweep["high"].max())
            for i in range(search_start, search_end):
                c_close = float(df_m15.iloc[i]["close"])
                if c_close > swing_high * 0.998:  # piccola tolleranza
                    return {
                        "trigger_type": "CHOCH",
                        "candle_idx":   i,
                        "entry":        c_close,
                        "ref_level":    swing_high,
                    }

    else:  # SELL
        # Cerco BOS bearish: close scende sotto un pivot low recente
        lows = sorted(pivots["pivot_lows"], key=lambda p: p[2])
        if not lows:
            return None
        ref_low = lows[-1][1]  # ultimo pivot low

        for i in range(search_start, search_end):
            c_close = float(df_m15.iloc[i]["close"])
            if c_close < ref_low:
                return {
                    "trigger_type": "BOS",
                    "candle_idx":   i,
                    "entry":        c_close,
                    "ref_level":    ref_low,
                }

        # CHOCH
        post_sweep = df_m15.iloc[sweep_idx:search_end]
        if len(post_sweep) >= 3:
            swing_low = float(post_sweep["low"].min())
            for i in range(search_start, search_end):
                c_close = float(df_m15.iloc[i]["close"])
                if c_close < swing_low * 1.002:
                    return {
                        "trigger_type": "CHOCH",
                        "candle_idx":   i,
                        "entry":        c_close,
                        "ref_level":    swing_low,
                    }

    return None


# ============================================================
# Step 4: SL e TP
# ============================================================

def compute_sl(sweep: dict, direction: str, buffer_pct: float = 0.0005) -> float:
    """
    SL dietro il massimo/minimo dello sweep.
    BUY:  SL = peak_price (minimo sweep) - buffer
    SELL: SL = peak_price (massimo sweep) + buffer
    """
    peak = sweep["peak_price"]
    buffer = peak * buffer_pct
    if direction == "BUY":
        return peak - buffer
    else:
        return peak + buffer


def find_tp_target(
    mfm: dict,
    entry: float,
    direction: str,
    swept_level: dict,
) -> Optional[dict]:
    """
    TP = prossima Liquidity Pool nella direzione del movimento,
    escludendo il livello appena sweepato.
    """
    if not mfm or not mfm.get("levels"):
        return None

    swept_price = swept_level["price"]
    levels      = mfm["levels"]

    if direction == "BUY":
        candidates = [
            lv for lv in levels
            if lv["kind"] == "high"
            and lv["price"] > entry
            and abs(lv["price"] - swept_price) / swept_price > 0.001
        ]
        if not candidates:
            return None
        return min(candidates, key=lambda lv: lv["price"])
    else:
        candidates = [
            lv for lv in levels
            if lv["kind"] == "low"
            and lv["price"] < entry
            and abs(lv["price"] - swept_price) / swept_price > 0.001
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda lv: lv["price"])


# ============================================================
# Quality Score
# ============================================================

def compute_quality(
    swept_level: dict,
    sweep: dict,
    trigger: dict,
    df_m15: pd.DataFrame,
) -> tuple[int, str]:
    """
    Quality Score LH [0-10]:
        Base:
        +3  livello CRITICAL o HIGH nella MFM
        +2  livello MEDIUM
        +2  trigger BOS (più affidabile di CHOCH)
        +1  trigger CHOCH

        Bonus:
        +2  sweep forte (penetrazione > 2x minima)
        +1  candela di rifiuto presente dopo sweep
        +1  livello con historical_touches >= 3
    """
    score = 0

    # Livello
    priority = swept_level.get("priority_label", "LOW")
    if priority == "CRITICAL":
        score += 3
    elif priority == "HIGH":
        score += 3
    elif priority == "MEDIUM":
        score += 2

    # Trigger
    if trigger["trigger_type"] == "BOS":
        score += 2
    else:
        score += 1

    # Sweep forte
    min_pen = swept_level["price"] * SWEEP_PENETRATION_MIN_PCT
    if sweep.get("penetration", 0) > min_pen * 2:
        score += 2

    # Historical touches
    if swept_level.get("historical_touches", 0) >= 3:
        score += 1

    # Candela di rifiuto dopo sweep
    sweep_idx = sweep["candle_idx"]
    if sweep_idx + 1 < len(df_m15) - 1:
        next_candle = df_m15.iloc[sweep_idx + 1]
        o = float(next_candle["open"])
        h = float(next_candle["high"])
        l = float(next_candle["low"])
        c = float(next_candle["close"])
        rng  = h - l
        body = abs(c - o)
        if rng > 0 and body / rng < 0.30:
            score += 1

    score = max(0, min(score, 10))

    if score >= 7:
        label = "HIGH"
    elif score >= 4:
        label = "MEDIUM"
    else:
        label = "LOW"

    return score, label


# ============================================================
# Entry point principale
# ============================================================

def generate_lh_signal(
    asset: str,
    df_m15: pd.DataFrame,
    mfm: dict,
    now: datetime,
) -> dict:
    """
    Genera un segnale Liquidity Hunter v1.0.

    Returns:
        {"signal": dict | None, "diagnostics": dict}
    """
    diag = {
        "strategy":        STRATEGY_NAME,
        "asset":           asset,
        "rejection":       None,
        "active_pools":    [],
        "sweep":           None,
        "trigger":         None,
    }

    def reject(reason: str) -> dict:
        diag["rejection"] = reason
        logger.info("LH [%s]: REJECT %s", asset, reason)
        return {"signal": None, "diagnostics": diag}

    if len(df_m15) < LIQUIDITY_SWEEP_LOOKBACK + 10:
        return reject("INSUFFICIENT_M15_DATA")

    current_price = float(df_m15.iloc[-1]["close"])

    # ── Step 1: Liquidity Pools attive ──────────────────────
    active_pools = find_active_liquidity_pools(mfm, current_price)
    diag["active_pools"] = [lv["label"] for lv in active_pools]

    if not active_pools:
        return reject("NO_ACTIVE_LIQUIDITY_POOL")

    # ── Step 2+3: Sweep + Trigger ────────────────────────────
    # Itera sui livelli attivi per priorità — prende il primo setup valido
    for level in active_pools:
        sweep = check_liquidity_sweep(df_m15, level)
        if sweep is None:
            continue

        # Direzione del movimento post-sweep
        direction = "BUY" if sweep["direction"] == "BEARISH" else "SELL"

        trigger = find_post_sweep_trigger(df_m15, sweep, direction)
        if trigger is None:
            continue

        # Setup trovato — calcola SL/TP
        diag["sweep"]   = sweep
        diag["trigger"] = trigger

        entry = trigger["entry"]
        sl    = compute_sl(sweep, direction)

        # Validità SL
        if direction == "BUY" and sl >= entry:
            continue
        if direction == "SELL" and sl <= entry:
            continue

        risk = abs(entry - sl)
        if risk <= 0:
            continue

        tp_level = find_tp_target(mfm, entry, direction, level)
        if tp_level is None:
            continue

        tp = tp_level["price"]

        # TP deve essere nella direzione corretta
        if direction == "BUY" and tp <= entry:
            continue
        if direction == "SELL" and tp >= entry:
            continue

        rr = abs(tp - entry) / risk

        # Quality Score
        quality_score, quality_label = compute_quality(level, sweep, trigger, df_m15)

        # LOW → nessun segnale
        if quality_label == "LOW":
            return reject(f"QUALITY_TOO_LOW (score={quality_score})")

        # Timestamp trigger
        try:
            trig_candle = df_m15.iloc[trigger["candle_idx"]]
            trig_ts_ms  = int(trig_candle["timestamp"])
            trig_dt     = datetime.fromtimestamp(trig_ts_ms / 1000, tz=timezone.utc)
        except Exception:
            trig_dt = now

        signal = {
            "signal_id":        str(uuid.uuid4()),
            "strategy_name":    STRATEGY_NAME,
            "strategy_version": STRATEGY_VERSION,
            "asset":            asset,
            "direction":        direction,
            "timestamp_setup":  trig_dt.isoformat(),

            # Prezzi
            "entry":     entry,
            "stop_loss": sl,
            "tp":        tp,
            "risk":      risk,
            "rr":        round(rr, 2),

            # Livello sweepato
            "swept_level_label":    level["label"],
            "swept_level_price":    level["price"],
            "swept_level_priority": level.get("priority_label"),
            "swept_level_touches":  level.get("historical_touches", 0),

            # Sweep
            "sweep_direction":  sweep["direction"],
            "sweep_peak_price": sweep["peak_price"],
            "sweep_candle_idx": sweep["candle_idx"],
            "sweep_penetration": round(sweep["penetration"], 6),

            # Trigger
            "trigger_type":     trigger["trigger_type"],
            "trigger_ref_level": trigger["ref_level"],

            # TP target
            "tp_label":    tp_level["label"],
            "tp_priority": tp_level.get("priority_label"),

            # Quality
            "quality_score": quality_score,
            "quality_label": quality_label,

            # Tracking
            "final_outcome": "OPEN",
            "expiry_bars":   EXPIRY_BARS_M15,
        }

        logger.info(
            "LH [%s %s]: SIGNAL entry=%.4f sl=%.4f tp=%.4f rr=%.2f "
            "level=%s sweep=%s trigger=%s quality=%d (%s)",
            asset, direction, entry, sl, tp, rr,
            level["label"], sweep["direction"],
            trigger["trigger_type"], quality_score, quality_label,
        )

        return {"signal": signal, "diagnostics": diag}

    return reject("NO_VALID_SETUP (nessun sweep + trigger trovato sui livelli attivi)")
