"""
indicators.py
Calcolo indicatori tecnici: EMA, ATR(14), pivot Support/Resistance.

Tutte le funzioni operano su pandas.DataFrame con colonne:
['timestamp', 'open', 'high', 'low', 'close', 'volume']
ordinate per timestamp crescente (piu' vecchia -> piu' recente).
"""

import pandas as pd
import numpy as np


def ema(series: pd.Series, period: int) -> pd.Series:
    """
    Exponential Moving Average standard (span-based, adjust=False
    per coerenza con la maggior parte delle piattaforme di trading).
    """
    return series.ewm(span=period, adjust=False).mean()


def add_emas(df: pd.DataFrame, periods: list) -> pd.DataFrame:
    """
    Aggiunge colonne ema_<period> al dataframe, calcolate sulla colonna 'close'.
    Ritorna il dataframe modificato (in-place + return per comodita').
    """
    for p in periods:
        df[f"ema_{p}"] = ema(df["close"], p)
    return df


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """
    Average True Range (Wilder smoothing via EMA con alpha=1/period,
    equivalente a ewm con span=period per la convenzione usata qui).

    True Range = max(
        high - low,
        abs(high - prev_close),
        abs(low - prev_close)
    )
    """
    high = df["high"]
    low = df["low"]
    prev_close = df["close"].shift(1)

    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()

    true_range = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

    # Wilder smoothing: alpha = 1/period
    atr_series = true_range.ewm(alpha=1.0 / period, adjust=False).mean()
    return atr_series


def add_atr(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    df["atr"] = atr(df, period)
    return df


def find_pivots(df: pd.DataFrame, lookback: int = 5) -> dict:
    """
    Identifica pivot high e pivot low semplici.

    Pivot High: massimo strettamente superiore ai massimi delle
                 `lookback` candele precedenti E successive.
    Pivot Low:  minimo strettamente inferiore ai minimi delle
                 `lookback` candele precedenti E successive.

    Nota: per definizione, una pivot richiede `lookback` candele successive
    disponibili -> gli ultimi `lookback` elementi del df non possono
    essere valutati come pivot confermati.

    Ritorna:
        {
            "pivot_highs": [(timestamp, price), ...],
            "pivot_lows":  [(timestamp, price), ...]
        }
    """
    highs = df["high"].values
    lows = df["low"].values
    timestamps = df["timestamp"].values
    n = len(df)

    pivot_highs = []
    pivot_lows = []

    for i in range(lookback, n - lookback):
        window_high_before = highs[i - lookback:i]
        window_high_after = highs[i + 1:i + 1 + lookback]
        if highs[i] > window_high_before.max() and highs[i] > window_high_after.max():
            pivot_highs.append((int(timestamps[i]), float(highs[i])))

        window_low_before = lows[i - lookback:i]
        window_low_after = lows[i + 1:i + 1 + lookback]
        if lows[i] < window_low_before.min() and lows[i] < window_low_after.min():
            pivot_lows.append((int(timestamps[i]), float(lows[i])))

    return {"pivot_highs": pivot_highs, "pivot_lows": pivot_lows}


def cluster_levels(levels: list, atr_value: float, atr_fraction: float = 0.5) -> list:
    """
    Raggruppa una lista di (timestamp, price) in zone, unendo i livelli
    che distano tra loro meno di (atr_value * atr_fraction).

    Ritorna una lista di livelli aggregati (prezzo medio della zona),
    ordinata per prezzo crescente. Ogni elemento e':
        {"price": float, "count": int}  # count = quante volte il livello e' stato toccato
    """
    if not levels:
        return []

    prices = sorted([p for _, p in levels])
    threshold = atr_value * atr_fraction if atr_value else 0

    clusters = []
    current_cluster = [prices[0]]

    for p in prices[1:]:
        if threshold > 0 and (p - current_cluster[-1]) <= threshold:
            current_cluster.append(p)
        else:
            clusters.append(current_cluster)
            current_cluster = [p]
    clusters.append(current_cluster)

    result = [
        {"price": float(np.mean(c)), "count": len(c)}
        for c in clusters
    ]
    return result


def nearest_level(price: float, levels: list, direction: str):
    """
    Trova il livello (S/R) piu' rilevante rispetto al prezzo corrente.

    direction = "support"    -> cerca il livello piu' vicino SOTTO il prezzo
    direction = "resistance" -> cerca il livello piu' vicino SOPRA il prezzo

    Ritorna il dict del livello {"price":..., "count":...} oppure None.
    """
    if not levels:
        return None

    if direction == "support":
        candidates = [lv for lv in levels if lv["price"] < price]
        if not candidates:
            return None
        return max(candidates, key=lambda lv: lv["price"])  # il piu' vicino sotto

    elif direction == "resistance":
        candidates = [lv for lv in levels if lv["price"] > price]
        if not candidates:
            return None
        return min(candidates, key=lambda lv: lv["price"])  # il piu' vicino sopra

    return None


def compute_all_indicators(df_h1: pd.DataFrame, df_h4: pd.DataFrame, config: dict):
    """
    Helper di alto livello: calcola EMA su H1 e H4 e ATR su H1.
    Modifica i dataframe in-place e li ritorna.
    """
    ema_periods = config.get("EMA_PERIODS", [21, 50, 100, 200])
    atr_period = config.get("ATR_PERIOD", 14)

    add_emas(df_h1, ema_periods)
    add_emas(df_h4, ema_periods)
    add_atr(df_h1, atr_period)

    return df_h1, df_h4
