"""
strategies/edge_lab/liquidity_engine.py
Edge Lab — Step 4: Liquidity Engine

Estende la Liquidity Map legacy (Weekly/Daily/Equal/H4 Swing)
aggiungendo i livelli di sessione intradayday (Asia/London/NY High-Low)
calcolati dal Session Engine (Step 3).

SEPARAZIONE NETTA:
    - money_flow_map.py  → legacy V4.1 / V4.1 Phase 1 (invariato)
    - liquidity_engine.py → Edge Lab (questo file)

Riutilizzo esplicito da money_flow_map.py:
    - _classify_priority()   → stessa logica CRITICAL/HIGH/MEDIUM/LOW
    - _count_historical_touches() → conferme storiche su D1
    - _priority_score()      → calcolo Priority Score

I livelli di sessione vengono aggiunti con pesi propri (vedi
LEVEL_TYPE_WEIGHTS_EL) e integrati nella stessa struttura dati
output per garantire compatibilità con i layer superiori
(Market Context Engine, Fibonacci Engine, OTE-SC).

Output:
    {
        "levels":            list[dict],   # tutti i livelli con Priority Score
        "nearest_above":     dict | None,
        "nearest_below":     dict | None,
        "top_targets_above": list[dict],
        "top_targets_below": list[dict],
        "current_price":     float,
        "session_levels":    dict,         # High/Low per sessione (raw)
    }
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import pandas as pd

# Riuso dalla Money Flow Map legacy (solo funzioni pure, nessun side effect)
from strategies.money_flow_map import (
    _classify_priority,
    _count_historical_touches,
    W_TIPO,
    W_DIST,
    W_CONF,
    MAX_DISTANCE_PCT,
    MAX_CONFIRMATIONS,
    TOUCH_TOLERANCE_PCT,
    EQUAL_LEVEL_TOLERANCE_PCT,
    PRIORITY_CRITICAL,
    PRIORITY_HIGH,
    PRIORITY_MEDIUM,
)

# Session Engine (Step 3)
from strategies.edge_lab.session_engine import (
    build_session_context,
    get_current_session,
)

# Pivot legacy (riuso puro)
from strategies.institutional_scanner_v3 import (
    find_pivots,
    H4_PIVOT_LOOKBACK,
)

logger = logging.getLogger("edge_lab.liquidity_engine")

# ============================================================
# Pesi per tipo di livello — Edge Lab
# Separati dai pesi legacy per poter ottimizzare indipendentemente
# ============================================================
LEVEL_TYPE_WEIGHTS_EL: dict[str, float] = {
    # Livelli strutturali (ereditati da money_flow_map)
    "Weekly High":       1.0,
    "Weekly Low":        1.0,
    "Daily High":        0.8,
    "Daily Low":         0.8,
    "Daily High (prev)": 0.6,
    "Daily Low (prev)":  0.6,
    "Equal Highs":       0.7,
    "Equal Lows":        0.7,
    "H4 Swing High":     0.5,
    "H4 Swing Low":      0.5,
    # Livelli di sessione intraday (nuovi in Edge Lab)
    "Asia High":         0.75,
    "Asia Low":          0.75,
    "London High":       0.80,
    "London Low":        0.80,
    "NY High":           0.70,
    "NY Low":            0.70,
    "Overlap High":      0.65,
    "Overlap Low":       0.65,
}

WEEKLY_LOOKBACK_DAYS = 7


# ============================================================
# Priority Score (versione Edge Lab — stessa formula, pesi EL)
# ============================================================

def _el_priority_score(
    level_label: str,
    level_price: float,
    current_price: float,
    df_d1: pd.DataFrame,
) -> float:
    """Priority Score usando i pesi Edge Lab e le funzioni pure legacy."""
    tipo = LEVEL_TYPE_WEIGHTS_EL.get(level_label, 0.5)

    if current_price == 0:
        dist_norm = 0.0
    else:
        dist_pct = abs(level_price - current_price) / current_price
        dist_norm = max(0.0, 1.0 - dist_pct / MAX_DISTANCE_PCT)

    touches = _count_historical_touches(df_d1, level_price)
    conf_norm = touches / MAX_CONFIRMATIONS

    score = tipo * W_TIPO + dist_norm * W_DIST + conf_norm * W_CONF
    return round(min(score, 1.0), 4)


# ============================================================
# Raccolta livelli strutturali (Weekly/Daily/Equal/H4)
# Identica a money_flow_map ma restituisce lista grezza senza score
# ============================================================

def _collect_structural_levels(
    df_h4: pd.DataFrame,
    df_d1: pd.DataFrame,
) -> list[tuple[str, float, str]]:
    """
    Ritorna lista di (label, price, kind) per i livelli strutturali.
    kind: "high" | "low"
    """
    raw: list[tuple[str, float, str]] = []

    if len(df_d1) >= 1:
        weekly = df_d1.iloc[-WEEKLY_LOOKBACK_DAYS:]
        raw.append(("Weekly High", float(weekly["high"].max()), "high"))
        raw.append(("Weekly Low",  float(weekly["low"].min()),  "low"))

        last_d1 = df_d1.iloc[-1]
        raw.append(("Daily High", float(last_d1["high"]), "high"))
        raw.append(("Daily Low",  float(last_d1["low"]),  "low"))

        if len(df_d1) >= 2:
            prev_d1 = df_d1.iloc[-2]
            raw.append(("Daily High (prev)", float(prev_d1["high"]), "high"))
            raw.append(("Daily Low (prev)",  float(prev_d1["low"]),  "low"))

    if len(df_h4) >= H4_PIVOT_LOOKBACK * 2 + 3:
        pivots = find_pivots(df_h4, H4_PIVOT_LOOKBACK)
        pivot_highs = sorted(pivots["pivot_highs"], key=lambda p: p[2])
        pivot_lows  = sorted(pivots["pivot_lows"],  key=lambda p: p[2])

        if pivot_highs:
            raw.append(("H4 Swing High", pivot_highs[-1][1], "high"))
        if pivot_lows:
            raw.append(("H4 Swing Low",  pivot_lows[-1][1],  "low"))

        for i in range(len(pivot_highs)):
            for j in range(i + 1, len(pivot_highs)):
                p1, p2 = pivot_highs[i][1], pivot_highs[j][1]
                if p1 != 0 and abs(p1 - p2) / p1 <= EQUAL_LEVEL_TOLERANCE_PCT:
                    raw.append(("Equal Highs", (p1 + p2) / 2, "high"))

        for i in range(len(pivot_lows)):
            for j in range(i + 1, len(pivot_lows)):
                p1, p2 = pivot_lows[i][1], pivot_lows[j][1]
                if p1 != 0 and abs(p1 - p2) / p1 <= EQUAL_LEVEL_TOLERANCE_PCT:
                    raw.append(("Equal Lows", (p1 + p2) / 2, "low"))

    return raw


# ============================================================
# Raccolta livelli di sessione (nuovi in Edge Lab)
# ============================================================

def _collect_session_levels(
    session_ctx: dict,
) -> list[tuple[str, float, str]]:
    """
    Estrae i livelli High/Low dalle sessioni calcolate dal Session Engine.
    Ritorna lista di (label, price, kind).
    """
    raw: list[tuple[str, float, str]] = []

    label_map = {
        "ASIA":               ("Asia High",    "Asia Low"),
        "LONDON":             ("London High",  "London Low"),
        "OVERLAP":            ("Overlap High", "Overlap Low"),
        "NEW_YORK":           ("NY High",      "NY Low"),
        "EUROPEAN_COMPOSITE": ("London High",  "London Low"),  # composite → London label
    }

    def _add_levels(levels_dict: dict | None, session_key: str):
        if levels_dict is None:
            return
        sess = levels_dict.get("session", session_key)
        hi_label, lo_label = label_map.get(sess, (f"{sess} High", f"{sess} Low"))
        h = levels_dict.get("high")
        l = levels_dict.get("low")
        if h and h > 0:
            raw.append((hi_label, float(h), "high"))
        if l and l > 0:
            raw.append((lo_label, float(l), "low"))

        # Per EUROPEAN_COMPOSITE aggiungi anche Overlap separato
        if sess == "EUROPEAN_COMPOSITE":
            overlap = levels_dict.get("overlap")
            if overlap:
                h2 = overlap.get("high")
                l2 = overlap.get("low")
                if h2 and h2 > 0:
                    raw.append(("Overlap High", float(h2), "high"))
                if l2 and l2 > 0:
                    raw.append(("Overlap Low",  float(l2), "low"))

    _add_levels(session_ctx.get("current_levels"),   session_ctx.get("current_session", ""))
    _add_levels(session_ctx.get("reference_levels"),  session_ctx.get("reference_session", ""))

    return raw


# ============================================================
# Build Liquidity Map — Edge Lab
# ============================================================

def build_el_liquidity_map(
    df_h4: pd.DataFrame,
    df_d1: pd.DataFrame,
    df_m15: pd.DataFrame,
    current_price: float,
    now: datetime,
) -> dict:
    """
    Costruisce la Liquidity Map Edge Lab combinando:
        - livelli strutturali (Weekly/Daily/Equal/H4 Swing) da money_flow_map
        - livelli di sessione intraday (Asia/London/NY) dal Session Engine

    Args:
        df_h4:         candele H4 (da candles_cache)
        df_d1:         candele D1 (da v3_candles_cache)
        df_m15:        candele M15 (da v3_candles_cache)
        current_price: prezzo corrente (close ultima candela M15)
        now:           datetime UTC corrente

    Returns:
        dict compatibile con il formato Money Flow Map legacy, più
        il campo "session_levels" con i livelli raw per sessione.
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    # Step 3: calcola sessione corrente e livelli di sessione
    session_ctx = build_session_context(df_m15, now)

    # Raccolta livelli
    structural = _collect_structural_levels(df_h4, df_d1)
    session_raw = _collect_session_levels(session_ctx)
    all_raw = structural + session_raw

    # Deduplica per prezzo (stessa logica money_flow_map)
    levels: list[dict] = []
    seen_prices: set = set()

    for label, price, kind in all_raw:
        if price == 0:
            continue
        # Bucket di prezzo: evita duplicati per livelli quasi identici
        bucket = round(price / (current_price * 0.0005)) if current_price else round(price, 1)
        if bucket in seen_prices:
            continue
        seen_prices.add(bucket)

        dist_pct = abs(price - current_price) / current_price if current_price else 0
        touches  = _count_historical_touches(df_d1, price)
        score    = _el_priority_score(label, price, current_price, df_d1)
        priority = _classify_priority(score)
        type_weight = LEVEL_TYPE_WEIGHTS_EL.get(label, 0.5)

        levels.append({
            "label":              label,
            "price":              price,
            "kind":               kind,
            "priority_score":     score,
            "priority_label":     priority,
            "distance_pct":       round(dist_pct, 6),
            "historical_touches": touches,
            "type_weight":        type_weight,
        })

    # Ordina per Priority Score decrescente
    levels.sort(key=lambda lv: lv["priority_score"], reverse=True)

    # Nearest above / below
    above = [lv for lv in levels if lv["kind"] == "high" and lv["price"] > current_price]
    below = [lv for lv in levels if lv["kind"] == "low"  and lv["price"] < current_price]

    nearest_above = min(above, key=lambda lv: lv["distance_pct"]) if above else None
    nearest_below = min(below, key=lambda lv: lv["distance_pct"]) if below else None

    top_targets_above = sorted(above, key=lambda lv: lv["priority_score"], reverse=True)[:3]
    top_targets_below = sorted(below, key=lambda lv: lv["priority_score"], reverse=True)[:3]

    logger.info(
        "EL Liquidity Map [%.4f]: %d livelli | above=%s (%.4f) | below=%s (%.4f) | "
        "session=%s ref=%s",
        current_price,
        len(levels),
        nearest_above["label"] if nearest_above else "N/A",
        nearest_above["price"] if nearest_above else 0,
        nearest_below["label"] if nearest_below else "N/A",
        nearest_below["price"] if nearest_below else 0,
        session_ctx["current_session"],
        session_ctx["reference_session"],
    )

    return {
        "levels":            levels,
        "nearest_above":     nearest_above,
        "nearest_below":     nearest_below,
        "top_targets_above": top_targets_above,
        "top_targets_below": top_targets_below,
        "current_price":     current_price,
        "session_levels":    session_ctx,   # raw session context per layer superiori
    }


# ============================================================
# Query helpers (usati da OTE-SC, Step 9)
# ============================================================

def find_nearest_liquidity_target(
    liq_map: dict,
    entry: float,
    direction: str,
) -> dict | None:
    """
    Trova il target di liquidità più vicino nella direzione del trade,
    privilegiando il Priority Score più alto tra i candidati.

    direction: "BUY" → cerca livelli "high" sopra l'entry
               "SELL" → cerca livelli "low" sotto l'entry
    """
    kind = "high" if direction == "BUY" else "low"
    if direction == "BUY":
        candidates = [
            lv for lv in liq_map["levels"]
            if lv["kind"] == kind and lv["price"] > entry
        ]
    else:
        candidates = [
            lv for lv in liq_map["levels"]
            if lv["kind"] == kind and lv["price"] < entry
        ]

    if not candidates:
        return None

    # Tra i candidati validi, prendi quello col Priority Score più alto
    # (spec OTE-SC: "nearest liquidity" → il più vicino con score massimo)
    return max(candidates, key=lambda lv: lv["priority_score"])


def find_session_sl_extreme(
    liq_map: dict,
    direction: str,
) -> float | None:
    """
    Estremo della sessione di riferimento per lo Stop Loss OTE-SC.

    BUY  → Low della sessione di riferimento
    SELL → High della sessione di riferimento

    Ritorna il prezzo oppure None se non disponibile.
    """
    session_ctx = liq_map.get("session_levels")
    if session_ctx is None:
        return None

    ref_levels = session_ctx.get("reference_levels")
    if ref_levels is None:
        return None

    if direction == "BUY":
        return ref_levels.get("low")
    else:
        return ref_levels.get("high")


# ============================================================
# Format summary (per logging, compatibile con money_flow_map)
# ============================================================

def format_el_liquidity_map_summary(liq_map: dict, asset: str) -> str:
    price = liq_map["current_price"]
    above = liq_map["nearest_above"]
    below = liq_map["nearest_below"]
    sess  = liq_map["session_levels"]

    lines = [
        f"EL Liquidity Map [{asset}] @ {price:,.4f} "
        f"| session={sess['current_session']} ref={sess['reference_session']}"
    ]

    if above:
        lines.append(
            f"  Nearest Above: {above['label']} @ {above['price']:,.4f} "
            f"(+{above['distance_pct']*100:.2f}%) "
            f"[{above['priority_label']} {above['priority_score']:.2f}]"
        )
    else:
        lines.append("  Nearest Above: N/A")

    if below:
        lines.append(
            f"  Nearest Below: {below['label']} @ {below['price']:,.4f} "
            f"(-{below['distance_pct']*100:.2f}%) "
            f"[{below['priority_label']} {below['priority_score']:.2f}]"
        )
    else:
        lines.append("  Nearest Below: N/A")

    ref = sess.get("reference_levels")
    if ref:
        lines.append(
            f"  Ref Session ({sess['reference_session']}): "
            f"H={ref['high']:,.4f} L={ref['low']:,.4f} "
            f"Range={ref['range']:,.4f} Mid={ref['midpoint']:,.4f}"
        )

    if liq_map["top_targets_above"]:
        lines.append("  Top Targets Above:")
        for i, lv in enumerate(liq_map["top_targets_above"], 1):
            lines.append(
                f"    {i}. {lv['label']} @ {lv['price']:,.4f} "
                f"[{lv['priority_label']} {lv['priority_score']:.2f}] "
                f"touches={lv['historical_touches']}"
            )

    if liq_map["top_targets_below"]:
        lines.append("  Top Targets Below:")
        for i, lv in enumerate(liq_map["top_targets_below"], 1):
            lines.append(
                f"    {i}. {lv['label']} @ {lv['price']:,.4f} "
                f"[{lv['priority_label']} {lv['priority_score']:.2f}] "
                f"touches={lv['historical_touches']}"
            )

    return "\n".join(lines)
