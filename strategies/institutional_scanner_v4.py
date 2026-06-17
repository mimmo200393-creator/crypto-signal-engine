"""
strategies/institutional_scanner_v4.py
Institutional Scanner Framework V4.0 — Daily Edition

Strategia indipendente da V3.2 Frozen. Riusa le funzioni di analisi
di mercato (pivot, struttura H4, zone, OTE, pullback, M30, BOS) da
institutional_scanner_v3.py, che sono pura logica di mercato e non
specifiche di una versione. Cambia SOLO quali condizioni sono
obbligatorie vs bonus, e lo schema di scoring.

Obiettivo: aumentare la frequenza dei segnali preservando l'integrita'
strutturale (pullback invalidation resta obbligatoria).

Mandatory:
    1. H4 dominant trend valido (non NEUTRAL)
    2. Pullback valido contro il trend H4
    3. Pullback non invalidato (Higher Low / Lower High violation)
    4. M15 BOS confermato (chiusura, non wick)
    5. R/R >= 2

Bonus (solo ranking, non bloccanti):
    M30 Structural Transition: +2
    OTE Confluence: +1
    Strong H4 Zone: +1
    Daily Alignment: +1

Score massimo: 5.

V3.2 e V4.0 coesistono come strategie indipendenti, tracciate
separatamente, per determinare empiricamente quale framework
performa meglio nel tempo.
"""

from datetime import datetime, timezone
from typing import Optional
import logging

import pandas as pd

logger = logging.getLogger("institutional_scanner_v4")

from strategies.institutional_scanner_v3 import (
    evaluate_daily_context,
    evaluate_h4_structure,
    find_pivots,
    build_h4_zones,
    price_in_zone,
    check_ote,
    evaluate_h1_pullback,
    evaluate_m30_transition,
    evaluate_m15_bos,
    get_session,
    find_m15_structure_target,
    find_h1_structure_target,
    find_opposing_h4_zone,
    _find_m15_swing,
    H4_PIVOT_LOOKBACK,
    M15_BOS_LOOKBACK,
    M15_SL_ATR_MULTIPLIER,
    MIN_TP1_ATR_MULTIPLE,
)

V4_ASSETS = ["PAXG_USDT", "BTC_USDT"]

MIN_RR = 2.0

# ============================================================
# Scoring (massimo 5) — solo ranking, mai bloccante
# ============================================================
SCORE_DAILY_ALIGNMENT = 1.0
SCORE_STRONG_ZONE = 1.0
SCORE_OTE = 1.0
SCORE_M30_TRANSITION = 2.0
SCORE_MAX = 5.0


# ============================================================
# Trend Filter Revision (V4.0 only) — Dow Theory + EMA50/200 H4
# ============================================================

def evaluate_ema_trend_h4(df_h4: pd.DataFrame) -> str:
    """
    Seconda fonte di valutazione del trend H4, indipendente dalla
    Dow Theory sui pivot. Usa EMA50/EMA200 calcolate su H4.

    Bullish: Close H4 > EMA50 H4 e EMA50 H4 > EMA200 H4
    Bearish: Close H4 < EMA50 H4 e EMA50 H4 < EMA200 H4
    Altrimenti: NEUTRAL
    """
    if len(df_h4) < 1 or "ema_50" not in df_h4.columns or "ema_200" not in df_h4.columns:
        return "NEUTRAL"

    last = df_h4.iloc[-1]
    close = float(last["close"])
    ema50 = float(last["ema_50"])
    ema200 = float(last["ema_200"])

    if close > ema50 and ema50 > ema200:
        return "BULLISH"
    if close < ema50 and ema50 < ema200:
        return "BEARISH"
    return "NEUTRAL"


def combine_h4_trend(dow_theory_structure: str, ema_trend: str) -> str:
    """
    Combina Dow Theory (pivot H4) ed EMA Trend (EMA50/200 H4) secondo
    la tabella di decisione V4.0:

        BULLISH + BULLISH = BULLISH
        BULLISH + NEUTRAL = BULLISH
        BULLISH + BEARISH = NEUTRAL (conflitto)
        BEARISH + BEARISH = BEARISH
        BEARISH + NEUTRAL = BEARISH
        BEARISH + BULLISH = NEUTRAL (conflitto)
        NEUTRAL + BULLISH = BULLISH
        NEUTRAL + BEARISH = BEARISH
        NEUTRAL + NEUTRAL = NEUTRAL
    """
    if dow_theory_structure == ema_trend:
        return dow_theory_structure

    if dow_theory_structure == "NEUTRAL":
        return ema_trend

    if ema_trend == "NEUTRAL":
        return dow_theory_structure

    # Dow Theory e EMA sono entrambi non-NEUTRAL ma diversi -> conflitto diretto
    return "NEUTRAL"


def _fallback_h4_pivot_for_invalidation(df_h4: pd.DataFrame, direction: str):
    """
    Quando il trend finale BULLISH/BEARISH deriva dall'EMA Trend
    (Dow Theory pura era NEUTRAL), evaluate_h4_structure() non
    popola last_higher_low/last_lower_high perche' la sua struttura
    pura non li richiede. Recuperiamo qui un pivot di riferimento
    valido per permettere comunque il controllo di invalidazione
    del pullback in evaluate_h1_pullback().
    """
    pivots = find_pivots(df_h4, H4_PIVOT_LOOKBACK)
    if direction == "BUY":
        lows = sorted(pivots["pivot_lows"], key=lambda p: p[2])
        return lows[-1][1] if lows else None
    else:
        highs = sorted(pivots["pivot_highs"], key=lambda p: p[2])
        return highs[-1][1] if highs else None


def generate_v4_signal(market_data: dict) -> dict:
    """
    Pipeline Institutional Scanner V4.0 Daily Edition.

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

    # --- MANDATORY 1: H4 dominant trend valido (Trend Filter Revision) ---
    # Combina Dow Theory (pivot H4) ed EMA50/200 H4 per ridurre i falsi
    # NEUTRAL quando il mercato ha gia' una direzione evidente secondo
    # le medie mobili. V3.2 Frozen NON e' toccato da questa modifica.
    h4_struct = evaluate_h4_structure(df_h4)
    dow_theory_structure = h4_struct["structure"]

    ema_trend = evaluate_ema_trend_h4(df_h4)

    structure = combine_h4_trend(dow_theory_structure, ema_trend)

    ema50_h4 = float(df_h4.iloc[-1]["ema_50"]) if "ema_50" in df_h4.columns else None
    ema200_h4 = float(df_h4.iloc[-1]["ema_200"]) if "ema_200" in df_h4.columns else None

    diagnostics["dow_theory_trend"] = dow_theory_structure
    diagnostics["ema50_h4"] = ema50_h4
    diagnostics["ema200_h4"] = ema200_h4
    diagnostics["ema_trend"] = ema_trend
    diagnostics["h4_structure"] = structure

    logger.info(
        "%s | Dow Theory Trend = %s | EMA50 H4 = %s EMA200 H4 = %s EMA Trend = %s | Final H4 Trend = %s",
        asset,
        dow_theory_structure,
        f"{ema50_h4:.4f}" if ema50_h4 is not None else "N/A",
        f"{ema200_h4:.4f}" if ema200_h4 is not None else "N/A",
        ema_trend,
        structure,
    )

    if structure == "NEUTRAL":
        diagnostics["rejections"].append("H4_STRUCTURE_NEUTRAL")
        return {"signal": None, "diagnostics": diagnostics}

    direction = "BUY" if structure == "BULLISH" else "SELL"

    # Se la Dow Theory pura era NEUTRAL ma il trend combinato e' BULLISH/
    # BEARISH grazie all'EMA, last_higher_low/last_lower_high non sono
    # popolati da evaluate_h4_structure(): recuperiamo un pivot di
    # riferimento valido per non perdere il controllo di invalidazione.
    if dow_theory_structure == "NEUTRAL":
        fallback_pivot = _fallback_h4_pivot_for_invalidation(df_h4, direction)
        if direction == "BUY":
            h4_struct["last_higher_low"] = fallback_pivot
        else:
            h4_struct["last_lower_high"] = fallback_pivot

    zones = build_h4_zones(df_h4, atr_h4)
    if not zones:
        diagnostics["rejections"].append("NO_H4_ZONES")
        return {"signal": None, "diagnostics": diagnostics}

    # --- MANDATORY 2 + 3: Pullback valido E non invalidato ---
    pullback = evaluate_h1_pullback(df_h1, df_m15, h4_struct, zones, direction)
    diagnostics["pullback"] = pullback

    if pullback["invalidated"]:
        diagnostics["rejections"].append("PULLBACK_INVALIDATED")
        return {"signal": None, "diagnostics": diagnostics}

    if not pullback["valid"]:
        diagnostics["rejections"].append("NO_VALID_PULLBACK")
        return {"signal": None, "diagnostics": diagnostics}

    # --- MANDATORY 4: M15 BOS confermato (chiusura, non wick) ---
    bos_confirmed = evaluate_m15_bos(df_m15, direction)
    diagnostics["m15_bos"] = bos_confirmed

    if not bos_confirmed:
        diagnostics["rejections"].append("NO_M15_BOS")
        return {"signal": None, "diagnostics": diagnostics}

    # --- Entry / Stop Loss ---
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

    # --- Take Profit ---
    tp1 = find_m15_structure_target(df_m15, direction, entry)
    tp2 = find_h1_structure_target(df_h1, direction, entry)
    tp3 = find_opposing_h4_zone(zones, direction, entry)

    if tp1 is None:
        diagnostics["rejections"].append("NO_TP1")
        return {"signal": None, "diagnostics": diagnostics}

    # --- MANDATORY 5: R/R >= 2 ---
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

    # --- Opportunity Filter (mantenuto da V3.2, non esplicitamente rimosso) ---
    tp1_distance = abs(tp1 - entry)
    if tp1_distance < MIN_TP1_ATR_MULTIPLE * atr_m15:
        diagnostics["rejections"].append("TP1_TOO_CLOSE")
        return {"signal": None, "diagnostics": diagnostics}

    # ============================================================
    # Tutte le condizioni obbligatorie superate: il segnale viene
    # generato. Da qui in poi solo bonus per il ranking.
    # ============================================================

    daily_context = evaluate_daily_context(df_d1) if len(df_d1) > 0 else "NEUTRAL"
    daily_aligned = (
        (direction == "BUY" and daily_context == "BULLISH") or
        (direction == "SELL" and daily_context == "BEARISH")
    )

    zone_used = next((z for z in zones if price_in_zone(entry, z, tolerance_pct=0.01)), zones[0])
    strong_zone = zone_used["is_strong"]

    ote_present = pullback["in_ote"]

    m30_transition = evaluate_m30_transition(df_m30, direction)

    session = get_session(now)

    score = 0.0
    if daily_aligned:
        score += SCORE_DAILY_ALIGNMENT
    if strong_zone:
        score += SCORE_STRONG_ZONE
    if ote_present:
        score += SCORE_OTE
    if m30_transition:
        score += SCORE_M30_TRANSITION

    score = max(0.0, min(score, SCORE_MAX))

    if score >= 4:
        quality_label = "HIGH"
    elif score >= 2:
        quality_label = "STANDARD"
    else:
        quality_label = "LOW"

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
        "quality_label": quality_label,
        "daily_context_status": daily_context,
        "h4_structure_status": structure,
        "h4_zone_status": "STRONG" if strong_zone else "VALID",
        "ote_present": ote_present,
        "pullback_type": pullback["pullback_type"],
        "pullback_invalidated": False,
        "m30_transition_status": "CONFIRMED" if m30_transition else "ABSENT",
        "m15_bos_confirmed": True,
        "session": session,
        "timestamp_setup": now.isoformat(),
    }

    return {"signal": signal, "diagnostics": diagnostics}
