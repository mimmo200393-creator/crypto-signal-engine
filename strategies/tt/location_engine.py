"""
strategies/tt/location_engine.py
TT — Location Engine (1H)

Risponde a: "WHERE COULD THE TRADE HAPPEN?"

Codice INDIPENDENTE -- stesso principio del Direction Engine. Duplica
concettualmente la stessa regola oggettiva di rilevamento OB/FVG gia'
validata altrove (ultima candela opposta prima di un impulso, gap a 3
candele) ma come codice proprio, non importato da order_block_engine.py
o fvg_engine.py. Nessun rischio di propagare modifiche tra strategie.

Logica (spec sezione 3):
    - Demand (POI bullish) = ultima candela ribassista prima di un
      impulso rialzista H1
    - Supply (POI bearish) = ultima candela rialzista prima di un
      impulso ribassista H1
    - FVG (sezione 3): gap a 3 candele, stessa definizione standard
    - NON tutte le zone disponibili -- solo le piu' significative
      rispetto al bias 4H (LONG -> Demand, SHORT -> Supply)
    - La POI rappresenta una LOCATION potenziale, NON ancora un'entry
"""

from __future__ import annotations

import pandas as pd

MIN_IMPULSE_ATR = 1.0       # forza minima dell'impulso per considerare un POI
MIN_BODY_RATIO = 0.5        # corpo/range minimo della candela di impulso
FVG_MIN_SIZE_ATR = 0.1      # gap minimo per considerare una FVG rilevante


def _manual_atr(df: pd.DataFrame, period: int = 14) -> float:
    """ATR calcolato a mano, nessuna dipendenza da indicators.py condiviso."""
    if df is None or len(df) < period + 1:
        return 0.0
    h = df["high"].astype(float).values
    l = df["low"].astype(float).values
    c = df["close"].astype(float).values
    return sum(max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1]))
              for i in range(-period, 0)) / period


def _detect_pois(df_h1: pd.DataFrame, asset: str, lookback_bars: int = 60) -> list:
    """
    Rileva Demand (POI bullish) e Supply (POI bearish) su H1.

    Stessa definizione oggettiva gia' usata altrove nel sistema:
    l'ultima candela di colore opposto prima di un impulso che supera
    MIN_IMPULSE_ATR * ATR, con corpo/range >= MIN_BODY_RATIO.

    Ritorna lista di {"poi_ref", "poi_type", "zone_high", "zone_low",
    "formation_ts", "displacement_atr", "index"}.
    """
    atr = _manual_atr(df_h1)
    if atr <= 0 or len(df_h1) < 10:
        return []

    n = len(df_h1)
    # Si adatta ai dati disponibili invece di richiedere un minimo fisso
    # (che farebbe fallire tutto su asset/timeframe con meno storico).
    effective_lookback = min(lookback_bars, n - 3)
    start = max(2, n - effective_lookback)
    data = df_h1.reset_index(drop=True)

    pois = []
    for i in range(start, n - 1):
        curr = data.iloc[i]
        c_open, c_close = float(curr["open"]), float(curr["close"])
        c_high, c_low = float(curr["high"]), float(curr["low"])
        c_range = c_high - c_low
        if c_range <= 0:
            continue
        c_body = abs(c_close - c_open)
        c_body_ratio = c_body / c_range

        is_bull_impulse = (c_close > c_open and c_body >= MIN_IMPULSE_ATR * atr
                           and c_body_ratio >= MIN_BODY_RATIO)
        is_bear_impulse = (c_close < c_open and c_body >= MIN_IMPULSE_ATR * atr
                           and c_body_ratio >= MIN_BODY_RATIO)

        if is_bull_impulse:
            for j in range(i - 1, max(i - 5, 0) - 1, -1):
                poi_candle = data.iloc[j]
                po, pc = float(poi_candle["open"]), float(poi_candle["close"])
                if pc < po:
                    ts = int(poi_candle.get("timestamp", 0))
                    pois.append({
                        "poi_ref": f"poi:{asset}:DEMAND:H1:{ts}",
                        "poi_type": "DEMAND",
                        "zone_high": float(poi_candle["high"]),
                        "zone_low": float(poi_candle["low"]),
                        "formation_ts": ts,
                        "displacement_atr": round(c_body / atr, 3),
                        "index": j,
                        "impulse_index": i,
                    })
                    break

        elif is_bear_impulse:
            for j in range(i - 1, max(i - 5, 0) - 1, -1):
                poi_candle = data.iloc[j]
                po, pc = float(poi_candle["open"]), float(poi_candle["close"])
                if pc > po:
                    ts = int(poi_candle.get("timestamp", 0))
                    pois.append({
                        "poi_ref": f"poi:{asset}:SUPPLY:H1:{ts}",
                        "poi_type": "SUPPLY",
                        "zone_high": float(poi_candle["high"]),
                        "zone_low": float(poi_candle["low"]),
                        "formation_ts": ts,
                        "displacement_atr": round(c_body / atr, 3),
                        "index": j,
                        "impulse_index": i,
                    })
                    break

    return pois


def _detect_fvg(df_h1: pd.DataFrame, lookback_bars: int = 60) -> list:
    """
    Rileva FVG (Fair Value Gap) su H1 -- gap a 3 candele, stessa
    definizione standard: candle[i].high < candle[i+2].low (bullish)
    o candle[i].low > candle[i+2].high (bearish).
    """
    atr = _manual_atr(df_h1)
    if atr <= 0 or len(df_h1) < 5:
        return []

    effective_lookback = min(lookback_bars, len(df_h1))
    data = df_h1.iloc[-effective_lookback:].reset_index(drop=True)
    fvgs = []

    for i in range(len(data) - 2):
        c1 = data.iloc[i]
        c3 = data.iloc[i + 2]
        c1_high, c1_low = float(c1["high"]), float(c1["low"])
        c3_high, c3_low = float(c3["high"]), float(c3["low"])

        if c1_high < c3_low:
            size = c3_low - c1_high
            if size / atr >= FVG_MIN_SIZE_ATR:
                fvgs.append({
                    "direction": "BULLISH", "zone_high": c3_low, "zone_low": c1_high,
                    "formation_ts": int(c3.get("timestamp", 0)),
                })
        if c1_low > c3_high:
            size = c1_low - c3_high
            if size / atr >= FVG_MIN_SIZE_ATR:
                fvgs.append({
                    "direction": "BEARISH", "zone_high": c1_low, "zone_low": c3_high,
                    "formation_ts": int(c3.get("timestamp", 0)),
                })

    return fvgs


def _ranges_overlap_center(low1, high1, low2, high2) -> bool:
    """Il centro della prima zona cade dentro la seconda (sovrapposizione sostanziale)."""
    mid1 = (low1 + high1) / 2
    return low2 <= mid1 <= high2


def _poi_quality(poi: dict, fvgs: list, is_tested: bool) -> int:
    """
    Quality score 0-10, pochi elementi forti (spec sezione 29, no 20 filtri):
      +4 displacement forte (>=2 ATR), altrimenti +2
      +3 FVG in confluenza (coerente in direzione)
      +1 ancora non testata (fresca)
    """
    score = 0
    disp = poi.get("displacement_atr", 0)
    score += 4 if disp >= 2.0 else 2

    poi_kind = "BULLISH" if poi["poi_type"] == "DEMAND" else "BEARISH"
    for fvg in fvgs:
        if fvg["direction"] != poi_kind:
            continue
        if _ranges_overlap_center(poi["zone_low"], poi["zone_high"],
                                  fvg["zone_low"], fvg["zone_high"]):
            score += 3
            break

    if not is_tested:
        score += 1

    return min(score, 10)


def _poi_is_tested(poi: dict, df_h1: pd.DataFrame) -> bool:
    """
    Il prezzo e' DAVVERO rientrato nella zona dopo la sua formazione?

    Esclude la candela di impulso stessa (poi["impulse_index"]): quella
    candela PARTE dalla zona per definizione (e' l'impulso che la crea),
    il suo wick che tocca il bordo non e' un "ritorno" -- e' la stessa
    formazione. Un vero test comincia SOLO dalla candela successiva
    all'impulso.
    """
    start_idx = poi.get("impulse_index", poi.get("index", 0)) + 1
    after = df_h1.iloc[start_idx:]
    if len(after) == 0:
        return False
    zh, zl = poi["zone_high"], poi["zone_low"]
    lows = after["low"].astype(float).values
    highs = after["high"].astype(float).values
    for lo, hi in zip(lows, highs):
        if lo <= zh and hi >= zl:
            return True
    return False


def select_best_poi(df_h1: pd.DataFrame, asset: str, direction_4h: str,
                    current_price: float) -> dict | None:
    """
    Entry point principale. Seleziona la POI PIU' SIGNIFICATIVA coerente
    col bias 4H (spec sezione 3): BULLISH -> solo Demand, BEARISH -> solo
    Supply. Non tutte le zone -- solo la migliore per qualita', ancora
    rilevante rispetto al prezzo corrente (non gia' superata).
    """
    if direction_4h not in ("BULLISH", "BEARISH"):
        return None

    wanted_type = "DEMAND" if direction_4h == "BULLISH" else "SUPPLY"

    pois = _detect_pois(df_h1, asset)
    pois = [p for p in pois if p["poi_type"] == wanted_type]
    if not pois:
        return None

    fvgs = _detect_fvg(df_h1)

    valid = []
    for poi in pois:
        if wanted_type == "DEMAND" and current_price < poi["zone_low"]:
            continue
        if wanted_type == "SUPPLY" and current_price > poi["zone_high"]:
            continue
        is_tested = _poi_is_tested(poi, df_h1)
        quality = _poi_quality(poi, fvgs, is_tested)
        poi_full = dict(poi, quality_score=quality, is_tested=is_tested)
        valid.append(poi_full)

    if not valid:
        return None

    # A parita' di quality_score, preferisci la POI PIU' VICINA al
    # prezzo attuale -- e' quella davvero raggiungibile per un Early
    # Signal. max() su un pareggio terrebbe la prima della lista
    # (cronologicamente la piu' vecchia, spesso la piu' lontana),
    # scartando una POI altrettanto valida ma molto piu' vicina
    # (bug trovato il 22/08: 132pt scelta invece di 15.5pt, stessa
    # qualita' 6/10).
    def _distance_from_price(p):
        if wanted_type == "DEMAND":
            return abs(current_price - p["zone_high"])
        return abs(current_price - p["zone_low"])

    return max(valid, key=lambda p: (p["quality_score"], -_distance_from_price(p)))
