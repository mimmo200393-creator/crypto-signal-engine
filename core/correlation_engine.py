"""
core/correlation_engine.py
Correlation Engine V2.1

Se piu' asset altamente correlati generano contemporaneamente segnali
nella stessa direzione, mantiene solo il segnale con Final Score piu'
elevato. Gli altri vengono marcati REJECTED con reason CORRELATION_FILTERED.

Correlazione calcolata su rendimenti percentuali H1 delle ultime
CORRELATION_LOOKBACK candele (default 100).

Tie-breaking:
    1. Final Score piu' alto
    2. Raw Score piu' alto
    3. Timestamp piu' vecchio (generato per primo)
"""

import logging
from typing import List, Dict
import numpy as np

from strategies.base import Signal

logger = logging.getLogger("correlation_engine")


def _pearson_correlation(r1: np.ndarray, r2: np.ndarray) -> float:
    if len(r1) < 2 or len(r2) < 2:
        return 0.0
    std1, std2 = r1.std(), r2.std()
    if std1 == 0 or std2 == 0:
        return 0.0
    return float(np.corrcoef(r1, r2)[0, 1])


def _returns(candles_df, lookback: int) -> np.ndarray:
    closes = candles_df["close"].values[-lookback-1:]
    if len(closes) < 2:
        return np.array([])
    return np.diff(closes) / closes[:-1]


def apply_correlation_filter(
    signals: List[Signal],
    candles_cache: Dict[str, object],
    threshold: float = 0.80,
    lookback: int = 100,
) -> List[Signal]:
    if len(signals) <= 1:
        return signals

    assets_in_play = list({s.asset for s in signals})
    returns_map: Dict[str, np.ndarray] = {}

    for asset in assets_in_play:
        df = candles_cache.get(asset)
        if df is None or len(df) < 2:
            returns_map[asset] = np.array([])
        else:
            returns_map[asset] = _returns(df, lookback)

    by_direction: Dict[str, List[Signal]] = {"LONG": [], "SHORT": []}
    for sig in signals:
        if sig.trade_status != "REJECTED":
            by_direction[sig.direction].append(sig)

    result = list(signals)

    for direction, dir_signals in by_direction.items():
        if len(dir_signals) <= 1:
            continue

        to_reject: set = set()

        for i in range(len(dir_signals)):
            for j in range(i + 1, len(dir_signals)):
                sig_a = dir_signals[i]
                sig_b = dir_signals[j]

                r_a = returns_map.get(sig_a.asset, np.array([]))
                r_b = returns_map.get(sig_b.asset, np.array([]))

                if len(r_a) == 0 or len(r_b) == 0:
                    continue

                min_len = min(len(r_a), len(r_b))
                corr = _pearson_correlation(r_a[-min_len:], r_b[-min_len:])

                if abs(corr) >= threshold:
                    logger.info(
                        "Correlazione %.3f tra %s e %s (%s): filtro applicato",
                        corr, sig_a.asset, sig_b.asset, direction
                    )
                    winner = _pick_winner(sig_a, sig_b)
                    loser_id = sig_b.signal_id if winner is sig_a else sig_a.signal_id
                    to_reject.add(loser_id)

        for sig in result:
            if sig.signal_id in to_reject:
                sig.trade_status = "REJECTED"
                sig.rejection_reason = "CORRELATION_FILTERED"
                logger.info(
                    "Segnale REJECTED per correlazione: %s %s %s (signal_id=%s)",
                    sig.strategy_name, sig.asset, sig.direction, sig.signal_id[:8]
                )

    return result


def _pick_winner(sig_a: Signal, sig_b: Signal) -> Signal:
    if sig_a.final_score != sig_b.final_score:
        return sig_a if sig_a.final_score > sig_b.final_score else sig_b
    if sig_a.raw_score != sig_b.raw_score:
        return sig_a if sig_a.raw_score > sig_b.raw_score else sig_b
    return sig_a if sig_a.timestamp <= sig_b.timestamp else sig_b
