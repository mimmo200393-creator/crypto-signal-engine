"""
strategy.py
Implementazione del setup V1.0 (FROZEN): Pullback EMA in trend.

Logica LONG e SHORT perfettamente simmetrica, come da spec.

ASSUNZIONI IMPLEMENTATIVE (da confermare, non sono criteri operativi
modificati ma scelte di implementazione dove la spec era ambigua):

1. La candela di "pullback" e' la candela H1 immediatamente precedente
   alla candela di trigger. Il trigger e' valutato sull'ultima candela
   H1 chiusa rispetto alla candela precedente.

2. ATR usato per pullback/SL/TP e' l'ATR(14) dell'ultima candela chiusa
   (il ricalcolo richiesto dalla spec al momento del trigger).

3. Take Profit "basato sul precedente swing significativo" = livello
   pivot (S/R) piu' vicino in direzione del trade, individuato tramite
   find_pivots + cluster_levels su H1. SE NESSUN LIVELLO E' DISPONIBILE
   in quella direzione, il setup viene SCARTATO (TP non determinabile
   -> non puo' essere validato). Questo e' un punto da confermare con
   l'utente: l'alternativa sarebbe un fallback ATR-based, ma cio'
   costituirebbe una modifica ai criteri di uscita non prevista dalla
   spec FROZEN.

4. "Supporto/Resistenza rilevante" (+1 punto di scoring) = esiste un
   livello S/R nella direzione OPPOSTA al TP (cioe' vicino all'entry,
   dal lato del pullback) entro 1x ATR dall'entry. Questo e' un bonus
   di confluenza, distinto dal livello usato per il TP.
"""

import pandas as pd
from core.indicators import find_pivots, cluster_levels, nearest_level


def _trend_h4_ok(df_h4: pd.DataFrame, direction: str) -> bool:
    last = df_h4.iloc[-1]
    if direction == "LONG":
        return last["ema_50"] > last["ema_100"] > last["ema_200"]
    else:  # SHORT
        return last["ema_50"] < last["ema_100"] < last["ema_200"]


def _trend_h1_ok(df_h1: pd.DataFrame, direction: str) -> bool:
    last = df_h1.iloc[-1]
    if direction == "LONG":
        return last["ema_21"] > last["ema_50"]
    else:  # SHORT
        return last["ema_21"] < last["ema_50"]


def _check_pullback(df_h1: pd.DataFrame, direction: str, atr_multiplier: float):
    """
    Valuta il pullback sulla candela precedente a quella di trigger
    (df_h1.iloc[-2]).

    Ritorna dict:
        {
            "pullback_ok": bool,
            "pullback_ema50": bool,
            "pullback_ema21": bool
        }

    Condizione pullback (per ciascuna EMA):
        - distanza tra prezzo (close) ed EMA <= atr_multiplier * ATR
          OPPURE
        - wick (low per LONG, high per SHORT) tocca l'EMA
          (cioe' l'EMA e' compresa nell'intervallo [low, high] della candela)
    """
    prev = df_h1.iloc[-2]
    atr_val = prev["atr"]

    def _near_or_touch(ema_value):
        close_distance = abs(prev["close"] - ema_value)
        within_distance = close_distance <= (atr_multiplier * atr_val)

        if direction == "LONG":
            wick_touch = prev["low"] <= ema_value <= prev["high"]
        else:
            wick_touch = prev["low"] <= ema_value <= prev["high"]

        return within_distance or wick_touch

    pullback_ema50 = _near_or_touch(prev["ema_50"])
    pullback_ema21 = _near_or_touch(prev["ema_21"])

    return {
        "pullback_ok": pullback_ema50 or pullback_ema21,
        "pullback_ema50": pullback_ema50,
        "pullback_ema21": pullback_ema21,
    }


def _check_trigger(df_h1: pd.DataFrame, direction: str) -> bool:
    """
    Trigger: chiusura dell'ultima candela H1 sopra il massimo (LONG)
    o sotto il minimo (SHORT) della candela precedente.
    """
    last = df_h1.iloc[-1]
    prev = df_h1.iloc[-2]

    if direction == "LONG":
        return last["close"] > prev["high"]
    else:
        return last["close"] < prev["low"]


def _get_sr_levels(df_h1: pd.DataFrame, pivot_lookback: int, atr_val: float):
    """
    Calcola pivot e cluster su H1. Ritorna (support_levels, resistance_levels),
    ciascuno lista di dict {"price":..., "count":...}.
    """
    pivots = find_pivots(df_h1, lookback=pivot_lookback)
    support_levels = cluster_levels(pivots["pivot_lows"], atr_val)
    resistance_levels = cluster_levels(pivots["pivot_highs"], atr_val)
    return support_levels, resistance_levels


def _evaluate(df_h1: pd.DataFrame, df_h4: pd.DataFrame, direction: str, config: dict):
    """
    Valutazione completa di un setup per la direzione data.
    Ritorna un dict con tutti i parametri del setup, oppure None
    se il setup non e' valido.
    """
    atr_multiplier = config.get("ATR_MULTIPLIER", 1.5)
    pullback_atr_fraction = 0.3  # da spec: distanza <= 0.3 * ATR (fisso, criterio operativo)
    min_rr = config.get("MIN_RR", 2.0)
    pivot_lookback = config.get("PIVOT_LOOKBACK", 5)

    last = df_h1.iloc[-1]
    atr_val = last["atr"]

    # --- 1. Trend H4 ---
    trend_h4_ok = _trend_h4_ok(df_h4, direction)

    # --- 2. Trend H1 ---
    trend_h1_ok = _trend_h1_ok(df_h1, direction)

    if not (trend_h4_ok and trend_h1_ok):
        return None

    # --- 3. Pullback (condizione preliminare, sulla candela precedente) ---
    pullback = _check_pullback(df_h1, direction, pullback_atr_fraction)
    if not pullback["pullback_ok"]:
        return None

    # --- 4. Trigger ---
    trigger_confirmed = _check_trigger(df_h1, direction)
    if not trigger_confirmed:
        return None

    # --- 5. Ricalcolo finale al momento del trigger ---
    entry = float(last["close"])

    if direction == "LONG":
        stop_loss = entry - atr_multiplier * atr_val
    else:
        stop_loss = entry + atr_multiplier * atr_val

    support_levels, resistance_levels = _get_sr_levels(df_h1, pivot_lookback, atr_val)

    if direction == "LONG":
        tp_level = nearest_level(entry, resistance_levels, "resistance")
    else:
        tp_level = nearest_level(entry, support_levels, "support")

    if tp_level is None:
        # Nessuno swing significativo disponibile in direzione del TP
        # -> setup non validabile (vedi nota in testa al modulo, punto 3)
        return None

    take_profit = tp_level["price"]

    risk = abs(entry - stop_loss)
    reward = abs(take_profit - entry)
    rr = reward / risk if risk > 0 else 0.0

    if rr < min_rr:
        return None

    # --- 6. Re-check finale trend (gia' ricalcolato sopra con dati aggiornati) ---
    # trend_h4_ok / trend_h1_ok sono stati calcolati sull'ultima candela
    # disponibile -> rappresentano gia' lo stato "al momento del trigger".

    # --- Confluenza S/R per scoring (livello opposto al TP, vicino all'entry) ---
    if direction == "LONG":
        sr_confluence = nearest_level(entry, support_levels, "support")
    else:
        sr_confluence = nearest_level(entry, resistance_levels, "resistance")

    sr_level_present = (
        sr_confluence is not None
        and abs(entry - sr_confluence["price"]) <= atr_val
    )

    setup = {
        "asset": None,  # da assegnare dal chiamante
        "setup": "Pullback EMA Trend",
        "direzione": direction,
        "entry": entry,
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "rr": rr,
        "atr_h1": float(atr_val),
        "support_level": support_levels[0]["price"] if support_levels else None,
        "resistance_level": resistance_levels[0]["price"] if resistance_levels else None,
        "trigger_type": "close_breakout",
        "trend_h4_ok": trend_h4_ok,
        "trend_h1_ok": trend_h1_ok,
        "pullback_ema50": pullback["pullback_ema50"],
        "pullback_ema21": pullback["pullback_ema21"],
        "sr_level_present": sr_level_present,
        "trigger_confirmed": trigger_confirmed,
        "timestamp_setup": int(last["timestamp"]),
    }

    return setup


def evaluate_long(df_h1: pd.DataFrame, df_h4: pd.DataFrame, config: dict):
    """Valuta un setup LONG. Ritorna dict setup o None."""
    return _evaluate(df_h1, df_h4, "LONG", config)


def evaluate_short(df_h1: pd.DataFrame, df_h4: pd.DataFrame, config: dict):
    """Valuta un setup SHORT. Ritorna dict setup o None."""
    return _evaluate(df_h1, df_h4, "SHORT", config)
