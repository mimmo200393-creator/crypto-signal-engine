"""
strategies/money_flow_map.py
Money Flow Map — V4.1 Phase 1

Costruisce una mappa classificata dei livelli di liquidita' rilevanti
per PAXG_USDT e BTC_USDT, assegnando a ciascun livello un Priority Score
basato su tre fattori:

    Priority Score = peso_tipo * W_TIPO + (1 - dist_norm) * W_DIST + conf_norm * W_CONF

    W_TIPO = 0.45  importanza strutturale del livello
    W_DIST = 0.45  vicinanza al prezzo attuale (piu' vicino = piu' alto)
    W_CONF = 0.10  conferme storiche (reazioni negli ultimi 30 giorni D1)

Classificazione output:
    CRITICAL >= 0.85
    HIGH     >= 0.70
    MEDIUM   >= 0.40
    LOW      <  0.40

Livelli monitorati:
    Weekly High/Low         (peso tipo 1.0)
    Daily High/Low          (peso tipo 0.8)
    Daily High/Low (prev)   (peso tipo 0.6)
    Equal Highs/Lows        (peso tipo 0.7)
    H4 Swing High/Low       (peso tipo 0.5)

Obiettivo Phase 1: capire dove si concentra la liquidita', quali livelli
sono piu' vicini e rilevanti, quale livello rappresenta il target piu'
probabile per il mercato. I pesi potranno essere ottimizzati sui dati
raccolti dopo 100-200 segnali reali.
"""

from typing import Optional
import pandas as pd

from strategies.institutional_scanner_v3 import (
    find_pivots,
    H4_PIVOT_LOOKBACK,
)

# ============================================================
# Pesi del Priority Score
# ============================================================
W_TIPO = 0.45
W_DIST = 0.45
W_CONF = 0.10

# Distanza massima oltre cui il fattore distanza vale 0
MAX_DISTANCE_PCT = 0.05   # 5%

# Numero massimo di conferme storiche per la normalizzazione
MAX_CONFIRMATIONS = 5

# Finestra per contare le reazioni storiche (candele D1)
CONFIRMATION_LOOKBACK_DAYS = 30

# Tolleranza per considerare che il prezzo ha "toccato" un livello (D1)
TOUCH_TOLERANCE_PCT = 0.003   # 0.3%

# Tolleranza per considerare due pivot H4 "equal" (Equal Highs/Lows)
EQUAL_LEVEL_TOLERANCE_PCT = 0.001

# ============================================================
# Pesi per tipo di livello
# ============================================================
LEVEL_TYPE_WEIGHTS = {
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
}

# ============================================================
# Soglie Priority Score
# ============================================================
PRIORITY_CRITICAL = 0.85
PRIORITY_HIGH     = 0.70
PRIORITY_MEDIUM   = 0.40


def _classify_priority(score: float) -> str:
    if score >= PRIORITY_CRITICAL:
        return "CRITICAL"
    if score >= PRIORITY_HIGH:
        return "HIGH"
    if score >= PRIORITY_MEDIUM:
        return "MEDIUM"
    return "LOW"


def _count_historical_touches(df_d1: pd.DataFrame, level_price: float) -> int:
    """
    Conta quante candele D1 negli ultimi CONFIRMATION_LOOKBACK_DAYS
    hanno toccato il livello (high o low entro TOUCH_TOLERANCE_PCT).
    Ritorna un intero tra 0 e MAX_CONFIRMATIONS (capped).
    """
    if len(df_d1) == 0 or level_price == 0:
        return 0

    recent = df_d1.iloc[-CONFIRMATION_LOOKBACK_DAYS:]
    tol = level_price * TOUCH_TOLERANCE_PCT
    count = 0
    for _, row in recent.iterrows():
        high = float(row["high"])
        low = float(row["low"])
        if (level_price - tol) <= high and low <= (level_price + tol):
            count += 1

    return min(count, MAX_CONFIRMATIONS)


def _priority_score(level_label: str, level_price: float,
                    current_price: float, df_d1: pd.DataFrame) -> float:
    """
    Calcola il Priority Score per un singolo livello.
    """
    # Componente tipo
    tipo = LEVEL_TYPE_WEIGHTS.get(level_label, 0.5)

    # Componente distanza (normalizzata su MAX_DISTANCE_PCT)
    if current_price == 0:
        dist_norm = 0.0
    else:
        dist_pct = abs(level_price - current_price) / current_price
        dist_norm = max(0.0, 1.0 - dist_pct / MAX_DISTANCE_PCT)

    # Componente conferme storiche
    touches = _count_historical_touches(df_d1, level_price)
    conf_norm = touches / MAX_CONFIRMATIONS

    score = tipo * W_TIPO + dist_norm * W_DIST + conf_norm * W_CONF
    return round(min(score, 1.0), 4)


def build_money_flow_map(df_h4: pd.DataFrame, df_d1: pd.DataFrame,
                         current_price: float) -> dict:
    """
    Costruisce la Money Flow Map completa con Priority Score per ogni livello.

    Ritorna:
    {
        "levels": [...],
        "nearest_above": dict | None,
        "nearest_below": dict | None,
        "top_targets_above": list,
        "top_targets_below": list,
        "current_price": float,
    }
    """
    raw_levels = []

    # Weekly High/Low (ultimi 7 giorni D1)
    if len(df_d1) >= 1:
        weekly = df_d1.iloc[-7:]
        raw_levels.append(("Weekly High", float(weekly["high"].max()), "high"))
        raw_levels.append(("Weekly Low",  float(weekly["low"].min()),  "low"))

        last_d1 = df_d1.iloc[-1]
        raw_levels.append(("Daily High", float(last_d1["high"]), "high"))
        raw_levels.append(("Daily Low",  float(last_d1["low"]),  "low"))

        if len(df_d1) >= 2:
            prev_d1 = df_d1.iloc[-2]
            raw_levels.append(("Daily High (prev)", float(prev_d1["high"]), "high"))
            raw_levels.append(("Daily Low (prev)",  float(prev_d1["low"]),  "low"))

    # H4 Swing High/Low + Equal Highs/Lows
    if len(df_h4) >= H4_PIVOT_LOOKBACK * 2 + 3:
        pivots = find_pivots(df_h4, H4_PIVOT_LOOKBACK)
        pivot_highs = sorted(pivots["pivot_highs"], key=lambda p: p[2])
        pivot_lows  = sorted(pivots["pivot_lows"],  key=lambda p: p[2])

        if pivot_highs:
            raw_levels.append(("H4 Swing High", pivot_highs[-1][1], "high"))
        if pivot_lows:
            raw_levels.append(("H4 Swing Low",  pivot_lows[-1][1],  "low"))

        for i in range(len(pivot_highs)):
            for j in range(i + 1, len(pivot_highs)):
                p1, p2 = pivot_highs[i][1], pivot_highs[j][1]
                if p1 != 0 and abs(p1 - p2) / p1 <= EQUAL_LEVEL_TOLERANCE_PCT:
                    raw_levels.append(("Equal Highs", (p1 + p2) / 2, "high"))

        for i in range(len(pivot_lows)):
            for j in range(i + 1, len(pivot_lows)):
                p1, p2 = pivot_lows[i][1], pivot_lows[j][1]
                if p1 != 0 and abs(p1 - p2) / p1 <= EQUAL_LEVEL_TOLERANCE_PCT:
                    raw_levels.append(("Equal Lows", (p1 + p2) / 2, "low"))

    # Calcola Priority Score per ogni livello
    levels = []
    seen_prices = set()

    for label, price, kind in raw_levels:
        if price == 0:
            continue
        price_key = round(price / (current_price * 0.0005)) if current_price else round(price, 1)
        if price_key in seen_prices:
            continue
        seen_prices.add(price_key)

        dist_pct = abs(price - current_price) / current_price if current_price else 0
        touches = _count_historical_touches(df_d1, price)
        score = _priority_score(label, price, current_price, df_d1)
        priority = _classify_priority(score)
        type_weight = LEVEL_TYPE_WEIGHTS.get(label, 0.5)

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

    levels.sort(key=lambda lv: lv["priority_score"], reverse=True)

    above = [lv for lv in levels if lv["kind"] == "high" and lv["price"] > current_price]
    nearest_above = min(above, key=lambda lv: lv["distance_pct"]) if above else None

    below = [lv for lv in levels if lv["kind"] == "low" and lv["price"] < current_price]
    nearest_below = min(below, key=lambda lv: lv["distance_pct"]) if below else None

    top_targets_above = sorted(
        [lv for lv in levels if lv["kind"] == "high" and lv["price"] > current_price],
        key=lambda lv: lv["priority_score"], reverse=True
    )[:3]

    top_targets_below = sorted(
        [lv for lv in levels if lv["kind"] == "low" and lv["price"] < current_price],
        key=lambda lv: lv["priority_score"], reverse=True
    )[:3]

    return {
        "levels":            levels,
        "nearest_above":     nearest_above,
        "nearest_below":     nearest_below,
        "top_targets_above": top_targets_above,
        "top_targets_below": top_targets_below,
        "current_price":     current_price,
    }


def format_money_flow_map_summary(mfm: dict, asset: str) -> str:
    """
    Formatta un riepilogo testuale della Money Flow Map per il logging.
    """
    price = mfm["current_price"]
    above = mfm["nearest_above"]
    below = mfm["nearest_below"]

    lines = [f"Money Flow Map [{asset}] @ {price:,.4f}"]

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

    if mfm["top_targets_above"]:
        lines.append("  Top Targets Above:")
        for i, lv in enumerate(mfm["top_targets_above"], 1):
            lines.append(
                f"    {i}. {lv['label']} @ {lv['price']:,.4f} "
                f"[{lv['priority_label']} {lv['priority_score']:.2f}] "
                f"touches={lv['historical_touches']}"
            )

    if mfm["top_targets_below"]:
        lines.append("  Top Targets Below:")
        for i, lv in enumerate(mfm["top_targets_below"], 1):
            lines.append(
                f"    {i}. {lv['label']} @ {lv['price']:,.4f} "
                f"[{lv['priority_label']} {lv['priority_score']:.2f}] "
                f"touches={lv['historical_touches']}"
            )

    return "\n".join(lines)
