"""
strategies/breakout_retest.py
Breakout Retest Strategy V1.0 — con diagnostica dettagliata
"""

from typing import Optional, Tuple
from datetime import datetime, timezone

from strategies.base import BaseStrategy, Signal
from core.indicators import find_pivots, cluster_levels, nearest_level


class BreakoutRetest(BaseStrategy):

    name = "Breakout Retest"
    version = "V1.0"

    PIVOT_LOOKBACK_PERIODS = 50
    MIN_TOUCHES = 2
    CLUSTER_ATR_FRACTION = 0.5
    RETEST_ATR_FRACTION = 0.3
    ATR_MULTIPLIER = 1.5
    MIN_RR = 2.0
    DB_SCORE_THRESHOLD = 8
    TELEGRAM_SCORE_THRESHOLD = 8

    def _significant_levels(self, df_h1, pivot_type: str, atr_val: float) -> list:
        lookback_df = df_h1.iloc[-self.PIVOT_LOOKBACK_PERIODS:].copy().reset_index(drop=True)
        pivots = find_pivots(lookback_df, lookback=5)
        raw = pivots["pivot_highs"] if pivot_type == "high" else pivots["pivot_lows"]
        clusters = cluster_levels(raw, atr_val, self.CLUSTER_ATR_FRACTION)
        return [c for c in clusters if c["count"] >= self.MIN_TOUCHES]

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

    def _score(self, trend_h4, trend_h1, breakout, retest, sr, trigger):
        score = 0
        if trend_h4: score += 3
        if trend_h1: score += 2
        if breakout: score += 2
        if retest:   score += 1
        if sr:       score += 1
        if trigger:  score += 1
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

        if len(df_h1) < 60:
            diag["rejection_reason"] = "INSUFFICIENT_DATA"
            return None, diag

        atr_val = float(df_h1.iloc[-1]["atr"])
        if atr_val <= 0:
            diag["rejection_reason"] = "ATR_ZERO"
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

        levels = self._significant_levels(df_h1, "high" if direction == "LONG" else "low", atr_val)
        sr_present = len(levels) > 0
        diag["conditions"]["Livelli S/R significativi (>=2 tocchi)"] = sr_present

        if not sr_present:
            diag["rejection_reason"] = "NO_SIGNIFICANT_LEVELS"
            return None, diag

        c_trigger  = df_h1.iloc[-1]
        c_retest   = df_h1.iloc[-2]
        c_breakout = df_h1.iloc[-3]

        if direction == "LONG":
            level = nearest_level(float(c_breakout["close"]), levels, "resistance")
            if level is None:
                diag["rejection_reason"] = "NO_RESISTANCE_LEVEL"
                return None, diag
            lp = level["price"]
            breakout_ok = float(c_breakout["close"]) > lp
            dist = abs(float(c_retest["close"]) - lp)
            wick = float(c_retest["low"]) <= lp <= float(c_retest["high"])
            retest_ok = (dist <= self.RETEST_ATR_FRACTION * atr_val) or wick
            trigger_ok = float(c_trigger["close"]) > float(c_retest["high"])
        else:
            level = nearest_level(float(c_breakout["close"]), levels, "support")
            if level is None:
                diag["rejection_reason"] = "NO_SUPPORT_LEVEL"
                return None, diag
            lp = level["price"]
            breakout_ok = float(c_breakout["close"]) < lp
            dist = abs(float(c_retest["close"]) - lp)
            wick = float(c_retest["low"]) <= lp <= float(c_retest["high"])
            retest_ok = (dist <= self.RETEST_ATR_FRACTION * atr_val) or wick
            trigger_ok = float(c_trigger["close"]) < float(c_retest["low"])

        diag["conditions"]["Breakout confermato"] = bool(breakout_ok)
        diag["conditions"]["Retest valido (<=0.3 ATR o wick)"] = bool(retest_ok)
        diag["conditions"]["Trigger H1"] = bool(trigger_ok)

        if not breakout_ok:
            diag["rejection_reason"] = "BREAKOUT_MISSING"
            return None, diag
        if not retest_ok:
            diag["rejection_reason"] = "RETEST_MISSING"
            return None, diag
        if not trigger_ok:
            diag["rejection_reason"] = "TRIGGER_NOT_CONFIRMED"
            return None, diag

        entry = float(c_trigger["close"])
        if direction == "LONG":
            sl = entry - self.ATR_MULTIPLIER * atr_val
            all_p = find_pivots(df_h1, lookback=5)
            tp_level = nearest_level(entry, cluster_levels(all_p["pivot_highs"], atr_val), "resistance")
        else:
            sl = entry + self.ATR_MULTIPLIER * atr_val
            all_p = find_pivots(df_h1, lookback=5)
            tp_level = nearest_level(entry, cluster_levels(all_p["pivot_lows"], atr_val), "support")

        if tp_level is None:
            diag["rejection_reason"] = "NO_TP_LEVEL"
            return None, diag

        tp = tp_level["price"]
        risk = abs(entry - sl)
        reward = abs(tp - entry)
        rr = reward / risk if risk > 0 else 0.0
        diag["conditions"][f"R/R >= {self.MIN_RR}"] = rr >= self.MIN_RR

        if rr < self.MIN_RR:
            diag["rejection_reason"] = f"RR_TOO_LOW ({rr:.2f} < {self.MIN_RR})"
            return None, diag

        raw_score = float(self._score(trend_h4, trend_h1, breakout_ok, retest_ok, True, trigger_ok))
        diag["raw_score"] = raw_score

        if raw_score < self.DB_SCORE_THRESHOLD:
            diag["rejection_reason"] = f"SCORE_TOO_LOW ({raw_score:.0f} < {self.DB_SCORE_THRESHOLD})"
            return None, diag

        ts = datetime.fromtimestamp(int(df_h1.iloc[-1]["timestamp"]) / 1000, tz=timezone.utc)

        signal = Signal(
            strategy_name=self.name, strategy_version=self.version,
            asset=asset, direction=direction,
            entry=entry, stop_loss=sl, take_profit=tp, rr=rr,
            raw_score=raw_score, final_score=raw_score, timestamp=ts,
            additional_context={
                "trend_h4_ok": True, "trend_h1_ok": True,
                "level_price": lp, "breakout_clean": breakout_ok,
                "retest_clean": retest_ok, "trigger_confirmed": trigger_ok,
                "atr_h1": atr_val,
                "send_telegram": raw_score >= self.TELEGRAM_SCORE_THRESHOLD,
            },
        )
        return signal, diag
