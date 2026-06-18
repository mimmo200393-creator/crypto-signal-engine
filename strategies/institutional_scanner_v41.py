"""
strategies/institutional_scanner_v41.py
Institutional Scanner Framework V4.1 — Intraday Wave Edition

Strategia indipendente da V3.2 Frozen e V4.0 Daily Edition. Riusa le
funzioni di analisi di mercato pure (pivot, struttura H4, zone, OTE,
BOS, sessione) da institutional_scanner_v3.py.

Filosofia: lo scanner ragiona come un radar di liquidita', non come
un giudice sequenziale. Un evento strutturale reale (BOS o CHOCH su
M15, con eventuale Liquidity Sweep di rafforzamento) genera l'alert.
Il contesto (EMA H4/H1, Dow Theory, Zone, S/R, OTE, Momentum, Sessione)
non blocca mai l'alert: serve solo a classificarne la qualita'.

    Trigger  -> genera il segnale (obbligatorio: almeno BOS o CHOCH)
    Contesto -> determina la qualita' (HIGH/MEDIUM/LOW), mai bloccante
    News     -> hard gate: nessun nuovo alert in finestra macro ad alto impatto

Asset: PAXG_USDT, BTC_USDT. Sessioni Londra/New York: bonus di
contesto, non gate (raccolta dati anche fuori sessione per validare
empiricamente se esiste una differenza statistica reale).

V4.1 e' un framework completamente indipendente, tracciato a parte,
per confronto empirico con V3.2 e V4.0.
"""

from datetime import datetime, timezone
from typing import Optional
import logging

import pandas as pd

from strategies.institutional_scanner_v3 import (
    find_pivots,
    evaluate_h4_structure,
    build_h4_zones,
    price_in_zone,
    check_ote,
    _find_m15_swing,
    evaluate_m15_bos,
    get_session,
    H4_PIVOT_LOOKBACK,
    M15_BOS_LOOKBACK,
    OTE_LOW,
    OTE_HIGH,
)
from strategies.institutional_scanner_v4 import (
    evaluate_ema_trend_h4,
    combine_h4_trend,
)

logger = logging.getLogger("institutional_scanner_v41")

V41_ASSETS = ["PAXG_USDT", "BTC_USDT"]

# ============================================================
# Parametri tecnici
# ============================================================
M15_CHOCH_LOOKBACK = 2          # stesso lookback simmetrico di BOS
SWEEP_LOOKBACK_CANDLES = 20     # finestra M15 per individuare swing da "raccogliere"
SWEEP_PENETRATION_MIN_PCT = 0.0005   # penetrazione minima oltre lo swing per contare come sweep
MOMENTUM_LOOKBACK = 5           # candele M15 per il momentum shift

# ============================================================
# Liquidity Map
# ============================================================
WEEKLY_LOOKBACK_DAYS = 7        # candele D1 aggregate per Weekly High/Low
EQUAL_LEVEL_TOLERANCE_PCT = 0.001   # 0.1% per considerare due livelli "equal"
LIQUIDITY_PROXIMITY_PCT = 0.003     # 0.3% di vicinanza per associare un trigger a un Source
SCORE_LIQUIDITY_CONTEXT = 1         # bonus Quality Score se il trigger avviene vicino a un Source

# ============================================================
# Watchlist Alert (preparatorio, non operativo)
# ============================================================
WATCHLIST_PROXIMITY_PCT = 0.005     # 0.5% di vicinanza per attivare un Watchlist Alert

# ============================================================
# Tradeability Filter (hard gate): rifiuta setup con stop troppo ampio
# per un framework intraday. Valori assoluti per PAXG (in punti),
# percentuale per BTC, per coerenza con la scala di prezzo di ciascuno.
# ============================================================
MAX_STOP_DISTANCE_PAXG_POINTS = 30.0
MAX_STOP_DISTANCE_BTC_PCT = 0.01     # 1%

# ============================================================
# News Filter (hard gate)
# ============================================================
NEWS_BLACKOUT_WINDOW_MINUTES = 30

# ============================================================
# Quality Score (max 12) — solo classificazione, mai bloccante
# ============================================================
SCORE_EMA_H4 = 2
SCORE_EMA_H1 = 2
SCORE_ZONE_H4 = 2
SCORE_SR = 1
SCORE_DOW_THEORY = 1
SCORE_MOMENTUM = 1
SCORE_OTE = 1
SCORE_SESSION = 1
SCORE_MAX = 12

QUALITY_HIGH_THRESHOLD = 9
QUALITY_MEDIUM_THRESHOLD = 4


# ============================================================
# EMA Trend (generalizzata per qualsiasi timeframe con EMA50/200)
# ============================================================

def evaluate_ema_trend(df: pd.DataFrame) -> str:
    """
    BULLISH se Close > EMA50 > EMA200, BEARISH se Close < EMA50 < EMA200,
    altrimenti NEUTRAL. Generica per H4 o H1.
    """
    if len(df) < 1 or "ema_50" not in df.columns or "ema_200" not in df.columns:
        return "NEUTRAL"

    last = df.iloc[-1]
    close = float(last["close"])
    ema50 = float(last["ema_50"])
    ema200 = float(last["ema_200"])

    if close > ema50 and ema50 > ema200:
        return "BULLISH"
    if close < ema50 and ema50 < ema200:
        return "BEARISH"
    return "NEUTRAL"


# ============================================================
# Change of Character (CHOCH)
# ============================================================

def evaluate_m15_choch(df_m15: pd.DataFrame, prevailing_structure: str) -> Optional[str]:
    """
    CHOCH = prima rottura strutturale nella direzione OPPOSTA alla
    struttura dominante, confermata da chiusura (non wick).

    Trend ribassista (prevailing_structure == "BEARISH"):
        CHOCH Bullish se Close chiude sopra l'ultimo Lower High.
    Trend rialzista (prevailing_structure == "BULLISH"):
        CHOCH Bearish se Close chiude sotto l'ultimo Higher Low.

    Se prevailing_structure è NEUTRAL, non c'è una struttura dominante
    di cui rilevare un "cambio di carattere": ritorna None.

    Ritorna "BULLISH", "BEARISH" o None.
    """
    if prevailing_structure not in ("BULLISH", "BEARISH"):
        return None
    if len(df_m15) < M15_CHOCH_LOOKBACK * 2 + 3:
        return None

    last_close = float(df_m15.iloc[-1]["close"])
    pivots = find_pivots(df_m15.iloc[:-1], M15_CHOCH_LOOKBACK)

    if prevailing_structure == "BEARISH":
        highs = sorted(pivots["pivot_highs"], key=lambda p: p[2])
        if not highs:
            return None
        last_lower_high = highs[-1][1]
        if last_close > last_lower_high:
            return "BULLISH"
        return None
    else:
        lows = sorted(pivots["pivot_lows"], key=lambda p: p[2])
        if not lows:
            return None
        last_higher_low = lows[-1][1]
        if last_close < last_higher_low:
            return "BEARISH"
        return None


# ============================================================
# Liquidity Sweep (su M15)
# ============================================================

def evaluate_m15_liquidity_sweep(df_m15: pd.DataFrame) -> Optional[str]:
    """
    Liquidity Sweep: il prezzo penetra brevemente oltre uno swing
    high/low recente (raccogliendo liquidità) per poi richiudere
    dentro il range, con corpo di chiusura opposto alla penetrazione.

    Ritorna "BEARISH" (sweep sopra i massimi, raccolta lato sell-side
    poi rifiuto verso il basso) o "BULLISH" (sweep sotto i minimi,
    raccolta lato buy-side poi rifiuto verso l'alto), oppure None.

    Nota: la direzione del sweep descrive DOVE è stata raccolta la
    liquidità, non la direzione finale del setup. La direzione del
    segnale è sempre determinata da BOS/CHOCH.
    """
    if len(df_m15) < SWEEP_LOOKBACK_CANDLES + 3:
        return None

    recent = df_m15.iloc[-(SWEEP_LOOKBACK_CANDLES + 1):-1]
    last = df_m15.iloc[-1]

    swing_high = float(recent["high"].max())
    swing_low = float(recent["low"].min())

    last_high = float(last["high"])
    last_low = float(last["low"])
    last_close = float(last["close"])
    last_open = float(last["open"])

    # Sweep sopra i massimi: penetra sopra swing_high ma chiude sotto
    # (rifiuto ribassista dopo raccolta liquidità lato sell-side)
    penetration_up = (last_high - swing_high) / swing_high if swing_high else 0
    if penetration_up > SWEEP_PENETRATION_MIN_PCT and last_close < swing_high and last_close < last_open:
        return "BEARISH"

    # Sweep sotto i minimi: penetra sotto swing_low ma chiude sopra
    # (rifiuto rialzista dopo raccolta liquidità lato buy-side)
    penetration_down = (swing_low - last_low) / swing_low if swing_low else 0
    if penetration_down > SWEEP_PENETRATION_MIN_PCT and last_close > swing_low and last_close > last_open:
        return "BULLISH"

    return None


def evaluate_m15_liquidity_sweep_detailed(df_m15: pd.DataFrame) -> Optional[dict]:
    """
    Variante di evaluate_m15_liquidity_sweep() che espone anche il
    prezzo esatto del picco di penetrazione (necessario come punto
    iniziale dell'impulso Fibonacci). Stessa logica di rilevamento,
    nessuna duplicazione di soglie o regole.

    Ritorna {"direction": "BULLISH"|"BEARISH", "peak_price": float}
    oppure None.
    """
    if len(df_m15) < SWEEP_LOOKBACK_CANDLES + 3:
        return None

    recent = df_m15.iloc[-(SWEEP_LOOKBACK_CANDLES + 1):-1]
    last = df_m15.iloc[-1]

    swing_high = float(recent["high"].max())
    swing_low = float(recent["low"].min())

    last_high = float(last["high"])
    last_low = float(last["low"])
    last_close = float(last["close"])
    last_open = float(last["open"])

    penetration_up = (last_high - swing_high) / swing_high if swing_high else 0
    if penetration_up > SWEEP_PENETRATION_MIN_PCT and last_close < swing_high and last_close < last_open:
        return {"direction": "BEARISH", "peak_price": last_high}

    penetration_down = (swing_low - last_low) / swing_low if swing_low else 0
    if penetration_down > SWEEP_PENETRATION_MIN_PCT and last_close > swing_low and last_close > last_open:
        return {"direction": "BULLISH", "peak_price": last_low}

    return None


# ============================================================
# Momentum Shift (su M15)
# ============================================================

def evaluate_m15_momentum(df_m15: pd.DataFrame) -> str:
    """
    Momentum semplice: confronta la chiusura attuale con quella di
    MOMENTUM_LOOKBACK candele prima. BULLISH se in salita, BEARISH
    se in discesa, NEUTRAL se variazione trascurabile.
    """
    if len(df_m15) < MOMENTUM_LOOKBACK + 1:
        return "NEUTRAL"

    current = float(df_m15.iloc[-1]["close"])
    past = float(df_m15.iloc[-1 - MOMENTUM_LOOKBACK]["close"])

    if past == 0:
        return "NEUTRAL"

    change_pct = (current - past) / past
    if change_pct > 0.0008:
        return "BULLISH"
    if change_pct < -0.0008:
        return "BEARISH"
    return "NEUTRAL"


# ============================================================
# Supporto/Resistenza reaction (su H1, riusa zone H4 come riferimento)
# ============================================================

def evaluate_sr_reaction(df_h1: pd.DataFrame, zones: list) -> bool:
    """
    True se il prezzo H1 corrente sta reagendo (è vicino, tolleranza
    più larga di una zona operativa) a una zona H4 di supporto/resistenza.
    """
    if len(df_h1) < 1 or not zones:
        return False
    current_price = float(df_h1.iloc[-1]["close"])
    return any(price_in_zone(current_price, z, tolerance_pct=0.006) for z in zones)


# ============================================================
# Liquidity Map
# ============================================================

def build_liquidity_map(df_h4: pd.DataFrame, df_d1: pd.DataFrame) -> dict:
    """
    Costruisce una mappa semplice dei livelli di liquidità rilevanti:
    Weekly High/Low, Daily High/Low, Equal Highs/Lows (su H4 pivot),
    Swing High/Low H4 (stessi pivot usati per la struttura).

    Definizioni (prima versione, volutamente semplici):
        Weekly High/Low = max/min degli ultimi WEEKLY_LOOKBACK_DAYS D1
        Daily High/Low  = max/min della candela D1 corrente o precedente
        Equal Highs/Lows = coppie di pivot H4 con differenza <= tolleranza
        Swing High/Low H4 = pivot H4 più recenti

    Ritorna {"levels": [...]}, ciascun livello e' un dict
    {"label": str, "price": float, "kind": "high"|"low"}.
    """
    levels = []

    if len(df_d1) >= 1:
        recent_d1 = df_d1.iloc[-WEEKLY_LOOKBACK_DAYS:]
        levels.append({"label": "Weekly High", "price": float(recent_d1["high"].max()), "kind": "high"})
        levels.append({"label": "Weekly Low", "price": float(recent_d1["low"].min()), "kind": "low"})

        last_d1 = df_d1.iloc[-1]
        levels.append({"label": "Daily High", "price": float(last_d1["high"]), "kind": "high"})
        levels.append({"label": "Daily Low", "price": float(last_d1["low"]), "kind": "low"})

        if len(df_d1) >= 2:
            prev_d1 = df_d1.iloc[-2]
            levels.append({"label": "Daily High (prev)", "price": float(prev_d1["high"]), "kind": "high"})
            levels.append({"label": "Daily Low (prev)", "price": float(prev_d1["low"]), "kind": "low"})

    pivots = find_pivots(df_h4, H4_PIVOT_LOOKBACK)
    pivot_highs = sorted(pivots["pivot_highs"], key=lambda p: p[2])
    pivot_lows = sorted(pivots["pivot_lows"], key=lambda p: p[2])

    if pivot_highs:
        levels.append({"label": "H4 Swing High", "price": pivot_highs[-1][1], "kind": "high"})
    if pivot_lows:
        levels.append({"label": "H4 Swing Low", "price": pivot_lows[-1][1], "kind": "low"})

    # Equal Highs / Equal Lows: coppie di pivot H4 ravvicinati entro tolleranza
    for i in range(len(pivot_highs)):
        for j in range(i + 1, len(pivot_highs)):
            p1, p2 = pivot_highs[i][1], pivot_highs[j][1]
            if p1 != 0 and abs(p1 - p2) / p1 <= EQUAL_LEVEL_TOLERANCE_PCT:
                levels.append({"label": "Equal Highs", "price": (p1 + p2) / 2, "kind": "high"})

    for i in range(len(pivot_lows)):
        for j in range(i + 1, len(pivot_lows)):
            p1, p2 = pivot_lows[i][1], pivot_lows[j][1]
            if p1 != 0 and abs(p1 - p2) / p1 <= EQUAL_LEVEL_TOLERANCE_PCT:
                levels.append({"label": "Equal Lows", "price": (p1 + p2) / 2, "kind": "low"})

    return {"levels": levels}


def find_liquidity_source(liquidity_map: dict, current_price: float, direction: str) -> Optional[dict]:
    """
    Identifica il livello di liquidità più vicino al prezzo attuale,
    sul lato coerente con la direzione del trigger:
        SELL -> tra i livelli "high" (liquidità sopra, raccolta dal sweep)
        BUY  -> tra i livelli "low" (liquidità sotto, raccolta dal sweep)
    Ritorna il livello entro LIQUIDITY_PROXIMITY_PCT dal prezzo attuale,
    oppure None se nessun livello e' abbastanza vicino.
    """
    kind = "high" if direction == "SELL" else "low"
    candidates = [lv for lv in liquidity_map["levels"] if lv["kind"] == kind]
    if not candidates:
        return None

    closest = min(candidates, key=lambda lv: abs(lv["price"] - current_price))
    if closest["price"] == 0:
        return None
    if abs(closest["price"] - current_price) / closest["price"] <= LIQUIDITY_PROXIMITY_PCT:
        return closest
    return None


def find_liquidity_target(liquidity_map: dict, current_price: float, direction: str) -> Optional[dict]:
    """
    Identifica il primo livello di liquidità non ancora raggiunto
    nella direzione del trade:
        SELL -> il livello "low" più vicino e ancora sotto al prezzo attuale
        BUY  -> il livello "high" più vicino e ancora sopra al prezzo attuale
    """
    kind = "low" if direction == "SELL" else "high"
    candidates = [lv for lv in liquidity_map["levels"] if lv["kind"] == kind]

    if direction == "SELL":
        not_reached = [lv for lv in candidates if lv["price"] < current_price]
        return max(not_reached, key=lambda lv: lv["price"]) if not_reached else None
    else:
        not_reached = [lv for lv in candidates if lv["price"] > current_price]
        return min(not_reached, key=lambda lv: lv["price"]) if not_reached else None


def find_watchlist_proximities(liquidity_map: dict, current_price: float) -> list:
    """
    Identifica tutti i livelli di liquidità entro WATCHLIST_PROXIMITY_PCT
    dal prezzo attuale, indipendentemente da bias o direzione (la
    Watchlist è preparatoria, non richiede ancora un trigger).

    Per ciascun livello "high" lo scenario potenziale è SELL (possibile
    resistenza), per ciascun livello "low" lo scenario potenziale è BUY
    (possibile supporto) — logica intenzionalmente semplice, senza
    considerare il trend H4 dominante in questa prima versione.

    Livelli con la stessa etichetta (es. più coppie "Equal Highs") e
    prezzo molto vicino tra loro vengono deduplicati, mantenendo solo
    il più vicino al prezzo attuale, per evitare ripetizioni quasi
    identiche nello stesso alert.

    Ritorna una lista di dict:
        {"label": str, "price": float, "kind": "high"|"low",
         "distance_pct": float, "potential_direction": "BUY"|"SELL"}
    """
    proximities = []
    for lv in liquidity_map["levels"]:
        if lv["price"] == 0:
            continue
        distance_pct = abs(lv["price"] - current_price) / lv["price"]
        if distance_pct <= WATCHLIST_PROXIMITY_PCT:
            potential_direction = "SELL" if lv["kind"] == "high" else "BUY"
            proximities.append({
                "label": lv["label"],
                "price": lv["price"],
                "kind": lv["kind"],
                "distance_pct": distance_pct,
                "potential_direction": potential_direction,
            })

    deduplicated = {}
    for p in proximities:
        key = p["label"]
        if key not in deduplicated or p["distance_pct"] < deduplicated[key]["distance_pct"]:
            deduplicated[key] = p

    return sorted(deduplicated.values(), key=lambda p: p["distance_pct"])


# ============================================================
# Fibonacci / OTE Entry Zone (qualità e pianificazione, non trigger)
# ============================================================

def calculate_v41_fibonacci(df_m15: pd.DataFrame, direction: str,
                             sweep_detail: Optional[dict]) -> Optional[dict]:
    """
    Calcola l'impulso Fibonacci sul movimento reale che ha originato
    il trigger (non un lookback generico), per ottenere una Entry Zone
    OTE coerente con il setup:

        SELL con Sweep: impulso da massimo del sweep a livello che
                         conferma il BOS bearish (chiusura).
        BUY  con Sweep: impulso da minimo del sweep a livello che
                         conferma il BOS bullish (chiusura).

        CHOCH senza Sweep precedente: impulso dal pivot strutturale
        rotto dal CHOCH (ultimo Lower High per CHOCH Bullish, ultimo
        Higher Low per CHOCH Bearish) all'estremo della candela che
        ha confermato la rottura (soluzione semplice, non il massimo
        rigore teorico, ma robusta e coerente con l'obiettivo V4.1).

    Ritorna {"start": float, "end": float, "ote_lower": float,
             "ote_upper": float, "in_ote": bool} oppure None se
    l'impulso non è calcolabile.
    """
    if len(df_m15) < 1:
        return None

    last = df_m15.iloc[-1]
    last_close = float(last["close"])

    start_price = None
    end_price = None

    if sweep_detail is not None:
        start_price = sweep_detail["peak_price"]
        swing_type = "low" if direction == "SELL" else "high"
        end_price = _find_m15_swing(df_m15.iloc[:-1], swing_type, M15_BOS_LOOKBACK)
    else:
        pivots = find_pivots(df_m15.iloc[:-1], M15_CHOCH_LOOKBACK)
        if direction == "BUY":
            highs = sorted(pivots["pivot_highs"], key=lambda p: p[2])
            if highs:
                start_price = highs[-1][1]
                end_price = float(last["high"])
        else:
            lows = sorted(pivots["pivot_lows"], key=lambda p: p[2])
            if lows:
                start_price = lows[-1][1]
                end_price = float(last["low"])

    if start_price is None or end_price is None:
        return None

    impulse = abs(start_price - end_price)
    if impulse <= 0:
        return None

    if direction == "BUY":
        ote_lower = end_price - impulse * OTE_HIGH
        ote_upper = end_price - impulse * OTE_LOW
    else:
        ote_lower = end_price + impulse * OTE_LOW
        ote_upper = end_price + impulse * OTE_HIGH

    lo, hi = min(ote_lower, ote_upper), max(ote_lower, ote_upper)
    in_ote = lo <= last_close <= hi

    return {
        "start": start_price,
        "end": end_price,
        "ote_lower": lo,
        "ote_upper": hi,
        "in_ote": in_ote,
    }


# ============================================================
# News Filter (hard gate)
# ============================================================

def is_news_blackout(macro_provider, now: datetime) -> Optional[dict]:
    """
    Ritorna l'evento macro attivo se ci troviamo entro
    NEWS_BLACKOUT_WINDOW_MINUTES da un evento ad alto impatto,
    altrimenti None. macro_provider deve implementare get_active_event.
    """
    if macro_provider is None:
        return None
    return macro_provider.get_active_event(now, NEWS_BLACKOUT_WINDOW_MINUTES)


# ============================================================
# Tradeability Filter (hard gate)
# ============================================================

def is_stop_too_wide(asset: str, entry: float, stop_loss: float) -> bool:
    """
    Hard gate intraday: rifiuta setup il cui Stop Loss naturale supera
    il limite massimo accettabile per quell'asset. PAXG in punti
    assoluti, BTC in percentuale (scale di prezzo molto diverse).
    Asset non esplicitamente coperti non vengono filtrati (nessun
    limite noto, comportamento permissivo di default).
    """
    distance = abs(entry - stop_loss)
    if asset == "PAXG_USDT":
        return distance > MAX_STOP_DISTANCE_PAXG_POINTS
    if asset == "BTC_USDT":
        if entry == 0:
            return False
        return (distance / entry) > MAX_STOP_DISTANCE_BTC_PCT
    return False


# ============================================================
# Pipeline principale
# ============================================================

def generate_v41_signal(market_data: dict) -> dict:
    """
    Pipeline Institutional Scanner V4.1 Intraday Wave Edition.

    market_data deve contenere:
        asset, df_h4, df_h1, df_m15 (con EMA/ATR gia' calcolati)
        df_d1 (opzionale, per la Liquidity Map)
        timestamp (datetime corrente)
        macro_provider (opzionale, per il News Filter)

    Ritorna {"signal": dict|None, "diagnostics": dict}.

    diagnostics contiene sempre: trigger_found (bool), trigger_types
    (lista), quality_score, quality_label, rejections.
    """
    asset = market_data["asset"]
    df_h4 = market_data["df_h4"]
    df_h1 = market_data["df_h1"]
    df_m15 = market_data["df_m15"]
    df_d1 = market_data.get("df_d1")
    now = market_data.get("timestamp", datetime.now(timezone.utc))
    macro_provider = market_data.get("macro_provider")

    diagnostics = {
        "asset": asset,
        "rejections": [],
        "trigger_found": False,
        "trigger_types": [],
    }

    # --- News Filter (hard gate, valutato per primo) ---
    active_event = is_news_blackout(macro_provider, now)
    if active_event:
        diagnostics["rejections"].append(f"NEWS_BLACKOUT_{active_event['type']}")
        diagnostics["active_news_event"] = active_event
        return {"signal": None, "diagnostics": diagnostics}

    if len(df_h4) < 15 or len(df_h1) < 20 or len(df_m15) < max(SWEEP_LOOKBACK_CANDLES + 3, 15):
        diagnostics["rejections"].append("INSUFFICIENT_DATA")
        return {"signal": None, "diagnostics": diagnostics}

    atr_m15 = float(df_m15.iloc[-1]["atr"]) if "atr" in df_m15.columns else 0
    atr_h4 = float(df_h4.iloc[-1]["atr"]) if "atr" in df_h4.columns else 0

    # --- Struttura H4 dominante (riferimento per BOS/CHOCH) ---
    # Combina Dow Theory pura (pivot H4) ed EMA50/200 H4, stessa logica
    # gia' validata in V4.0, per ridurre i falsi NEUTRAL. dow_theory_h4
    # puro resta disponibile separatamente per il Quality Score.
    h4_struct = evaluate_h4_structure(df_h4)
    dow_theory_h4 = h4_struct["structure"]
    ema_h4_trend_for_structure = evaluate_ema_trend_h4(df_h4)
    dominant_h4_structure = combine_h4_trend(dow_theory_h4, ema_h4_trend_for_structure)

    diagnostics["dow_theory_h4"] = dow_theory_h4
    diagnostics["dominant_h4_structure"] = dominant_h4_structure

    # --- TRIGGER: BOS o CHOCH (obbligatorio almeno uno) ---
    bos_direction = None
    if dominant_h4_structure in ("BULLISH", "BEARISH"):
        bos_signal_direction = "BUY" if dominant_h4_structure == "BULLISH" else "SELL"
        if evaluate_m15_bos(df_m15, bos_signal_direction):
            bos_direction = dominant_h4_structure

    choch_direction = evaluate_m15_choch(df_m15, dominant_h4_structure)

    diagnostics["bos_direction"] = bos_direction
    diagnostics["choch_direction"] = choch_direction

    # Conflitto: BOS e CHOCH presenti ma in direzioni diverse -> no alert
    if bos_direction and choch_direction and bos_direction != choch_direction:
        diagnostics["rejections"].append("BOS_CHOCH_CONFLICT")
        return {"signal": None, "diagnostics": diagnostics}

    structural_direction = bos_direction or choch_direction

    if structural_direction is None:
        diagnostics["rejections"].append("NO_STRUCTURAL_TRIGGER")
        return {"signal": None, "diagnostics": diagnostics}

    diagnostics["trigger_found"] = True
    if bos_direction:
        diagnostics["trigger_types"].append("BOS")
    if choch_direction:
        diagnostics["trigger_types"].append("CHOCH")

    # --- Liquidity Sweep (rafforza il trigger, non determina la direzione) ---
    sweep_detail = evaluate_m15_liquidity_sweep_detailed(df_m15)
    sweep_direction = sweep_detail["direction"] if sweep_detail else None
    diagnostics["sweep_direction"] = sweep_direction
    if sweep_direction:
        diagnostics["trigger_types"].append("LIQUIDITY_SWEEP")

    direction = "BUY" if structural_direction == "BULLISH" else "SELL"

    # --- Liquidity Map: identifica Source e Target ---
    current_price_for_map = float(df_m15.iloc[-1]["close"])
    liquidity_map = build_liquidity_map(df_h4, df_d1 if df_d1 is not None else pd.DataFrame())
    liquidity_source = find_liquidity_source(liquidity_map, current_price_for_map, direction)
    liquidity_target = find_liquidity_target(liquidity_map, current_price_for_map, direction)

    diagnostics["liquidity_source"] = liquidity_source["label"] if liquidity_source else None
    diagnostics["liquidity_target"] = liquidity_target["label"] if liquidity_target else None

    # --- Fibonacci / OTE Entry Zone (qualità e pianificazione, non trigger) ---
    fibonacci = calculate_v41_fibonacci(df_m15, direction, sweep_detail)
    fib_in_ote = fibonacci["in_ote"] if fibonacci else False
    diagnostics["fibonacci"] = fibonacci

    # ============================================================
    # Trigger soddisfatto: il segnale viene generato. Da qui in poi
    # solo contesto per il Quality Score (mai bloccante).
    # ============================================================

    ema_h4 = evaluate_ema_trend(df_h4)
    ema_h1 = evaluate_ema_trend(df_h1)
    momentum = evaluate_m15_momentum(df_m15)
    session = get_session(now)

    zones = build_h4_zones(df_h4, atr_h4) if atr_h4 > 0 else []
    in_h4_zone = any(
        price_in_zone(float(df_h1.iloc[-1]["close"]), z, tolerance_pct=0.006) for z in zones
    ) if zones else False
    sr_reaction = evaluate_sr_reaction(df_h1, zones)
    ote_present = fib_in_ote

    ema_h4_aligned = ema_h4 == structural_direction
    ema_h1_aligned = ema_h1 == structural_direction
    dow_aligned = dow_theory_h4 == structural_direction
    momentum_aligned = momentum == structural_direction
    session_bonus = session in ("LONDON", "NEW_YORK")

    score = 0
    if ema_h4_aligned:
        score += SCORE_EMA_H4
    if ema_h1_aligned:
        score += SCORE_EMA_H1
    if in_h4_zone:
        score += SCORE_ZONE_H4
    if sr_reaction:
        score += SCORE_SR
    if dow_aligned:
        score += SCORE_DOW_THEORY
    if momentum_aligned:
        score += SCORE_MOMENTUM
    if ote_present:
        score += SCORE_OTE
    if session_bonus:
        score += SCORE_SESSION
    if liquidity_source is not None:
        score += SCORE_LIQUIDITY_CONTEXT

    score = max(0, min(score, SCORE_MAX))

    if score >= QUALITY_HIGH_THRESHOLD:
        quality_label = "HIGH"
    elif score >= QUALITY_MEDIUM_THRESHOLD:
        quality_label = "MEDIUM"
    else:
        quality_label = "LOW"

    diagnostics["quality_score"] = score
    diagnostics["quality_label"] = quality_label

    entry = float(df_m15.iloc[-1]["close"])

    swing_type = "low" if direction == "BUY" else "high"
    structural_swing = _find_m15_swing(df_m15.iloc[:-1], swing_type, M15_BOS_LOOKBACK)

    if atr_m15 <= 0:
        diagnostics["rejections"].append("ATR_ZERO")
        return {"signal": None, "diagnostics": diagnostics}

    if direction == "BUY":
        sl_atr = entry - 1.5 * atr_m15
        stop_loss = min(structural_swing, sl_atr) if structural_swing is not None else sl_atr
    else:
        sl_atr = entry + 1.5 * atr_m15
        stop_loss = max(structural_swing, sl_atr) if structural_swing is not None else sl_atr

    risk = abs(entry - stop_loss)
    if risk <= 0:
        diagnostics["rejections"].append("RISK_ZERO")
        return {"signal": None, "diagnostics": diagnostics}

    # --- Tradeability Filter (hard gate): stop troppo ampio per intraday ---
    if is_stop_too_wide(asset, entry, stop_loss):
        diagnostics["rejections"].append("STOP_TOO_WIDE")
        diagnostics["stop_distance"] = risk
        logger.info(
            "%s | V4.1 REJECT: STOP_TOO_WIDE (distanza=%.2f, limite=%s)",
            asset, risk,
            f"{MAX_STOP_DISTANCE_PAXG_POINTS}pt" if asset == "PAXG_USDT"
            else f"{MAX_STOP_DISTANCE_BTC_PCT*100:.1f}%",
        )
        return {"signal": None, "diagnostics": diagnostics}

    # Target: 2x il rischio (R/R 1:2) come riferimento intraday (non
    # vincolante come V3.2/V4.0: lo scanner segnala l'opportunità, la
    # gestione del trade resta al trader).
    if direction == "BUY":
        take_profit = entry + 2.0 * risk
    else:
        take_profit = entry - 2.0 * risk

    rr = abs(take_profit - entry) / risk

    signal = {
        "asset": asset,
        "direction": direction,
        "entry": entry,
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "rr": rr,
        "trigger_types": list(diagnostics["trigger_types"]),
        "sweep_direction": sweep_direction,
        "bos_direction": bos_direction,
        "choch_direction": choch_direction,
        "quality_score": score,
        "quality_label": quality_label,
        "ema_h4": ema_h4,
        "ema_h1": ema_h1,
        "dow_theory_h4": dow_theory_h4,
        "momentum": momentum,
        "in_h4_zone": in_h4_zone,
        "sr_reaction": sr_reaction,
        "ote_present": ote_present,
        "session": session,
        "liquidity_source": liquidity_source["label"] if liquidity_source else None,
        "liquidity_target": liquidity_target["label"] if liquidity_target else None,
        "liquidity_target_price": liquidity_target["price"] if liquidity_target else None,
        "ote_entry_low": fibonacci["ote_lower"] if fibonacci else None,
        "ote_entry_high": fibonacci["ote_upper"] if fibonacci else None,
        "ote_in_zone_now": fib_in_ote,
        "timestamp_setup": now.isoformat(),
    }

    logger.info(
        "%s | V4.1 ALERT [%s] trigger=%s quality=%d/%d (%s) session=%s",
        asset, direction, diagnostics["trigger_types"], score, SCORE_MAX,
        quality_label, session,
    )
    logger.info(
        "%s | Quality breakdown: EMA_H4=%s(%s) EMA_H1=%s(%s) ZONE_H4=%s SR=%s "
        "DOW_THEORY=%s(%s) MOMENTUM=%s(%s) OTE=%s SESSION=%s LIQUIDITY_CTX=%s | totale=%d/%d",
        asset,
        "OK" if ema_h4_aligned else "NO", ema_h4,
        "OK" if ema_h1_aligned else "NO", ema_h1,
        "OK" if in_h4_zone else "NO",
        "OK" if sr_reaction else "NO",
        "OK" if dow_aligned else "NO", dow_theory_h4,
        "OK" if momentum_aligned else "NO", momentum,
        "OK" if ote_present else "NO",
        "OK" if session_bonus else "NO",
        "OK" if liquidity_source is not None else "NO",
        score, SCORE_MAX,
    )
    logger.info(
        "%s | Liquidity context: Source=%s Target=%s(%s) | OTE Entry Zone=%s-%s (in zona ora=%s)",
        asset,
        liquidity_source["label"] if liquidity_source else "N/A",
        liquidity_target["label"] if liquidity_target else "N/A",
        f"{liquidity_target['price']:.4f}" if liquidity_target else "N/A",
        f"{fibonacci['ote_lower']:.4f}" if fibonacci else "N/A",
        f"{fibonacci['ote_upper']:.4f}" if fibonacci else "N/A",
        fib_in_ote,
    )

    return {"signal": signal, "diagnostics": diagnostics}
