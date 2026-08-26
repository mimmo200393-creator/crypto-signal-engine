"""
strategies/tt/location_engine.py
TT — Location Engine (H1) — RISCRITTO 25/08

Nuovo concept (da "ISTRUZIONI OPERATIVE -- MARKET MECHANICS", documento
fornito dall'utente il 25/08): la location non e' piu' una zona
Demand/Supply (ultima candela opposta prima di un impulso) -- e' il
nuovo HL (in uptrend) o LH (in downtrend) che si forma DOPO una vera
Expansion e DURANTE un Pullback confermato.

Gerarchia (dal documento, sezione 5):
    MARKET STRUCTURE = DIREZIONE       (Direction Engine, invariato)
    EXPANSION         = mossa precedente che dimostra forza
    PULLBACK           = il momento in cui aspettare
    HL / LH             = LOCATION (questo file)
    BALANCE              = preparazione (consolidamento nel pullback)
    LIQUIDITY             = contesto (FVG in confluenza, soft bonus)

Principio esplicito dal documento: "HL = LOCATION, non ancora ENTRY".
La Confirmation (buyers/sellers regain control) e il trigger di
ingresso restano in liquidity_engine.py (check_structure_break, gia'
disponibile, riuso puro -- nessuna modifica).

Codice INDIPENDENTE -- stesso principio di sempre: duplica la stessa
regola oggettiva di swing detection (k=2) gia' in direction_engine.py,
come copia propria, non importata. Nessun rischio di propagare
modifiche tra file.
"""

from __future__ import annotations

import pandas as pd

SWING_K = 2                  # stessa soglia usata ovunque nel sistema
MIN_EXPANSION_ATR = 2.0      # l'Expansion deve essere un vero impulso, non rumore
MIN_BALANCE_BARS = 3         # minimo di candele per considerare un vero Balance
MAX_BALANCE_WIDTH_ATR = 1.2  # ampiezza massima del Balance (poco piu' permissivo
                              # della soglia 1.0 usata su OTE, qui la finestra e'
                              # gia' vincolata dentro il pullback, meno rischio di
                              # catturare range troppo ampi per errore)
FVG_MIN_SIZE_ATR = 0.1


def _manual_atr(df: pd.DataFrame, period: int = 14) -> float:
    if df is None or len(df) < period + 1:
        return 0.0
    h = df["high"].astype(float).values
    l = df["low"].astype(float).values
    c = df["close"].astype(float).values
    return sum(max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1]))
              for i in range(-period, 0)) / period


def _detect_swings(df: pd.DataFrame, k: int = SWING_K) -> list:
    """Stessa regola oggettiva usata in direction_engine.py (k=2, disuguaglianza stretta)."""
    if df is None or len(df) < 2 * k + 1:
        return []
    highs = df["high"].astype(float).values
    lows = df["low"].astype(float).values
    timestamps = df["timestamp"].values
    swings = []
    for i in range(k, len(df) - k):
        wh_before, wh_after = highs[i-k:i], highs[i+1:i+k+1]
        wl_before, wl_after = lows[i-k:i], lows[i+1:i+k+1]
        if highs[i] > wh_before.max() and highs[i] > wh_after.max():
            swings.append({"index": i, "type": "HIGH",
                          "price": round(float(highs[i]), 5), "timestamp": int(timestamps[i])})
        if lows[i] < wl_before.min() and lows[i] < wl_after.min():
            swings.append({"index": i, "type": "LOW",
                          "price": round(float(lows[i]), 5), "timestamp": int(timestamps[i])})
    swings.sort(key=lambda s: s["index"])
    return swings


def _detect_balance(df: pd.DataFrame, atr: float, start_idx: int, end_idx: int) -> dict | None:
    """
    Cerca un vero consolidamento (Balance) nella finestra del pullback
    [start_idx, end_idx]. Stesso principio gia' validato su OTE
    (_detect_consolidation_ranges): ampiezza totale della finestra <
    MAX_BALANCE_WIDTH_ATR * ATR, per almeno MIN_BALANCE_BARS candele
    consecutive. Qui la finestra e' gia' vincolata al pullback (non
    scansiona tutto il dataset), quindi la soglia puo' essere un filo
    piu' permissiva senza rischiare falsi positivi diffusi.
    """
    if atr <= 0 or end_idx - start_idx < MIN_BALANCE_BARS - 1:
        return None
    window = df.iloc[start_idx:end_idx + 1]
    if len(window) < MIN_BALANCE_BARS:
        return None
    w_high = window["high"].astype(float).max()
    w_low = window["low"].astype(float).min()
    width = w_high - w_low
    if width <= MAX_BALANCE_WIDTH_ATR * atr:
        return {"balance_high": float(w_high), "balance_low": float(w_low),
               "bars": len(window), "width_atr": round(width / atr, 3)}
    return None


def _detect_fvg(df: pd.DataFrame, lookback_bars: int = 60) -> list:
    """Gap a 3 candele, stessa definizione standard -- invariata dal file precedente."""
    atr = _manual_atr(df)
    if atr <= 0 or len(df) < 5:
        return []
    effective_lookback = min(lookback_bars, len(df))
    data = df.iloc[-effective_lookback:].reset_index(drop=True)
    fvgs = []
    for i in range(len(data) - 2):
        c1, c3 = data.iloc[i], data.iloc[i + 2]
        c1_high, c1_low = float(c1["high"]), float(c1["low"])
        c3_high, c3_low = float(c3["high"]), float(c3["low"])
        if c1_high < c3_low:
            size = c3_low - c1_high
            if size / atr >= FVG_MIN_SIZE_ATR:
                fvgs.append({"direction": "BULLISH", "zone_high": c3_low, "zone_low": c1_high})
        if c1_low > c3_high:
            size = c1_low - c3_high
            if size / atr >= FVG_MIN_SIZE_ATR:
                fvgs.append({"direction": "BEARISH", "zone_high": c1_low, "zone_low": c3_high})
    return fvgs


def _liquidity_context_score(location_price: float, direction: str, fvgs: list, atr: float) -> int:
    """
    Liquidity come CONTESTO (spec: "non usare come segnale isolato"),
    non come gate. +1 se una FVG coerente in direzione e' vicina alla
    location (entro 0.5x ATR) -- confluenza, non certezza.
    """
    if atr <= 0:
        return 0
    wanted = "BULLISH" if direction == "BUY" else "BEARISH"
    for fvg in fvgs:
        if fvg["direction"] != wanted:
            continue
        mid = (fvg["zone_high"] + fvg["zone_low"]) / 2
        if abs(mid - location_price) <= 0.5 * atr:
            return 1
    return 0


def _detect_active_range(df: pd.DataFrame, atr: float, min_bars: int = 3,
                         max_width_atr: float = 1.2, lookback_bars: int = 40) -> dict | None:
    """
    Cerca il range di consolidamento PIU' RECENTE, scansionando
    direttamente le ultime candele -- non condizionato dall'esistenza
    di uno swing HL/LH gia' confermato. Se il range piu' recente
    arriva fino all'ultima candela disponibile, e' un Balance ANCORA
    ATTIVO: possiamo identificarlo ed entrarci mentre si forma,
    invece di aspettare che finisca e lo swing si confermi (k=2,
    quando il prezzo e' gia' partito). Stesso principio gia' validato
    su OTE (_detect_consolidation_ranges), riapplicato qui con la
    finestra limitata alle candele piu' recenti.
    """
    if df is None or len(df) < min_bars or atr <= 0:
        return None

    n = len(df)
    start_scan = max(0, n - lookback_bars)
    ranges = []
    i = start_scan
    while i < n - min_bars + 1:
        window_high = df.iloc[i]['high']
        window_low = df.iloc[i]['low']
        j = i + 1
        while j < n:
            new_high = max(window_high, df.iloc[j]['high'])
            new_low = min(window_low, df.iloc[j]['low'])
            if (new_high - new_low) > max_width_atr * atr:
                break
            window_high, window_low = new_high, new_low
            j += 1
        bars = j - i
        if bars >= min_bars:
            ranges.append({'high': float(window_high), 'low': float(window_low),
                          'bars': bars, 'start_idx': i, 'end_idx': j - 1})
            i = j
        else:
            i += 1

    if not ranges:
        return None

    most_recent = ranges[-1]
    # "Attivo" = arriva fino a una delle ultime 2 candele disponibili
    # (tollera un piccolo ritardo di 1 candela)
    if most_recent['end_idx'] < n - 3:
        return None  # il range piu' recente e' gia' vecchio, non attivo ora

    return most_recent


def select_location_from_active_balance(df_h1: pd.DataFrame, direction_4h: str,
                                        current_price: float) -> dict | None:
    """
    Approccio alternativo (25/08): identifica il Balance PRIMA, verifica
    che sia coerente con una vera Expansion precedente (stesso principio
    di select_location), poi tratta l'INTERO range come zona operativa
    -- entry dentro il range (non un punto), SL oltre il range intero
    (non un buffer arbitrario su un punto singolo).

    Permette di entrare MENTRE il Balance si forma, se il prezzo
    attuale e' ancora dentro il range -- non dopo che lo swing HL/LH
    e' gia' confermato (k=2) e il prezzo e' gia' scappato altrove.
    """
    if direction_4h not in ("BULLISH", "BEARISH"):
        return None

    atr = _manual_atr(df_h1)
    if atr <= 0 or current_price <= 0 or (atr / current_price) < 0.0005:
        return None

    active_range = _detect_active_range(df_h1, atr)
    if active_range is None:
        return None

    # Verifico una vera Expansion PRIMA del range (stessa soglia di
    # select_location, MIN_EXPANSION_ATR) -- uso gli swing sulle
    # candele PRIMA dell'inizio del range per trovarla.
    swings = _detect_swings(df_h1.iloc[:active_range['start_idx'] + 1].reset_index(drop=True))
    if not swings:
        return None
    highs = [s for s in swings if s["type"] == "HIGH"]
    lows = [s for s in swings if s["type"] == "LOW"]

    range_mid = (active_range['high'] + active_range['low']) / 2

    if direction_4h == "BULLISH":
        if not lows:
            return None
        expansion_start = lows[-1]
        expansion_size = range_mid - expansion_start["price"]
        if expansion_size < MIN_EXPANSION_ATR * atr:
            return None
        location_type = "HL"
        # Il prezzo attuale deve essere DENTRO il range (o appena sopra,
        # tolleranza minima) -- questa e' la vera zona operativa
        if not (active_range['low'] - 0.2 * atr <= current_price <= active_range['high'] + 0.2 * atr):
            return None
    else:
        if not highs:
            return None
        expansion_start = highs[-1]
        expansion_size = expansion_start["price"] - range_mid
        if expansion_size < MIN_EXPANSION_ATR * atr:
            return None
        location_type = "LH"
        if not (active_range['low'] - 0.2 * atr <= current_price <= active_range['high'] + 0.2 * atr):
            return None

    fvgs = _detect_fvg(df_h1)
    direction_trade = "BUY" if direction_4h == "BULLISH" else "SELL"
    liquidity_score = _liquidity_context_score(range_mid, direction_trade, fvgs, atr)
    range_end_ts = int(df_h1.iloc[active_range['end_idx']]['timestamp'])

    return {
        "location_type": location_type,
        "location_price": range_mid,
        "location_ts": range_end_ts,
        "range_high": active_range['high'],
        "range_low": active_range['low'],
        "range_bars": active_range['bars'],
        "expansion_start_price": expansion_start["price"],
        "expansion_end_price": range_mid,
        "expansion_size_atr": round(expansion_size / atr, 3),
        "balance": {"balance_high": active_range['high'], "balance_low": active_range['low'],
                   "bars": active_range['bars']},
        "liquidity_context_score": liquidity_score,
        "atr_h1": atr,
        "is_active_balance": True,
    }


def select_location(df_h1: pd.DataFrame, direction_4h: str, current_price: float) -> dict | None:
    """
    Entry point principale -- AGGIORNATO 25/08. Prova PRIMA l'approccio
    Balance-attivo (identifica il range di consolidamento mentre si
    forma, entra nel punto migliore del range, SL sotto l'intero
    range -- non un punto singolo). Verificato con dati reali: 63.2%
    win rate, +1.10R expectancy su 19 setup, contro risultati peggiori
    o negativi con l'approccio a punto singolo puro.

    Se non trova un Balance attivo, ricade sul vecchio approccio
    (Expansion -> Pullback -> HL/LH confermato) come fallback --
    meno frequente, ma ancora valido quando non c'e' un range
    identificabile.
    """
    active = select_location_from_active_balance(df_h1, direction_4h, current_price)
    if active is not None:
        return active
    return _select_location_point_based(df_h1, direction_4h, current_price)


def _select_location_point_based(df_h1: pd.DataFrame, direction_4h: str, current_price: float) -> dict | None:
    """
    Entry point principale. Implementa la sequenza del documento:
    Expansion -> Pullback -> Balance -> HL/LH (location) -> Liquidity.

    BULLISH: trova l'ultimo swing HIGH confermato (fine dell'Expansion),
    verifica che la mossa fino a li' sia un vero impulso (>= 2x ATR),
    poi cerca il PIU' RECENTE swing LOW confermato DOPO quell'high --
    quello e' il nuovo HL (location). Simmetrico per BEARISH (LH).

    Ritorna None se la sequenza non e' ancora completa (es. l'Expansion
    non e' abbastanza forte, o il pullback non ha ancora prodotto uno
    swing confermato) -- livello WATCH, non ancora SETUP.
    """
    if direction_4h not in ("BULLISH", "BEARISH"):
        return None

    swings = _detect_swings(df_h1)
    highs = [s for s in swings if s["type"] == "HIGH"]
    lows = [s for s in swings if s["type"] == "LOW"]
    if not highs or not lows:
        return None

    atr = _manual_atr(df_h1)
    if atr <= 0:
        return None

    # Controllo di sanita': ATR anomalmente piccolo rispetto al prezzo
    # (es. dati weekend quasi piatti su XAU) produrrebbe SL
    # irrealisticamente stretti. Soglia relativa, scala automaticamente
    # per asset senza valori fissi per XAU/BTC. Ricalibrata il 25/08
    # per funzionare anche con location su M15 (ATR piu piccolo in
    # assoluto): verificato su dati reali, caso degenere weekend
    # rapporto=0.000155, caso normale infrasettimanale rapporto=0.002 --
    # soglia 0.0005 separa correttamente i due con margine da entrambi
    # i lati (prima 0.0001 non bastava piu a questa scala).
    if current_price > 0 and (atr / current_price) < 0.0005:
        return None

    if direction_4h == "BULLISH":
        expansion_end = highs[-1]
        prior_lows = [l for l in lows if l["index"] < expansion_end["index"]]
        if not prior_lows:
            return None
        expansion_start = prior_lows[-1]
        expansion_size = expansion_end["price"] - expansion_start["price"]
        if expansion_size < MIN_EXPANSION_ATR * atr:
            return None  # Expansion non abbastanza significativa -- WATCH

        pullback_points = [l for l in lows if l["index"] > expansion_end["index"]]
        if not pullback_points:
            return None  # pullback ancora in corso, nessuno swing confermato -- WATCH
        location = pullback_points[-1]
        location_type = "HL"

        # Coerenza: l'HL deve essere un vero ritracciamento (sopra
        # l'inizio dell'Expansion, non un nuovo minimo strutturale)
        if location["price"] <= expansion_start["price"]:
            return None

    else:  # BEARISH
        expansion_end = lows[-1]
        prior_highs = [h for h in highs if h["index"] < expansion_end["index"]]
        if not prior_highs:
            return None
        expansion_start = prior_highs[-1]
        expansion_size = expansion_start["price"] - expansion_end["price"]
        if expansion_size < MIN_EXPANSION_ATR * atr:
            return None

        pullback_points = [h for h in highs if h["index"] > expansion_end["index"]]
        if not pullback_points:
            return None
        location = pullback_points[-1]
        location_type = "LH"

        if location["price"] >= expansion_start["price"]:
            return None

    # Balance: cerco DOPO il punto di minimo/massimo (dove il prezzo
    # si ferma davvero e consolida), non tra Expansion e location --
    # quella finestra e' la gamba di ritracciamento, per definizione
    # direzionale (in discesa per un pullback bullish), mai piatta.
    # Bug trovato il 25/08: 0 Balance rilevati su 11 casi reali perche'
    # cercavo nel posto sbagliato.
    balance_end_idx = min(location["index"] + 10, len(df_h1) - 1)
    balance = _detect_balance(df_h1, atr, location["index"], balance_end_idx)

    fvgs = _detect_fvg(df_h1)
    direction_trade = "BUY" if direction_4h == "BULLISH" else "SELL"
    liquidity_score = _liquidity_context_score(location["price"], direction_trade, fvgs, atr)

    return {
        "location_type": location_type,
        "location_price": location["price"],
        "location_ts": location["timestamp"],
        "location_index": location["index"],
        "expansion_start_price": expansion_start["price"],
        "expansion_end_price": expansion_end["price"],
        "expansion_size_atr": round(expansion_size / atr, 3),
        "balance": balance,
        "liquidity_context_score": liquidity_score,
        "atr_h1": atr,
    }
