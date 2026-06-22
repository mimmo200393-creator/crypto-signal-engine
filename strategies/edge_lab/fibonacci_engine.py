"""
strategies/edge_lab/fibonacci_engine.py
Edge Lab — Step 7: Fibonacci Engine

Calcola tutti i livelli Fibonacci di ritracciamento sul range della
sessione di riferimento (fornito dal Session Engine, Step 3).

Livelli: 0%, 23.6%, 38.2%, 50%, 61.8%, 78.6%, 100%
Zona OTE: 61.8% – 78.6% (delega a compute_ote_zone di session_engine)

BUY  → ritracciamento dal High verso il Low
SELL → ritracciamento dal Low verso il High

Non duplica la logica OTE: importa compute_ote_zone da session_engine
e aggiunge i livelli intermedi (23.6%, 38.2%, 50%) per dashboard
e analisi futura.
"""

from __future__ import annotations

import logging
from typing import Optional

from strategies.edge_lab.session_engine import compute_ote_zone

logger = logging.getLogger("edge_lab.fibonacci_engine")

# Livelli Fibonacci standard
FIB_LEVELS = {
    "fib_0":    0.000,
    "fib_236":  0.236,
    "fib_382":  0.382,
    "fib_500":  0.500,
    "fib_618":  0.618,
    "fib_786":  0.786,
    "fib_1000": 1.000,
}


def compute_fibonacci_levels(
    session_levels: dict,
    direction: str,
) -> Optional[dict]:
    """
    Calcola tutti i livelli Fibonacci sul range della sessione di riferimento.

    Args:
        session_levels: dict con "high", "low", "range" (da Session Engine)
        direction:      "BUY" | "SELL"

    Returns:
        {
            "direction":    str,
            "session_high": float,
            "session_low":  float,
            "range":        float,
            "fib_0":        float,   # estremo di partenza
            "fib_236":      float,
            "fib_382":      float,
            "fib_500":      float,
            "fib_618":      float,
            "fib_786":      float,
            "fib_1000":     float,   # estremo opposto
            "ote_low":      float,   # min della zona OTE
            "ote_high":     float,   # max della zona OTE
        }
        oppure None se session_levels non disponibile o range <= 0.
    """
    if session_levels is None:
        return None

    high = session_levels.get("high")
    low  = session_levels.get("low")

    if high is None or low is None:
        return None

    high = float(high)
    low  = float(low)
    rng  = high - low

    if rng <= 0:
        logger.warning("Fibonacci: range sessione <= 0 (high=%.4f low=%.4f)", high, low)
        return None

    # Calcolo livelli in base alla direzione
    if direction == "BUY":
        # Ritracciamento dal High verso il Low
        # fib_0 = High (punto di partenza), fib_1000 = Low (obiettivo)
        levels = {
            key: high - ratio * rng
            for key, ratio in FIB_LEVELS.items()
        }
    else:
        # Ritracciamento dal Low verso il High
        # fib_0 = Low (punto di partenza), fib_1000 = High (obiettivo)
        levels = {
            key: low + ratio * rng
            for key, ratio in FIB_LEVELS.items()
        }

    # OTE zone da session_engine (unica fonte di verità)
    ote = compute_ote_zone(session_levels, direction)
    ote_low  = ote["ote_low"]  if ote else levels["fib_786"]
    ote_high = ote["ote_high"] if ote else levels["fib_618"]

    result = {
        "direction":    direction,
        "session_high": high,
        "session_low":  low,
        "range":        rng,
        "ote_low":      ote_low,
        "ote_high":     ote_high,
        **levels,
    }

    logger.debug(
        "Fibonacci [%s] range=%.4f OTE=[%.4f-%.4f] "
        "fib_618=%.4f fib_786=%.4f",
        direction, rng, ote_low, ote_high,
        levels["fib_618"], levels["fib_786"],
    )

    return result


def price_in_ote(price: float, fib: dict) -> bool:
    """True se il prezzo è dentro la zona OTE [ote_low, ote_high]."""
    if fib is None:
        return False
    return fib["ote_low"] <= price <= fib["ote_high"]


def nearest_fib_level(price: float, fib: dict) -> tuple[str, float]:
    """
    Ritorna (nome_livello, prezzo) del livello Fibonacci più vicino al prezzo.
    Utile per logging e dashboard.
    """
    if fib is None:
        return ("N/A", 0.0)

    fib_prices = {k: fib[k] for k in FIB_LEVELS}
    nearest = min(fib_prices.items(), key=lambda kv: abs(kv[1] - price))
    return nearest


def format_fibonacci_summary(fib: dict, current_price: float) -> str:
    """Riepilogo testuale per logging."""
    if fib is None:
        return "Fibonacci: N/A"

    in_ote = price_in_ote(current_price, fib)
    nearest_name, nearest_price = nearest_fib_level(current_price, fib)

    return (
        f"Fibonacci [{fib['direction']}] "
        f"H={fib['session_high']:,.4f} L={fib['session_low']:,.4f} "
        f"range={fib['range']:,.4f} | "
        f"OTE=[{fib['ote_low']:,.4f}-{fib['ote_high']:,.4f}] "
        f"price={current_price:,.4f} "
        f"in_ote={in_ote} nearest={nearest_name}({nearest_price:,.4f})"
    )
