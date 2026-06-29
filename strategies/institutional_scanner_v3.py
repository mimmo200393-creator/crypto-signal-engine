"""
strategies/institutional_scanner_v3.py
Institutional Scanner Framework V3.2 — FROZEN

Asset: PAXG_USDT e BTC_USDT, valutati indipendentemente.

Filosofia: assistente decisionale di trend-following strutturale.
Identifica il trend dominante (Dow Theory su H4), attende un pullback
sano, verifica la transizione strutturale su M30, ed esegue solo su
Break of Structure confermato su M15.

Sequenza:
    Trend (H4 Dow Theory)
    -> Pullback (H1, con invalidazione)
    -> Transition (M30, obbligatoria)
    -> Continuation/Execution (M15 BOS, obbligatorio)

Score massimo: 9.

Versione: V3.2 Frozen — non modificare fino a 100 segnali per asset,
salvo correzioni di bug tecnici. PAXG_USDT e BTC_USDT sono valutati
e tracciati in modo completamente indipendente.
"""

from datetime import datetime, timezone
from typing import Optional

import numpy as np
import pandas as pd


V3_ASSETS = ["PAXG_USDT", "BTC_USDT"]

# ============================================================
# Parametri H4 — Dow Theory Structure
# ============================================================
H4_PIVOT_LOOKBACK = 3
ZONE_CLUSTER_ATR_FRACTION = 0.5
ZONE_MIN_PIVOT_COUNT = 2
STRONG_ZONE_MIN_PIVOT_COUNT = 3

# ============================================================
# Parametri H1 — Pullback
# ============================================================
H1_OPPOSING_STRUCTURE_MIN_CANDLES = 3
ACCELERATION_ATR_MULTIPLIER = 1.5

# ============================================================
# Parametri M30/M15
# ============================================================
M15_BOS_LOOKBACK = 3

# ============================================================
# Risk Management
# ============================================================
M15_SL_ATR_MULTIPLIER = 1.5
MIN_RR = 2.0
RR_BONUS_THRESHOLD = 3.0
MIN_TP1_ATR_MULTIPLE = 1.0

# ============================================================
# Fibonacci / OTE
# ============================================================
FIBONACCI_LEVELS = [0.0, 0.5, 0.62, 0.786, 1.0]
OTE_LOW = 0.62
OTE_HIGH = 0.786
OTE_TOLERANCE_PCT = 0.0015

# ============================================================
# Scoring (massimo 9)
# ============================================================
SCORE_DAILY_ALIGNMENT = 1.0
SCORE_STRONG_ZONE = 2.0
SCORE_OTE = 1.0
SCORE_M30_TRANSITION = 2.0
SCORE_M15_BOS = 2.0
SCORE_RR_BONUS = 1.0
SCORE_MAX = 9.0


# ============================================================
# Daily Context (Quality enhancement, non bloccante)
# ============================================================

def evaluate_daily_context(df_d1: pd.DataFrame) -> str:
    """Ritorna BULLISH / BEARISH / NEUTRAL in base a EMA50/EMA200 D1."""
    if len(df_d1) < 1 or "ema_50" not in df_d1.columns or "ema_200" not in df_d1.columns:
        return "NEUTRAL"

    last = df_d1.iloc[-1]
    close = float(last["close"])
    ema50 = float(last["ema_50"])
    ema200 = float(last["ema_200"])

    if close > ema50 and close > ema200:
        return "BULLISH"
    if close < ema50 and close < ema200:
        return "BEARISH"
    return "NEUTRAL"


# ============================================================
# H4 Dow Theory Structure (mandatory)
# ============================================================

def find_pivots(df: pd.DataFrame, lookback: int = 3) -> dict:
    """Pivot High/Low simmetrici con lookback configurabile."""
    highs = df["high"].values
    lows = df["low"].values
    timestamps = df["timestamp"].values
    n = len(df)

    pivot_highs, pivot_lows = [], []

    for i in range(lookback, n - lookback):
        before_h = highs[i - lookback:i]
        after_h = highs[i + 1:i + 1 + lookback]
        if highs[i] > before_h.max() and highs[i] > after_h.max():
            pivot_highs.append((int(timestamps[i]), float(highs[i]), i))

        before_l = lows[i - lookback:i]
        after_l = lows[i + 1:i + 1 + lookback]
        if lows[i] < before_l.min() and lows[i] < after_l.min():
            pivot_lows.append((int(timestamps[i]), float(lows[i]), i))

    return {"pivot_highs": pivot_highs, "pivot_lows": pivot_lows}


def evaluate_h4_structure(df_h4: pd.DataFrame) -> dict:
    """
    Determina la struttura dominante H4 secondo Dow Theory.

    Bullish: sequenza di Higher Highs + Higher Lows.
    Bearish: sequenza di Lower Highs + Lower Lows.
    Neutral: sequenze mescolate (es. HH seguito da LL).

    Ritorna anche l'ultimo Higher Low (per BUY) o Lower High (per SELL)
    rilevante per il tracking di invalidazione del pullback.
    """
    pivots = find_pivots(df_h4, H4_PIVOT_LOOKBACK)
    highs = sorted(pivots["pivot_highs"], key=lambda p: p[2])
    lows = sorted(pivots["pivot_lows"], key=lambda p: p[2])

    if len(highs) < 2 or len(lows) < 2:
        return {"structure": "NEUTRAL", "last_higher_low": None, "last_lower_high": None}

    last_highs = highs[-2:]
    last_lows = lows[-2:]

    higher_highs = last_highs[1][1] > last_highs[0][1]
    higher_lows = last_lows[1][1] > last_lows[0][1]
    lower_highs = last_highs[1][1] < last_highs[0][1]
    lower_lows = last_lows[1][1] < last_lows[0][1]

    if higher_highs and higher_lows:
        structure = "BULLISH"
    elif lower_highs and lower_lows:
        structure = "BEARISH"
    else:
        structure = "NEUTRAL"

    return {
        "structure": structure,
        "last_higher_low": last_lows[-1][1] if structure == "BULLISH" else None,
        "last_lower_high": last_highs[-1][1] if structure == "BEARISH" else None,
    }


# ============================================================
# H4 Operational Zones
# ============================================================

def build_h4_zones(df_h4: pd.DataFrame, atr_h4: float) -> list:
    """Costruisce le zone H4 con upper/lower boundary e pivot count."""
    pivots = find_pivots(df_h4, H4_PIVOT_LOOKBACK)
    all_pivots = [(t, p) for t, p, _ in pivots["pivot_highs"] + pivots["pivot_lows"]]
    if not all_pivots:
        return []

    threshold = atr_h4 * ZONE_CLUSTER_ATR_FRACTION if atr_h4 else 0
    prices = sorted([p for _, p in all_pivots])

    clusters = []
    current = [prices[0]]
    for p in prices[1:]:
        if threshold > 0 and (p - current[-1]) <= threshold:
            current.append(p)
        else:
            clusters.append(current)
            current = [p]
    clusters.append(current)

    zones = []
    for c in clusters:
        pivot_count = len(c)
        if pivot_count < ZONE_MIN_PIVOT_COUNT:
            continue
        zones.append({
            "upper_boundary": float(max(c)),
            "lower_boundary": float(min(c)),
            "pivot_count": pivot_count,
            "is_strong": pivot_count >= STRONG_ZONE_MIN_PIVOT_COUNT,
        })

    return zones


def price_in_zone(price: float, zone: dict, tolerance_pct: float = 0.003) -> bool:
    span = zone["upper_boundary"] - zone["lower_boundary"]
    tol = span * tolerance_pct + zone["upper_boundary"] * tolerance_pct
    return (zone["lower_boundary"] - tol) <= price <= (zone["upper_boundary"] + tol)


# ============================================================
# Fibonacci / OTE
# ============================================================

def check_ote(df_h1: pd.DataFrame, direction: str) -> bool:
    """
    Applica Fibonacci all'ultimo impulso H1 significativo (ultime 30 candele)
    e verifica se il prezzo corrente e' nell'area OTE (0.62-0.786).
    """
    if len(df_h1) < 30:
        return False

    recent = df_h1.iloc[-30:]
    swing_high = float(recent["high"].max())
    swing_low = float(recent["low"].min())
    impulse = swing_high - swing_low
    current_price = float(df_h1.iloc[-1]["close"])

    if impulse <= 0:
        return False

    if direction == "BUY":
        ote_upper = swing_high - impulse * OTE_LOW
        ote_lower = swing_high - impulse * OTE_HIGH
    else:
        ote_lower = swing_low + impulse * OTE_LOW
        ote_upper = swing_low + impulse * OTE_HIGH

    lo, hi = min(ote_lower, ote_upper), max(ote_lower, ote_upper)
    tol = hi * OTE_TOLERANCE_PCT
    return (lo - tol) <= current_price <= (hi + tol)


# ============================================================
# H1 Pullback Assessment
# ============================================================

def evaluate_h1_pullback(df_h1: pd.DataFrame, df_m15: pd.DataFrame,
                          h4_structure: dict, zones: list, direction: str) -> dict:
    """
    Verifica se e' in corso un pullback valido contro il trend H4,
    e se e' stato invalidato.

    Almeno una condizione tra:
        - ritracciamento in zona H4 operativa
        - ritracciamento nell'area OTE
        - almeno 3 candele M15 consecutive contro il trend H4
        - struttura H1 temporanea opposta a H4
    """
    current_price = float(df_h1.iloc[-1]["close"])

    in_zone = any(price_in_zone(current_price, z, tolerance_pct=0.004) for z in zones)
    in_ote = check_ote(df_h1, direction)

    opposing_m15 = False
    if len(df_m15) >= H1_OPPOSING_STRUCTURE_MIN_CANDLES:
        last_n = df_m15.iloc[-H1_OPPOSING_STRUCTURE_MIN_CANDLES:]
        if direction == "BUY":
            opposing_m15 = all(float(c["close"]) < float(c["open"]) for _, c in last_n.iterrows())
        else:
            opposing_m15 = all(float(c["close"]) > float(c["open"]) for _, c in last_n.iterrows())

    h1_pivots = find_pivots(df_h1, 3)
    temp_opposing_structure = False
    if direction == "BUY" and len(h1_pivots["pivot_highs"]) >= 2:
        last_two = sorted(h1_pivots["pivot_highs"], key=lambda p: p[2])[-2:]
        temp_opposing_structure = last_two[1][1] < last_two[0][1]
    elif direction == "SELL" and len(h1_pivots["pivot_lows"]) >= 2:
        last_two = sorted(h1_pivots["pivot_lows"], key=lambda p: p[2])[-2:]
        temp_opposing_structure = last_two[1][1] > last_two[0][1]

    conditions_met = [in_zone, in_ote, opposing_m15, temp_opposing_structure]
    valid_pullback = any(conditions_met)

    if in_zone:
        pullback_type = "H4_ZONE"
    elif in_ote:
        pullback_type = "OTE"
    elif opposing_m15:
        pullback_type = "M15_OPPOSING_CANDLES"
    elif temp_opposing_structure:
        pullback_type = "TEMP_H1_STRUCTURE"
    else:
        pullback_type = "NONE"

    invalidated = False
    if direction == "BUY" and h4_structure.get("last_higher_low") is not None:
        invalidated = current_price < h4_structure["last_higher_low"]
    elif direction == "SELL" and h4_structure.get("last_lower_high") is not None:
        invalidated = current_price > h4_structure["last_lower_high"]

    return {
        "valid": valid_pullback and not invalidated,
        "invalidated": invalidated,
        "pullback_type": pullback_type,
        "in_ote": in_ote,
    }


# ============================================================
# M30 Structural Transition (mandatory)
# ============================================================

def evaluate_m30_transition(df_m30: pd.DataFrame, direction: str) -> bool:
    """
    BUY: break sopra l'ultimo Lower High + formazione di un Higher Low.
    SELL: break sotto l'ultimo Higher Low + formazione di un Lower High.
    """
    if len(df_m30) < 15:
        return False

    pivots = find_pivots(df_m30, 3)
    current_price = float(df_m30.iloc[-1]["close"])

    if direction == "BUY":
        highs = sorted(pivots["pivot_highs"], key=lambda p: p[2])
        lows = sorted(pivots["pivot_lows"], key=lambda p: p[2])
        if len(highs) < 1 or len(lows) < 2:
            return False
        last_lh = highs[-1][1]
        break_above = current_price > last_lh
        higher_low_formed = lows[-1][1] > lows[-2][1]
        return break_above and higher_low_formed
    else:
        highs = sorted(pivots["pivot_highs"], key=lambda p: p[2])
        lows = sorted(pivots["pivot_lows"], key=lambda p: p[2])
        if len(lows) < 1 or len(highs) < 2:
            return False
        last_hl = lows[-1][1]
        break_below = current_price < last_hl
        lower_high_formed = highs[-1][1] < highs[-2][1]
        return break_below and lower_high_formed


# ============================================================
# M15 Final Trigger — Break of Structure (mandatory)
# ============================================================

def _find_m15_swing(df_m15: pd.DataFrame, swing_type: str, lookback: int = M15_BOS_LOOKBACK):
    highs = df_m15["high"].values
    lows = df_m15["low"].values
    n = len(df_m15)

    for i in range(n - lookback - 1, lookback - 1, -1):
        if swing_type == "high":
            before = highs[i - lookback:i]
            after = highs[i + 1:i + 1 + lookback]
            if len(before) == lookback and len(after) == lookback:
                if highs[i] > before.max() and highs[i] > after.max():
                    return float(highs[i])
        else:
            before = lows[i - lookback:i]
            after = lows[i + 1:i + 1 + lookback]
            if len(before) == lookback and len(after) == lookback:
                if lows[i] < before.min() and lows[i] < after.min():
                    return float(lows[i])
    return None


def evaluate_m15_bos(df_m15: pd.DataFrame, direction: str) -> bool:
    """
    BOS confermato solo da CLOSE oltre il livello strutturale,
    non da semplice penetrazione di wick.
    """
    if len(df_m15) < M15_BOS_LOOKBACK + 3:
        return False

    last_close = float(df_m15.iloc[-1]["close"])

    if direction == "BUY":
        swing_high = _find_m15_swing(df_m15.iloc[:-1], "high", M15_BOS_LOOKBACK)
        return swing_high is not None and last_close > swing_high
    else:
        swing_low = _find_m15_swing(df_m15.iloc[:-1], "low", M15_BOS_LOOKBACK)
        return swing_low is not None and last_close < swing_low


# ============================================================
# Session
# ============================================================

def get_session(dt: datetime) -> str:
    h = dt.hour
    if 7 <= h < 10:
        return "LONDON"
    if 13 <= h < 16:
        return "NEW_YORK"
    return "ASIA"


# ============================================================
# Target identification (TP1/TP2/TP3)
# ============================================================

def find_m15_structure_target(df_m15: pd.DataFrame, direction: str, entry: float) -> Optional[float]:
    pivots = find_pivots(df_m15, M15_BOS_LOOKBACK)
    if direction == "BUY":
        candidates = [p for _, p, _ in pivots["pivot_highs"] if p > entry]
        return min(candidates) if candidates else None
    else:
        candidates = [p for _, p, _ in pivots["pivot_lows"] if p < entry]
        return max(candidates) if candidates else None


def find_h1_structure_target(df_h1: pd.DataFrame, direction: str, entry: float) -> Optional[float]:
    pivots = find_pivots(df_h1, 3)
    if direction == "BUY":
        candidates = [p for _, p, _ in pivots["pivot_highs"] if p > entry]
        return min(candidates) if candidates else None
    else:
        candidates = [p for _, p, _ in pivots["pivot_lows"] if p < entry]
        return max(candidates) if candidates else None


def find_opposing_h4_zone(zones: list, direction: str, entry: float) -> Optional[float]:
    if direction == "BUY":
        candidates = [z["lower_boundary"] for z in zones if z["lower_boundary"] > entry]
        return min(candidates) if candidates else None
    else:
        candidates = [z["upper_boundary"] for z in zones if z["upper_boundary"] < entry]
        return max(candidates) if candidates else None


# ============================================================
# Pipeline principale
# ============================================================

def generate_v3_signal(market_data: dict) -> dict:
    """
    Pipeline completa Institutional Scanner Framework V3.2.

    market_data deve contenere:
        asset, df_d1, df_h4, df_h1, df_m30, df_m15 (con EMA/ATR gia' calcolati)
        timestamp (datetime corrente)

    Ritorna {"signal": dict|None, "diagnostics": dict}.
    """
    asset = market_data["asset"]
    df_d1 = market_data["df_d1"]
    df_h4 = market_data["df_h4"]
    df_h1 = market_data["df_h1"]
    df_m30 = market_data["df_m30"]
    df_m15 = market_data["df_m15"]
    now = market_data.get("timestamp", datetime.now(timezone.utc))

    diagnostics = {"asset": asset, "rejections": []}

    if len(df_h4) < 15 or len(df_h1) < 35 or len(df_m30) < 20 or len(df_m15) < 10:
        diagnostics["rejections"].append("INSUFFICIENT_DATA")
        return {"signal": None, "diagnostics": diagnostics}

    atr_h4 = float(df_h4.iloc[-1]["atr"]) if "atr" in df_h4.columns else 0
    atr_m15 = float(df_m15.iloc[-1]["atr"]) if "atr" in df_m15.columns else 0

    if atr_h4 <= 0 or atr_m15 <= 0:
        diagnostics["rejections"].append("ATR_ZERO")
        return {"signal": None, "diagnostics": diagnostics}

    daily_context = evaluate_daily_context(df_d1) if len(df_d1) > 0 else "NEUTRAL"
    diagnostics["daily_context"] = daily_context

    h4_struct = evaluate_h4_structure(df_h4)
    structure = h4_struct["structure"]
    diagnostics["h4_structure"] = structure

    if structure == "NEUTRAL":
        diagnostics["rejections"].append("H4_STRUCTURE_NEUTRAL")
        return {"signal": None, "diagnostics": diagnostics}

    direction = "BUY" if structure == "BULLISH" else "SELL"

    zones = build_h4_zones(df_h4, atr_h4)
    if not zones:
        diagnostics["rejections"].append("NO_H4_ZONES")
        return {"signal": None, "diagnostics": diagnostics}

    pullback = evaluate_h1_pullback(df_h1, df_m15, h4_struct, zones, direction)
    diagnostics["pullback"] = pullback

    if pullback["invalidated"]:
        diagnostics["rejections"].append("PULLBACK_INVALIDATED")
        return {"signal": None, "diagnostics": diagnostics}

    if not pullback["valid"]:
        diagnostics["rejections"].append("NO_VALID_PULLBACK")
        return {"signal": None, "diagnostics": diagnostics}

    last_h1 = df_h1.iloc[-1]
    last_h1_body = abs(float(last_h1["close"]) - float(last_h1["open"]))
    atr_h1 = float(df_h1.iloc[-1]["atr"]) if "atr" in df_h1.columns else 0
    is_bearish_h1 = float(last_h1["close"]) < float(last_h1["open"])
    is_bullish_h1 = float(last_h1["close"]) > float(last_h1["open"])

    strong_accel_against = False
    if atr_h1 > 0 and last_h1_body > ACCELERATION_ATR_MULTIPLIER * atr_h1:
        if direction == "BUY" and is_bearish_h1:
            strong_accel_against = True
        elif direction == "SELL" and is_bullish_h1:
            strong_accel_against = True

    if strong_accel_against:
        diagnostics["rejections"].append("STRONG_ACCELERATION_AGAINST_SETUP")
        return {"signal": None, "diagnostics": diagnostics}

    m30_transition = evaluate_m30_transition(df_m30, direction)
    diagnostics["m30_transition"] = m30_transition

    if not m30_transition:
        diagnostics["rejections"].append("NO_M30_TRANSITION")
        return {"signal": None, "diagnostics": diagnostics}

    bos_confirmed = evaluate_m15_bos(df_m15, direction)
    diagnostics["m15_bos"] = bos_confirmed

    if not bos_confirmed:
        diagnostics["rejections"].append("NO_M15_BOS")
        return {"signal": None, "diagnostics": diagnostics}

    entry = float(df_m15.iloc[-1]["close"])

    swing_type = "low" if direction == "BUY" else "high"
    structural_swing = _find_m15_swing(df_m15.iloc[:-1], swing_type, M15_BOS_LOOKBACK)

    if direction == "BUY":
        sl_atr = entry - M15_SL_ATR_MULTIPLIER * atr_m15
        sl_structure = structural_swing if structural_swing is not None else sl_atr
        stop_loss = min(sl_structure, sl_atr)
    else:
        sl_atr = entry + M15_SL_ATR_MULTIPLIER * atr_m15
        sl_structure = structural_swing if structural_swing is not None else sl_atr
        stop_loss = max(sl_structure, sl_atr)

    tp1 = find_m15_structure_target(df_m15, direction, entry)
    tp2 = find_h1_structure_target(df_h1, direction, entry)
    tp3 = find_opposing_h4_zone(zones, direction, entry)

    if tp1 is None:
        diagnostics["rejections"].append("NO_TP1")
        return {"signal": None, "diagnostics": diagnostics}

    risk = abs(entry - stop_loss)
    reward = abs(tp1 - entry)
    if risk <= 0:
        diagnostics["rejections"].append("RISK_ZERO")
        return {"signal": None, "diagnostics": diagnostics}

    rr = reward / risk
    diagnostics["rr"] = rr

    if rr < MIN_RR:
        diagnostics["rejections"].append(f"RR_TOO_LOW_{rr:.2f}")
        return {"signal": None, "diagnostics": diagnostics}

    tp1_distance = abs(tp1 - entry)
    if tp1_distance < MIN_TP1_ATR_MULTIPLE * atr_m15:
        diagnostics["rejections"].append("TP1_TOO_CLOSE")
        return {"signal": None, "diagnostics": diagnostics}

    ote_present = pullback["in_ote"]

    zone_used = next((z for z in zones if price_in_zone(entry, z, tolerance_pct=0.01)), zones[0])
    strong_zone = zone_used["is_strong"]

    session = get_session(now)

    daily_aligned = (
        (direction == "BUY" and daily_context == "BULLISH") or
        (direction == "SELL" and daily_context == "BEARISH")
    )

    score = 0.0
    if daily_aligned:
        score += SCORE_DAILY_ALIGNMENT
    if strong_zone:
        score += SCORE_STRONG_ZONE
    if ote_present:
        score += SCORE_OTE
    score += SCORE_M30_TRANSITION
    score += SCORE_M15_BOS
    if rr >= RR_BONUS_THRESHOLD:
        score += SCORE_RR_BONUS

    score = max(0.0, min(score, SCORE_MAX))

    signal = {
        "asset": asset,
        "direction": direction,
        "entry": entry,
        "stop_loss": stop_loss,
        "tp1": tp1,
        "tp2": tp2,
        "tp3": tp3,
        "rr": rr,
        "signal_quality": score,
        "daily_context_status": daily_context,
        "h4_structure_status": structure,
        "h4_zone_status": "STRONG" if strong_zone else "VALID",
        "ote_present": ote_present,
        "pullback_type": pullback["pullback_type"],
        "pullback_invalidated": False,
        "m30_transition_status": "CONFIRMED",
        "m15_bos_confirmed": True,
        "session": session,
        "timestamp_setup": now.isoformat(),
    }

    return {"signal": signal, "diagnostics": diagnostics}
