"""
strategies/tt/direction_engine.py
TT — Direction Engine (4H)

Risponde a UNA sola domanda: "IN WHICH DIRECTION SHOULD WE LOOK FOR TRADES?"

Codice COMPLETAMENTE INDIPENDENTE -- nessun import da trend_engine.py,
liquidity_hunter.py o qualunque modulo condiviso con TRB/LH. TT deve
poter cambiare senza mai rischiare di alterare il comportamento di
un'altra strategia (e viceversa). Duplica concettualmente la stessa
regola oggettiva di rilevamento swing gia' validata altrove (k=2,
disuguaglianza stretta) ma come codice proprio, non riusato.

Logica (spec sezione 2):
    1. Rileva swing high/low confermati su H4
    2. Identifica l'ultimo BOS (break of structure) significativo
    3. Bullish: ultimo BOS ha rotto uno swing high in salita (HH+HL)
       Bearish: ultimo BOS ha rotto uno swing low in discesa (LH+LL)
    4. Costruisce lo Swing Range dal BOS piu' recente:
         Bullish: Swing Low -> Swing High
         Bearish: Swing High -> Swing Low
"""

from __future__ import annotations

import pandas as pd

SWING_CONFIRM_K = 2  # candele per lato per confermare uno swing


def _detect_swings(df: pd.DataFrame, k: int = SWING_CONFIRM_K) -> list:
    """
    Rileva swing high/low confermati. Regola oggettiva: swing STRETTO
    (disuguaglianza stretta, non >=) -- in una zona piatta ogni barra
    sembrerebbe uno swing altrimenti.

    Ritorna lista di {"index", "type", "price", "timestamp"} in ordine
    cronologico.
    """
    if df is None or len(df) < 2 * k + 1:
        return []

    highs = df["high"].astype(float).values
    lows = df["low"].astype(float).values
    timestamps = df["timestamp"].values

    swings = []
    for i in range(k, len(df) - k):
        window_h_before = highs[i - k:i]
        window_h_after = highs[i + 1:i + k + 1]
        window_l_before = lows[i - k:i]
        window_l_after = lows[i + 1:i + k + 1]

        if highs[i] > window_h_before.max() and highs[i] > window_h_after.max():
            swings.append({
                "index": i, "type": "HIGH",
                "price": round(float(highs[i]), 5),
                "timestamp": int(timestamps[i]),
            })

        if lows[i] < window_l_before.min() and lows[i] < window_l_after.min():
            swings.append({
                "index": i, "type": "LOW",
                "price": round(float(lows[i]), 5),
                "timestamp": int(timestamps[i]),
            })

    swings.sort(key=lambda s: s["index"])
    return swings


def _find_last_bos(df: pd.DataFrame, swings: list) -> dict | None:
    """
    Trova l'ultimo BOS (Break of Structure): la candela piu' recente
    che CHIUDE oltre l'ultimo swing confermato nella sua direzione.

    Bullish BOS: chiusura sopra l'ultimo swing HIGH confermato.
    Bearish BOS: chiusura sotto l'ultimo swing LOW confermato.

    Cammina all'indietro dalla candela piu' recente cercando la PRIMA
    rottura (quella piu' vicina a oggi).
    """
    if not swings or df is None or len(df) == 0:
        return None

    closes = df["close"].astype(float).values
    n = len(df)

    swing_highs = [s for s in swings if s["type"] == "HIGH"]
    swing_lows = [s for s in swings if s["type"] == "LOW"]

    if not swing_highs and not swing_lows:
        return None

    # Cammina all'indietro dall'ultima candela, cerca la prima rottura
    for i in range(n - 1, -1, -1):
        close_i = closes[i]

        # Swing high piu' recente PRIMA di questa candela
        prior_highs = [s for s in swing_highs if s["index"] < i]
        if prior_highs:
            last_high = prior_highs[-1]
            if close_i > last_high["price"]:
                return {
                    "direction": "BULLISH", "bos_price": last_high["price"],
                    "bos_ts": int(df["timestamp"].values[i]),
                    "broken_swing": last_high,
                }

        prior_lows = [s for s in swing_lows if s["index"] < i]
        if prior_lows:
            last_low = prior_lows[-1]
            if close_i < last_low["price"]:
                return {
                    "direction": "BEARISH", "bos_price": last_low["price"],
                    "bos_ts": int(df["timestamp"].values[i]),
                    "broken_swing": last_low,
                }

    return None


def _hh_hl_pattern(swings: list) -> str:
    """
    Verifica il pattern Dow classico sugli ultimi due swing per tipo:
    HH+HL (bullish) o LH+LL (bearish). Serve come conferma addizionale
    al BOS, non come unico criterio.
    """
    highs = [s for s in swings if s["type"] == "HIGH"]
    lows = [s for s in swings if s["type"] == "LOW"]

    if len(highs) < 2 or len(lows) < 2:
        return "NEUTRAL"

    hh = highs[-1]["price"] > highs[-2]["price"]
    hl = lows[-1]["price"] > lows[-2]["price"]
    lh = highs[-1]["price"] < highs[-2]["price"]
    ll = lows[-1]["price"] < lows[-2]["price"]

    if hh and hl:
        return "BULLISH"
    if lh and ll:
        return "BEARISH"
    return "NEUTRAL"


def compute_direction_4h(df_h4: pd.DataFrame) -> dict:
    """
    Entry point principale. Ritorna:
    {
        "direction": "BULLISH"|"BEARISH"|"NEUTRAL",
        "swing_range_low": float|None,
        "swing_range_high": float|None,
        "last_bos_price": float|None,
        "last_bos_ts": int|None,
        "dow_pattern": "BULLISH"|"BEARISH"|"NEUTRAL",  # solo informativo
    }

    NEUTRAL se: non ci sono abbastanza swing, nessun BOS trovato, o il
    BOS piu' recente non e' confermato dal pattern Dow (troppo rumore
    per fidarsi).
    """
    swings = _detect_swings(df_h4)
    if len(swings) < 2:  # serve almeno uno swing high E uno low per costruire un range
        return {"direction": "NEUTRAL", "swing_range_low": None,
                "swing_range_high": None, "last_bos_price": None,
                "last_bos_ts": None, "dow_pattern": "NEUTRAL"}

    bos = _find_last_bos(df_h4, swings)
    dow = _hh_hl_pattern(swings)

    if bos is None:
        return {"direction": "NEUTRAL", "swing_range_low": None,
                "swing_range_high": None, "last_bos_price": None,
                "last_bos_ts": None, "dow_pattern": dow}

    direction = bos["direction"]

    # Costruisci lo Swing Range dal BOS piu' recente (spec sezione 2)
    swing_highs = [s for s in swings if s["type"] == "HIGH"]
    swing_lows = [s for s in swings if s["type"] == "LOW"]

    if direction == "BULLISH":
        # Swing Low -> Swing High: l'ultimo low prima del BOS, l'high rotto
        prior_lows = [s for s in swing_lows if s["index"] < bos["broken_swing"]["index"]]
        range_low = prior_lows[-1]["price"] if prior_lows else swing_lows[-1]["price"]
        range_high = bos["bos_price"]
    else:
        prior_highs = [s for s in swing_highs if s["index"] < bos["broken_swing"]["index"]]
        range_high = prior_highs[-1]["price"] if prior_highs else swing_highs[-1]["price"]
        range_low = bos["bos_price"]

    return {
        "direction": direction,
        "swing_range_low": round(range_low, 5),
        "swing_range_high": round(range_high, 5),
        "last_bos_price": bos["bos_price"],
        "last_bos_ts": bos["bos_ts"],
        "dow_pattern": dow,
    }
