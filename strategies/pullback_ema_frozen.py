"""
strategies/pullback_ema_frozen.py
Wrapper adapter della Pullback EMA Trend Strategy V1.0 Frozen.
VINCOLO ASSOLUTO: adapter puro, delega a core.strategy V1.0 invariato.
"""

from typing import Optional, Tuple
from datetime import datetime, timezone

from strategies.base import BaseStrategy, Signal
from core import strategy as v1_strategy
from core import scoring as v1_scoring


class PullbackEMAFrozen(BaseStrategy):

    name = "Pullback EMA Trend"
    version = "V1.0"

    DB_SCORE_THRESHOLD = 8
    TELEGRAM_SCORE_THRESHOLD = 8

    def generate_signal(self, market_data: dict) -> Optional[Signal]:
        signal, _ = self.generate_signal_with_diagnostics(market_data)
        return signal

    def generate_signal_with_diagnostics(self, market_data: dict) -> Tuple[Optional[Signal], dict]:
        df_h1 = market_data["df_h1"]
        df_h4 = market_data["df_h4"]
        config = market_data["config"]
        asset = market_data["asset"]
        direction = market_data["direction"]

        last_h1 = df_h1.iloc[-1]
        last_h4 = df_h4.iloc[-1]

        if direction == "LONG":
            trend_h4 = last_h4["ema_50"] > last_h4["ema_100"] > last_h4["ema_200"]
            trend_h1 = last_h1["ema_21"] > last_h1["ema_50"]
        else:
            trend_h4 = last_h4["ema_50"] < last_h4["ema_100"] < last_h4["ema_200"]
            trend_h1 = last_h1["ema_21"] < last_h1["ema_50"]

        atr = float(last_h1["atr"])
        close = float(last_h1["close"])
        ema21 = float(last_h1["ema_21"])
        ema50 = float(last_h1["ema_50"])

        if direction == "LONG":
            dist_ema21 = abs(close - ema21)
            dist_ema50 = abs(close - ema50)
            wick_ema21 = float(df_h1.iloc[-1]["low"]) <= ema21
            wick_ema50 = float(df_h1.iloc[-1]["low"]) <= ema50
        else:
            dist_ema21 = abs(close - ema21)
            dist_ema50 = abs(close - ema50)
            wick_ema21 = float(df_h1.iloc[-1]["high"]) >= ema21
            wick_ema50 = float(df_h1.iloc[-1]["high"]) >= ema50

        pullback_ema21 = (dist_ema21 <= 0.3 * atr) or wick_ema21
        pullback_ema50 = (dist_ema50 <= 0.3 * atr) or wick_ema50
        pullback_ok = pullback_ema21 or pullback_ema50

        if direction == "LONG":
            trigger_ok = float(df_h1.iloc[-1]["close"]) > float(df_h1.iloc[-2]["high"])
        else:
            trigger_ok = float(df_h1.iloc[-1]["close"]) < float(df_h1.iloc[-2]["low"])

        diag = {
            "conditions": {
                f"Trend H4 ({direction})": bool(trend_h4),
                f"Trend H1 ({direction})": bool(trend_h1),
                "Pullback EMA21 o EMA50": bool(pullback_ok),
                "Trigger H1": bool(trigger_ok),
            },
            "raw_score": None,
            "rejection_reason": None,
        }

        if not trend_h4:
            diag["rejection_reason"] = "TREND_H4_INVALID"
            return None, diag
        if not trend_h1:
            diag["rejection_reason"] = "TREND_H1_INVALID"
            return None, diag
        if not pullback_ok:
            diag["rejection_reason"] = "PULLBACK_MISSING"
            return None, diag
        if not trigger_ok:
            diag["rejection_reason"] = "TRIGGER_NOT_CONFIRMED"
            return None, diag

        if direction == "LONG":
            setup = v1_strategy.evaluate_long(df_h1, df_h4, config)
        else:
            setup = v1_strategy.evaluate_short(df_h1, df_h4, config)

        if setup is None:
            diag["rejection_reason"] = "V1_STRATEGY_NO_SETUP"
            return None, diag

        setup["asset"] = asset
        raw_score = float(v1_scoring.compute_score(setup))
        diag["raw_score"] = raw_score

        classification = v1_scoring.classify_score(
            int(raw_score),
            self.DB_SCORE_THRESHOLD,
            self.TELEGRAM_SCORE_THRESHOLD,
        )

        if not classification["save_to_db"]:
            diag["rejection_reason"] = f"SCORE_TOO_LOW ({raw_score:.0f} < {self.DB_SCORE_THRESHOLD})"
            return None, diag

        last_ts_ms = int(df_h1.iloc[-1]["timestamp"])
        ts = datetime.fromtimestamp(last_ts_ms / 1000, tz=timezone.utc)

        signal = Signal(
            strategy_name=self.name,
            strategy_version=self.version,
            asset=asset,
            direction=direction,
            entry=setup["entry"],
            stop_loss=setup["stop_loss"],
            take_profit=setup["take_profit"],
            rr=setup["rr"],
            raw_score=raw_score,
            final_score=raw_score,
            timestamp=ts,
            additional_context={
                "pullback_ema50":    setup.get("pullback_ema50"),
                "pullback_ema21":    setup.get("pullback_ema21"),
                "trend_h4_ok":       setup.get("trend_h4_ok"),
                "trend_h1_ok":       setup.get("trend_h1_ok"),
                "sr_level_present":  setup.get("sr_level_present"),
                "trigger_confirmed": setup.get("trigger_confirmed"),
                "trigger_type":      setup.get("trigger_type"),
                "atr_h1":            setup.get("atr_h1"),
                "support_level":     setup.get("support_level"),
                "resistance_level":  setup.get("resistance_level"),
                "setup_name":        setup.get("setup"),
                "label":             classification["label"],
                "send_telegram":     classification["send_telegram"],
                "macro_event":       None,
            },
        )
        return signal, diag
