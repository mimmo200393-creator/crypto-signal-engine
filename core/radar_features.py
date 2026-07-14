"""
core/radar_features.py
Feature Engine del Market Radar — calcoli GREZZI dal movimento del prezzo.

Tutti i calcoli usano SOLO candele CHIUSE (no lookahead). Le funzioni
ritornano numeri grezzi; le soglie stanno nella State Machine.

Soglie calibrate sui dati M15 reali (BTC/XAU, 2026-07-13):
  - velocita': mediana ~0.04-0.27, p90 ~0.27-0.63, max ~1.3
    → una soglia utile e' ~0.6 (top ~10% dei movimenti), NON 1.0.
  - ATR M15: ~0.17-0.20% del prezzo su entrambi gli asset.
"""
from __future__ import annotations
import pandas as pd


def atr(df: pd.DataFrame, period: int = 14) -> float:
    """ATR classico sulle ultime `period` candele CHIUSE. Ritorna float o None."""
    if len(df) < period + 1:
        return None
    h, l, c = df["high"], df["low"], df["close"]
    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    val = tr.rolling(period).mean().iloc[-1]
    return float(val) if pd.notna(val) else None


def ema(series: pd.Series, period: int) -> float:
    if len(series) < period:
        return None
    return float(series.ewm(span=period, adjust=False).mean().iloc[-1])


def velocity(df: pd.DataFrame, lookback: int, atr_val: float) -> float:
    """
    Velocita' normalizzata: |close[-1] - close[-lookback]| / (lookback * ATR).
    Alta = il prezzo ha percorso molta distanza in poche candele.
    Usa solo candele chiuse. Ritorna None se dati insufficienti.
    """
    if atr_val is None or atr_val <= 0 or len(df) < lookback + 1:
        return None
    rng = abs(df["close"].iloc[-1] - df["close"].iloc[-lookback])
    return round(rng / (lookback * atr_val), 4)


def extension(df: pd.DataFrame, ema_period: int, atr_val: float) -> float:
    """
    Distanza del prezzo dalla EMA in unita' di ATR. Alta = esteso/lontano
    dalla media. Segno positivo = sopra la EMA, negativo = sotto.
    """
    if atr_val is None or atr_val <= 0:
        return None
    e = ema(df["close"], ema_period)
    if e is None:
        return None
    return round((df["close"].iloc[-1] - e) / atr_val, 4)


def exhaustion(df: pd.DataFrame, lookback: int, atr_period: int) -> float:
    """
    Rapporto di contrazione: ATR(ultime `lookback` candele) / ATR(intero
    periodo). < 1 = la volatilita' recente si sta contraendo → il movimento
    perde forza. Piu' basso = piu' esaurimento.

    Usa solo candele chiuse. Ritorna None se dati insufficienti.
    Nota: e' un rapporto di ATR, complementare ai "corpi decrescenti" che
    il dev puo' aggiungere come secondo segnale se serve.
    """
    if len(df) < atr_period + lookback + 1:
        return None
    full_atr = atr(df, atr_period)
    recent_atr = atr(df.iloc[-(lookback + atr_period):], lookback) if lookback >= 1 else None
    # ATR sulle ultime `lookback` candele: TR medio recente
    h, l, c = df["high"], df["low"], df["close"]
    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    recent = tr.iloc[-lookback:].mean()
    if full_atr is None or full_atr <= 0 or pd.isna(recent):
        return None
    return round(float(recent) / full_atr, 4)


def body_shrinking(df: pd.DataFrame, n: int = 3) -> bool:
    """True se i corpi delle ultime n candele sono decrescenti (perdita di
    spinta). Segnale complementare all'exhaustion ATR."""
    if len(df) < n:
        return False
    bodies = (df["close"] - df["open"]).abs().iloc[-n:].tolist()
    return all(bodies[i] < bodies[i - 1] for i in range(1, len(bodies)))
