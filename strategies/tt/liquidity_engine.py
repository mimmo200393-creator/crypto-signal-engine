"""
strategies/tt/liquidity_engine.py
TT — Liquidity Model (1H)

Risponde a: "DOVE PUO' ESSERE PRESA LIQUIDITA' VICINO A QUESTA POI?"

Codice INDIPENDENTE -- stesso principio delle fasi precedenti. Nessun
import da liquidity_engine.py (Edge Lab) o money_flow_map.py.

Logica (spec sezione 4): per ogni POI, la liquidity NON e' una lista
generica -- e' collegata alla zona specifica:
    - liquidity SOPRA la POI (rilevante per SELL: buy-side liquidity)
    - liquidity SOTTO la POI (rilevante per BUY: sell-side liquidity)
    - tipo (swing / equal high-low)
    - distanza dalla POI
    - il candidato piu' vicino/rilevante = possibile sweep target

Tipi di liquidity rilevati (sottoinsieme volutamente piccolo, spec
sezione 31 "non aggiungere 20 filtri"):
    - Swing High / Swing Low (stessa regola k=2 gia' usata nel
      Direction Engine, duplicata qui per isolamento)
    - Equal Highs / Equal Lows (tolleranza percentuale)
"""

from __future__ import annotations

import pandas as pd

SWING_K = 2
EQUAL_LEVEL_TOLERANCE_PCT = 0.0015


def _detect_swings(df: pd.DataFrame, k: int = SWING_K) -> list:
    """Stessa regola oggettiva del Direction Engine, duplicata per isolamento."""
    if df is None or len(df) < 2 * k + 1:
        return []

    highs = df["high"].astype(float).values
    lows = df["low"].astype(float).values
    timestamps = df["timestamp"].values

    swings = []
    for i in range(k, len(df) - k):
        window_h_before = highs[i - k:i]
        window_h_after = highs[i + 1:i + k + 1]
        window_l_before = lows[i - k:i]
        window_l_after = lows[i + 1:i + k + 1]

        if highs[i] > window_h_before.max() and highs[i] > window_h_after.max():
            swings.append({"type": "HIGH", "price": round(float(highs[i]), 5),
                           "timestamp": int(timestamps[i]), "index": i})
        if lows[i] < window_l_before.min() and lows[i] < window_l_after.min():
            swings.append({"type": "LOW", "price": round(float(lows[i]), 5),
                           "timestamp": int(timestamps[i]), "index": i})

    swings.sort(key=lambda s: s["index"])
    return swings


def _detect_equal_levels(swings: list, tolerance_pct: float = EQUAL_LEVEL_TOLERANCE_PCT) -> list:
    """
    Equal Highs / Equal Lows: due o piu' swing dello stesso tipo entro
    una tolleranza percentuale -- indicano un livello dove multipli
    stop/ordini si accumulano (liquidity piu' densa di un singolo swing).
    """
    highs = [s for s in swings if s["type"] == "HIGH"]
    lows = [s for s in swings if s["type"] == "LOW"]
    equal_levels = []

    for group, kind in ((highs, "EQUAL_HIGH"), (lows, "EQUAL_LOW")):
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                p1, p2 = group[i]["price"], group[j]["price"]
                if p1 == 0:
                    continue
                if abs(p1 - p2) / p1 <= tolerance_pct:
                    equal_levels.append({
                        "type": kind,
                        "price": round((p1 + p2) / 2, 5),
                        "timestamp": max(group[i]["timestamp"], group[j]["timestamp"]),
                    })

    return equal_levels


def build_liquidity_for_poi(df_h1: pd.DataFrame, poi: dict, current_price: float) -> dict:
    """
    Entry point principale. Costruisce lo scenario di liquidity per UNA
    POI specifica (spec sezione 4 -- "voglio uno scenario, non
    'Liquidity detected'").

    Per una Demand (BUY): il sweep target rilevante e' SOTTO la POI
    (sell-side liquidity -- il prezzo la prende, poi riparte su).
    Per una Supply (SELL): il sweep target e' SOPRA (buy-side liquidity).
    """
    swings = _detect_swings(df_h1)
    equal_levels = _detect_equal_levels(swings)

    all_levels = []
    for s in swings:
        all_levels.append({
            "type": "SWING_HIGH" if s["type"] == "HIGH" else "SWING_LOW",
            "price": s["price"], "timestamp": s["timestamp"],
        })
    all_levels.extend(equal_levels)

    above = [lv for lv in all_levels if lv["price"] > poi["zone_high"]]
    below = [lv for lv in all_levels if lv["price"] < poi["zone_low"]]

    for lv in above:
        lv["distance_from_poi"] = round(lv["price"] - poi["zone_high"], 5)
    for lv in below:
        lv["distance_from_poi"] = round(poi["zone_low"] - lv["price"], 5)

    above.sort(key=lambda lv: lv["distance_from_poi"])
    below.sort(key=lambda lv: lv["distance_from_poi"])

    nearest_above = above[0] if above else None
    nearest_below = below[0] if below else None

    if poi["poi_type"] == "DEMAND":
        sweep_target = nearest_below
    else:
        sweep_target = nearest_above

    return {
        "above": above,
        "below": below,
        "nearest_above": nearest_above,
        "nearest_below": nearest_below,
        "sweep_target": sweep_target,
    }
