"""
strategies/liquidity_hunter.py
Liquidity Hunter v1.1

Fix v1.1:
    - Bug 1: direzione corretta dopo sweep
      Sweep BULLISH (Low sweepato) → BUY (istituzionali cacciano stop SELL → rimbalzo)
      Sweep BEARISH (High sweepato) → SELL (istituzionali cacciano stop BUY → ribasso)
    - Bug 2: SL con buffer minimo in punti assoluti per evitare SL troppo stretto
    - Bug 3: RR minimo 1.5 — evita trade con risk/reward non favorevole

Pipeline:
    1. Liquidity Pool identificata dalla Money Flow Map
    2. Prezzo raggiunge la zona (proximity <= LIQUIDITY_PROXIMITY_PCT)
    3. Sweep confermato nelle ultime LIQUIDITY_SWEEP_LOOKBACK candele M15
    4. BOS o CHOCH M15 nella direzione del movimento post-sweep
    5. Entry alla chiusura del trigger

Gestione trade:
    SL: dietro il peak dello sweep + buffer
    TP: prossima Liquidity Pool nella direzione del movimento
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
STRATEGY_VERSION = "v1.1"

EXPIRY_BARS_M15           = 96      # 24h operative
LIQUIDITY_PROXIMITY_PCT   = 0.003   # 0.30%
LIQUIDITY_SWEEP_LOOKBACK  = 4       # candele M15
SWEEP_PENETRATION_MIN_PCT = 0.0003  # penetrazione minima oltre il livello
SL_BUFFER_PCT             = 0.001   # 0.10% buffer dietro peak sweep (era 0.05%)
ATR_SL_FLOOR_MULT         = 0.8     # floor distanza SL = 0.8×ATR M15 (propagato da V41P1 Sprint 13c)
MIN_RR                    = 1.5     # RR minimo — evita trade sbilanciati

# Quality Score bonus
SCORE_HIGH_PRIORITY_LEVEL = 3
SCORE_MEDIUM_PRIORITY_LEVEL = 2
SCORE_BOS_TRIGGER         = 2
SCORE_CHOCH_TRIGGER       = 1
SCORE_SWEEP_STRONG        = 2
SCORE_TOUCHES             = 1
SCORE_REJECTION_CANDLE    = 1


# ============================================================
# Step 1: Identifica Liquidity Pool attive
# ============================================================

def find_active_liquidity_pools(
    mfm: dict,
    current_price: float,
) -> list[dict]:
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
    Cerca sweep di un livello di liquidità nelle ultime
    LIQUIDITY_SWEEP_LOOKBACK candele M15.

    Sweep BEARISH (livello HIGH sweepato):
        High supera il livello + penetrazione minima
        Close richiude SOTTO il livello
        → Istituzionali cacciano stop BUY sopra il livello
        → Movimento atteso: SELL

    Sweep BULLISH (livello LOW sweepato):
        Low scende sotto il livello + penetrazione minima
        Close richiude SOPRA il livello
        → Istituzionali cacciano stop SELL sotto il livello
        → Movimento atteso: BUY
    """
    if len(df_m15) < LIQUIDITY_SWEEP_LOOKBACK + 2:
        return None

    lvl_price = level["price"]
    kind      = level["kind"]
    tol       = lvl_price * SWEEP_PENETRATION_MIN_PCT

    # Fix off-by-one: l'ultima candela (iloc[-1]) è trattata come CHIUSA
    # ovunque nel runner (monitoring stop, current_price), quindi deve
    # essere inclusa anche nella ricerca sweep. end punta all'ultima riga
    # valida; il range la include (era range(end-1, ...) che la escludeva).
    end   = len(df_m15) - 1
    start = max(1, end - LIQUIDITY_SWEEP_LOOKBACK)

    for i in range(end, start - 1, -1):
        candle  = df_m15.iloc[i]
        c_high  = float(candle["high"])
        c_low   = float(candle["low"])
        c_close = float(candle["close"])

        if kind == "high":
            # Sweep BEARISH: high supera il livello HIGH, close richiude sotto
            # → istituzionali cacciano stop BUY → movimento atteso SELL
            penetration = c_high - lvl_price
            if penetration >= tol and c_close < lvl_price:
                return {
                    "direction":        "BEARISH",
                    "expected_trade":   "SELL",
                    "peak_price":       c_high,
                    "candle_idx":       i,
                    "penetration":      penetration,
                    "level_price":      lvl_price,
                    "level_label":      level["label"],
                }

        else:  # kind == "low"
            # Sweep BULLISH: low scende sotto il livello LOW, close richiude sopra
            # → istituzionali cacciano stop SELL → movimento atteso BUY
            penetration = lvl_price - c_low
            if penetration >= tol and c_close > lvl_price:
                return {
                    "direction":        "BULLISH",
                    "expected_trade":   "BUY",
                    "peak_price":       c_low,
                    "candle_idx":       i,
                    "penetration":      penetration,
                    "level_price":      lvl_price,
                    "level_label":      level["label"],
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

    direction: "BUY" o "SELL" (da sweep["expected_trade"])
    """
    sweep_idx    = sweep["candle_idx"]
    search_start = sweep_idx + 1
    # Fix off-by-one: l'ultima candela è chiusa e va valutata come possibile
    # trigger. loop_end = len(df_m15) è il bound ESCLUSIVO per range(), così
    # range(search_start, loop_end) include l'ultimo indice len-1.
    # search_end resta il bound per gli slice iloc[:search_end] (già corretto).
    loop_end     = len(df_m15)
    search_end   = len(df_m15) - 1

    if search_start >= loop_end:
        return None

    pre_sweep = df_m15.iloc[:sweep_idx]
    if len(pre_sweep) < M15_BOS_LOOKBACK * 2 + 1:
        return None

    pivots = find_pivots(pre_sweep, M15_BOS_LOOKBACK)

    if direction == "BUY":
        highs = sorted(pivots["pivot_highs"], key=lambda p: p[2])
        if not highs:
            return None
        ref_high = highs[-1][1]

        for i in range(search_start, loop_end):
            c_close = float(df_m15.iloc[i]["close"])
            if c_close > ref_high:
                return {
                    "trigger_type": "BOS",
                    "candle_idx":   i,
                    "entry":        c_close,
                    "ref_level":    ref_high,
                }

        post_sweep = df_m15.iloc[sweep_idx:search_end]
        if len(post_sweep) >= 3:
            swing_high = float(post_sweep["high"].max())
            for i in range(search_start, loop_end):
                c_close = float(df_m15.iloc[i]["close"])
                if c_close > swing_high * 0.998:
                    return {
                        "trigger_type": "CHOCH",
                        "candle_idx":   i,
                        "entry":        c_close,
                        "ref_level":    swing_high,
                    }

    else:  # SELL
        lows = sorted(pivots["pivot_lows"], key=lambda p: p[2])
        if not lows:
            return None
        ref_low = lows[-1][1]

        for i in range(search_start, loop_end):
            c_close = float(df_m15.iloc[i]["close"])
            if c_close < ref_low:
                return {
                    "trigger_type": "BOS",
                    "candle_idx":   i,
                    "entry":        c_close,
                    "ref_level":    ref_low,
                }

        post_sweep = df_m15.iloc[sweep_idx:search_end]
        if len(post_sweep) >= 3:
            swing_low = float(post_sweep["low"].min())
            for i in range(search_start, loop_end):
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

def _compute_atr_m15(df_m15: pd.DataFrame, period: int = 14) -> float | None:
    """
    ATR M15 (Wilder/SMA semplice su True Range) calcolato in modo
    autocontenuto: LH non riceve ATR dal runner, quindi lo derivo qui
    dal df_m15 già disponibile. Ritorna None se dati insufficienti.
    """
    if df_m15 is None or len(df_m15) < period + 1:
        return None
    try:
        high  = df_m15["high"].astype(float)
        low   = df_m15["low"].astype(float)
        close = df_m15["close"].astype(float)
        prev_close = close.shift(1)
        tr = pd.concat([
            (high - low),
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ], axis=1).max(axis=1)
        atr = tr.rolling(window=period, min_periods=period).mean().iloc[-1]
        if pd.isna(atr) or atr <= 0:
            return None
        return float(atr)
    except Exception:
        return None


def compute_sl(
    sweep: dict,
    direction: str,
    entry: float | None = None,
    atr_m15: float | None = None,
) -> float:
    """
    SL dietro il peak dello sweep con buffer SL_BUFFER_PCT.

    BUY:  peak = minimo dello sweep → SL = peak - buffer
    SELL: peak = massimo dello sweep → SL = peak + buffer

    Buffer minimo garantito per evitare SL troppo stretti.

    Floor ATR (propagato da V41P1, Sprint 13c):
        Il buffer percentuale fisso (0.10%) è tarato sulla volatilità di BTC.
        Su asset a bassa volatilità come PAXG (ATR M15 ~3-4 punti), 0.10% può
        essere più stretto del rumore normale → stop-out prematuri che
        sembrano segnali errati ma sono solo SL mal posizionati.
        Se entry e atr_m15 sono forniti, si impone che la distanza entry→SL
        sia almeno ATR_SL_FLOOR_MULT * atr_m15.
    """
    peak   = sweep["peak_price"]
    buffer = peak * SL_BUFFER_PCT

    if direction == "BUY":
        sl = peak - buffer
    else:
        sl = peak + buffer

    # Floor ATR: garantisce una distanza minima strutturale entry→SL
    if entry is not None and atr_m15 is not None and atr_m15 > 0:
        min_dist = ATR_SL_FLOOR_MULT * atr_m15
        if direction == "BUY":
            sl = min(sl, entry - min_dist)
        else:
            sl = max(sl, entry + min_dist)

    return sl


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
    score = 0

    priority = swept_level.get("priority_label", "LOW")
    if priority in ("CRITICAL", "HIGH"):
        score += SCORE_HIGH_PRIORITY_LEVEL
    elif priority == "MEDIUM":
        score += SCORE_MEDIUM_PRIORITY_LEVEL

    if trigger["trigger_type"] == "BOS":
        score += SCORE_BOS_TRIGGER
    else:
        score += SCORE_CHOCH_TRIGGER

    min_pen = swept_level["price"] * SWEEP_PENETRATION_MIN_PCT
    if sweep.get("penetration", 0) > min_pen * 2:
        score += SCORE_SWEEP_STRONG

    if swept_level.get("historical_touches", 0) >= 3:
        score += SCORE_TOUCHES

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
            score += SCORE_REJECTION_CANDLE

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
    diag = {
        "strategy":     STRATEGY_NAME,
        "asset":        asset,
        "rejection":    None,
        "active_pools": [],
        "sweep":        None,
        "trigger":      None,
    }

    def reject(reason: str) -> dict:
        diag["rejection"] = reason
        logger.info("LH [%s]: REJECT %s", asset, reason)
        return {"signal": None, "diagnostics": diag}

    if len(df_m15) < LIQUIDITY_SWEEP_LOOKBACK + 10:
        return reject("INSUFFICIENT_M15_DATA")

    current_price = float(df_m15.iloc[-1]["close"])

    # ATR M15 per il floor sullo SL (fix propagato da V41P1)
    atr_m15 = _compute_atr_m15(df_m15)

    # ── Step 1: Liquidity Pools attive ──────────────────────
    active_pools = find_active_liquidity_pools(mfm, current_price)
    diag["active_pools"] = [lv["label"] for lv in active_pools]

    if not active_pools:
        return reject("NO_ACTIVE_LIQUIDITY_POOL")

    # ── Step 2+3: Sweep + Trigger ────────────────────────────
    for level in active_pools:
        sweep = check_liquidity_sweep(df_m15, level)
        if sweep is None:
            continue

        # ── Fix Bug 1: direzione corretta ────────────────────
        # Sweep BULLISH (Low sweepato) → BUY
        # Sweep BEARISH (High sweepato) → SELL
        direction = sweep["expected_trade"]

        trigger = find_post_sweep_trigger(df_m15, sweep, direction)
        if trigger is None:
            continue

        diag["sweep"]   = sweep
        diag["trigger"] = trigger

        entry = trigger["entry"]

        # ── Fix Bug 2: SL con buffer adeguato + floor ATR ───
        sl   = compute_sl(sweep, direction, entry=entry, atr_m15=atr_m15)
        risk = abs(entry - sl)

        # Validità SL
        if direction == "BUY" and sl >= entry:
            logger.debug("LH [%s]: SL_INVALID_BUY sl=%.4f entry=%.4f", asset, sl, entry)
            continue
        if direction == "SELL" and sl <= entry:
            logger.debug("LH [%s]: SL_INVALID_SELL sl=%.4f entry=%.4f", asset, sl, entry)
            continue
        if risk <= 0:
            continue

        tp_level = find_tp_target(mfm, entry, direction, level)
        if tp_level is None:
            continue

        tp = tp_level["price"]

        if direction == "BUY" and tp <= entry:
            continue
        if direction == "SELL" and tp >= entry:
            continue

        rr = abs(tp - entry) / risk

        # ── Fix Bug 3: RR minimo ─────────────────────────────
        if rr < MIN_RR:
            logger.info("LH [%s]: RR_TOO_LOW (%.2f < %.1f)", asset, rr, MIN_RR)
            continue

        quality_score, quality_label = compute_quality(level, sweep, trigger, df_m15)

        if quality_label == "LOW":
            return reject(f"QUALITY_TOO_LOW (score={quality_score})")

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

            "entry":     entry,
            "stop_loss": sl,
            "tp":        tp,
            "risk":      risk,
            "rr":        round(rr, 2),

            "swept_level_label":    level["label"],
            "swept_level_price":    level["price"],
            "swept_level_priority": level.get("priority_label"),
            "swept_level_touches":  level.get("historical_touches", 0),

            "sweep_direction":   sweep["direction"],
            "sweep_peak_price":  sweep["peak_price"],
            "sweep_candle_idx":  sweep["candle_idx"],
            "sweep_penetration": round(sweep["penetration"], 6),

            "trigger_type":      trigger["trigger_type"],
            "trigger_ref_level": trigger["ref_level"],

            "tp_label":    tp_level["label"],
            "tp_priority": tp_level.get("priority_label"),

            "quality_score": quality_score,
            "quality_label": quality_label,

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
