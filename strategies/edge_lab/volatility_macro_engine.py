"""
strategies/edge_lab/volatility_macro_engine.py
Edge Lab — Step 6: Volatility + Macro Engine

Due componenti distinte in un unico modulo:

1. VOLATILITY ENGINE
   Calcola il regime di volatilità su M15 e H1 per Edge Lab.
   Differenze rispetto a core/market_regime.py (legacy, invariato):
     - Opera su M15 (non solo H1) — necessario per OTE-SC
     - Aggiunge ATR percentuale rispetto al prezzo (ATR%)
     - Classifica: EXPANDING / NORMAL / CONTRACTING / SPIKE
     - Calcola "kill zone" di alta volatilità (news/open sessione)
     - Non modifica né importa market_regime.py

2. MACRO ENGINE
   Wrapper Edge Lab attorno a core/macro.py (riuso diretto).
   Aggiunge:
     - is_news_blackout(): hard gate per OTE-SC (30 min prima/dopo)
     - get_macro_context(): dict completo per Market Context Engine
     - Compatibilità con YAMLMacroProvider esistente

SEPARAZIONE NETTA:
   - core/market_regime.py   → legacy V2.1 (invariato)
   - core/macro.py           → riusato direttamente (provider YAML)
   - volatility_macro_engine.py → Edge Lab (questo file)
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

import numpy as np
import pandas as pd

# Riuso diretto del macro provider legacy (interfaccia astratta, nessun side effect)
from core.macro import MacroEventProvider

logger = logging.getLogger("edge_lab.volatility_macro")

# ============================================================
# Parametri Volatility Engine
# ============================================================

# Lookback per ATR ratio (quante candele confrontare)
ATR_LOOKBACK_M15 = 20
ATR_LOOKBACK_H1  = 20

# Soglie ratio ATR_corrente / ATR_media per classificazione
ATR_CONTRACTING_RATIO = 0.70   # < 70% della media → CONTRACTING
ATR_NORMAL_LOW_RATIO  = 0.70   # 70–130% → NORMAL
ATR_NORMAL_HIGH_RATIO = 1.30
ATR_EXPANDING_RATIO   = 1.30   # 130–200% → EXPANDING
ATR_SPIKE_RATIO       = 2.00   # > 200% → SPIKE

# Hard gate: non entrare in posizione con volatilità SPIKE
VOLATILITY_BLOCK_ON_SPIKE = True

# Finestre di alta volatilità intraday (UTC) — kill zones per OTE-SC
# Apertura Londra, Apertura NY, Chiusura NY
HIGH_VOL_WINDOWS_UTC = [
    (7, 45,  8, 30),    # Pre-Londra / Open Londra
    (12, 30, 13, 30),   # News US / transizione Overlap
    (15, 30, 16, 30),   # Open NYSE
    (21, 30, 22, 15),   # Chiusura NY / fine sessione
]

# ============================================================
# Parametri Macro Engine
# ============================================================

NEWS_BLACKOUT_MINUTES = 30   # minuti prima/dopo evento macro ad alto impatto
MACRO_WINDOW_MINUTES  = 60   # finestra di rilevamento evento (come legacy)


# ============================================================
# Volatility Engine
# ============================================================

def _wilder_atr_series(df: pd.DataFrame, period: int = 14) -> np.ndarray:
    """
    Calcola la serie ATR Wilder completa.
    Ritorna array numpy di lunghezza len(df).
    """
    n = len(df)
    high = df["high"].values
    low  = df["low"].values
    close = df["close"].values

    tr = np.zeros(n)
    tr[0] = high[0] - low[0]
    for i in range(1, n):
        tr[i] = max(
            high[i] - low[i],
            abs(high[i] - close[i - 1]),
            abs(low[i]  - close[i - 1]),
        )

    atr = np.zeros(n)
    if n < period + 1:
        atr[:] = tr.mean() if n > 0 else 0
        return atr

    atr[period] = tr[1:period + 1].mean()
    alpha = 1.0 / period
    for i in range(period + 1, n):
        atr[i] = atr[i - 1] * (1 - alpha) + tr[i] * alpha

    return atr


def classify_volatility(atr_current: float, atr_avg: float) -> str:
    """
    Classifica il regime di volatilità rispetto alla media.

    CONTRACTING → ATR corrente < 70% della media
    NORMAL      → 70%–130%
    EXPANDING   → 130%–200%
    SPIKE       → > 200%
    """
    if atr_avg <= 0:
        return "NORMAL"

    ratio = atr_current / atr_avg

    if ratio >= ATR_SPIKE_RATIO:
        return "SPIKE"
    if ratio >= ATR_EXPANDING_RATIO:
        return "EXPANDING"
    if ratio >= ATR_NORMAL_LOW_RATIO:
        return "NORMAL"
    return "CONTRACTING"


def is_high_vol_window(dt: datetime) -> bool:
    """
    True se l'ora UTC corrente cade in una delle kill zone di alta volatilità
    definite in HIGH_VOL_WINDOWS_UTC.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    utc = dt.astimezone(timezone.utc)
    t = utc.hour * 60 + utc.minute

    for h_start, m_start, h_end, m_end in HIGH_VOL_WINDOWS_UTC:
        start = h_start * 60 + m_start
        end   = h_end   * 60 + m_end
        if start <= t <= end:
            return True
    return False


def compute_volatility_context(
    df_m15: pd.DataFrame,
    df_h1: pd.DataFrame,
    now: datetime,
    current_price: float,
) -> dict:
    """
    Calcola il contesto di volatilità completo per Edge Lab.

    Args:
        df_m15:        candele M15
        df_h1:         candele H1
        now:           datetime UTC corrente
        current_price: prezzo corrente

    Returns:
        {
            "regime_m15":       str,   # CONTRACTING/NORMAL/EXPANDING/SPIKE
            "regime_h1":        str,
            "atr_m15":          float,
            "atr_h1":           float,
            "atr_pct_m15":      float, # ATR M15 come % del prezzo
            "atr_pct_h1":       float,
            "atr_ratio_m15":    float, # ATR corrente / ATR media
            "atr_ratio_h1":     float,
            "is_high_vol_window": bool,
            "is_tradeable":     bool,  # False se SPIKE o kill zone
            "block_reason":     str | None,
        }
    """
    # ATR M15
    atr_m15_series = _wilder_atr_series(df_m15, 14) if len(df_m15) > 15 else np.array([0.0])
    atr_m15_current = float(atr_m15_series[-1])
    atr_m15_avg = float(atr_m15_series[-(ATR_LOOKBACK_M15 + 1):-1].mean()) \
        if len(atr_m15_series) > ATR_LOOKBACK_M15 else atr_m15_current

    # ATR H1
    atr_h1_series = _wilder_atr_series(df_h1, 14) if len(df_h1) > 15 else np.array([0.0])
    atr_h1_current = float(atr_h1_series[-1])
    atr_h1_avg = float(atr_h1_series[-(ATR_LOOKBACK_H1 + 1):-1].mean()) \
        if len(atr_h1_series) > ATR_LOOKBACK_H1 else atr_h1_current

    regime_m15 = classify_volatility(atr_m15_current, atr_m15_avg)
    regime_h1  = classify_volatility(atr_h1_current,  atr_h1_avg)

    atr_ratio_m15 = atr_m15_current / atr_m15_avg if atr_m15_avg > 0 else 1.0
    atr_ratio_h1  = atr_h1_current  / atr_h1_avg  if atr_h1_avg  > 0 else 1.0

    atr_pct_m15 = atr_m15_current / current_price if current_price > 0 else 0.0
    atr_pct_h1  = atr_h1_current  / current_price if current_price > 0 else 0.0

    in_kill_zone = is_high_vol_window(now)

    # Tradeabilità: blocca solo SPIKE (per OTE-SC)
    block_reason: Optional[str] = None
    if VOLATILITY_BLOCK_ON_SPIKE and regime_m15 == "SPIKE":
        block_reason = f"VOLATILITY_SPIKE_M15 (ratio={atr_ratio_m15:.2f})"
    elif VOLATILITY_BLOCK_ON_SPIKE and regime_h1 == "SPIKE":
        block_reason = f"VOLATILITY_SPIKE_H1 (ratio={atr_ratio_h1:.2f})"

    logger.info(
        "Volatility [M15=%s ratio=%.2f | H1=%s ratio=%.2f] "
        "kill_zone=%s tradeable=%s",
        regime_m15, atr_ratio_m15,
        regime_h1,  atr_ratio_h1,
        in_kill_zone,
        block_reason is None,
    )

    return {
        "regime_m15":         regime_m15,
        "regime_h1":          regime_h1,
        "atr_m15":            atr_m15_current,
        "atr_h1":             atr_h1_current,
        "atr_pct_m15":        round(atr_pct_m15, 6),
        "atr_pct_h1":         round(atr_pct_h1,  6),
        "atr_ratio_m15":      round(atr_ratio_m15, 4),
        "atr_ratio_h1":       round(atr_ratio_h1,  4),
        "is_high_vol_window": in_kill_zone,
        "is_tradeable":       block_reason is None,
        "block_reason":       block_reason,
    }


# ============================================================
# Macro Engine
# ============================================================

def is_news_blackout(
    macro_provider: MacroEventProvider,
    now: datetime,
    window_minutes: int = NEWS_BLACKOUT_MINUTES,
) -> Optional[dict]:
    """
    Hard gate per OTE-SC: ritorna il dict evento se siamo dentro
    la finestra di blackout (±window_minutes dall'evento macro),
    altrimenti None.

    Compatibile con YAMLMacroProvider e qualsiasi implementazione
    di MacroEventProvider.
    """
    if macro_provider is None:
        return None

    event = macro_provider.get_active_event(now, window_minutes)
    if event is None:
        return None

    mtr = event.get("minutes_to_release", 0)
    if abs(mtr) <= window_minutes:
        logger.info(
            "Macro blackout: %s in %d min (window=%d min)",
            event.get("type", "UNKNOWN"), mtr, window_minutes
        )
        return event

    return None


def get_macro_context(
    macro_provider: MacroEventProvider,
    now: datetime,
) -> dict:
    """
    Calcola il contesto macro completo per Market Context Engine.

    Returns:
        {
            "event":          dict | None,   # evento rilevante o None
            "is_blackout":    bool,
            "minutes_to_event": int | None,
            "macro_risk":     str,           # LOW / MEDIUM / HIGH
        }
    """
    event = macro_provider.get_active_event(now, MACRO_WINDOW_MINUTES) \
        if macro_provider else None

    if event is None:
        return {
            "event":            None,
            "is_blackout":      False,
            "minutes_to_event": None,
            "macro_risk":       "LOW",
        }

    mtr = event.get("minutes_to_release", 999)
    is_blackout = abs(mtr) <= NEWS_BLACKOUT_MINUTES

    if abs(mtr) <= NEWS_BLACKOUT_MINUTES:
        macro_risk = "HIGH"
    elif abs(mtr) <= 120:
        macro_risk = "MEDIUM"
    else:
        macro_risk = "LOW"

    return {
        "event":            event,
        "is_blackout":      is_blackout,
        "minutes_to_event": mtr,
        "macro_risk":       macro_risk,
    }


# ============================================================
# Format summary
# ============================================================

def format_volatility_summary(vol_ctx: dict) -> str:
    tradeable_str = "✓ TRADEABLE" if vol_ctx["is_tradeable"] else f"✗ BLOCKED ({vol_ctx['block_reason']})"
    kill_zone_str = " [KILL ZONE]" if vol_ctx["is_high_vol_window"] else ""
    return (
        f"Volatility: M15={vol_ctx['regime_m15']} (ratio={vol_ctx['atr_ratio_m15']:.2f} "
        f"ATR={vol_ctx['atr_m15']:.4f} {vol_ctx['atr_pct_m15']*100:.3f}%) | "
        f"H1={vol_ctx['regime_h1']} (ratio={vol_ctx['atr_ratio_h1']:.2f} "
        f"ATR={vol_ctx['atr_h1']:.4f}) | "
        f"{tradeable_str}{kill_zone_str}"
    )


def format_macro_summary(macro_ctx: dict) -> str:
    if macro_ctx["event"] is None:
        return "Macro: nessun evento rilevante"
    ev = macro_ctx["event"]
    mtr = macro_ctx["minutes_to_event"]
    blackout_str = " ⚠️ BLACKOUT" if macro_ctx["is_blackout"] else ""
    return (
        f"Macro: {ev.get('type','?')} in {mtr}min "
        f"[risk={macro_ctx['macro_risk']}]{blackout_str}"
    )
