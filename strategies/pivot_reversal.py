"""
strategies/pivot_reversal.py
Pivot Reversal V1.0

LONG:
  - Trend H4: EMA50 > EMA100 > EMA200
  - Trend H1: EMA21 > EMA50
  - Supporto: Pivot Low ultimi 50 H1, >= 2 tocchi, cluster <= 0.5 ATR
  - Rejection candle: candle[-2] wick inferiore >= 2 * corpo reale
  - Trigger: candle[-1] close > candle[-2] high
  - SL = 1.5 x ATR, TP = swing significativo, R/R >= 2
SHORT: speculare
"""

from typing import Optional
from datetime import datetime, timezone

from strategies.base import BaseStrategy, Signal
from core.indicators import find_pivots, cluster_levels, nearest_level


class PivotReversal(BaseStrategy):

    name = "Pivot Reversal"
    version = "V1.0"

    PIVOT_LOOKBACK_PERIODS = 50
    MIN_TOUCHES = 2
    CLUSTER_ATR_FRACTION = 0.5
    WICK_BODY_RATIO = 2.0
    ATR_MULTIPLIER = 1.5
    MIN_RR = 2.0
    DB_SCORE_THRESHOLD = 8
    TELEGRAM_SCORE_THRESHOLD = 9

    def _significant_levels(self, df_h1, pivot_type: str, atr_val: float) -> list:
        lookback_df = df_h1.iloc[-self.PIVOT_LOOKBACK_PERIODS:].copy().reset_index(drop=True)
        pivots = find_pivots(lookback_df, lookback=5)
        raw = pivots["pivot_lows"] if pivot_type == "low" else pivots["pivot_highs"]
        clusters = cluster_levels(raw, atr_val, self.CLUSTER_ATR_FRACTION)
        return [c for c in clusters if c["count"] >= self.MIN_TOUCHES]

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

    def _check_rejection(self, candle, direction: str) -> bool:
        o = float(candle["open"]); h = float(candle["high"])
        l = float(candle["low"]);  c = float(candle["close"])
        body = abs(c - o)
        if body == 0:
            return True
        wick = (min(o, c) - l) if direction == "LONG" else (h - max(o, c))
        return wick >= self.WICK_BODY_RATIO * body

    def _at_level(self, candle, lp: float, atr_val: float, direction: str) -> bool:
        if direction == "LONG":
            return abs(float(candle["low"]) - lp) <= 0.5 * atr_val
        else:
            return abs(float(candle["high"]) - lp) <= 0.5 * atr_val

    def _score(self, trend_h4: bool, trend_h1: bool,
               at_level: bool, rejection: bool, trigger: bool) -> int:
        score = 0
        if trend_h4:  score += 3
        if trend_h1:  score += 2
        if at_level:  score += 2
        if rejection: score += 2
        if trigger:   score += 1
        return min(score, 10)

    def generate_signal(self, market_data: dict) -> Optional[Signal]:
        df_h1 = market_data["df_h1"]
        df_h4 = market_data["df_h4"]
        asset = market_data["asset"]
        direction = market_data["direction"]

        if len(df_h1) < 55:
            return None

        if not (self._trend_h4(df_h4, direction) and self._trend_h1(df_h1, direction)):
            return None

        atr_val = float(df_h1.iloc[-1]["atr"])
        if atr_val <= 0:
            return None

        pivot_type = "low" if direction == "LONG" else "high"
        levels = self._significant_levels(df_h1, pivot_type, atr_val)
        if not levels:
            return None

        c_trigger   = df_h1.iloc[-1]
        c_rejection = df_h1.iloc[-2]
        entry = float(c_trigger["close"])

        if direction == "LONG":
            level = nearest_level(float(c_rejection["low"]), levels, "support")
        else:
            level = nearest_level(float(c_rejection["high"]), levels, "resistance")

        if level is None:
            return None
        lp = level["price"]

        at_level = self._at_level(c_rejection, lp, atr_val, direction)
        rejection_ok = self._check_rejection(c_rejection, direction)

        if not (at_level and rejection_ok):
            return None

        if direction == "LONG":
            trigger_ok = float(c_trigger["close"]) > float(c_rejection["high"])
        else:
            trigger_ok = float(c_trigger["close"]) < float(c_rejection["low"])

        if not trigger_ok:
            return None

        if direction == "LONG":
            sl = entry - self.ATR_MULTIPLIER * atr_val
            all_p = find_pivots(df_h1, lookback=5)
            tp_level = nearest_level(entry, cluster_levels(all_p["pivot_highs"], atr_val), "resistance")
        else:
            sl = entry + self.ATR_MULTIPLIER * atr_val
            all_p = find_pivots(df_h1, lookback=5)
            tp_level = nearest_level(entry, cluster_levels(all_p["pivot_lows"], atr_val), "support")

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
            at_level, rejection_ok, trigger_ok
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
                "trend_h4_ok":       True,
                "trend_h1_ok":       True,
                "level_price":       lp,
                "at_level":          at_level,
                "rejection_strong":  rejection_ok,
                "trigger_confirmed": trigger_ok,
                "atr_h1":            atr_val,
                "send_telegram":     raw_score >= self.TELEGRAM_SCORE_THRESHOLD,
            },
        )
