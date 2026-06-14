"""
core/market_regime.py
Market Regime Detector V2.1

Regimi:
    TRENDING       -> ADX(14) H4 > 25
    RANGING        -> ADX(14) H4 < 20
    LOW_VOLATILITY -> ATR H1 < 80% della media ATR H1 ultime 20 candele
    HIGH_VOLATILITY-> ATR H1 > 150% della media ATR H1 ultime 20 candele
    NEUTRAL        -> nessuna condizione soddisfatta

Bonus/Penalty per strategia:
    TRENDING:       +1 per PullbackEMAFrozen, BreakoutRetest
    RANGING:        +1 per PivotReversal, LiquiditySweep
    LOW_VOLATILITY: +1 per CompressionBreakout
    HIGH_VOLATILITY: -1 per tutte le strategie
    NEUTRAL:         0 per tutte
"""

import numpy as np
import pandas as pd
import logging

logger = logging.getLogger("market_regime")

TRENDING = "TRENDING"
RANGING = "RANGING"
LOW_VOLATILITY = "LOW_VOLATILITY"
HIGH_VOLATILITY = "HIGH_VOLATILITY"
NEUTRAL = "NEUTRAL"

ADX_TRENDING_THRESHOLD = 25.0
ADX_RANGING_THRESHOLD = 20.0
ATR_LOW_VOL_RATIO = 0.80
ATR_HIGH_VOL_RATIO = 1.50
ATR_VOL_LOOKBACK = 20

FAVORABLE_REGIMES = {
    "Pullback EMA Trend": [TRENDING],
    "Breakout Retest":    [TRENDING],
    "Pivot Reversal":     [RANGING],
    "Liquidity Sweep":    [RANGING],
    "Compression Breakout": [LOW_VOLATILITY],
}


def _compute_adx(df_h4: pd.DataFrame, period: int = 14) -> float:
    if len(df_h4) < period * 2 + 1:
        return 20.0

    high  = df_h4["high"].values
    low   = df_h4["low"].values
    close = df_h4["close"].values
    n = len(high)

    tr = np.zeros(n)
    for i in range(1, n):
        tr[i] = max(
            high[i] - low[i],
            abs(high[i] - close[i-1]),
            abs(low[i]  - close[i-1])
        )

    plus_dm  = np.zeros(n)
    minus_dm = np.zeros(n)
    for i in range(1, n):
        up   = high[i] - high[i-1]
        down = low[i-1] - low[i]
        plus_dm[i]  = up   if up > down and up > 0 else 0
        minus_dm[i] = down if down > up and down > 0 else 0

    alpha = 1.0 / period

    def _wilder(arr):
        out = np.zeros(n)
        out[period] = arr[1:period+1].sum()
        for i in range(period+1, n):
            out[i] = out[i-1] * (1 - alpha) + arr[i]
        return out

    atr_w   = _wilder(tr)
    plus_w  = _wilder(plus_dm)
    minus_w = _wilder(minus_dm)

    with np.errstate(divide="ignore", invalid="ignore"):
        plus_di  = np.where(atr_w > 0, 100 * plus_w  / atr_w, 0)
        minus_di = np.where(atr_w > 0, 100 * minus_w / atr_w, 0)
        dx = np.where((plus_di + minus_di) > 0,
                      100 * np.abs(plus_di - minus_di) / (plus_di + minus_di), 0)

    adx_arr = np.zeros(n)
    adx_arr[period * 2] = dx[period:period*2+1].mean()
    for i in range(period * 2 + 1, n):
        adx_arr[i] = adx_arr[i-1] * (1 - alpha) + dx[i] * alpha

    return float(adx_arr[-1])


def detect_regime(df_h1: pd.DataFrame, df_h4: pd.DataFrame) -> str:
    if len(df_h1) >= ATR_VOL_LOOKBACK + 1:
        atr_current = float(df_h1["atr"].iloc[-1])
        atr_avg = float(df_h1["atr"].iloc[-(ATR_VOL_LOOKBACK + 1):-1].mean())

        if atr_avg > 0:
            ratio = atr_current / atr_avg
            if ratio > ATR_HIGH_VOL_RATIO:
                return HIGH_VOLATILITY
            if ratio < ATR_LOW_VOL_RATIO:
                return LOW_VOLATILITY

    adx = _compute_adx(df_h4)
    if adx > ADX_TRENDING_THRESHOLD:
        return TRENDING
    if adx < ADX_RANGING_THRESHOLD:
        return RANGING

    return NEUTRAL


def get_regime_bonus(strategy_name: str, regime: str) -> int:
    if regime == HIGH_VOLATILITY:
        return -1

    favorable = FAVORABLE_REGIMES.get(strategy_name, [])
    if regime in favorable:
        return 1

    return 0
