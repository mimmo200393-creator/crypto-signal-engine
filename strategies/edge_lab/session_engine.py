"""
strategies/edge_lab/session_engine.py
Edge Lab — Step 3: Session Engine

Traccia High/Low/Range/Midpoint per ogni sessione di mercato,
fornisce la sessione di riferimento per la strategia OTE-SC e
gestisce automaticamente CET/CEST (Europe/Rome).

SESSIONI (UTC):
    ASIA     22:01 (giorno D-1) — 07:59
    LONDON   08:00 — 13:29
    OVERLAP  13:30 — 16:30
    NEW_YORK 16:31 — 22:00

ROTAZIONE sessione di riferimento OTE-SC:
    ASIA     → usa NEW_YORK precedente
    LONDON   → usa ASIA
    OVERLAP  → usa LONDON
    NEW_YORK → usa EUROPEAN SESSION COMPOSITE
               (max High London/Overlap, min Low London/Overlap)

Le candele M15 vengono lette dalla tabella v3_candles_cache
(condivisa con V3.2/V4.0/V4.1).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

logger = logging.getLogger("edge_lab.session_engine")

# ============================================================
# Costanti sessione (minuti UTC da mezzanotte)
# ============================================================
# ASIA:     22:01 UTC (giorno precedente) → 07:59 UTC
# LONDON:   08:00 → 13:29 UTC
# OVERLAP:  13:30 → 16:30 UTC
# NEW_YORK: 16:31 → 22:00 UTC

SESSION_BOUNDARIES = {
    "ASIA":     (22 * 60 + 1,  7 * 60 + 59),   # cross-midnight
    "LONDON":   (8 * 60,       13 * 60 + 29),
    "OVERLAP":  (13 * 60 + 30, 16 * 60 + 30),
    "NEW_YORK": (16 * 60 + 31, 22 * 60),
}

ROME_TZ = ZoneInfo("Europe/Rome")


# ============================================================
# Determinazione sessione corrente
# ============================================================

def get_current_session(dt: datetime) -> str:
    """
    Ritorna la sessione corrente in UTC.
    dt deve essere timezone-aware (UTC consigliato).
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    utc = dt.astimezone(timezone.utc)
    t = utc.hour * 60 + utc.minute

    # LONDON, OVERLAP, NEW_YORK: no cross-midnight, verifica semplice
    if 8 * 60 <= t <= 13 * 60 + 29:
        return "LONDON"
    if 13 * 60 + 30 <= t <= 16 * 60 + 30:
        return "OVERLAP"
    if 16 * 60 + 31 <= t <= 22 * 60:
        return "NEW_YORK"
    # Tutto il resto: ASIA (22:01-07:59 cross-midnight)
    return "ASIA"


def get_reference_session(current_session: str) -> str | list[str]:
    """
    Sessione di riferimento per il calcolo Fibonacci/SL/TP in OTE-SC.

    NEW_YORK → ["LONDON", "OVERLAP"]  (composite europeo)
    Tutti gli altri → singola sessione
    """
    rotation = {
        "ASIA":     "NEW_YORK",
        "LONDON":   "ASIA",
        "OVERLAP":  "LONDON",
        "NEW_YORK": ["LONDON", "OVERLAP"],
    }
    return rotation[current_session]


# ============================================================
# Calcolo High/Low sessione da candele M15
# ============================================================

def _session_start_utc(dt: datetime, session: str) -> datetime:
    """
    Calcola il timestamp UTC di inizio della sessione
    per la data di dt.
    """
    date = dt.date()
    if session == "LONDON":
        return datetime(date.year, date.month, date.day, 8, 0, tzinfo=timezone.utc)
    if session == "OVERLAP":
        return datetime(date.year, date.month, date.day, 13, 30, tzinfo=timezone.utc)
    if session == "NEW_YORK":
        return datetime(date.year, date.month, date.day, 16, 31, tzinfo=timezone.utc)
    # ASIA: inizia alle 22:01 del giorno precedente
    prev = date - timedelta(days=1)
    return datetime(prev.year, prev.month, prev.day, 22, 1, tzinfo=timezone.utc)


def _session_end_utc(dt: datetime, session: str) -> datetime:
    date = dt.date()
    if session == "LONDON":
        return datetime(date.year, date.month, date.day, 13, 29, tzinfo=timezone.utc)
    if session == "OVERLAP":
        return datetime(date.year, date.month, date.day, 16, 30, tzinfo=timezone.utc)
    if session == "NEW_YORK":
        return datetime(date.year, date.month, date.day, 22, 0, tzinfo=timezone.utc)
    # ASIA: termina alle 07:59 dello stesso giorno di dt
    return datetime(date.year, date.month, date.day, 7, 59, tzinfo=timezone.utc)


def compute_session_levels(
    df_m15: pd.DataFrame,
    session: str,
    reference_dt: datetime,
) -> dict | None:
    """
    Calcola High/Low/Range/Midpoint per una sessione specifica
    usando le candele M15.

    Args:
        df_m15:       DataFrame candele M15 con colonne timestamp(ms), high, low
        session:      "ASIA" | "LONDON" | "OVERLAP" | "NEW_YORK"
        reference_dt: datetime UTC di riferimento (tipicamente "ora corrente")
                      usato per determinare quale giorno cercare la sessione.

    Returns:
        dict con keys: session, high, low, range, midpoint, candle_count
        oppure None se non ci sono candele disponibili per quella sessione.
    """
    if len(df_m15) == 0:
        return None

    start = _session_start_utc(reference_dt, session)
    end = _session_end_utc(reference_dt, session)

    # Converti timestamp ms → datetime UTC per il filtraggio
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)

    mask = (df_m15["timestamp"] >= start_ms) & (df_m15["timestamp"] <= end_ms)
    session_candles = df_m15[mask]

    if len(session_candles) == 0:
        logger.debug(
            "Session Engine: nessuna candela M15 per %s "
            "[%s — %s]", session, start.isoformat(), end.isoformat()
        )
        return None

    high = float(session_candles["high"].max())
    low = float(session_candles["low"].min())
    rng = high - low
    mid = (high + low) / 2

    return {
        "session":      session,
        "high":         high,
        "low":          low,
        "range":        rng,
        "midpoint":     mid,
        "candle_count": len(session_candles),
        "start_utc":    start.isoformat(),
        "end_utc":      end.isoformat(),
    }


def compute_european_composite(
    df_m15: pd.DataFrame,
    reference_dt: datetime,
) -> dict | None:
    """
    Calcola il European Session Composite per NEW_YORK:
    max(London High, Overlap High) e min(London Low, Overlap Low).

    Usato come sessione di riferimento quando la sessione corrente
    è NEW_YORK.
    """
    london = compute_session_levels(df_m15, "LONDON", reference_dt)
    overlap = compute_session_levels(df_m15, "OVERLAP", reference_dt)

    if london is None and overlap is None:
        return None

    highs = [s["high"] for s in [london, overlap] if s is not None]
    lows  = [s["low"]  for s in [london, overlap] if s is not None]

    high = max(highs)
    low  = min(lows)
    rng  = high - low
    mid  = (high + low) / 2

    return {
        "session":      "EUROPEAN_COMPOSITE",
        "high":         high,
        "low":          low,
        "range":        rng,
        "midpoint":     mid,
        "candle_count": (london["candle_count"] if london else 0) +
                        (overlap["candle_count"] if overlap else 0),
        "london":       london,
        "overlap":      overlap,
    }


# ============================================================
# Entry point principale
# ============================================================

def build_session_context(
    df_m15: pd.DataFrame,
    now: datetime,
) -> dict:
    """
    Punto di ingresso principale del Session Engine.

    Calcola:
    - sessione corrente
    - sessione di riferimento OTE-SC
    - livelli (High/Low/Range/Midpoint) della sessione corrente
    - livelli della sessione di riferimento

    Args:
        df_m15: candele M15 (dalla v3_candles_cache), con timestamp in ms
        now:    datetime UTC corrente

    Returns:
        {
            "current_session":    str,
            "reference_session":  str | list,
            "current_levels":     dict | None,
            "reference_levels":   dict | None,
            "european_composite": dict | None,  # solo se NEW_YORK
        }
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    current = get_current_session(now)
    ref = get_reference_session(current)

    current_levels = compute_session_levels(df_m15, current, now)

    # Sessione di riferimento
    if current == "NEW_YORK":
        eu_composite = compute_european_composite(df_m15, now)
        reference_levels = eu_composite
        ref_label = "EUROPEAN_COMPOSITE"
    else:
        # Singola sessione precedente
        ref_session = ref  # str
        # Per ASIA il riferimento è NEW_YORK del giorno precedente
        if current == "ASIA":
            prev_day = now - timedelta(days=1)
            reference_levels = compute_session_levels(df_m15, ref_session, prev_day)
        else:
            reference_levels = compute_session_levels(df_m15, ref_session, now)
        ref_label = ref_session
        eu_composite = None

    logger.info(
        "Session Engine [%s]: current=%s ref=%s | "
        "current_levels=%s | ref_levels=%s",
        now.strftime("%H:%M UTC"),
        current,
        ref_label,
        f"H={current_levels['high']:.4f} L={current_levels['low']:.4f}"
            if current_levels else "N/A",
        f"H={reference_levels['high']:.4f} L={reference_levels['low']:.4f}"
            if reference_levels else "N/A",
    )

    return {
        "current_session":    current,
        "reference_session":  ref_label,
        "current_levels":     current_levels,
        "reference_levels":   reference_levels,
        "european_composite": eu_composite,
    }


# ============================================================
# Fibonacci OTE (riutilizzato da Step 7 Fibonacci Engine)
# ============================================================

def compute_ote_zone(session_levels: dict, direction: str) -> dict | None:
    """
    Calcola la zona OTE (Optimal Trade Entry) 61.8%–78.6% del range
    della sessione di riferimento.

    direction: "BUY" → ritracciamento dal High verso il Low
               "SELL" → ritracciamento dal Low verso il High

    Returns:
        {"ote_low": float, "ote_high": float, "fib_618": float,
         "fib_786": float, "session_high": float, "session_low": float}
        oppure None se i livelli non sono disponibili.
    """
    if session_levels is None:
        return None

    high = session_levels["high"]
    low  = session_levels["low"]
    rng  = high - low

    if rng <= 0:
        return None

    if direction == "BUY":
        # Pullback verso il basso: 61.8% e 78.6% dal High
        fib_618 = high - 0.618 * rng
        fib_786 = high - 0.786 * rng
        ote_low  = fib_786
        ote_high = fib_618
    else:
        # Rally verso l'alto: 61.8% e 78.6% dal Low
        fib_618 = low + 0.618 * rng
        fib_786 = low + 0.786 * rng
        ote_low  = fib_618
        ote_high = fib_786

    return {
        "ote_low":      ote_low,
        "ote_high":     ote_high,
        "fib_618":      fib_618,
        "fib_786":      fib_786,
        "session_high": high,
        "session_low":  low,
    }


# ============================================================
# Verifica OTE Touch (usato da Step 9 OTE-SC)
# ============================================================

def check_ote_touch(candle: pd.Series, ote_zone: dict) -> bool:
    """
    Verifica se la candela M15 corrente ha toccato la zona OTE
    (almeno High o Low della candela entra nella zona 61.8%-78.6%).

    Questo è l'OTE_TOUCH richiesto dalla spec OTE-SC.
    """
    if ote_zone is None:
        return False

    candle_high = float(candle["high"])
    candle_low  = float(candle["low"])
    ote_low     = ote_zone["ote_low"]
    ote_high    = ote_zone["ote_high"]

    # Almeno un estremo della candela cade nella zona OTE
    high_in_zone = ote_low <= candle_high <= ote_high
    low_in_zone  = ote_low <= candle_low  <= ote_high
    # Oppure la candela attraversa completamente la zona
    spans_zone   = candle_low <= ote_low and candle_high >= ote_high

    return high_in_zone or low_in_zone or spans_zone


# ============================================================
# Utility: ora corrente in CET/CEST (per logging/display)
# ============================================================

def utc_to_rome(dt: datetime) -> datetime:
    """Converte un datetime UTC in Europe/Rome (CET/CEST automatico)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(ROME_TZ)
