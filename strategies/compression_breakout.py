"""
strategies/compression_breakout.py
Compression Breakout V1.0 — con diagnostica dettagliata
"""

from typing import Optional, Tuple
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
    TELEGRAM_SCORE_THRESHOLD = 8

    def _trend_h4(self, df_h4, direction):
        last = df_h4.iloc[-1]
        return (last["ema_50"] > last["ema_100"] > last["ema_200"]
                if direction == "LONG"
                else last["ema_50"] < last["ema_100"] < last["ema_200"])

    def _trend_h1(self, df_h1, direction):
        last = df_h1.iloc[-1]
        return (last["ema_21"] > last["ema_50"]
                if direction == "LONG"
                else last["ema_21"] < last["ema_50"])

    def _score(self, trend_h4, trend_h1, compression, range_tight, breakout):
        score = 0
        if trend_h4:    score += 3
        if trend_h1:    score += 2
        if compression: score += 2
        if range_tight: score += 2
        if breakout:    score += 1
        return min(score, 10)

    def generate_signal(self, market_data: dict) -> Optional[Signal]:
        signal, _ = self.generate_signal_with_diagnostics(market_data)
        return signal

    def generate_signal_with_diagnostics(self, market_data: dict) -> Tuple[Optional[Signal], dict]:
        df_h1 = market_data["df_h1"]
        df_h4 = market_data["df_h4"]
        asset = market_data["asset"]
        direction = market_data["direction"]

        diag = {"conditions": {}, "raw_score": None, "rejection_reason": None}

        min_len = self.COMPRESSION_ATR_LOOKBACK + self.RANGE_LOOKBACK + 5
        if len(df_h1) < min_len:
            diag["rejection_reason"] = "INSUFFICIENT_DATA"
            return None, diag

        trend_h4 = self._trend_h4(df_h4, direction)
        trend_h1 = self._trend_h1(df_h1, direction)
        diag["conditions"][f"Trend H4 ({direction})"] = bool(trend_h4)
        diag["conditions"][f"Trend H1 ({direction})"] = bool(trend_h1)

        if not trend_h4:
            diag["rejection_reason"] = "TREND_H4_INVALID"
            return None, diag
        if not trend_h1:
            diag["rejection_reason"] = "TREND_H1_INVALID"
            return None, diag

        atr_current = float(df_h1.iloc[-1]["atr"])
        if atr_current <= 0:
            diag["rejection_reason"] = "ATR_ZERO"
            return None, diag

        atr_avg = df_h1["atr"].iloc[-(self.COMPRESSION_ATR_LOOKBACK + 1):-1].mean()
        compression_ok = atr_current < self.COMPRESSION_ATR_RATIO * atr_avg

        range_candles = df_h1.iloc[-(self.RANGE_LOOKBACK + 1):-1]
        range_high = float(range_candles["high"].max())
        range_low  = float(range_candles["low"].min())
        range_size = range_high - range_low
        range_tight = range_size <= 1.0 * atr_current

        c_last = df_h1.iloc[-1]
        breakout_ok = (float(c_last["close"]) > range_high if direction == "LONG"
                       else float(c_last["close"]) < range_low)

        diag["conditions"][f"ATR compresso (<{self.COMPRESSION_ATR_RATIO*100:.0f}% media)"] = bool(compression_ok)
        diag["conditions"]["Range ristretto (<=1 ATR)"] = bool(range_tight)
        diag["conditions"]["Breakout dal range"] = bool(breakout_ok)
        diag["conditions"]["ATR ratio"] = f"{atr_current/atr_avg:.2f}" if atr_avg > 0 else "N/A"

        if not compression_ok:
            diag["rejection_reason"] = f"ATR_NOT_COMPRESSED ({atr_current/atr_avg:.2f} >= {self.COMPRESSION_ATR_RATIO})"
            return None, diag
        if not range_tight:
            diag["rejection_reason"] = f"RANGE_TOO_WIDE ({range_size:.4f} > {atr_current:.4f})"
            return None, diag
        if not breakout_ok:
            diag["rejection_reason"] = "BREAKOUT_MISSING"
            return None, diag

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
            diag["rejection_reason"] = "NO_TP_LEVEL"
            return None, diag

        tp = tp_level["price"]
        risk = abs(entry - sl)
        reward = abs(tp - entry)
        rr = reward / risk if risk > 0 else 0.0
        diag["conditions"][f"R/R >= {self.MIN_RR}"] = rr >= self.MIN_RR

        if rr < self.MIN_RR:
            diag["rejection_reason"] = f"RR_TOO_LOW ({rr:.2f})"
            return None, diag

        raw_score = float(self._score(trend_h4, trend_h1, compression_ok, range_tight, breakout_ok))
        diag["raw_score"] = raw_score

        if raw_score < self.DB_SCORE_THRESHOLD:
            diag["rejection_reason"] = f"SCORE_TOO_LOW ({raw_score:.0f})"
            return None, diag

        ts = datetime.fromtimestamp(int(df_h1.iloc[-1]["timestamp"]) / 1000, tz=timezone.utc)

        signal = Signal(
            strategy_name=self.name, strategy_version=self.version,
            asset=asset, direction=direction,
            entry=entry, stop_loss=sl, take_profit=tp, rr=rr,
            raw_score=raw_score, final_score=raw_score, timestamp=ts,
            additional_context={
                "trend_h4_ok": True, "trend_h1_ok": True,
                "compression_ok": compression_ok, "range_tight": range_tight,
                "breakout_ok": breakout_ok, "atr_current": atr_current,
                "atr_avg": float(atr_avg), "atr_h1": atr_current,
                "send_telegram": raw_score >= self.TELEGRAM_SCORE_THRESHOLD,
            },
        )
        return signal, diag
