"""
strategies/compression_breakout.py
Compression Breakout V1.0

LONG:
  - Trend H4: EMA50 > EMA100 > EMA200
  - Trend H1: EMA21 > EMA50
  - Compressione: ATR H1 attuale < 80% media ATR ultime 10 candele
  - Range ristretto: (max - min) ultime 5 candele <= 1 ATR
  - Breakout: candle[-1] close > max range
  - SL = 1.5 x ATR, TP = swing significativo, R/R >= 2
SHORT: speculare
"""

from typing import Optional
from datetime import datetime, timezone

from strategies.base import BaseStrategy, Signal
from core.indicators import find_pivots, cluster_levels, nearest_level


class CompressionBreakout(BaseStrategy):

    name = "Compression Breakout"
    version = "V1.0"

    COMPRESSION_ATR_RATIO = 0.80
    COMPRESSION_ATR_LOOKBACK = 10
    RANGE_LOOKBACK = 5
    ATR_MULTIPLIER = 1.5
    MIN_RR = 2.0
    DB_SCORE_THRESHOLD = 8
    TELEGRAM_SCORE_THRESHOLD = 9

    def _trend_h4(self, df_h4, direction: str) -> bool:
        last = df_h4.iloc[-1]
        return (last["ema_50"] > last["ema_100"] > last["ema_200"]
                if direction == "LONG"
                else last["ema_50"] < last["ema_100"] < last["ema_200"])

    def _trend_h1(self, df_h1, direction: str) -> bool:
        last = df_h1.iloc[-1]
        return (last["ema_21"] > last["ema_50"]
                if direction == "LONG"
                else last["ema_21"] < last["ema_50"])

    def _score(self, trend_h4: bool, trend_h1: bool,
               compression: bool, range_tight: bool, breakout: bool) -> int:
        score = 0
        if trend_h4:    score += 3
        if trend_h1:    score += 2
        if compression: score += 2
        if range_tight: score += 2
        if breakout:    score += 1
        return min(score, 10)

    def generate_signal(self, market_data: dict) -> Optional[Signal]:
        df_h1 = market_data["df_h1"]
        df_h4 = market_data["df_h4"]
        asset = market_data["asset"]
        direction = market_data["direction"]

        min_len = self.COMPRESSION_ATR_LOOKBACK + self.RANGE_LOOKBACK + 5
        if len(df_h1) < min_len:
            return None

        if not (self._trend_h4(df_h4, direction) and self._trend_h1(df_h1, direction)):
            return None

        atr_current = float(df_h1.iloc[-1]["atr"])
        if atr_current <= 0:
            return None

        atr_avg = df_h1["atr"].iloc[-(self.COMPRESSION_ATR_LOOKBACK + 1):-1].mean()
        compression_ok = atr_current < self.COMPRESSION_ATR_RATIO * atr_avg

        range_candles = df_h1.iloc[-(self.RANGE_LOOKBACK + 1):-1]
        range_high = float(range_candles["high"].max())
        range_low  = float(range_candles["low"].min())
        range_size = range_high - range_low
        range_tight = range_size <= 1.0 * atr_current

        if not (compression_ok and range_tight):
            return None

        c_last = df_h1.iloc[-1]
        if direction == "LONG":
            breakout_ok = float(c_last["close"]) > range_high
        else:
            breakout_ok = float(c_last["close"]) < range_low

        if not breakout_ok:
            return None

        entry = float(c_last["close"])

        if direction == "LONG":
            sl = entry - self.ATR_MULTIPLIER * atr_current
            all_p = find_pivots(df_h1, lookback=5)
            tp_level = nearest_level(entry, cluster_levels(all_p["pivot_highs"], atr_current), "resistance")
        else:
            sl = entry + self.ATR_MULTIPLIER * atr_current
            all_p = find_pivots(df_h1, lookback=5)
            tp_level = nearest_level(entry, cluster_levels(all_p["pivot_lows"], atr_current), "support")

        if tp_level is None:
            return None

        tp = tp_level["price"]
        risk = abs(entry - sl)
        reward = abs(tp - entry)
        rr = reward / risk if risk > 0 else 0.0

        if rr < self.MIN_RR:
            return None

        raw_score = float(self._score(
            self._trend_h4(df_h4, direction),
            self._trend_h1(df_h1, direction),
            compression_ok, range_tight, breakout_ok
        ))

        if raw_score < self.DB_SCORE_THRESHOLD:
            return None

        ts = datetime.fromtimestamp(int(df_h1.iloc[-1]["timestamp"]) / 1000, tz=timezone.utc)

        return Signal(
            strategy_name=self.name,
            strategy_version=self.version,
            asset=asset,
            direction=direction,
            entry=entry,
            stop_loss=sl,
            take_profit=tp,
            rr=rr,
            raw_score=raw_score,
            final_score=raw_score,
            timestamp=ts,
            additional_context={
                "trend_h4_ok":    True,
                "trend_h1_ok":    True,
                "compression_ok": compression_ok,
                "range_tight":    range_tight,
                "breakout_ok":    breakout_ok,
                "atr_current":    atr_current,
                "atr_avg":        float(atr_avg),
                "range_high":     range_high,
                "range_low":      range_low,
                "atr_h1":         atr_current,
                "send_telegram":  raw_score >= self.TELEGRAM_SCORE_THRESHOLD,
            },
        )
