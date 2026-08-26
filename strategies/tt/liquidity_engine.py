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

from strategies.tt.direction_engine import compute_direction_4h, _detect_swings as _detect_swings_h4
from strategies.tt.location_engine import select_location


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
    "PREVIOUS_STRUCTURE": 5,  # last_bos_price -- "Previous HH/LL", la
                              # referenza esplicita del documento 25/08
                              # per il TP, priorita' massima
}

# Baseline non calibrate -- da validare via backtest (spec sezione 9, 25).
PROXIMITY_POINTS = {"XAU_USD": 12.5, "BTC_USDT": 150.0}
MIN_RR = 1.5
SL_BUFFER_ATR = 1.0  # ricalibrato il 25/08: la location e' un PUNTO (lo
                      # swing HL/LH), senza ampiezza propria -- 0.3x dava
                      # SEMPRE e SOLO 0.3x ATR di rischio, senza eccezione
                      # (verificato su 11/11 trade reali), gonfiando il RR
                      # e l'expectancy in modo irrealistico (+3.39R con
                      # 0.3x, +0.76R -- piu credibile -- con 1.0x, stesso
                      # win rate). Stesso principio del fix OTE (0.2x->0.5x).


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


def evaluate_execution_v2(df_m5: pd.DataFrame, direction: str) -> dict:
    """
    Confirmation per il nuovo setup STRUCTURE_PULLBACK (documento
    25/08): "buyers/sellers regain control" = rottura di uno swing
    M5 nella direzione del trade. Stessa funzione check_structure_break
    gia' validata (usata anche dal vecchio setup CONSERVATIVE) --
    riuso puro, nessuna modifica.

    A differenza del vecchio flusso, qui NON serve sweep+reazione
    prima: il Touch sulla location (HL/LH) e' gia' di per se' il
    punto di osservazione -- la Confirmation e' l'unico trigger.
    """
    structure = check_structure_break(df_m5, direction, from_index=0)
    if not structure["confirmed"]:
        return {"entry_confirmed": False, "structure": structure,
               "rejection": "NO_STRUCTURE_CONFIRMATION"}
    return {"entry_confirmed": True, "structure": structure, "rejection": None}


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

def _direction_to_trade_side(direction_4h: str):
    if direction_4h == "BULLISH":
        return "BUY"
    if direction_4h == "BEARISH":
        return "SELL"
    return None


def evaluate_setup(asset: str, df_h4, df_h1, df_m30, df_m15, current_price: float,
                   reaction_map_score: float = None, regime: str = None) -> dict:
    """
    Entry point principale -- RISCRITTO 25/08, framework multi-timeframe
    (idea dell'utente): niente gate rigidi tra timeframe. Ogni
    combinazione viene calcolata, classificata e registrata -- sono i
    dati, nel tempo, a dover dire quale combinazione ha edge, non una
    decisione presa a priori su un campione piccolo.

        H4          = macro trend/regime (contesto, MAI un gate)
        H1 + M30    = direzione TATTICA (guida davvero il trade)
        M15         = Balance + HL/LH + setup (location)
        M5          = entry (immediato, verificato il 25/08)

    La direzione tattica (H1+M30, devono concordare tra loro -- se
    discordano tra loro, quella si' che e' vera incertezza, non un
    disaccordo col macro) determina il trade. La relazione con H4
    determina solo la CLASSIFICAZIONE:

        macro == tattica       -> MACRO_CONTINUATION_<direzione>
        macro != tattica       -> TACTICAL_COUNTERTREND_<direzione>
        macro == NEUTRAL       -> TACTICAL_ONLY_<direzione>

    Ogni combinazione passa (nessun rigetto qui) -- la classificazione
    viene salvata nel segnale per poter misurare dopo, con dati veri,
    quale combinazione vince davvero.
    """
    diag = {"asset": asset}

    def reject(reason):
        diag["rejection"] = reason
        return {"signal": None, "diagnostics": diag}

    dir_h4 = compute_direction_4h(df_h4)["direction"]
    diag["direction_4h"] = dir_h4

    dir_h1 = compute_direction_4h(df_h1)["direction"]
    dir_m30 = compute_direction_4h(df_m30)["direction"]
    diag["direction_1h"] = dir_h1
    diag["direction_30m"] = dir_m30

    # La direzione TATTICA guida il trade -- H1 e M30 devono concordare
    # tra loro (quella e' vera incertezza a breve termine, diversa dal
    # disaccordo col macro H4). Se non concordano, o sono NEUTRAL,
    # non c'e' ancora una direzione tattica chiara -- non rigettiamo
    # per un disaccordo col macro (quello si misura), ma serve comunque
    # un chiaro accordo TATTICO per sapere in che direzione guardare.
    if dir_h1 == "NEUTRAL" or dir_m30 == "NEUTRAL" or dir_h1 != dir_m30:
        return reject(f"TACTICAL_DIRECTION_UNCLEAR (H1={dir_h1} M30={dir_m30})")

    tactical_direction = dir_h1  # H1 e M30 concordano, questa e' la direzione tattica

    if dir_h4 == "NEUTRAL":
        mtf_combination = f"TACTICAL_ONLY_{tactical_direction}"
    elif dir_h4 == tactical_direction:
        mtf_combination = f"MACRO_CONTINUATION_{tactical_direction}"
    else:
        mtf_combination = f"TACTICAL_COUNTERTREND_{tactical_direction}"
    diag["mtf_combination"] = mtf_combination

    trade_side = _direction_to_trade_side(tactical_direction)
    dir_ctx = compute_direction_4h(df_h4)  # per last_bos_price/swing_range, serve l'oggetto completo

    # Location su M15 (non piu' H1) -- verificato il 25/08: la stessa
    # logica Expansion/Pullback/Balance/HL-LH su H1 produceva solo
    # ~0.5 setup/giorno (il pattern e' raro su quella scala). Su M15
    # lo stesso pattern si ripete naturalmente piu' spesso (~4/giorno,
    # qualita' per lo piu' HIGH) mantenendo intatta la logica -- non
    # e' stata allentata nessuna soglia, solo cambiata la scala.
    location = select_location(df_m15, tactical_direction, current_price)
    if location is None:
        return reject("NO_VALID_LOCATION")  # Expansion insuff. o Pullback non confermato -- WATCH
    diag["location"] = location

    pd_info = compute_premium_discount(location["location_price"],
                                       dir_ctx["swing_range_low"], dir_ctx["swing_range_high"])
    diag["premium_discount"] = pd_info

    ctx_15m = compute_15m_context(df_m15)
    diag["context_15m"] = ctx_15m

    # Proximity / NO_CHASE (spec sezione 12): se il prezzo e' gia'
    # scappato oltre la location prima della Confirmation, non si
    # insegue -- si aspetta il prossimo Pullback.
    #
    # Per il Balance attivo, questo controllo e' gia' stato fatto
    # correttamente dentro select_location_from_active_balance
    # (contro l'intero range, non il suo centro) -- lo salto qui per
    # non rigettare erroneamente casi validi dove il prezzo e' vicino
    # a un bordo del range ma lontano dal centro (location_price).
    if location.get("is_active_balance"):
        diag["proximity_points"] = 0.0
    else:
        proximity_threshold = PROXIMITY_POINTS.get(asset, 15.0)
        loc_price = location["location_price"]
        if trade_side == "BUY":
            distance = current_price - loc_price
        else:
            distance = loc_price - current_price
        diag["proximity_points"] = round(distance, 4)

        if distance < 0:
            return reject("PRICE_ALREADY_PAST_LOCATION")
        if distance > proximity_threshold:
            return reject(f"NO_CHASE ({distance:.2f} > {proximity_threshold})")

    loc_price = location["location_price"]

    atr_h1 = location["atr_h1"]
    sl_buffer = SL_BUFFER_ATR * atr_h1 if atr_h1 > 0 else 0

    # Se e' un Balance attivo (idea del 25/08): entry al punto migliore
    # del range, SL sotto/sopra l'INTERO range -- non un punto singolo.
    # Verificato: 63.2% WR, +1.10R contro risultati peggiori col solo
    # punto. Altrimenti (fallback), stesso comportamento di prima.
    if location.get("is_active_balance"):
        if trade_side == "BUY":
            planned_entry = location["range_low"]
            planned_sl = location["range_low"] - sl_buffer
        else:
            planned_entry = location["range_high"]
            planned_sl = location["range_high"] + sl_buffer
    elif trade_side == "BUY":
        planned_entry = loc_price
        planned_sl = loc_price - sl_buffer  # oltre l'invalidazione dell'HL
    else:
        planned_entry = loc_price
        planned_sl = loc_price + sl_buffer  # oltre l'invalidazione del LH

    risk = abs(planned_entry - planned_sl)
    if risk <= 0:
        return reject("SL_NOT_CALCULABLE")

    # TP: Previous HH/LL (last_bos_price, priorita' massima) + swing
    # H1 nella direzione del trade -- non piu' zone di liquidita'
    # generiche. select_dynamic_target riusa il tetto di distanza e
    # il fallback 1:2 gia' corretti il 24/08.
    candidates = _build_tp_candidates(df_m15, dir_ctx, trade_side, planned_entry)
    target = select_dynamic_target({"above": candidates} if trade_side == "BUY"
                                   else {"below": candidates},
                                   None, planned_entry, planned_sl, trade_side, atr=atr_h1)
    if target is None:
        return reject("NO_DYNAMIC_TARGET_AVAILABLE")

    planned_tp = target["price"]
    planned_rr = target["rr"]
    if planned_rr < MIN_RR:
        return reject(f"RR_INSUFFICIENT ({planned_rr:.2f} < {MIN_RR})")

    quality_score, quality_label = _compute_signal_quality_v2(
        location, pd_info, ctx_15m, trade_side, planned_rr,
        reaction_map_score=reaction_map_score, regime=regime)

    now = datetime.now(timezone.utc)
    signal = {
        "asset": asset, "direction": trade_side, "direction_4h": dir_h4,
        "direction_1h": dir_h1, "direction_30m": dir_m30,
        "mtf_combination": mtf_combination,
        "swing_range_low": dir_ctx["swing_range_low"], "swing_range_high": dir_ctx["swing_range_high"],
        "last_bos_price": dir_ctx["last_bos_price"], "last_bos_ts": dir_ctx["last_bos_ts"],

        "poi_type": location["location_type"],
        "poi_high": location.get("range_high", loc_price),
        "poi_low": location.get("range_low", loc_price),
        "poi_quality": quality_score, "poi_ref": f"loc:{asset}:{location['location_type']}:{location['location_ts']}",

        "liquidity_type": target["type"], "liquidity_level": planned_tp,
        "liquidity_direction": "above" if trade_side == "BUY" else "below",
        "liquidity_distance_pct": None, "sweep_target_level": None,  # niente piu' sweep a questo livello

        "pd_zone": pd_info["pd_zone"], "pd_pct": pd_info["pd_pct"],

        "ctx_15m_structure": ctx_15m["ctx_15m_structure"], "ctx_15m_momentum": ctx_15m["ctx_15m_momentum"],
        "ctx_15m_note": ctx_15m["ctx_15m_note"],

        "proximity_points": diag["proximity_points"], "signal_created_at": now.isoformat(),

        "planned_entry": round(planned_entry, 5), "planned_sl": round(planned_sl, 5),
        "planned_tp": round(planned_tp, 5), "planned_rr": planned_rr,
        "planned_tp_type": target["type"], "planned_tp_ref": f"{target['type']}@{target['price']}",

        "quality_score": quality_score, "quality_label": quality_label,
        "setup_type": "STRUCTURE_PULLBACK",  # nuovo tipo, distinto da AGGRESSIVE/CONSERVATIVE
        "expiry_bars_waiting": 96,  # 8h (cicli da 5min) -- il nuovo meccanismo di
                                    # Confirmation richiede naturalmente piu' tempo del
                                    # vecchio sweep+reazione. Validato il 25/08: un caso
                                    # reale si e' confermato a 2.8h, oltre le 2h di prima.

        "expansion_start_price": location["expansion_start_price"],
        "expansion_end_price": location["expansion_end_price"],
        "expansion_size_atr": location["expansion_size_atr"],
        "balance_detected": location["balance"] is not None,

        "context_snapshot": {"direction": dir_ctx, "location": location,
                             "premium_discount": pd_info, "context_15m": ctx_15m},
    }

    diag["status"] = "SETUP_CREATED"
    return {"signal": signal, "diagnostics": diag}


def _build_tp_candidates(df_h1, dir_ctx: dict, trade_side: str, entry: float) -> list:
    """
    Costruisce i candidati TP: Previous HH/LL (last_bos_price, massima
    priorita') + swing H1 nella direzione del trade. Sostituisce la
    vecchia liquidity map generica -- il documento chiede esplicitamente
    "Previous HH -> next Liquidity", non zone di liquidita' qualunque.
    """
    candidates = []
    if dir_ctx.get("last_bos_price") is not None:
        bos = dir_ctx["last_bos_price"]
        if (trade_side == "BUY" and bos > entry) or (trade_side == "SELL" and bos < entry):
            candidates.append({"price": bos, "type": "PREVIOUS_STRUCTURE"})

    swings = _detect_swings_h4(df_h1)  # stessa funzione, riusata su H1 qui
    target_type = "HIGH" if trade_side == "BUY" else "LOW"
    for s in swings:
        if s["type"] != target_type:
            continue
        if trade_side == "BUY" and s["price"] > entry:
            candidates.append({"price": s["price"], "type": "SWING_HIGH"})
        elif trade_side == "SELL" and s["price"] < entry:
            candidates.append({"price": s["price"], "type": "SWING_LOW"})

    return candidates


def _compute_signal_quality_v2(location: dict, pd_info: dict, ctx_15m: dict,
                               trade_side: str, planned_rr: float,
                               reaction_map_score: float = None,
                               regime: str = None) -> tuple:
    """
    Quality Score v2 -- stessa filosofia della precedente (pochi
    elementi forti, soft factor, mai hard gate), adattata al nuovo
    concept: Expansion forte, Balance presente, Liquidity context
    (FVG in confluenza), Premium/Discount, RR, Reaction Map, regime.
    """
    score = 3

    if location["expansion_size_atr"] >= 3.0:
        score += 3
    elif location["expansion_size_atr"] >= 2.0:
        score += 2

    if location["balance"] is not None:
        score += 2  # vero consolidamento durante il pullback -- preparazione confermata

    score += location["liquidity_context_score"]  # +1 se FVG in confluenza

    if is_preferred_zone(pd_info.get("pd_zone"), trade_side):
        score += 1

    if planned_rr >= 2.0:
        score += 2

    if reaction_map_score is not None and reaction_map_score >= 5:
        score += 1

    if regime == "TRANSITIONAL":
        score -= 2

    score = max(0, min(score, 12))
    label = "HIGH" if score >= 8 else ("MEDIUM" if score >= 5 else "LOW")
    return score, label
