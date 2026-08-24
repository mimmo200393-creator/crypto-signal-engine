"""
strategies/tt/liquidity_engine.py
TT — Liquidity + Premium/Discount + 15M Context + 5M Execution +
      Dynamic Target + Signal Orchestration (Fasi 4-9 consolidate)

File unico, come richiesto -- i singoli pezzi sono piccoli, separarli
in tanti file avrebbe aggiunto solo overhead. Direction Engine (4H) e
Location Engine (1H, POI) restano file a se' (piu' grandi, piu'
fondamentali) in direction_engine.py e location_engine.py.

Codice INDIPENDENTE -- nessun import da liquidity_engine.py Edge Lab,
money_flow_map.py, order_block_engine.py o altri moduli condivisi con
TRB/LH. Le uniche dipendenze interne sono direction_engine.py e
location_engine.py (entrambi dentro strategies/tt/, stesso pacchetto).

Sezioni (in ordine, ognuna usa solo quelle sopra):
    1. Costanti e helper condivisi (_manual_atr, _detect_swings)
    2. Liquidity Model (spec sezione 4)
    3. Premium / Discount (spec sezione 5)
    4. 15M Intermediate Context (spec sezione 7)
    5. 5M Execution: Sweep + Reaction + Structure (spec sezioni 13-18)
    6. Dynamic Target (spec sezioni 20-24)
    7. Signal Orchestration: Proximity + Early Signal (spec sezioni 8-12, 26)
"""

from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

from strategies.tt.direction_engine import compute_direction_4h
from strategies.tt.location_engine import select_best_poi, _detect_pois


# ============================================================
# 1. COSTANTI E HELPER CONDIVISI
# ============================================================

SWING_K = 2
EQUAL_LEVEL_TOLERANCE_PCT = 0.0015
MIN_IMPULSE_ATR = 1.0
MIN_BODY_RATIO = 0.5

MOMENTUM_LOOKBACK = 3
DECEL_THRESHOLD = 0.7
ACCEL_THRESHOLD = 1.3

REJECTION_WICK_RATIO = 2.0
REJECTION_CLOSE_ZONE = 1 / 3

LEVEL_SIGNIFICANCE = {
    "SWING_HIGH": 2, "SWING_LOW": 2,
    "EQUAL_HIGH": 3, "EQUAL_LOW": 3,
    "IMPULSE_HIGH": 1, "IMPULSE_LOW": 1,
    "OPPOSING_POI": 4,
}

# Baseline non calibrate -- da validare via backtest (spec sezione 9, 25).
PROXIMITY_POINTS = {"XAU_USD": 12.5, "BTC_USDT": 150.0}
MIN_RR = 1.5
SL_BUFFER_ATR = 0.3


def _manual_atr(df: pd.DataFrame, period: int = 14) -> float:
    """ATR calcolato a mano, nessuna dipendenza da moduli condivisi."""
    if df is None or len(df) < period + 1:
        return 0.0
    h = df["high"].astype(float).values
    l = df["low"].astype(float).values
    c = df["close"].astype(float).values
    return sum(max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1]))
              for i in range(-period, 0)) / period


def _detect_swings(df: pd.DataFrame, k: int = SWING_K) -> list:
    """
    Regola oggettiva condivisa da TUTTE le sezioni di questo file
    (liquidity levels, 15M structure, 5M structure break): swing
    STRETTO (disuguaglianza stretta), k candele per lato.
    """
    if df is None or len(df) < 2 * k + 1:
        return []
    highs = df["high"].astype(float).values
    lows = df["low"].astype(float).values
    timestamps = df["timestamp"].values if "timestamp" in df.columns else list(range(len(df)))

    swings = []
    for i in range(k, len(df) - k):
        if highs[i] > highs[i-k:i].max() and highs[i] > highs[i+1:i+k+1].max():
            ts_val = timestamps[i]
            swings.append({"type": "HIGH", "price": round(float(highs[i]), 5),
                           "timestamp": int(ts_val), "index": i})
        if lows[i] < lows[i-k:i].min() and lows[i] < lows[i+1:i+k+1].min():
            ts_val = timestamps[i]
            swings.append({"type": "LOW", "price": round(float(lows[i]), 5),
                           "timestamp": int(ts_val), "index": i})
    swings.sort(key=lambda s: s["index"])
    return swings


# ============================================================
# 2. LIQUIDITY MODEL (spec sezione 4)
# ============================================================

def _detect_equal_levels(swings: list, tolerance_pct: float = EQUAL_LEVEL_TOLERANCE_PCT) -> list:
    """Equal Highs/Lows: swing dello stesso tipo entro una tolleranza percentuale."""
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
                        "type": kind, "price": round((p1 + p2) / 2, 5),
                        "timestamp": max(group[i]["timestamp"], group[j]["timestamp"]),
                    })
    return equal_levels


def _detect_impulse_levels(df_h1: pd.DataFrame) -> list:
    """
    Punti di lancio di impulsi forti (stesso concetto di LH, codice
    indipendente): IMPULSE_LOW per impulsi rialzisti, IMPULSE_HIGH per
    ribassisti. Livello di liquidity aggiuntivo, non una POI.
    """
    atr = _manual_atr(df_h1)
    if atr <= 0 or len(df_h1) < 10:
        return []
    data = df_h1.reset_index(drop=True)
    levels = []
    for i in range(len(data)):
        row = data.iloc[i]
        o, h, l, c = float(row["open"]), float(row["high"]), float(row["low"]), float(row["close"])
        rng = h - l
        if rng <= 0:
            continue
        body = abs(c - o)
        body_ratio = body / rng
        if body < MIN_IMPULSE_ATR * atr or body_ratio < MIN_BODY_RATIO:
            continue
        ts = int(row.get("timestamp", 0))
        if c > o:
            levels.append({"type": "IMPULSE_LOW", "price": round(l, 5), "timestamp": ts,
                          "displacement_atr": round(body / atr, 3)})
        else:
            levels.append({"type": "IMPULSE_HIGH", "price": round(h, 5), "timestamp": ts,
                          "displacement_atr": round(body / atr, 3)})
    return levels


def build_liquidity_for_poi(df_h1: pd.DataFrame, poi: dict, current_price: float) -> dict:
    """
    Scenario di liquidity per UNA POI specifica: livelli sopra/sotto,
    con distanza, piu' il sweep target (per la conferma di entry in
    Fase 5M -- NON il target di uscita, vedi select_dynamic_target).

    Demand (BUY): sweep target sotto (sell-side liquidity).
    Supply (SELL): sweep target sopra (buy-side liquidity).
    """
    swings = _detect_swings(df_h1)
    equal_levels = _detect_equal_levels(swings)
    impulse_levels = _detect_impulse_levels(df_h1)

    all_levels = []
    for s in swings:
        all_levels.append({"type": "SWING_HIGH" if s["type"] == "HIGH" else "SWING_LOW",
                           "price": s["price"], "timestamp": s["timestamp"]})
    all_levels.extend(equal_levels)
    all_levels.extend(impulse_levels)

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

    sweep_target = nearest_below if poi["poi_type"] == "DEMAND" else nearest_above

    return {
        "above": above, "below": below,
        "nearest_above": nearest_above, "nearest_below": nearest_below,
        "sweep_target": sweep_target,
    }


# ============================================================
# 3. PREMIUM / DISCOUNT (spec sezione 5)
# ============================================================

EQUILIBRIUM_BAND_PCT = 5.0


def compute_premium_discount(price: float, swing_range_low: float,
                             swing_range_high: float) -> dict:
    """
    {"pd_zone": "DISCOUNT"|"EQUILIBRIUM"|"PREMIUM", "pd_pct": float}
    0 = swing_range_low, 100 = swing_range_high. Solo registrato,
    MAI hard gate (spec: "voglio poter verificare statisticamente").
    """
    rng = swing_range_high - swing_range_low
    if rng <= 0:
        return {"pd_zone": None, "pd_pct": None}
    pct = round((price - swing_range_low) / rng * 100.0, 2)
    if abs(pct - 50.0) <= EQUILIBRIUM_BAND_PCT:
        zone = "EQUILIBRIUM"
    elif pct < 50.0:
        zone = "DISCOUNT"
    else:
        zone = "PREMIUM"
    return {"pd_zone": zone, "pd_pct": pct}


def is_preferred_zone(pd_zone: str, direction: str) -> bool:
    """LONG preferisce DISCOUNT, SHORT preferisce PREMIUM -- solo soft factor."""
    if direction == "BUY":
        return pd_zone == "DISCOUNT"
    if direction == "SELL":
        return pd_zone == "PREMIUM"
    return False


# ============================================================
# 4. 15M INTERMEDIATE CONTEXT (spec sezione 7)
# ============================================================

def _m15_structure_direction(df: pd.DataFrame, swings: list) -> str:
    """
    Direzione strutturale (solo la direzione, non il range completo --
    quello e' compito del Direction Engine sul 4H). Stessa regola BOS.
    """
    if not swings or df is None or len(df) == 0:
        return "NEUTRAL"
    closes = df["close"].astype(float).values
    swing_highs = [s for s in swings if s["type"] == "HIGH"]
    swing_lows = [s for s in swings if s["type"] == "LOW"]
    for i in range(len(df) - 1, -1, -1):
        close_i = closes[i]
        prior_highs = [s for s in swing_highs if s["index"] < i]
        if prior_highs and close_i > prior_highs[-1]["price"]:
            return "BULLISH"
        prior_lows = [s for s in swing_lows if s["index"] < i]
        if prior_lows and close_i < prior_lows[-1]["price"]:
            return "BEARISH"
    return "NEUTRAL"


def _compute_momentum(df: pd.DataFrame, lookback: int = MOMENTUM_LOOKBACK) -> str:
    """ACCELERATING/DECELERATING/FLAT: corpi recenti vs precedenti."""
    if df is None or len(df) < lookback * 2:
        return "FLAT"
    bodies = (df["close"].astype(float) - df["open"].astype(float)).abs()
    recent_avg = bodies.iloc[-lookback:].mean()
    prior_avg = bodies.iloc[-lookback*2:-lookback].mean()
    if prior_avg <= 0:
        return "FLAT"
    if recent_avg < prior_avg * DECEL_THRESHOLD:
        return "DECELERATING"
    if recent_avg > prior_avg * ACCEL_THRESHOLD:
        return "ACCELERATING"
    return "FLAT"


def compute_15m_context(df_m15: pd.DataFrame) -> dict:
    """
    {"ctx_15m_structure": ..., "ctx_15m_momentum": ..., "ctx_15m_note": str}
    Solo descrittivo -- NON un entry trigger (spec sezione 7).
    """
    swings = _detect_swings(df_m15)
    structure = _m15_structure_direction(df_m15, swings)
    momentum = _compute_momentum(df_m15)
    note = f"M15 {structure.lower()}, momentum {momentum.lower()}"
    return {"ctx_15m_structure": structure, "ctx_15m_momentum": momentum, "ctx_15m_note": note}


# ============================================================
# 5. 5M EXECUTION: Sweep + Reaction + Structure (spec sezioni 13-18)
# ============================================================

def check_sweep(df_m5: pd.DataFrame, direction: str, sweep_level: float) -> dict:
    """
    Sweep confermato: wick perfora sweep_level, chiusura dalla parte
    giusta (rigetto). BUY: low<level ma close>level. SELL: speculare.
    """
    if df_m5 is None or len(df_m5) == 0:
        return {"confirmed": False}
    for i in range(len(df_m5) - 1, max(len(df_m5) - 10, -1), -1):
        row = df_m5.iloc[i]
        h, l, c = float(row["high"]), float(row["low"]), float(row["close"])
        if direction == "BUY":
            if l < sweep_level and c > sweep_level:
                return {"confirmed": True, "sweep_index": i, "sweep_level_hit": l}
        else:
            if h > sweep_level and c < sweep_level:
                return {"confirmed": True, "sweep_index": i, "sweep_level_hit": h}
    return {"confirmed": False}


def check_reaction(df_m5: pd.DataFrame, direction: str, from_index: int = None) -> dict:
    """
    Candela di rigetto: wick nella direzione del sweep >= 2x il corpo,
    chiusura nel terzo esterno del range. Modello semplice, codificabile.
    """
    if df_m5 is None or len(df_m5) == 0:
        return {"confirmed": False}
    start = from_index if from_index is not None else max(0, len(df_m5) - 10)
    for i in range(start, len(df_m5)):
        row = df_m5.iloc[i]
        o, h, l, c = float(row["open"]), float(row["high"]), float(row["low"]), float(row["close"])
        rng = h - l
        if rng <= 0:
            continue
        body = abs(c - o)
        if direction == "BUY":
            lower_wick = min(o, c) - l
            if body > 0 and lower_wick >= REJECTION_WICK_RATIO * body:
                if (c - l) / rng >= (1 - REJECTION_CLOSE_ZONE):
                    return {"confirmed": True, "reaction_index": i, "reaction_type": "bullish_rejection"}
            elif body == 0 and lower_wick > 0:
                return {"confirmed": True, "reaction_index": i, "reaction_type": "bullish_rejection"}
        else:
            upper_wick = h - max(o, c)
            if body > 0 and upper_wick >= REJECTION_WICK_RATIO * body:
                if (h - c) / rng >= (1 - REJECTION_CLOSE_ZONE):
                    return {"confirmed": True, "reaction_index": i, "reaction_type": "bearish_rejection"}
            elif body == 0 and upper_wick > 0:
                return {"confirmed": True, "reaction_index": i, "reaction_type": "bearish_rejection"}
    return {"confirmed": False}


def check_structure_break(df_m5: pd.DataFrame, direction: str, from_index: int = None) -> dict:
    """Rottura di uno swing M5 significativo (Lower High per BUY, Higher Low per SELL)."""
    if df_m5 is None or len(df_m5) < 2 * SWING_K + 1:
        return {"confirmed": False}
    swings = _detect_swings(df_m5)
    if not swings:
        return {"confirmed": False}
    closes = df_m5["close"].astype(float).values
    start = from_index if from_index is not None else 0
    swing_highs = [s for s in swings if s["type"] == "HIGH"]
    swing_lows = [s for s in swings if s["type"] == "LOW"]
    for i in range(start, len(df_m5)):
        close_i = closes[i]
        if direction == "BUY":
            prior_highs = [s for s in swing_highs if s["index"] < i]
            if prior_highs and close_i > prior_highs[-1]["price"]:
                return {"confirmed": True, "break_index": i, "broken_level": prior_highs[-1]["price"]}
        else:
            prior_lows = [s for s in swing_lows if s["index"] < i]
            if prior_lows and close_i < prior_lows[-1]["price"]:
                return {"confirmed": True, "break_index": i, "broken_level": prior_lows[-1]["price"]}
    return {"confirmed": False}


def evaluate_execution(df_m5: pd.DataFrame, direction: str, sweep_level: float,
                       setup_type: str = "CONSERVATIVE") -> dict:
    """TOUCH (implicito) -> SWEEP -> REACTION -> [STRUCTURE se CONSERVATIVE] -> ENTRY."""
    sweep = check_sweep(df_m5, direction, sweep_level)
    if not sweep["confirmed"]:
        return {"entry_confirmed": False, "sweep": sweep, "reaction": None,
               "structure": None, "rejection": "NO_SWEEP"}

    reaction = check_reaction(df_m5, direction, from_index=sweep.get("sweep_index"))
    if not reaction["confirmed"]:
        return {"entry_confirmed": False, "sweep": sweep, "reaction": reaction,
               "structure": None, "rejection": "NO_REACTION"}

    if setup_type == "AGGRESSIVE":
        return {"entry_confirmed": True, "sweep": sweep, "reaction": reaction,
               "structure": None, "rejection": None}

    structure = check_structure_break(df_m5, direction, from_index=reaction.get("reaction_index"))
    if not structure["confirmed"]:
        return {"entry_confirmed": False, "sweep": sweep, "reaction": reaction,
               "structure": structure, "rejection": "NO_STRUCTURE_CONFIRMATION"}

    return {"entry_confirmed": True, "sweep": sweep, "reaction": reaction,
           "structure": structure, "rejection": None}


# ============================================================
# 6. DYNAMIC TARGET (spec sezioni 20-24)
# ============================================================

def select_dynamic_target(liq_scenario: dict, opposing_poi, entry: float,
                          sl: float, direction: str, atr: float = None):
    """
    Target piu' SIGNIFICATIVO raggiungibile con RR sufficiente, ENTRO
    un tetto massimo di distanza (5x ATR, stessa soglia gia' validata
    su LH) -- oltre quella soglia un target "tecnicamente valido" e'
    comunque irrealistico: ci vorrebbero giorni di movimento
    ininterrotto. Se nessun candidato supera RR entro il tetto:
    fallback a RR fisso 1:2 (stesso principio di LH, bug trovato
    il 24/08: target scelto a distanza eccessiva causava piu'
    invalidazioni PRICE_PASSED_SL_BEFORE_ENTRY -- il prezzo aveva
    piu' tempo di invertirsi prima che l'entry scattasse).

    Direzione del TARGET = direzione del TRADE (sopra per BUY, sotto
    per SELL) -- diverso dallo sweep_target (Fase 5, usato per
    confermare l'entry, che invece e' dalla parte opposta).
    """
    FALLBACK_RR = 2.0
    MAX_DISTANCE_ATR = 5.0

    candidates = []
    relevant = liq_scenario.get("above", []) if direction == "BUY" else liq_scenario.get("below", [])
    for lv in relevant:
        candidates.append({"price": lv["price"], "type": lv["type"],
                          "significance": LEVEL_SIGNIFICANCE.get(lv["type"], 1)})

    if opposing_poi is not None:
        op_price = opposing_poi["zone_low"] if direction == "BUY" else opposing_poi["zone_high"]
        candidates.append({"price": op_price, "type": "OPPOSING_POI",
                          "significance": LEVEL_SIGNIFICANCE["OPPOSING_POI"]})

    risk = abs(entry - sl)
    if risk <= 0:
        return None

    max_distance = MAX_DISTANCE_ATR * atr if atr else float("inf")

    valid = []
    for c in candidates:
        reward = abs(c["price"] - entry)
        if reward > max_distance:
            continue
        rr = round(reward / risk, 3)
        if rr >= MIN_RR:
            c["rr"] = rr
            valid.append(c)

    if valid:
        best = max(valid, key=lambda c: (c["significance"], -c["rr"]))
        return {"price": best["price"], "type": best["type"], "rr": best["rr"]}

    if not candidates:
        return None

    # Nessun candidato entro il tetto supera il RR minimo: fallback
    # a rapporto fisso 1:2, un target vicino e raggiungibile.
    if atr:
        fallback_price = entry + FALLBACK_RR * risk if direction == "BUY" else entry - FALLBACK_RR * risk
        return {"price": round(fallback_price, 5), "type": "FALLBACK_FIXED_RR", "rr": FALLBACK_RR}

    return None


# ============================================================
# 7. SIGNAL ORCHESTRATION: Proximity + Early Signal (spec sezioni 8-12, 26)
# ============================================================

def _compute_signal_quality(poi: dict, pd_info: dict, ctx_15m: dict,
                            trade_side: str, planned_rr: float,
                            reaction_map_score: float = None,
                            regime: str = None) -> tuple:
    """
    Quality Score del SEGNALE (spec sezione 29) -- diverso da poi["quality_score"]
    (quello e' solo la qualita' della location, calcolato nel Location
    Engine). Questo combina i pochi elementi che la spec elenca:
    qualita' POI, Premium/Discount allineato, 15M coerente, RR.

    Aggiunti il 24/08, come SOFT factor (mai hard gate, coerente col
    resto della funzione):
      - Reaction Map: l'unico engine con edge positivo confermato su
        piu' strategie indipendenti (V41P1 +4.8%, TRB +4.1%, verificato
        su centinaia di trade). TT era isolato da questo dato.
      - Regime TRANSITIONAL: confermato tossico su due strategie con
        centinaia di trade (TRB 26.5% WR vs 40%+ negli altri regimi,
        V41P1 24.7% vs 35-38%). Penalita, non blocco.

    Pochi elementi forti, non 20 filtri (spec esplicita). Solo SOFT
    factor -- non blocca mai (il blocco vero e' gia' avvenuto sopra,
    su RR insufficiente/nessun target/nessuna POI).
    """
    score = 3  # base: il setup e' arrivato fin qui, tutti i gate hard sono gia' passati

    score += round(poi.get("quality_score", 0) / 10 * 3)  # fino a +3, scalato da poi_quality (0-10)

    if is_preferred_zone(pd_info.get("pd_zone"), trade_side):
        score += 2  # Premium/Discount allineato (spec sezione 5)

    if ctx_15m.get("ctx_15m_momentum") == "DECELERATING":
        score += 2  # il prezzo rallenta avvicinandosi -- buon segno (spec sezione 7)

    if planned_rr >= 2.0:
        score += 2

    if reaction_map_score is not None and reaction_map_score >= 5:
        score += 1  # bonus contenuto: edge reale ma modesto (+4-5%), non uno dei fattori forti

    if regime == "TRANSITIONAL":
        score -= 2  # penalita, non blocco -- il setup puo' ancora passare con score alto altrove

    score = max(0, min(score, 12))
    label = "HIGH" if score >= 8 else ("MEDIUM" if score >= 5 else "LOW")
    return score, label


def _direction_to_trade_side(direction_4h: str):
    if direction_4h == "BULLISH":
        return "BUY"
    if direction_4h == "BEARISH":
        return "SELL"
    return None


def evaluate_setup(asset: str, df_h4, df_h1, df_m15, current_price: float,
                   reaction_map_score: float = None, regime: str = None) -> dict:
    """
    Entry point principale. Sequenza (spec sezione 33): Direction ->
    Location -> Liquidity -> Premium/Discount -> 15M Context ->
    Proximity -> Early Signal. Ogni gate ferma la catena con un motivo
    esplicito.
    """
    diag = {"asset": asset}

    def reject(reason):
        diag["rejection"] = reason
        return {"signal": None, "diagnostics": diag}

    dir_ctx = compute_direction_4h(df_h4)
    diag["direction_4h"] = dir_ctx["direction"]
    if dir_ctx["direction"] == "NEUTRAL":
        return reject("DIRECTION_NOT_CLEAR")

    trade_side = _direction_to_trade_side(dir_ctx["direction"])

    poi = select_best_poi(df_h1, asset, dir_ctx["direction"], current_price)
    if poi is None:
        return reject("NO_VALID_POI")
    diag["poi"] = poi

    liq = build_liquidity_for_poi(df_h1, poi, current_price)
    if liq["sweep_target"] is None:
        return reject("NO_LIQUIDITY_TARGET")
    diag["liquidity"] = liq

    poi_mid = (poi["zone_high"] + poi["zone_low"]) / 2
    pd_info = compute_premium_discount(poi_mid, dir_ctx["swing_range_low"], dir_ctx["swing_range_high"])
    diag["premium_discount"] = pd_info

    ctx_15m = compute_15m_context(df_m15)
    diag["context_15m"] = ctx_15m

    proximity_threshold = PROXIMITY_POINTS.get(asset, 15.0)
    if trade_side == "BUY":
        edge = poi["zone_high"]
        distance = current_price - edge
    else:
        edge = poi["zone_low"]
        distance = edge - current_price
    diag["proximity_points"] = round(distance, 4)

    if distance < 0:
        return reject("PRICE_ALREADY_PAST_EDGE")
    if distance > proximity_threshold:
        return reject(f"TOO_FAR ({distance:.2f} > {proximity_threshold})")

    atr_h1 = _manual_atr(df_h1)
    sl_buffer = SL_BUFFER_ATR * atr_h1 if atr_h1 > 0 else 0

    if trade_side == "BUY":
        planned_entry = poi["zone_high"]
        planned_sl = poi["zone_low"] - sl_buffer
    else:
        planned_entry = poi["zone_low"]
        planned_sl = poi["zone_high"] + sl_buffer

    risk = abs(planned_entry - planned_sl)
    if risk <= 0:
        return reject("SL_NOT_CALCULABLE")

    opposing_type = "SUPPLY" if trade_side == "BUY" else "DEMAND"
    all_pois = _detect_pois(df_h1, asset)
    opposing_candidates = [p for p in all_pois if p["poi_type"] == opposing_type]
    opposing_poi = None
    if opposing_candidates:
        if trade_side == "BUY":
            valid_opposing = [p for p in opposing_candidates if p["zone_low"] > planned_entry]
            if valid_opposing:
                opposing_poi = min(valid_opposing, key=lambda p: p["zone_low"])
        else:
            valid_opposing = [p for p in opposing_candidates if p["zone_high"] < planned_entry]
            if valid_opposing:
                opposing_poi = max(valid_opposing, key=lambda p: p["zone_high"])

    target = select_dynamic_target(liq, opposing_poi, planned_entry, planned_sl, trade_side, atr=atr_h1)
    if target is None:
        return reject("NO_DYNAMIC_TARGET_AVAILABLE")

    planned_tp = target["price"]
    planned_rr = target["rr"]
    if planned_rr < MIN_RR:
        return reject(f"RR_INSUFFICIENT ({planned_rr:.2f} < {MIN_RR})")

    quality_score, quality_label = _compute_signal_quality(
        poi, pd_info, ctx_15m, trade_side, planned_rr,
        reaction_map_score=reaction_map_score, regime=regime)

    now = datetime.now(timezone.utc)
    signal = {
        "asset": asset, "direction": trade_side, "direction_4h": dir_ctx["direction"],
        "swing_range_low": dir_ctx["swing_range_low"], "swing_range_high": dir_ctx["swing_range_high"],
        "last_bos_price": dir_ctx["last_bos_price"], "last_bos_ts": dir_ctx["last_bos_ts"],

        "poi_type": poi["poi_type"], "poi_high": poi["zone_high"], "poi_low": poi["zone_low"],
        "poi_quality": poi["quality_score"], "poi_ref": poi["poi_ref"],

        "liquidity_type": liq["sweep_target"]["type"], "liquidity_level": liq["sweep_target"]["price"],
        "liquidity_direction": "below" if trade_side == "BUY" else "above",
        "liquidity_distance_pct": None, "sweep_target_level": liq["sweep_target"]["price"],

        "pd_zone": pd_info["pd_zone"], "pd_pct": pd_info["pd_pct"],

        "ctx_15m_structure": ctx_15m["ctx_15m_structure"], "ctx_15m_momentum": ctx_15m["ctx_15m_momentum"],
        "ctx_15m_note": ctx_15m["ctx_15m_note"],

        "proximity_points": round(distance, 4), "signal_created_at": now.isoformat(),

        "planned_entry": round(planned_entry, 5), "planned_sl": round(planned_sl, 5),
        "planned_tp": round(planned_tp, 5), "planned_rr": planned_rr,
        "planned_tp_type": target["type"], "planned_tp_ref": f"{target['type']}@{target['price']}",

        "quality_score": quality_score, "quality_label": quality_label,
        "setup_type": "CONSERVATIVE",

        "context_snapshot": {"direction": dir_ctx, "poi": poi, "liquidity": liq,
                             "premium_discount": pd_info, "context_15m": ctx_15m},
    }

    diag["status"] = "EARLY_SIGNAL_CREATED"
    return {"signal": signal, "diagnostics": diag}
