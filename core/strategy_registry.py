"""
core/strategy_registry.py
Registry centralizzato delle strategie V2.1.

Carica automaticamente tutte le strategie abilitate dal config YAML.
Il Signal Engine itera su registry.active_strategies.
"""

import logging
from typing import List
from strategies.base import BaseStrategy

logger = logging.getLogger("strategy_registry")


class StrategyRegistry:
    def __init__(self):
        self._strategies: List[BaseStrategy] = []

    def register(self, strategy: BaseStrategy):
        self._strategies.append(strategy)
        logger.info("Strategia registrata: %s %s", strategy.name, strategy.version)

    @property
    def active_strategies(self) -> List[BaseStrategy]:
        return list(self._strategies)

    def __len__(self):
        return len(self._strategies)


def build_registry(config: dict) -> StrategyRegistry:
    registry = StrategyRegistry()
    strategies_cfg = config.get("STRATEGIES", {})

    if strategies_cfg.get("pullback_ema", {}).get("enabled", True):
        from strategies.pullback_ema_frozen import PullbackEMAFrozen
        registry.register(PullbackEMAFrozen())

    if strategies_cfg.get("breakout_retest", {}).get("enabled", False):
        from strategies.breakout_retest import BreakoutRetest
        registry.register(BreakoutRetest())

    if strategies_cfg.get("liquidity_sweep", {}).get("enabled", False):
        from strategies.liquidity_sweep import LiquiditySweep
        registry.register(LiquiditySweep())

    if strategies_cfg.get("compression_breakout", {}).get("enabled", False):
        from strategies.compression_breakout import CompressionBreakout
        registry.register(CompressionBreakout())

    if strategies_cfg.get("pivot_reversal", {}).get("enabled", False):
        from strategies.pivot_reversal import PivotReversal
        registry.register(PivotReversal())

    logger.info("Registry caricato: %d strategie attive", len(registry))
    return registry
