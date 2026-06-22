"""
strategies/edge_lab/sr_engine.py
Edge Lab — Step 5: S/R Engine

Costruisce zone Support/Resistance multi-timeframe (H4, H1, M15)
per i segnali Edge Lab, in particolare per OTE-SC (Step 9).

SEPARAZIONE NETTA:
    - build_h4_zones() in institutional_scanner_v3.py → legacy V3/V4 (invariato)
    - sr_engine.py → Edge Lab (questo file)

Riutilizzo esplicito:
    - find_pivots()   da institutional_scanner_v3 (funzione pura)
    - add_atr()       da core.indicators (funzione pura)

Differenze rispetto al legacy:
    - Multi-timeframe: H4 + H1 + M15 (legacy solo H4)
    - Classificazione zona: STRONG / VALID / WEAK basata su pivot count
      e timeframe di origine
    - SR_REACTION: verifica se il prezzo corrente sta reagendo a una zona
    - Output unificato per Market Context Engine (Step 8)
"""

from __future__ import annotations

import logging
from typing import Optional

import pandas as pd
import numpy as np

from strategies.institutional_scanner_v3 import find_pivots

logger = logging.getLogger("edge_lab.sr_engine")

# ============================================================
# Parametri
# ============================================================

# Lookback pivot per timeframe
PIVOT_LOOKBACK_H4  = 3
PIVOT_LOOKBACK_H1  = 5
PIVOT_LOOKBACK_M15 = 3

# Clustering: zona se pivot distano meno di ATR * frazione
CLUSTER_ATR_FRACTION_H4  = 0.5
CLUSTER_ATR_FRACTION_H1  = 0.4
CLUSTER_ATR_FRACTION_M15 = 0.3

# Soglie pivot count per classificazione forza zona
STRONG_MIN_PIVOTS = 3
VALID_MIN_PIVOTS  = 2   # minimo per includere la zona

# Pesi timeframe per il SR Score (usato da Market Context Engine)
TF_WEIGHT = {
    "H4":  1.0,
    "H1":  0.7,
    "M15": 0.4,
}

# Tolleranza % per price_in_sr_zone
ZONE_TOLERANCE_PCT = 0.003   # 0.3%

# Tolleranza % per SR_REACTION (prezzo vicino alla zona)
SR_REACTION_PCT = 0.005      # 0.5%


# ============================================================
# Costruzione zone per singolo timeframe
# ============================================================

def _build_zones_for_tf(
    df: pd.DataFrame,
    atr: float,
    timeframe: str,
    pivot_lookback: int,
    cluster_atr_fraction: float,
) -> list[dict]:
    """
    Costruisce le zone S/R per un singolo timeframe.

    Ogni zona è un dict:
        {
            "upper":      float,
            "lower":      float,
            "mid":        float,
            "pivot_count":int,
            "strength":   "STRONG" | "VALID" | "WEAK",
            "timeframe":  str,
            "tf_weight":  float,
        }
    """
    if len(df) < pivot_lookback * 2 + 3 or atr <= 0:
        return []

    pivots = find_pivots(df, pivot_lookback)
    all_prices = (
        [p for _, p, _ in pivots["pivot_highs"]] +
        [p for _, p, _ in pivots["pivot_lows"]]
    )
    if not all_prices:
        return []

    threshold = atr * cluster_atr_fraction
    prices = sorted(all_prices)

    # Clustering greedy
    clusters: list[list[float]] = []
    current = [prices[0]]
    for p in prices[1:]:
        if (p - current[-1]) <= threshold:
            current.append(p)
        else:
            clusters.append(current)
            current = [p]
    clusters.append(current)

    zones = []
    tf_weight = TF_WEIGHT.get(timeframe, 0.4)

    for c in clusters:
        pc = len(c)
        if pc < VALID_MIN_PIVOTS:
            continue

        upper = float(max(c))
        lower = float(min(c))
        mid   = float(np.mean(c))

        if pc >= STRONG_MIN_PIVOTS:
            strength = "STRONG"
        else:
            strength = "VALID"

        zones.append({
            "upper":       upper,
            "lower":       lower,
            "mid":         mid,
            "pivot_count": pc,
            "strength":    strength,
            "timeframe":   timeframe,
            "tf_weight":   tf_weight,
        })

    return zones


# ============================================================
# Calcolo ATR semplice (senza dipendenze circolari da core)
# ============================================================

def _simple_atr(df: pd.DataFrame, period: int = 14) -> float:
    """ATR Wilder su un DataFrame con colonne high/low/close."""
    if len(df) < period + 1:
        return 0.0
    high = df["high"].values
    low  = df["low"].values
    prev_close = df["close"].shift(1).values
    tr = np.maximum(
        high - low,
        np.maximum(
            np.abs(high - prev_close),
            np.abs(low  - prev_close),
        )
    )
    # Wilder: media semplice delle prime `period` barre, poi smoothing
    atr_arr = np.zeros(len(tr))
    atr_arr[period] = float(np.nanmean(tr[1:period + 1]))
    alpha = 1.0 / period
    for i in range(period + 1, len(tr)):
        atr_arr[i] = atr_arr[i - 1] * (1 - alpha) + tr[i] * alpha
    return float(atr_arr[-1]) if atr_arr[-1] > 0 else float(np.nanmean(tr[1:]))


# ============================================================
# Build SR Map — Entry point principale
# ============================================================

def build_sr_map(
    df_h4: pd.DataFrame,
    df_h1: pd.DataFrame,
    df_m15: pd.DataFrame,
) -> dict:
    """
    Costruisce la mappa S/R multi-timeframe (H4 + H1 + M15).

    Args:
        df_h4:  candele H4 (da candles_cache)
        df_h1:  candele H1 (da candles_cache)
        df_m15: candele M15 (da v3_candles_cache)

    Returns:
        {
            "zones":        list[dict],   # tutte le zone, ordinate per mid
            "zones_h4":     list[dict],
            "zones_h1":     list[dict],
            "zones_m15":    list[dict],
            "atr_h4":       float,
            "atr_h1":       float,
            "atr_m15":      float,
        }
    """
    atr_h4  = _simple_atr(df_h4,  14) if len(df_h4)  > 15 else 0.0
    atr_h1  = _simple_atr(df_h1,  14) if len(df_h1)  > 15 else 0.0
    atr_m15 = _simple_atr(df_m15, 14) if len(df_m15) > 15 else 0.0

    zones_h4  = _build_zones_for_tf(df_h4,  atr_h4,  "H4",  PIVOT_LOOKBACK_H4,  CLUSTER_ATR_FRACTION_H4)
    zones_h1  = _build_zones_for_tf(df_h1,  atr_h1,  "H1",  PIVOT_LOOKBACK_H1,  CLUSTER_ATR_FRACTION_H1)
    zones_m15 = _build_zones_for_tf(df_m15, atr_m15, "M15", PIVOT_LOOKBACK_M15, CLUSTER_ATR_FRACTION_M15)

    all_zones = zones_h4 + zones_h1 + zones_m15
    all_zones.sort(key=lambda z: z["mid"])

    logger.info(
        "SR Map: %d zone totali (H4=%d H1=%d M15=%d) | ATR H4=%.4f H1=%.4f M15=%.4f",
        len(all_zones), len(zones_h4), len(zones_h1), len(zones_m15),
        atr_h4, atr_h1, atr_m15,
    )

    return {
        "zones":     all_zones,
        "zones_h4":  zones_h4,
        "zones_h1":  zones_h1,
        "zones_m15": zones_m15,
        "atr_h4":    atr_h4,
        "atr_h1":    atr_h1,
        "atr_m15":   atr_m15,
    }


# ============================================================
# Query helpers (usati da OTE-SC e Market Context Engine)
# ============================================================

def price_in_sr_zone(price: float, zone: dict) -> bool:
    """
    True se il prezzo è dentro o molto vicino alla zona
    (entro ZONE_TOLERANCE_PCT di ogni lato).
    """
    tol = zone["mid"] * ZONE_TOLERANCE_PCT
    return (zone["lower"] - tol) <= price <= (zone["upper"] + tol)


def find_zones_at_price(
    sr_map: dict,
    price: float,
    min_strength: str = "VALID",
) -> list[dict]:
    """
    Ritorna tutte le zone che contengono il prezzo corrente,
    filtrate per forza minima (STRONG > VALID > WEAK).
    """
    strength_order = {"STRONG": 2, "VALID": 1, "WEAK": 0}
    min_val = strength_order.get(min_strength, 1)

    return [
        z for z in sr_map["zones"]
        if price_in_sr_zone(price, z)
        and strength_order.get(z["strength"], 0) >= min_val
    ]


def check_sr_reaction(
    sr_map: dict,
    price: float,
    direction: str,
) -> bool:
    """
    True se il prezzo è vicino a una zona S/R rilevante nella direzione
    del trade (supporto per BUY, resistenza per SELL).

    Usato come bonus di contesto in OTE-SC (Quality Score).

    direction: "BUY" → cerca zone sotto o al prezzo (supporto)
               "SELL" → cerca zone sopra o al prezzo (resistenza)
    """
    for zone in sr_map["zones"]:
        tol = zone["mid"] * SR_REACTION_PCT
        if direction == "BUY":
            # Zona di supporto: zone il cui upper è vicino al prezzo da sotto
            if (zone["upper"] - tol) <= price <= (zone["upper"] + tol):
                return True
        else:
            # Zona di resistenza: zone il cui lower è vicino al prezzo da sopra
            if (zone["lower"] - tol) <= price <= (zone["lower"] + tol):
                return True
    return False


def find_nearest_sr_zone(
    sr_map: dict,
    price: float,
    direction: str,
    min_strength: str = "VALID",
) -> Optional[dict]:
    """
    Trova la zona S/R più vicina nella direzione indicata.

    direction: "BUY"  → zona di supporto più vicina sotto il prezzo
               "SELL" → zona di resistenza più vicina sopra il prezzo
    """
    strength_order = {"STRONG": 2, "VALID": 1, "WEAK": 0}
    min_val = strength_order.get(min_strength, 1)

    if direction == "BUY":
        candidates = [
            z for z in sr_map["zones"]
            if z["mid"] < price
            and strength_order.get(z["strength"], 0) >= min_val
        ]
        return max(candidates, key=lambda z: z["mid"]) if candidates else None
    else:
        candidates = [
            z for z in sr_map["zones"]
            if z["mid"] > price
            and strength_order.get(z["strength"], 0) >= min_val
        ]
        return min(candidates, key=lambda z: z["mid"]) if candidates else None


def get_sr_score(
    sr_map: dict,
    price: float,
    direction: str,
) -> float:
    """
    Calcola uno score S/R composito [0.0 – 1.0] basato su:
        - presenza di zone al prezzo corrente
        - forza (STRONG / VALID) delle zone
        - peso timeframe (H4 > H1 > M15)

    Usato dal Market Context Engine (Step 8) come componente
    del contesto qualitativo.
    """
    zones_at = find_zones_at_price(sr_map, price, min_strength="VALID")
    if not zones_at:
        return 0.0

    strength_bonus = {"STRONG": 1.0, "VALID": 0.5, "WEAK": 0.2}
    scores = [
        z["tf_weight"] * strength_bonus.get(z["strength"], 0.3)
        for z in zones_at
    ]
    # Normalizza: score massimo teorico = tf_weight_H4 * strength_STRONG = 1.0
    raw = sum(scores) / len(scores)
    return round(min(raw, 1.0), 4)


# ============================================================
# Format summary (per logging)
# ============================================================

def format_sr_map_summary(sr_map: dict, price: float) -> str:
    lines = [
        f"SR Map: {len(sr_map['zones'])} zone "
        f"(H4={len(sr_map['zones_h4'])} H1={len(sr_map['zones_h1'])} "
        f"M15={len(sr_map['zones_m15'])}) | price={price:,.4f}"
    ]

    zones_at = find_zones_at_price(sr_map, price)
    if zones_at:
        lines.append(f"  Prezzo IN zona S/R ({len(zones_at)}):")
        for z in zones_at:
            lines.append(
                f"    [{z['timeframe']}] {z['lower']:,.4f}–{z['upper']:,.4f} "
                f"({z['strength']}, {z['pivot_count']} pivot)"
            )
    else:
        nearest_sup = find_nearest_sr_zone(sr_map, price, "BUY")
        nearest_res = find_nearest_sr_zone(sr_map, price, "SELL")
        if nearest_sup:
            lines.append(
                f"  Nearest Support: [{nearest_sup['timeframe']}] "
                f"{nearest_sup['lower']:,.4f}–{nearest_sup['upper']:,.4f} "
                f"({nearest_sup['strength']})"
            )
        if nearest_res:
            lines.append(
                f"  Nearest Resistance: [{nearest_res['timeframe']}] "
                f"{nearest_res['lower']:,.4f}–{nearest_res['upper']:,.4f} "
                f"({nearest_res['strength']})"
            )

    return "\n".join(lines)
