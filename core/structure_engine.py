"""
core/structure_engine.py
Structure Engine V1.0 — Moduli condivisi per BOS, CHOCH, Pullback Invalidation

Corregge i tre errori critici identificati nell'Audit di Consolidamento:
    1. BOS: lookback da 2 a 3, persistenza di 3 candele
    2. CHOCH: valuta la struttura M15 REALE (non confronta con H4)
    3. Pullback Invalidation: gate universale per tutte le strategie

Aggiunge campi informativi (non bloccanti) per raccolta dati:
    - Displacement detection
    - Volume ratio al trigger
    - Premium/Discount zone

Tutte le funzioni sono PURE: accettano DataFrame e parametri,
ritornano dati strutturati. Nessuno stato interno. Nessuna
dipendenza da altri moduli del progetto eccetto pandas/numpy.

Usage:
    from core.structure_engine import (
        evaluate_bos_v2,
        evaluate_choch_v2,
        is_pullback_valid,
        check_displacement,
        compute_volume_ratio,
        compute_premium_discount,
    )
"""

from __future__ import annotations

import logging
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger("structure_engine")

# ============================================================
# Parametri (configurabili, documentati)
# ============================================================

# BOS
BOS_LOOKBACK = 3            # candele per lato per definire uno swing M15 (era 2)
BOS_PERSISTENCE = 3         # il BOS resta valido per N candele dopo il rilevamento (era 1)
BOS_MIN_PENETRATION_PCT = 0.0003  # penetrazione minima oltre lo swing (0.03%)

# CHOCH
CHOCH_LOOKBACK = 3          # lookback per i pivot M15 che definiscono la struttura
CHOCH_MIN_PIVOTS = 2        # minimo 2 highs E 2 lows per determinare la struttura

# Displacement
DISP_BODY_PCT = 0.60        # rapporto corpo/range minimo per candela impulsiva
DISP_MIN_CANDLES = 2        # candele impulsive consecutive minime
DISP_ATR_MULT = 1.5         # ampiezza totale minima in multipli di ATR


# ============================================================
# Utility: find_swing con lookback configurabile
# ============================================================

def find_swing(df: pd.DataFrame, swing_type: str, lookback: int = BOS_LOOKBACK) -> Optional[dict]:
    """
    Trova lo swing high o low più recente nel DataFrame.
    A differenza di _find_m15_swing() originale, ritorna un dict con
    prezzo, indice e timestamp — non solo il prezzo.

    Args:
        df: DataFrame con colonne high, low, timestamp
        swing_type: "high" o "low"
        lookback: candele per lato (default: BOS_LOOKBACK=3)

    Returns:
        {"price": float, "index": int, "timestamp": int} oppure None
    """
    if len(df) < lookback * 2 + 1:
        return None

    values = df["high"].values if swing_type == "high" else df["low"].values
    timestamps = df["timestamp"].values
    n = len(df)

    for i in range(n - lookback - 1, lookback - 1, -1):
        before = values[i - lookback:i]
        after = values[i + 1:i + 1 + lookback]

        if len(before) < lookback or len(after) < lookback:
            continue

        if swing_type == "high":
            if values[i] > before.max() and values[i] > after.max():
                return {"price": float(values[i]), "index": i, "timestamp": int(timestamps[i])}
        else:
            if values[i] < before.min() and values[i] < after.min():
                return {"price": float(values[i]), "index": i, "timestamp": int(timestamps[i])}

    return None


def find_recent_pivots(df: pd.DataFrame, lookback: int = CHOCH_LOOKBACK,
                        max_pivots: int = 6) -> dict:
    """
    Trova gli ultimi N pivot highs e lows nel DataFrame.
    Ritorna liste ordinate per timestamp crescente.

    Returns:
        {
            "highs": [{"price": float, "index": int, "timestamp": int}, ...],
            "lows":  [{"price": float, "index": int, "timestamp": int}, ...],
        }
    """
    if len(df) < lookback * 2 + 1:
        return {"highs": [], "lows": []}

    highs_arr = df["high"].values
    lows_arr = df["low"].values
    timestamps = df["timestamp"].values
    n = len(df)

    pivot_highs = []
    pivot_lows = []

    for i in range(lookback, n - lookback):
        before_h = highs_arr[i - lookback:i]
        after_h = highs_arr[i + 1:i + 1 + lookback]
        if highs_arr[i] > before_h.max() and highs_arr[i] > after_h.max():
            pivot_highs.append({
                "price": float(highs_arr[i]),
                "index": i,
                "timestamp": int(timestamps[i]),
            })

        before_l = lows_arr[i - lookback:i]
        after_l = lows_arr[i + 1:i + 1 + lookback]
        if lows_arr[i] < before_l.min() and lows_arr[i] < after_l.min():
            pivot_lows.append({
                "price": float(lows_arr[i]),
                "index": i,
                "timestamp": int(timestamps[i]),
            })

    # Ritorna gli ultimi max_pivots, ordinati per timestamp crescente
    return {
        "highs": pivot_highs[-max_pivots:],
        "lows": pivot_lows[-max_pivots:],
    }


# ============================================================
# BOS V2 — con lookback=3 e persistenza
# ============================================================

def evaluate_bos_v2(df_m15: pd.DataFrame, direction: str,
                     lookback: int = BOS_LOOKBACK,
                     persistence: int = BOS_PERSISTENCE,
                     min_penetration_pct: float = BOS_MIN_PENETRATION_PCT) -> dict:
    """
    Break of Structure V2: corregge i 3 errori dell'implementazione V1.

    Fix 1: lookback=3 anziché 2 (swing più strutturale)
    Fix 2: controlla le ultime `persistence` candele, non solo l'ultima
    Fix 3: richiede penetrazione minima per evitare BOS su micro-movimenti

    Args:
        df_m15: DataFrame M15 con colonne high, low, close, timestamp
        direction: "BUY" o "SELL"
        lookback: candele per lato per il pivot (default 3)
        persistence: candele da controllare per il BOS (default 3)
        min_penetration_pct: penetrazione minima in percentuale (default 0.03%)

    Returns:
        {
            "confirmed": bool,
            "ref_level": float or None,        # il livello di swing rotto
            "trigger_candle_idx": int or None,  # indice della candela che ha rotto
            "penetration_pct": float,           # penetrazione percentuale
            "displacement": bool,               # la candela trigger ha displacement?
        }
    """
    result = {
        "confirmed": False,
        "ref_level": None,
        "trigger_candle_idx": None,
        "penetration_pct": 0.0,
        "displacement": False,
    }

    if len(df_m15) < lookback * 2 + persistence + 3:
        return result

    # Trova lo swing di riferimento (escludendo le ultime `persistence` candele)
    df_for_swing = df_m15.iloc[:-persistence]
    swing_type = "high" if direction == "BUY" else "low"
    swing = find_swing(df_for_swing, swing_type, lookback)

    if swing is None:
        return result

    result["ref_level"] = swing["price"]

    # Controlla se QUALSIASI delle ultime `persistence` candele ha chiuso oltre
    for offset in range(-persistence, 0):
        candle = df_m15.iloc[offset]
        close = float(candle["close"])
        high = float(candle["high"])
        low = float(candle["low"])
        open_ = float(candle["open"])

        if direction == "BUY":
            if close > swing["price"]:
                penetration = (close - swing["price"]) / swing["price"] if swing["price"] != 0 else 0
                if penetration >= min_penetration_pct:
                    body = abs(close - open_)
                    range_ = high - low
                    has_displacement = (body / range_ > DISP_BODY_PCT) if range_ > 0 else False

                    result["confirmed"] = True
                    result["trigger_candle_idx"] = len(df_m15) + offset
                    result["penetration_pct"] = penetration
                    result["displacement"] = has_displacement
                    return result

        else:  # SELL
            if close < swing["price"]:
                penetration = (swing["price"] - close) / swing["price"] if swing["price"] != 0 else 0
                if penetration >= min_penetration_pct:
                    body = abs(close - open_)
                    range_ = high - low
                    has_displacement = (body / range_ > DISP_BODY_PCT) if range_ > 0 else False

                    result["confirmed"] = True
                    result["trigger_candle_idx"] = len(df_m15) + offset
                    result["penetration_pct"] = penetration
                    result["displacement"] = has_displacement
                    return result

    return result


# ============================================================
# CHOCH V2 — valuta la struttura M15 REALE
# ============================================================

def classify_m15_structure(df_m15: pd.DataFrame,
                            lookback: int = CHOCH_LOOKBACK,
                            min_pivots: int = CHOCH_MIN_PIVOTS) -> dict:
    """
    Determina la struttura corrente del timeframe M15 basandosi sulla
    sequenza dei suoi pivot — NON confrontando con H4.

    La struttura M15 è:
        BEARISH: gli ultimi 2+ highs fanno Lower Highs E gli ultimi 2+ lows fanno Lower Lows
        BULLISH: gli ultimi 2+ highs fanno Higher Highs E gli ultimi 2+ lows fanno Higher Lows
        NEUTRAL: la sequenza è mista o non ci sono abbastanza pivot

    Returns:
        {
            "structure": "BULLISH" | "BEARISH" | "NEUTRAL",
            "last_higher_low": float or None,   # ultimo HL (se BULLISH)
            "last_lower_high": float or None,    # ultimo LH (se BEARISH)
            "pivot_count": int,                  # pivot totali trovati
        }
    """
    pivots = find_recent_pivots(df_m15, lookback, max_pivots=6)
    highs = pivots["highs"]
    lows = pivots["lows"]

    result = {
        "structure": "NEUTRAL",
        "last_higher_low": None,
        "last_lower_high": None,
        "pivot_count": len(highs) + len(lows),
    }

    if len(highs) < min_pivots or len(lows) < min_pivots:
        return result

    # Confronta gli ultimi 2 highs e gli ultimi 2 lows
    last_highs = highs[-2:]
    last_lows = lows[-2:]

    hh = last_highs[1]["price"] > last_highs[0]["price"]  # Higher High
    hl = last_lows[1]["price"] > last_lows[0]["price"]     # Higher Low
    lh = last_highs[1]["price"] < last_highs[0]["price"]   # Lower High
    ll = last_lows[1]["price"] < last_lows[0]["price"]     # Lower Low

    if hh and hl:
        result["structure"] = "BULLISH"
        result["last_higher_low"] = last_lows[-1]["price"]
    elif lh and ll:
        result["structure"] = "BEARISH"
        result["last_lower_high"] = last_highs[-1]["price"]

    return result


def evaluate_choch_v2(df_m15: pd.DataFrame,
                       lookback: int = CHOCH_LOOKBACK,
                       min_pivots: int = CHOCH_MIN_PIVOTS) -> dict:
    """
    Change of Character V2: valuta il CHOCH sulla struttura M15 REALE.

    Corregge l'errore critico dell'implementazione V1 che confrontava
    M15 con la Dow Theory H4 anziché valutare la struttura M15.

    Un CHOCH bullish si verifica quando:
        1. La struttura M15 era BEARISH (LH + LL)
        2. L'ultima candela chiude sopra l'ultimo Lower High

    Un CHOCH bearish si verifica quando:
        1. La struttura M15 era BULLISH (HH + HL)
        2. L'ultima candela chiude sotto l'ultimo Higher Low

    Args:
        df_m15: DataFrame M15 con colonne high, low, close, timestamp
        lookback: candele per lato per i pivot (default 3)
        min_pivots: minimo pivot highs E lows necessari (default 2)

    Returns:
        {
            "confirmed": bool,
            "direction": "BULLISH" | "BEARISH" | None,
            "prev_structure": "BULLISH" | "BEARISH" | "NEUTRAL",
            "ref_level": float or None,       # il livello rotto (LH o HL)
            "penetration_pct": float,
            "displacement": bool,
        }
    """
    result = {
        "confirmed": False,
        "direction": None,
        "prev_structure": "NEUTRAL",
        "ref_level": None,
        "penetration_pct": 0.0,
        "displacement": False,
    }

    if len(df_m15) < lookback * 2 + 5:
        return result

    # Classifica la struttura M15 usando le candele PRECEDENTI all'ultima
    # (l'ultima candela è quella che potrebbe rompere la struttura)
    m15_structure = classify_m15_structure(df_m15.iloc[:-1], lookback, min_pivots)
    result["prev_structure"] = m15_structure["structure"]

    if m15_structure["structure"] == "NEUTRAL":
        return result  # nessuna struttura da invertire

    last = df_m15.iloc[-1]
    close = float(last["close"])
    open_ = float(last["open"])
    high = float(last["high"])
    low = float(last["low"])

    # CHOCH Bullish: struttura M15 era BEARISH, close rompe l'ultimo LH
    if m15_structure["structure"] == "BEARISH":
        ref = m15_structure["last_lower_high"]
        if ref is not None and close > ref:
            penetration = (close - ref) / ref if ref != 0 else 0
            body = abs(close - open_)
            range_ = high - low
            has_disp = (body / range_ > DISP_BODY_PCT) if range_ > 0 else False

            result["confirmed"] = True
            result["direction"] = "BULLISH"
            result["ref_level"] = ref
            result["penetration_pct"] = penetration
            result["displacement"] = has_disp

    # CHOCH Bearish: struttura M15 era BULLISH, close rompe l'ultimo HL
    elif m15_structure["structure"] == "BULLISH":
        ref = m15_structure["last_higher_low"]
        if ref is not None and close < ref:
            penetration = (ref - close) / ref if ref != 0 else 0
            body = abs(close - open_)
            range_ = high - low
            has_disp = (body / range_ > DISP_BODY_PCT) if range_ > 0 else False

            result["confirmed"] = True
            result["direction"] = "BEARISH"
            result["ref_level"] = ref
            result["penetration_pct"] = penetration
            result["displacement"] = has_disp

    return result


# ============================================================
# Pullback Invalidation
# ============================================================

def is_pullback_valid(df_h4: pd.DataFrame, direction: str,
                       h4_structure: dict) -> dict:
    """
    Verifica se il pullback è ancora valido (non invalidato).

    Un pullback è INVALIDATO quando:
        - BUY: il prezzo chiude sotto l'ultimo Higher Low H4
        - SELL: il prezzo chiude sopra l'ultimo Lower High H4

    L'invalidazione significa che il trend dominante si è invertito
    e non si dovrebbe più operare in quella direzione.

    Args:
        df_h4: DataFrame H4
        direction: "BUY" o "SELL"
        h4_structure: dict da evaluate_h4_structure() con
                      last_higher_low e last_lower_high

    Returns:
        {
            "valid": bool,           # True se il pullback è ancora valido
            "invalidated": bool,     # True se il livello chiave è stato violato
            "ref_level": float or None,  # il livello di riferimento
            "current_price": float,
        }
    """
    result = {
        "valid": True,
        "invalidated": False,
        "ref_level": None,
        "current_price": 0.0,
    }

    if len(df_h4) < 2:
        return result

    current_price = float(df_h4.iloc[-1]["close"])
    result["current_price"] = current_price

    if direction == "BUY":
        ref = h4_structure.get("last_higher_low")
        if ref is not None:
            result["ref_level"] = ref
            if current_price < ref:
                result["valid"] = False
                result["invalidated"] = True
    else:  # SELL
        ref = h4_structure.get("last_lower_high")
        if ref is not None:
            result["ref_level"] = ref
            if current_price > ref:
                result["valid"] = False
                result["invalidated"] = True

    return result


# ============================================================
# Displacement Detection (informativo)
# ============================================================

def check_displacement(df: pd.DataFrame, direction: str,
                        atr_value: float,
                        body_pct: float = DISP_BODY_PCT,
                        min_candles: int = DISP_MIN_CANDLES,
                        atr_mult: float = DISP_ATR_MULT,
                        lookback: int = 5) -> dict:
    """
    Verifica se è avvenuto un displacement (movimento impulsivo)
    nelle ultime `lookback` candele.

    Un displacement è una sequenza di candele consecutive con:
        - corpo > body_pct del range (candela impulsiva)
        - stessa direzione
        - ampiezza totale > atr_mult × ATR

    Returns:
        {
            "confirmed": bool,
            "magnitude": float,        # ampiezza totale del displacement
            "candle_count": int,        # candele impulsive consecutive
            "magnitude_atr": float,     # ampiezza in multipli di ATR
        }
    """
    result = {
        "confirmed": False,
        "magnitude": 0.0,
        "candle_count": 0,
        "magnitude_atr": 0.0,
    }

    if len(df) < lookback + 1 or atr_value <= 0:
        return result

    recent = df.iloc[-lookback:]
    consecutive = 0
    total_move = 0.0

    for _, candle in recent.iterrows():
        open_ = float(candle["open"])
        close = float(candle["close"])
        high = float(candle["high"])
        low = float(candle["low"])
        body = abs(close - open_)
        range_ = high - low

        if range_ <= 0:
            consecutive = 0
            total_move = 0.0
            continue

        is_impulsive = body / range_ > body_pct

        if direction == "BUY":
            is_directional = close > open_
        else:
            is_directional = close < open_

        if is_impulsive and is_directional:
            consecutive += 1
            total_move += body
        else:
            consecutive = 0
            total_move = 0.0

    result["candle_count"] = consecutive
    result["magnitude"] = total_move
    result["magnitude_atr"] = total_move / atr_value if atr_value > 0 else 0

    if consecutive >= min_candles and total_move >= atr_mult * atr_value:
        result["confirmed"] = True

    return result


# ============================================================
# Volume Ratio (informativo)
# ============================================================

def compute_volume_ratio(df: pd.DataFrame, avg_period: int = 20) -> dict:
    """
    Calcola il rapporto tra il volume dell'ultima candela e la media
    delle ultime `avg_period` candele.

    Returns:
        {
            "ratio": float,            # volume_corrente / media
            "classification": str,     # CLIMAX / HIGH / NORMAL / LOW
            "current_volume": float,
            "avg_volume": float,
        }
    """
    result = {
        "ratio": 1.0,
        "classification": "NORMAL",
        "current_volume": 0.0,
        "avg_volume": 0.0,
    }

    if "volume" not in df.columns or len(df) < avg_period + 1:
        return result

    current_vol = float(df.iloc[-1]["volume"])
    avg_vol = float(df.iloc[-(avg_period + 1):-1]["volume"].mean())

    result["current_volume"] = current_vol
    result["avg_volume"] = avg_vol

    if avg_vol <= 0:
        return result

    ratio = current_vol / avg_vol
    result["ratio"] = ratio

    if ratio > 3.0:
        result["classification"] = "CLIMAX"
    elif ratio > 1.5:
        result["classification"] = "HIGH"
    elif ratio < 0.7:
        result["classification"] = "LOW"
    else:
        result["classification"] = "NORMAL"

    return result


# ============================================================
# Premium / Discount (informativo)
# ============================================================

def compute_premium_discount(current_price: float,
                              range_high: float,
                              range_low: float) -> dict:
    """
    Calcola la posizione del prezzo nel range (sessione, giorno, swing).

    Premium Zone: > 50% del range (zona cara, favorevole per SELL)
    Discount Zone: < 50% del range (zona a sconto, favorevole per BUY)
    Equilibrium: intorno al 50%

    Returns:
        {
            "position": float,         # 0.0 = bottom, 1.0 = top
            "zone": str,               # PREMIUM / DISCOUNT / EQUILIBRIUM
            "buy_favorable": bool,     # True se il prezzo è in Discount (< 0.45)
            "sell_favorable": bool,    # True se il prezzo è in Premium (> 0.55)
        }
    """
    result = {
        "position": 0.5,
        "zone": "EQUILIBRIUM",
        "buy_favorable": False,
        "sell_favorable": False,
    }

    range_size = range_high - range_low
    if range_size <= 0:
        return result

    position = (current_price - range_low) / range_size
    position = max(0.0, min(1.0, position))  # clamp [0, 1]

    result["position"] = round(position, 4)

    if position < 0.45:
        result["zone"] = "DISCOUNT"
        result["buy_favorable"] = True
    elif position > 0.55:
        result["zone"] = "PREMIUM"
        result["sell_favorable"] = True
    else:
        result["zone"] = "EQUILIBRIUM"

    return result


# ============================================================
# Convenience: build context enrichment dict
# ============================================================

def build_structure_context(df_m15: pd.DataFrame,
                             df_h4: pd.DataFrame,
                             direction: str,
                             h4_structure: dict,
                             atr_m15: float = 0.0,
                             session_high: float = 0.0,
                             session_low: float = 0.0) -> dict:
    """
    Costruisce un dict con tutte le informazioni strutturali
    e contestuali per un segnale, da aggiungere al signal dict
    o al market_context.

    Uso tipico nei runner:
        ctx = build_structure_context(df_m15, df_h4, direction, h4_struct, atr_m15)
        signal.update(ctx)

    Returns:
        {
            "bos_v2": dict,              # risultato evaluate_bos_v2
            "choch_v2": dict,            # risultato evaluate_choch_v2
            "m15_structure": dict,       # struttura M15 corrente
            "pullback_valid": dict,      # risultato is_pullback_valid
            "displacement": dict,        # risultato check_displacement
            "volume_ratio": dict,        # volume ratio dell'ultima candela
            "premium_discount": dict,    # posizione nel range sessione
        }
    """
    # BOS V2
    bos = evaluate_bos_v2(df_m15, direction)

    # CHOCH V2
    choch = evaluate_choch_v2(df_m15)

    # Struttura M15
    m15_struct = classify_m15_structure(df_m15)

    # Pullback Invalidation
    pb_valid = is_pullback_valid(df_h4, direction, h4_structure)

    # Displacement (informativo)
    disp = check_displacement(df_m15, direction, atr_m15)

    # Volume Ratio (informativo)
    vol = compute_volume_ratio(df_m15)

    # Premium/Discount (informativo)
    current_price = float(df_m15.iloc[-1]["close"]) if len(df_m15) > 0 else 0
    pd_zone = compute_premium_discount(current_price, session_high, session_low)

    return {
        "bos_v2": bos,
        "choch_v2": choch,
        "m15_structure": m15_struct,
        "pullback_valid": pb_valid,
        "displacement": disp,
        "volume_ratio": vol,
        "premium_discount": pd_zone,
    }
