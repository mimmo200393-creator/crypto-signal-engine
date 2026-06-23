"""
strategies/edge_lab/trend_engine.py
Edge Lab — Step 2: Trend Engine (già consegnato, ricostruito per questa sessione)

Dow Theory: HH+HL=BULLISH, LH+LL=BEARISH, misto=NEUTRAL
EMA Status: allineamento EMA21>50>100>200 (senza prezzo)
EMA slope: EMA50 corrente vs 5 candele fa
H4 e H1 calcolati indipendentemente.

Matrice combinazione:
  BULLISH+BULLISH=BULLISH, BEARISH+BEARISH=BEARISH
  BULLISH+NEUTRAL=BULLISH, BEARISH+NEUTRAL=BEARISH
  NEUTRAL+BULLISH=NEUTRAL, NEUTRAL+BEARISH=NEUTRAL
  NEUTRAL+NEUTRAL=NEUTRAL
  BULLISH+BEARISH=TRANSITION, BEARISH+BULLISH=TRANSITION
"""

from __future__ import annotations
import logging
import pandas as pd
import numpy as np

logger = logging.getLogger("edge_lab.trend_engine")

MOMENTUM_LOOKBACK = 5


def _add_emas(df: pd.DataFrame, periods: list) -> pd.DataFrame:
    for p in periods:
        col = f"ema_{p}"
        if col not in df.columns:
            df[col] = df["close"].ewm(span=p, adjust=False).mean()
    return df


def _dow_theory(df: pd.DataFrame, lookback: int = 3) -> str:
    if len(df) < lookback * 2 + 3:
        return "NEUTRAL"
    highs = df["high"].values
    lows  = df["low"].values
    n = len(highs)
    pivot_highs, pivot_lows = [], []
    for i in range(lookback, n - lookback):
        if highs[i] > highs[i-lookback:i].max() and highs[i] > highs[i+1:i+1+lookback].max():
            pivot_highs.append(highs[i])
        if lows[i] < lows[i-lookback:i].min() and lows[i] < lows[i+1:i+1+lookback].min():
            pivot_lows.append(lows[i])
    if len(pivot_highs) < 2 or len(pivot_lows) < 2:
        return "NEUTRAL"
    hh = pivot_highs[-1] > pivot_highs[-2]
    hl = pivot_lows[-1]  > pivot_lows[-2]
    lh = pivot_highs[-1] < pivot_highs[-2]
    ll = pivot_lows[-1]  < pivot_lows[-2]
    if hh and hl:
        return "BULLISH"
    if lh and ll:
        return "BEARISH"
    return "NEUTRAL"


def _ema_alignment(df: pd.DataFrame, periods: list) -> str:
    last = df.iloc[-1]
    vals = [last.get(f"ema_{p}", None) for p in sorted(periods)]
    if any(v is None for v in vals):
        return "NEUTRAL"
    if all(vals[i] > vals[i+1] for i in range(len(vals)-1)):
        return "BULLISH"
    if all(vals[i] < vals[i+1] for i in range(len(vals)-1)):
        return "BEARISH"
    return "NEUTRAL"


def combine_trends(h4_direction: str, h1_direction: str) -> str:
    """H1 guida la direzione. H4 è contesto informativo."""
    return h1_direction


def compute_ema_slope(df: pd.DataFrame, period: int = 50, lookback: int = MOMENTUM_LOOKBACK) -> str:
    col = f"ema_{period}"
    if col not in df.columns or len(df) < lookback + 1:
        return "FLAT"
    curr = float(df[col].iloc[-1])
    prev = float(df[col].iloc[-(lookback+1)])
    if curr > prev:
        return "UP"
    if curr < prev:
        return "DOWN"
    return "FLAT"


def compute_trend_h4(df_h4: pd.DataFrame, ema_periods: list, momentum_lookback: int = MOMENTUM_LOOKBACK) -> dict:
    if len(df_h4) < max(ema_periods) + 5:
        return {"direction": "NEUTRAL", "dow": "NEUTRAL", "ema": "NEUTRAL", "ema_slope": "FLAT"}
    _add_emas(df_h4, ema_periods)
    dow = _dow_theory(df_h4)
    ema = _ema_alignment(df_h4, ema_periods)
    direction = _combine_dow_ema(dow, ema)
    slope = compute_ema_slope(df_h4, 50, momentum_lookback)
    return {"direction": direction, "dow": dow, "ema": ema, "ema_slope": slope}


def compute_trend_h1(df_h1: pd.DataFrame, ema_periods: list, momentum_lookback: int = MOMENTUM_LOOKBACK) -> dict:
    if len(df_h1) < max(ema_periods) + 5:
        return {"direction": "NEUTRAL", "dow": "NEUTRAL", "ema": "NEUTRAL", "ema_slope": "FLAT"}
    _add_emas(df_h1, ema_periods)
    dow = _dow_theory(df_h1)
    ema = _ema_alignment(df_h1, ema_periods)
    direction = _combine_dow_ema(dow, ema)
    slope = compute_ema_slope(df_h1, 50, momentum_lookback)
    return {"direction": direction, "dow": dow, "ema": ema, "ema_slope": slope}


def combine_trends(h4_direction: str, h1_direction: str) -> str:
    matrix = {
        ("BULLISH","BULLISH"): "BULLISH",
        ("BEARISH","BEARISH"): "BEARISH",
        ("BULLISH","NEUTRAL"): "BULLISH",
        ("BEARISH","NEUTRAL"): "BEARISH",
        ("NEUTRAL","BULLISH"): "NEUTRAL",
        ("NEUTRAL","BEARISH"): "NEUTRAL",
        ("NEUTRAL","NEUTRAL"): "NEUTRAL",
        ("BULLISH","BEARISH"): "TRANSITION",
        ("BEARISH","BULLISH"): "TRANSITION",
    }
    return matrix.get((h4_direction, h1_direction), "NEUTRAL")
