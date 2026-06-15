"""
strategies/liquidity_sweep.py
Liquidity Sweep V1.0 — con diagnostica dettagliata
"""

from typing import Optional, Tuple
from datetime import datetime, timezone

from strategies.base import BaseStrategy, Signal
from core.indicators import find_pivots, cluster_levels, nearest_level


class LiquiditySweep(BaseStrategy):

    name = "Liquidity Sweep"
    version = "V1.0"

    PIVOT_LOOKBACK_PERIODS = 50
    MIN_TOUCHES = 2
    CLUSTER_ATR_FRACTION = 0.5
    WICK_BODY_RATIO = 2.0
    ATR_MULTIPLIER = 1.5
    MIN_RR = 2.0
    DB_SCORE_THRESHOLD = 8
    TELEGRAM_SCORE_THRESHOLD = 8

    def _significant_levels(self, df_h1, pivot_type, atr_val):
        lookback_df = df_h1.iloc[-self.PIVOT_LOOKBACK_PERIODS:].copy().reset_index(drop=True)
        pivots = find_pivots(lookback_df, lookback=5)
        raw = pivots["pivot_lows"] if pivot_type == "low" else pivots["pivot_highs"]
        clusters = cluster_levels(raw, atr_val, self.CLUSTER_ATR_FRACTION)
        return [c for c in clusters if c["count"] >= self.MIN_TOUCHES]

    def _check_sweep(self, candle, lp, direction):
        o, h, l, c = float(candle["open"]), float(candle["high"]), float(candle["low"]), float(candle["close"])
        return l < lp < c if direction == "LONG" else c < lp < h

    def _check_rejection(self, candle, direction):
        o, h, l, c = float(candle["open"]), float(candle["high"]), float(candle["low"]), float(candle["close"])
        body = abs(c - o)
        if body == 0:
            return True
        wick = (min(o, c) - l) if direction == "LONG" else (h - max(o, c))
        return wick >= self.WICK_BODY_RATIO * body

    def _score(self, sr, sweep, rejection, trigger):
        score = 0
        if sr:        score += 3
        if sweep:     score += 3
        if rejection: score += 2
        if trigger:   score += 2
        return min(score, 10)

    def generate_signal(self, market_data: dict) -> Optional[Signal]:
        signal, _ = self.generate_signal_with_diagnostics(market_data)
        return signal

    def generate_signal_with_diagnostics(self, market_data: dict) -> Tuple[Optional[Signal], dict]:
        df_h1 = market_data["df_h1"]
        asset = market_data["asset"]
        direction = market_data["direction"]

        diag = {"conditions": {}, "raw_score": None, "rejection_reason": None}

        if len(df_h1) < 55:
            diag["rejection_reason"] = "INSUFFICIENT_DATA"
            return None, diag

        atr_val = float(df_h1.iloc[-1]["atr"])
        if atr_val <= 0:
            diag["rejection_reason"] = "ATR_ZERO"
            return None, diag

        pivot_type = "low" if direction == "LONG" else "high"
        levels = self._significant_levels(df_h1, pivot_type, atr_val)
        sr_present = len(levels) > 0
        diag["conditions"]["Livelli S/R significativi (>=2 tocchi)"] = sr_present

        if not sr_present:
            diag["rejection_reason"] = "NO_SIGNIFICANT_LEVELS"
            return None, diag

        c_trigger = df_h1.iloc[-1]
        c_sweep   = df_h1.iloc[-2]
        entry = float(c_trigger["close"])

        if direction == "LONG":
            level = nearest_level(float(c_sweep["low"]), levels, "support")
        else:
            level = nearest_level(float(c_sweep["high"]), levels, "resistance")

        if level is None:
            diag["rejection_reason"] = "NO_LEVEL_FOUND"
            return None, diag
        lp = level["price"]

        sweep_ok = self._check_sweep(c_sweep, lp, direction)
        rejection_ok = self._check_rejection(c_sweep, direction)
        trigger_ok = (float(c_trigger["close"]) > float(c_sweep["high"])
                      if direction == "LONG"
                      else float(c_trigger["close"]) < float(c_sweep["low"]))

        diag["conditions"]["Sweep del livello (low<S<close)"] = bool(sweep_ok)
        diag["conditions"]["Rejection wick (>=2x corpo)"] = bool(rejection_ok)
        diag["conditions"]["Trigger H1"] = bool(trigger_ok)

        if not sweep_ok:
            diag["rejection_reason"] = "SWEEP_MISSING"
            return None, diag
        if not rejection_ok:
            diag["rejection_reason"] = "REJECTION_WICK_TOO_SMALL"
            return None, diag
        if not trigger_ok:
            diag["rejection_reason"] = "TRIGGER_NOT_CONFIRMED"
            return None, diag

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
            diag["rejection_reason"] = f"RR_TOO_LOW ({rr:.2f})"
            return None, diag

        raw_score = float(self._score(True, sweep_ok, rejection_ok, trigger_ok))
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
                "level_price": lp, "sweep_clean": sweep_ok,
                "rejection_strong": rejection_ok, "trigger_confirmed": trigger_ok,
                "atr_h1": atr_val,
                "send_telegram": raw_score >= self.TELEGRAM_SCORE_THRESHOLD,
            },
        )
        return signal, diag
