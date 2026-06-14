"""
strategies/pullback_ema_frozen.py
Wrapper adapter della Pullback EMA Trend Strategy V1.0 Frozen.

VINCOLO ASSOLUTO: questo modulo e' un adapter puro.
NON contiene logica di strategia propria.
Delega interamente a core.strategy (V1.0 invariato) e core.scoring (V1.0 invariato).
"""

from typing import Optional
from datetime import datetime, timezone

from strategies.base import BaseStrategy, Signal
from core import strategy as v1_strategy
from core import scoring as v1_scoring


class PullbackEMAFrozen(BaseStrategy):

    name = "Pullback EMA Trend"
    version = "V1.0"

    DB_SCORE_THRESHOLD = 8
    TELEGRAM_SCORE_THRESHOLD = 9

    def generate_signal(self, market_data: dict) -> Optional[Signal]:
        df_h1 = market_data["df_h1"]
        df_h4 = market_data["df_h4"]
        config = market_data["config"]
        asset = market_data["asset"]
        direction = market_data["direction"]

        if direction == "LONG":
            setup = v1_strategy.evaluate_long(df_h1, df_h4, config)
        else:
            setup = v1_strategy.evaluate_short(df_h1, df_h4, config)

        if setup is None:
            return None

        setup["asset"] = asset

        raw_score = float(v1_scoring.compute_score(setup))
        classification = v1_scoring.classify_score(
            int(raw_score),
            self.DB_SCORE_THRESHOLD,
            self.TELEGRAM_SCORE_THRESHOLD,
        )

        if not classification["save_to_db"]:
            return None

        last_ts_ms = int(df_h1.iloc[-1]["timestamp"])
        ts = datetime.fromtimestamp(last_ts_ms / 1000, tz=timezone.utc)

        return Signal(
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
