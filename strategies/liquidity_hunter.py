"""
strategies/liquidity_hunter.py
Liquidity Hunter v3.2 — Confluence Engine

FILOSOFIA: gli engine non bloccano il trade, contribuiscono a determinarne
probabilita' e qualita'. La decisione nasce dalla combinazione.

    v2.0 = 6 gate obbligatori -> 0 segnali in 5 giorni (misurato)
    v3.0 = punteggio di confluenza, 9 fattori binari
    v3.1 = punteggio GRADUATO + anticipazione del setup

RUOLI (uno per concetto):
    Order Block  -> ZONA di ingresso
    FVG          -> QUALITA' della zona (sovrapposizione, distanza, purezza)
    Candlestick  -> il mercato REAGISCE (qualita' del pattern, non solo direzione)
    Liquidity    -> SWEEP (con recenza) e TARGET (con spazio disponibile)
    Reaction Map -> CONFLUENZA complessiva
    Structure    -> trend, premium/discount

ANTICIPAZIONE (v3.1):
    Un setup non nasce quando il prezzo tocca la zona: nasce prima.
        prezzo NELLA zona  -> TRIGGERED, entry a mercato
        prezzo VICINO      -> WATCHING, entry PENDENTE al bordo della zona
        prezzo lontano     -> nessun setup
    Perche' pendente e non a mercato: misurato sui dati, entrando a mercato
    con prezzo a 0.5-1% dalla zona il rischio passa da 2.6 a 10.8 ATR
    (lo stop resta al punto di invalidazione strutturale). L'ordine pendente
    al bordo della zona mantiene il rischio corretto E anticipa il setup.

STOP LOSS: strutturale, oltre l'Order Block + buffer ATR.
    OB rialzista -> stop SOTTO la zona: se il prezzo chiude li', il supporto
    ha ceduto e la tesi e' morta. Mai stretto per far tornare l'RR.

TAKE PROFIT: scala a 2 livelli strutturali + livelli di liquidita' informativi.
    TP1 = primo target strutturale vicino (OB opposto / FVG / zona RM)
    TP2 = secondo target strutturale (se disponibile)
    Livelli di liquidita' (Equal Lows/Highs): solo informativi, non TP.
    Se non esiste nessun target strutturale: il segnale NON viene emesso.

TRACCIAMENTO: `tp` resta TP1, cosi' lh_db e il Decision Ledger continuano a
    funzionare e la serie storica degli esiti non si rompe.

PESI: tutti i fattori valgono al massimo 1 punto. Deliberato — non abbiamo
    dati per pesarli diversamente e pesi inventati inquinerebbero proprio i
    dati che servono a calibrarli. `confluence_factors` registra il valore di
    ogni fattore in ogni segnale: sara' quello a permettere la calibrazione.

NON usiamo ob.quality_score: misurato sul Ledger, order_block_conf alta rende
    +0.068R contro +0.483R della bassa (invertito, p=0.048, consistente su
    BTC/XAU e BUY/SELL). Usiamo fatti verificabili, non quel giudizio.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

import pandas as pd

logger = logging.getLogger("liquidity_hunter")

STRATEGY_NAME    = "LH"
STRATEGY_VERSION = "v3.2"

OB_PROXIMITY_PCT = 0.010     # distanza max dell'OB dal prezzo

# CALIBRAZIONE 23/07/2026 — soglie misurate sulla distribuzione REALE dei
# punteggi (133 setup ricostruiti dal DB), non scelte a priori.
#   distribuzione: min 1.90, mediana 3.15, max 6.30
#   con le soglie iniziali (MED 5.0 / HIGH 6.5): HIGH 0%, MEDIUM 13%, LOW 87%
#   -> HIGH era irraggiungibile e quasi tutto finiva LOW (quindi non notificato)
# Il punteggio massimo teorico e' 9, ma nella pratica diversi fattori
# contribuiscono di rado (candlestick ~0, reaction_map basso su XAU), quindi
# la scala utile si ferma intorno a 6.3. Le soglie seguono quella scala.
MIN_SCORE        = 3.5       # su 9 — sotto, il setup e' debole (solo per il TRADE)
QUALITY_HIGH_MIN = 5.0
QUALITY_MED_MIN  = 4.2

ASSET_PARAMS = {
    "BTC_USDT": {
        "sl_buffer_atr": 0.5, "min_rr": 1.0, "expiry_bars": 12,
        "max_zone_atr": 2.0, "min_zone_atr": 0.25, "tp1_max_atr": 3.0,
        "watch_max_atr": 1.5,
        "liq_tight_atr": 3.0,
        "liq_ample_atr": 10.0,
        # Restart Zone Engine (v3.5) -- configurabili, da ottimizzare
        # quando ci sara' campione reale. Nessuno di questi e' stato
        # validato su dati storici, sono punti di partenza ragionevoli.
        "launch_body_ratio": 0.5,          # soglia "candela decisa" (M5)
        "min_zone_width_points": 20,        # v3.9: sotto questa ampiezza
                                            # la zona e' allargata a questo
                                            # minimo -- una candela M5 col
                                            # corpo minuscolo puo' produrre
                                            # una zona quasi puntiforme,
                                            # inutilizzabile. Non calibrato
                                            # su dati storici.
        "zone_merge_tolerance_points": 50, # sotto questa distanza, due
                                            # Restart Zone della stessa
                                            # direzione vengono fuse
        "min_impulse_atr": 0.8,        # UNICO vero filtro: forza minima
                                        # dell'impulso M15 per considerarlo
        "impulse_lookback_bars": 16,   # ~4h su M15: quanto indietro cercare
        "max_zones_per_scan": 5,       # tetto per ciclo (post-merge, le
                                        # migliori N per punteggio) -- non
                                        # e' un filtro di qualita', solo
                                        # anti-inondazione
        # v3.6: detection multi-timeframe -- H1/M30 trovano l'impulso
        # (piu' significativo, meno rumore), M15+M5 raffinano la zona
        # precisa. Nessuna soglia calibrata su dati storici.
        "min_impulse_atr_h1": 1.0,
        "impulse_lookback_bars_h1": 12,   # 12h
        "min_impulse_atr_m30": 0.9,
        "impulse_lookback_bars_m30": 16,  # 8h
        # v3.7: ricorrenza -- quante volte da questa zona e' REALMENTE
        # ripartito un impulso, non solo quante volte il prezzo l'ha
        # toccata. Nessun valore calibrato su dati storici.
        "recurrence_confirmation_bars": 6,          # ~1.5h su M15
        "recurrence_invalidate_after_failures": 2,
        "recurrence_max_age_days": 14,    # zona mai rivisitata dopo 14
                                           # giorni -> STALE (il mercato
                                           # e' andato avanti, non spreca
                                           # piu' cicli di monitoraggio)
    },
    "XAU_USD": {
        "sl_buffer_atr": 0.3, "min_rr": 1.2, "expiry_bars": 18,
        "max_zone_atr": 2.0, "min_zone_atr": 0.25, "tp1_max_atr": 3.0,
        "watch_max_atr": 1.5,
        "liq_tight_atr": 3.0,
        "liq_ample_atr": 10.0,
        "launch_body_ratio": 0.5,
        "min_zone_width_points": 4,   # v3.9: sotto 4$ la zona viene
                                       # allargata a questo minimo
        "zone_merge_tolerance_points": 10,
        "min_impulse_atr": 0.8,
        "impulse_lookback_bars": 16,
        "max_zones_per_scan": 5,
        "min_impulse_atr_h1": 1.0,
        "impulse_lookback_bars_h1": 12,
        "min_impulse_atr_m30": 0.9,
        "impulse_lookback_bars_m30": 16,
        "recurrence_confirmation_bars": 6,
        "recurrence_invalidate_after_failures": 2,
        "recurrence_max_age_days": 14,
    },
}
DEFAULT_PARAMS = ASSET_PARAMS["BTC_USDT"]

# Bonus/penalita' per la ricorrenza -- applicati DOPO lo scoring base.
# Nessun valore calibrato su dati storici, punto di partenza da tarare.
RECURRENCE_BONUS_PER_RESTART = 15
RECURRENCE_BONUS_MAX = 45          # tetto: 3+ restart confermati
RECURRENCE_PENALTY_PER_FAILURE = 10
RECURRENCE_PENALTY_MAX = 30

ALLOWED_SESSIONS = {
    "XAU_USD":  ("ASIA", "LONDON", "NEW_YORK", "OVERLAP"),
    "BTC_USDT": ("LONDON", "NEW_YORK", "OVERLAP"),
}


def _params(asset: str) -> dict:
    return ASSET_PARAMS.get(asset, DEFAULT_PARAMS)


def _get_session(now: datetime) -> str:
    t = now.hour * 60 + now.minute
    if 7 * 60 <= t < 12 * 60:
        return "LONDON"
    if 12 * 60 <= t < 13 * 60 + 30:
        return "OVERLAP"
    if 13 * 60 + 30 <= t <= 21 * 60:
        return "NEW_YORK"
    return "ASIA"


def _reject(reason: str, zone: dict = None) -> dict:
    logger.info("LH: REJECT %s", reason)
    return {"signal": None, "diagnostics": {"rejection": reason, "zone": zone}}


def _clamp(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


def _overlap(h1, l1, h2, l2) -> float:
    """Frazione di sovrapposizione tra due zone (0-1)."""
    ov = max(0.0, min(h1, h2) - max(l1, l2))
    smaller = min(h1 - l1, h2 - l2)
    return ov / smaller if smaller > 0 else 0.0


# ============================================================
# Order Block — la ZONA di ingresso
# ============================================================

def _find_best_ob(mie_context: dict, want_dir: str, price: float,
                  max_zone_atr: float, min_zone_atr: float, atr: float) -> Optional[dict]:
    """OB piu' vicino nella direzione, entro OB_PROXIMITY_PCT.
    Priorita' FRESH > TESTED > MITIGATED > BREAKER, poi distanza.
    Zone troppo larghe o troppo strette scartate: le prime hanno entry
    impreciso e rischio ingestibile, le seconde sono rumore di prezzo,
    non struttura (soglia 0.25 ATR misurata sulla distribuzione reale
    degli OB in signals.db — gap naturale tra 0.21 e 0.33)."""
    prio = {"FRESH": 0, "TESTED": 1, "MITIGATED": 2, "BREAKER": 3}
    best, best_d, best_p = None, None, 99
    for ob in (mie_context.get("mie_order_block_order_blocks") or []):
        if ob.get("direction") != want_dir or ob.get("status") not in prio:
            continue
        zh, zl = ob.get("zone_high"), ob.get("zone_low")
        if zh is None or zl is None:
            continue
        zh, zl = float(zh), float(zl)
        if atr > 0:
            zone_atr = abs(zh - zl) / atr
            if zone_atr > max_zone_atr or zone_atr < min_zone_atr:
                continue
        mid = (zh + zl) / 2
        d = abs(price - mid) / price if price > 0 else 1
        if d > OB_PROXIMITY_PCT:
            continue
        p = prio[ob["status"]]
        if p < best_p or (p == best_p and (best_d is None or d < best_d)):
            best, best_d, best_p = ob, d, p
    return best


def _ob_position(ob: dict, high: float, low: float, price: float,
                 atr: float, watch_max_atr: float) -> tuple:
    """
    Dove si trova il prezzo rispetto alla zona OB?
    Ritorna (stato, distanza_in_atr):
        TRIGGERED  -> la candela tocca/entra nella zona: entry a mercato
        WATCHING   -> vicino (entro watch_max_atr): entry PENDENTE al bordo
        FAR        -> lontano: nessun setup
    """
    zh, zl = float(ob["zone_high"]), float(ob["zone_low"])
    if low <= zh and high >= zl:
        return ("TRIGGERED", 0.0)
    d = min(abs(price - zh), abs(price - zl))
    d_atr = d / atr if atr > 0 else 999
    if d_atr <= watch_max_atr:
        return ("WATCHING", d_atr)
    return ("FAR", d_atr)


# ============================================================
# Punteggio di confluenza — GRADUATO (0..1 per fattore)
# ============================================================

def _score_confluence(direction: str, want_dir: str, ob: dict,
                      mie_context: dict, entry: float, atr: float,
                      session: str, asset: str, params: dict) -> tuple:
    """Ritorna (score, factors). Ogni fattore vale da 0 a 1."""
    f = {}
    zh, zl = float(ob["zone_high"]), float(ob["zone_low"])
    up = direction == "BUY"

    # 1. OB formato in un trend coerente (doc 005, passo 1)
    f["ob_trend_aligned"] = 1.0 if ob.get("trend_at_formation") == want_dir else 0.0

    # 2. Freschezza dell'OB — graduata: piu' e' vergine, meglio e'
    f["ob_freshness"] = {"FRESH": 1.0, "TESTED": 0.5,
                          "MITIGATED": 0.25, "BREAKER": 0.0}.get(ob.get("status"), 0.0)

    # 3. Premium/Discount — equilibrium vale meta'
    pdz = mie_context.get("mie_structure_premium_discount") or {}
    zone = pdz.get("zone", "EQUILIBRIUM") if isinstance(pdz, dict) else "EQUILIBRIUM"
    if (up and zone == "DISCOUNT") or (not up and zone == "PREMIUM"):
        f["premium_discount"] = 1.0
    elif zone == "EQUILIBRIUM":
        f["premium_discount"] = 0.5
    else:
        f["premium_discount"] = 0.0

    # 4. Reaction Map — graduata sul confluence_score (50->0, 90->1)
    want_reaction = "BOUNCE_UP" if up else "BOUNCE_DOWN"
    rm = 0.0
    for key in ("mie_reaction_map_strongest_below", "mie_reaction_map_strongest_above"):
        z = mie_context.get(key)
        if isinstance(z, dict) and z.get("expected_reaction") == want_reaction:
            rm = max(rm, _clamp((z.get("confluence_score", 0) - 50) / 40.0))
    f["reaction_map"] = rm

    # 5. FVG — qualita' della zona (sovrapposizione, distanza, purezza)
    fvg = mie_context.get("mie_fvg_nearest_open_bullish" if up
                          else "mie_fvg_nearest_open_bearish")
    fv = 0.0
    if isinstance(fvg, dict) and fvg.get("status") in ("OPEN", "PARTIALLY_FILLED"):
        fv += 0.25                                   # esiste un gap aperto
        if fvg.get("during_displacement"):
            fv += 0.25                               # nato da impulso (criterio doc)
        fzh, fzl = fvg.get("zone_high"), fvg.get("zone_low")
        if fzh is not None and fzl is not None:
            fzh, fzl = float(fzh), float(fzl)
            ov = _overlap(zh, zl, fzh, fzl)
            if ov > 0:
                fv += 0.25 * _clamp(ov)              # sovrapposta all'OB
            elif atr > 0:
                gap = min(abs(zl - fzh), abs(fzl - zh))
                fv += 0.15 * _clamp(1 - gap / (2 * atr))   # vicina all'OB
        fill = float(fvg.get("fill_percentage") or 0)
        fv += 0.25 * _clamp(1 - fill / 100.0)        # ancora "pulita"
    f["fvg_quality"] = _clamp(fv)

    # 6. Sweep di liquidita' — conta la RECENZA, non la presenza
    sw = 0.0
    for lv in (mie_context.get("mie_liquidity_levels") or []):
        if not lv.get("swept"):
            continue
        ba = lv.get("swept_bars_ago")
        if ba is None:
            sw = max(sw, 0.3)
        else:
            sw = max(sw, _clamp(1 - float(ba) / 20.0))
    f["liquidity_sweep"] = sw

    # 7. Candlestick — qualita', zona, e se nasce sull'OB
    cs_dir = mie_context.get("mie_candlestick_strongest_direction")
    cs = 0.0
    if mie_context.get("mie_candlestick_has_confirmation"):
        if (up and cs_dir == "BULLISH") or (not up and cs_dir == "BEARISH"):
            cs += 0.45
            pq = float(mie_context.get("mie_candlestick_pattern_quality_score") or 0)
            cs += 0.30 * _clamp(pq / 100.0)
            if mie_context.get("mie_candlestick_in_reaction_zone"):
                cs += 0.10
            zsc = mie_context.get("mie_candlestick_zone_confluence_score")
            if zsc is not None and float(zsc) >= 70:
                cs += 0.15
        elif cs_dir:
            cs = -0.25
    f["candlestick"] = round(cs, 3)

    # 8. Spazio davanti al trade
    targets = mie_context.get("mie_liquidity_buy_targets" if up
                              else "mie_liquidity_sell_targets") or []
    dists = [abs(float(t["price"]) - entry) / atr
             for t in targets if t.get("price") and atr > 0]
    if not dists:
        f["liquidity_space"] = 0.3
    else:
        nearest = min(dists)
        tight = params.get("liq_tight_atr", 3.0)
        ample = params.get("liq_ample_atr", 10.0)
        if nearest < tight:
            f["liquidity_space"] = 0.0
        else:
            f["liquidity_space"] = _clamp((nearest - tight) / (ample - tight))

    # 9. Sessione attiva per l'asset
    f["session_active"] = 1.0 if session in ALLOWED_SESSIONS.get(asset, ()) else 0.0

    return round(sum(f.values()), 2), {k: round(v, 3) for k, v in f.items()}


def _quality_label(score: float) -> str:
    if score >= QUALITY_HIGH_MIN:
        return "HIGH"
    if score >= QUALITY_MED_MIN:
        return "MEDIUM"
    return "LOW"


# ============================================================
# Take Profit — scala strutturale + livelli liquidita' informativi
# ============================================================

def _build_tp_ladder(direction: str, entry: float, risk: float, atr: float,
                     mie_context: dict, params: dict) -> dict:
    """
    Costruisce i target separando livelli STRUTTURALI (operativi) da
    livelli di LIQUIDITA' (informativi).

    Ritorna {
        "structural": [(price, label), ...],   # max 2, TP operativi
        "liquidity":  [(price, label), ...],   # informativi, Equal Lows/Highs etc.
    }

    CAMBIAMENTO v3.2: i livelli di liquidita' (Equal Lows/Highs) non sono
    piu' etichettati come TP2/TP3. Sono target potenziali a lungo termine
    (mediana 29 ATR su BTC, 12.6 su XAU) incompatibili con la durata di
    un trade LH (6-18 barre). Vengono mostrati come informazione di
    contesto nella notifica, non come obiettivi operativi.
    """
    up = direction == "BUY"
    tp1_max = params.get("tp1_max_atr", 3.0) * atr if atr > 0 else 0

    def ahead(p): return p > entry if up else p < entry
    def near_ok(p): return not tp1_max or abs(p - entry) <= tp1_max

    # ── Target strutturali: OB opposto, FVG, zona RM ────────
    near = []
    opp = "BEARISH" if up else "BULLISH"
    for ob in (mie_context.get("mie_order_block_order_blocks") or []):
        if ob.get("direction") != opp or ob.get("status") == "EXPIRED":
            continue
        mid = ob.get("zone_midpoint")
        if mid is None:
            zh, zl = ob.get("zone_high"), ob.get("zone_low")
            if zh is None or zl is None:
                continue
            mid = (float(zh) + float(zl)) / 2
        mid = float(mid)
        if ahead(mid) and near_ok(mid):
            near.append((mid, f"OB_{str(ob.get('id','?'))[:4]}"))

    fvg = mie_context.get("mie_fvg_nearest_open_bearish" if up
                          else "mie_fvg_nearest_open_bullish")
    if isinstance(fvg, dict):
        for edge in ("zone_low", "zone_high"):
            v = fvg.get(edge)
            if v and ahead(float(v)) and near_ok(float(v)):
                near.append((float(v), "FVG")); break

    for key in ("mie_reaction_map_strongest_above", "mie_reaction_map_strongest_below"):
        z = mie_context.get(key)
        if isinstance(z, dict):
            mid = z.get("zone_midpoint")
            if mid and ahead(float(mid)) and near_ok(float(mid)):
                near.append((float(mid), "RM_ZONE"))

    structural = []
    if near:
        near.sort(key=lambda t: abs(t[0] - entry))
        for tp_candidate in near[:2]:
            structural.append(tp_candidate)

    structural = [(round(p, 4), l) for p, l in structural]

    # ── Livelli di liquidita': informativi, NON target operativi ──
    liq = mie_context.get("mie_liquidity_buy_targets" if up
                          else "mie_liquidity_sell_targets") or []
    liq_levels = sorted(
        [(round(float(t["price"]), 4), t.get("label", "LIQ")) for t in liq
         if t.get("price") and ahead(float(t["price"]))],
        key=lambda t: abs(t[0] - entry),
    )

    return {"structural": structural, "liquidity": liq_levels}


# ============================================================
# RESTART ZONE ENGINE (v3.4) — informativo, separato dal segnale di trading
#
# CAMBIO DI APPROCCIO rispetto a v3.3 (che leggeva Reaction Map):
# l'utente ha chiesto di ragionare "impulso-first" invece che "SMC-first".
#
# Verificato leggendo order_block_engine.py: l'Order Block Engine e' GIA'
# impulso-first (trova prima la candela di impulso >= 1 ATR, POI cammina
# indietro fino a 5 candele per trovare l'ultima candela opposta -- quella
# diventa l'OB). Non serve reinventare questa parte: la riusiamo leggendo
# gli OB gia' calcolati da mie_context, invece di duplicare la detection.
#
# Il problema reale non era "SMC crea la zona invece dell'impulso" -- era
# che la zona finale usa l'INTERO range wick-to-wick della candela M15
# opposta (order_block_engine.py righe ~361-362): su un asset volatile
# una singola candela M15 puo' essere larga 40-60$, esattamente il caso
# segnalato (zona 4224-4282 su XAU).
#
# FIX: per ogni OB attivo, raffiniamo zone_high/zone_low scendendo alle
# candele M5 che compongono quella candela M15 -- troviamo il preciso
# punto di lancio (l'estremo reale + il corpo della candela M5 che lo
# contiene), invece dell'intera candela M15. SMC (has_bos, has_sweep_before,
# has_fvg, Reaction Map) entra SOLO come punteggio di conferma sulla zona
# gia' raffinata -- mai per crearla, come richiesto.
#
# ONESTA' SUI LIMITI:
#   - Se le candele M5 per quella finestra non sono disponibili (gap dati),
#     si ricade sulla zona M15 intera (non raffinata) e lo si segnala
#     esplicitamente (campo "m5_refined": False) invece di fingere
#     precisione che non c'e'.
#   - La regola di raffinamento (candela M5 con l'estremo + il suo corpo)
#     e' una scelta di design ragionevole, non validata su dati storici --
#     va verificata quando ci sara' campione (le zone sono davvero piu'
#     precise E utili, o troppo strette per essere raggiunte?).


# ============================================================
# Distanza massima per l'alert di zona: deliberatamente PIU' AMPIA di
# watch_max_atr (1.5, usato dal segnale di trading). Il trading vuole
# essere vicino per rischio/precisione; l'alert informativo vuole avvisare
# prima, quando la zona e' ancora lontana. Nessun dato storico dietro
# questi numeri -- punto di partenza da tarare.
#
# Per ASSET, non un valore globale: con ATR M15 XAU ~3.5-4 punti, una
# finestra di 3.0 ATR sarebbe solo ~11-12 punti -- piu' STRETTA della
# soglia "vicinissima" (15 punti, vedi ZONE_SCAN_NEAR_POINTS). I due
# livelli di avviso collasserebbero in uno solo su XAU.
ZONE_SCAN_MAX_ATR = {
    "BTC_USDT": 3.0,
    "XAU_USD":  6.0,
}
ZONE_SCAN_MAX_ATR_DEFAULT = 3.0

# Secondo avviso, piu' urgente: quando il prezzo e' a pochi punti dalla
# zona (punti assoluti, non ATR -- qui conta la precisione di entrata).
ZONE_SCAN_NEAR_POINTS = {
    "BTC_USDT": 75,
    "XAU_USD":  15,
}


def _child_window(df_child: pd.DataFrame, parent_ts, parent_duration_ms: int,
                  lookback_extra_ms: int) -> pd.DataFrame:
    """
    Generalizzazione di "candele figlie dentro una candela madre": ritorna
    le candele df_child che compongono la candela iniziata a parent_ts
    con durata parent_duration_ms, allargata di lookback_extra_ms PRIMA
    dell'inizio -- il vero punto di svolta puo' cadere al bordo.

    Usata sia per M5 dentro M15 (parent_duration_ms=15min) sia per M15
    dentro M30/H1 (parent_duration_ms=30min/60min) -- stessa funzione,
    stessa regola, applicata a qualunque livello.
    """
    try:
        ts0 = int(float(parent_ts))
    except (TypeError, ValueError):
        return df_child.iloc[0:0]
    ts_start = ts0 - lookback_extra_ms
    ts_end = ts0 + parent_duration_ms
    mask = (df_child["timestamp"] >= ts_start) & (df_child["timestamp"] < ts_end)
    return df_child[mask].sort_values("timestamp")


def _m5_window_for_m15_candle(df_m5: pd.DataFrame, formation_ts, lookback_extra_ms: int = 5*60*1000) -> pd.DataFrame:
    """Alias retrocompatibile: M5 dentro una candela M15 (15 min)."""
    return _child_window(df_m5, formation_ts, 15*60*1000, lookback_extra_ms)


def _is_accelerating_candle(row, direction: str, body_ratio_threshold: float) -> bool:
    """
    Definizione OGGETTIVA di "candela in accelerazione": deterministica,
    stessa regola applicata sempre, nessuna interpretazione caso per caso.

    Vera SOLO se ENTRAMBE le condizioni sono soddisfatte:
      1. Direzione: bullish per zona BULLISH, bearish per zona BEARISH.
      2. Corpo deciso: |close-open| / (high-low) >= body_ratio_threshold
         (configurabile per asset in ASSET_PARAMS["launch_body_ratio"]).

    Un piccolo rimbalzo nella direzione giusta ma dominato da wick (corpo
    sotto la soglia) NON conta come accelerazione -- e' rumore, non
    convinzione.
    """
    o, h, l, c = float(row["open"]), float(row["high"]), float(row["low"]), float(row["close"])
    rng = h - l
    if rng <= 0:
        return False
    body_ratio = abs(c - o) / rng

    if direction == "BULLISH":
        return c > o and body_ratio >= body_ratio_threshold
    else:
        return c < o and body_ratio >= body_ratio_threshold


def _find_launch_candle(window, direction: str, body_ratio_threshold: float):
    """
    Trova la candela M5 da cui e' REALMENTE partita l'accelerazione --
    non necessariamente quella con l'estremo assoluto (che puo' essere
    solo uno spike/stop-hunt rientrato nella stessa candela), e non
    necessariamente la prima candela di colore giusto incontrata (che
    puo' essere solo un piccolo rimbalzo insignificante).

    REGOLA OGGETTIVA (sempre la stessa): si cammina all'INDIETRO dalla
    fine della finestra M5 e si prende la PRIMA candela incontrata che
    NON e' "in accelerazione" secondo _is_accelerating_candle.

    Se OGNI candela nella finestra risulta "in accelerazione", fallback
    sull'estremo assoluto.
    """
    rows = list(window.iterrows())
    for _, row in reversed(rows):
        if not _is_accelerating_candle(row, direction, body_ratio_threshold):
            return row

    if direction == "BULLISH":
        idx = window["low"].astype(float).idxmin()
    else:
        idx = window["high"].astype(float).idxmax()
    return window.loc[idx]


# ============================================================
# RILEVAMENTO DIRETTO DELL'IMPULSO (v3.5)
#
# CAMBIO RISPETTO A v3.4: prima le Restart Zone nascevano SOLO dagli
# Order Block gia' registrati da order_block_engine.py -- che ha una sua
# logica interna (lookback limitato, conteggio massimo, propri criteri
# di scadenza). Se quell'engine non aveva gia' un OB attivo in quel
# momento, la zona non esisteva per LH, indipendentemente da quanto
# fosse forte l'impulso reale sul grafico. Causa diretta dello zero
# notifiche del 07/08.
#
# ORA: LH rileva l'impulso DA SOLO, direttamente dalle candele M15,
# senza dipendere dal registro OB di un altro engine. L'UNICO vero
# filtro e' la forza dell'impulso stesso (min_impulse_atr) -- non SMC.
# Order Block, FVG (via Reaction Map), BOS, sweep diventano bonus di
# SOVRAPPOSIZIONE sulla zona gia' trovata: "questa zona coincide anche
# con un OB/zona Reaction Map registrati? Punti in piu'." Mai un
# prerequisito per l'esistenza della zona.
# ============================================================

def _detect_impulses(df: pd.DataFrame, atr: float, min_impulse_atr: float,
                     lookback_bars: int, duration_ms: int, timeframe_label: str) -> list:
    """
    Rileva candele di impulso reale nelle ultime lookback_bars barre, su
    QUALUNQUE timeframe (M15, M30, H1 -- stessa regola, generalizzata da
    _detect_m15_impulses). UNICO filtro: |close-open| >= min_impulse_atr
    * ATR -- la forza del movimento stesso, non un giudizio SMC.

    duration_ms: durata di una barra di questo timeframe (15/30/60 min in
    ms) -- serve al drill-down successivo per sapere quanto e' "larga"
    la candela di impulso trovata.

    Ritorna lista di {"index", "timestamp", "direction", "displacement_atr",
    "duration_ms", "timeframe"}.
    """
    if atr <= 0 or len(df) == 0:
        return []

    n = len(df)
    start = max(0, n - lookback_bars)
    impulses = []

    for i in range(start, n):
        row = df.iloc[i]
        o, c = float(row["open"]), float(row["close"])
        body = abs(c - o)
        disp_atr = body / atr
        if disp_atr < min_impulse_atr:
            continue
        impulses.append({
            "index": i,
            "timestamp": int(row["timestamp"]),
            "direction": "BULLISH" if c > o else "BEARISH",
            "displacement_atr": round(disp_atr, 3),
            "duration_ms": duration_ms,
            "timeframe": timeframe_label,
        })

    return impulses


def _manual_atr(df: pd.DataFrame, period: int = 14) -> float:
    """
    ATR calcolato a mano sulle ultime `period` candele -- stesso fallback
    gia' usato per M15 quando mie_context non ha l'ATR pronto. Usato per
    H1/M30 dato che mie_context oggi espone solo l'ATR M15
    (mie_volatility_atr_m15), non verificato se H1/M30 sono disponibili.
    """
    if df is None or len(df) < period + 1:
        return 0.0
    h = df["high"].astype(float).values
    l = df["low"].astype(float).values
    c = df["close"].astype(float).values
    return sum(max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1]))
              for i in range(-period, 0)) / period


def _zone_from_impulse(impulse: dict, df_m15: pd.DataFrame, df_m5, body_ratio_threshold: float) -> tuple:
    """
    Costruisce la Restart Zone per un impulso rilevato su H1/M30/M15.

    DRILL-DOWN a piu' livelli (v3.6): se l'impulso viene da H1 o M30
    (timeframe "medio/forte" per trovarlo), prima si RESTRINGE a M15
    (si trova la candela M15 di lancio dentro la finestra H1/M30, stessa
    regola oggettiva _find_launch_candle), poi si RAFFINA ulteriormente
    su M5 dentro quella specifica candela M15. Stessa identica funzione
    (_find_launch_candle) applicata fractalmente a due livelli.

    FALLBACK a catena: se M5 non c'e', resta la precisione M15 (la
    candela M15 di lancio trovata, range intero). Se nemmeno M15 e'
    disponibile per il drill-down, fallback sull'intera candela H1/M30
    originale (raro, solo se anche i dati M15 mancano).

    Ritorna (zone_high, zone_low, refined: bool). refined=True solo se
    si e' arrivati fino a M5.
    """
    direction = impulse["direction"]
    ts = impulse["timestamp"]
    duration_ms = impulse.get("duration_ms", 15*60*1000)
    timeframe = impulse.get("timeframe", "M15")

    # ── Livello 1: se l'impulso viene da H1/M30, restringo a M15 ──
    m15_ts = ts
    m15_row = None
    if timeframe in ("H1", "M30") and df_m15 is not None and len(df_m15) > 0:
        m15_window = _child_window(df_m15, ts, duration_ms, lookback_extra_ms=15*60*1000)
        if len(m15_window) > 0:
            m15_core = _find_launch_candle(m15_window, direction, body_ratio_threshold)
            m15_ts = int(m15_core["timestamp"])
            m15_row = m15_core

    # ── Livello 2: raffino su M5 dentro la candela M15 individuata ──
    if df_m5 is not None and len(df_m5) > 0:
        m5_window = _child_window(df_m5, m15_ts, 15*60*1000, lookback_extra_ms=5*60*1000)
        if len(m5_window) > 0:
            core = _find_launch_candle(m5_window, direction, body_ratio_threshold)
            if direction == "BULLISH":
                zone_low = float(core["low"])
                zone_high = max(float(core["open"]), float(core["close"]))
                if zone_high <= zone_low:
                    zone_high = zone_low + 0.01
            else:
                zone_high = float(core["high"])
                zone_low = min(float(core["open"]), float(core["close"]))
                if zone_low >= zone_high:
                    zone_low = zone_high - 0.01
            return round(zone_high, 4), round(zone_low, 4), True

    # ── Fallback 1: precisione M15 (se avevo gia' trovato la candela di lancio) ──
    if m15_row is not None:
        zh, zl = float(m15_row["high"]), float(m15_row["low"])
        if zh > zl:
            return round(zh, 4), round(zl, 4), False

    # ── Fallback 2: candela immediatamente prima dell'impulso originale ──
    idx = impulse.get("index", 0)
    fallback_idx = idx - 1 if idx > 0 else idx
    src_df = df_m15  # ultima risorsa: usa comunque M15 se disponibile
    if src_df is None or fallback_idx < 0 or fallback_idx >= len(src_df):
        return None, None, False
    fb = src_df.iloc[fallback_idx]
    zone_high = float(fb["high"])
    zone_low = float(fb["low"])
    if zone_high <= zone_low:
        return None, None, False
    return round(zone_high, 4), round(zone_low, 4), False


def _ranges_overlap(low1, high1, low2, high2) -> bool:
    """
    ATTENZIONE (trovato 08/08, verificato con test): questo era un
    controllo di CONTATTO tecnico (anche un solo punto in comune conta
    come sovrapposizione). Con una Restart Zone piccola (4-10$) e un OB
    largo (spesso 40-60$, l'intera candela M15), bastava toccare il
    bordo ESTREMO dell'OB -- dalla parte opposta a dove probabilmente
    era nato il vero impulso -- per ottenere comunque il bonus pieno.
    Verificato concretamente: zona [4278,4282] contro OB [4224,4282]
    otteneva "sovrapposizione" e bonus OB-FRESH+FVG, pur toccando l'OB
    solo per un pelo, a 58$ dal suo vero punto di lancio.

    Ora richiede sovrapposizione SOSTANZIALE, non solo contatto: il
    CENTRO della Restart Zone deve cadere dentro l'altra zona. Garantisce
    che il punto di lancio individuato sia davvero dentro la zona di
    confronto, non solo al suo margine estremo.
    """
    mid1 = (low1 + high1) / 2
    return low2 <= mid1 <= high2


def _is_decelerating(df_m15: pd.DataFrame, lookback: int = 3) -> bool:
    """
    Le ultime `lookback` candele M15 mostrano corpi piu' piccoli delle
    `lookback` precedenti? Stesso principio gia' usato dal Market Radar
    (contrazione = perdita di forza dell'impulso) -- riusato qui, non
    reinventato. Un impulso che rallenta avvicinandosi alla zona ha piu'
    probabilita' di rispettarla che di sfondarla.
    """
    if df_m15 is None or len(df_m15) < lookback * 2:
        return False
    bodies = (df_m15["close"].astype(float) - df_m15["open"].astype(float)).abs()
    recent_avg = bodies.iloc[-lookback:].mean()
    prior_avg = bodies.iloc[-lookback*2:-lookback].mean()
    if prior_avg <= 0:
        return False
    return recent_avg < prior_avg * 0.7  # almeno 30% di contrazione


def _score_restart_zone(zone_high: float, zone_low: float, displacement_atr: float,
                        mie_context: dict, refined: bool,
                        zone_kind: str = None, df_m15: pd.DataFrame = None,
                        swing_zones: list = None) -> tuple:
    """
    Punteggio di ARRICCHIMENTO (0-100) sulla zona gia' individuata
    dall'impulso. Nessuno di questi fattori puo' eliminare la zona --
    solo alzarne il punteggio.

    v3.12: aggiunto swing_zones (lista di dict dalla tabella
    lh_swing_zones) -- se la zona coincide con un vecchio swing H4/D1,
    e' una confluenza strutturale forte (quel livello e' guardato da
    molti piu' partecipanti di un OB M15). Bonus proporzionato al
    timeframe: D1 (+15) > H4 (+10). Solo il match piu' alto conta.
    """
    factors = []
    score = 0.0

    # Forza dell'impulso -- componente primaria, sempre presente
    disp_score = min(displacement_atr / 2.5, 1.0) * 30.0
    score += disp_score
    factors.append(f"IMPULSO({displacement_atr:.1f}ATR)")

    # Sovrapposizione con un Order Block registrato (qualunque stato
    # tranne INVALIDATED/EXPIRED -- anche un OB gia' testato conferma
    # che li' e' successo qualcosa di strutturalmente rilevante)
    for ob in (mie_context.get("mie_order_block_order_blocks") or []):
        if ob.get("status") in ("INVALIDATED", "EXPIRED"):
            continue
        obh, obl = ob.get("zone_high"), ob.get("zone_low")
        if obh is None or obl is None:
            continue
        if _ranges_overlap(zone_low, zone_high, float(obl), float(obh)):
            score += 20.0
            factors.append("ORDER_BLOCK")
            if ob.get("has_bos"):
                score += 15.0
                factors.append("BOS")
            if ob.get("has_sweep_before"):
                score += 15.0
                factors.append("SWEEP")
            if ob.get("has_fvg"):
                score += 10.0
                factors.append("FVG")
            # v3.11: OB FRESH (mai testato) + FVG insieme -- il setup che
            # storicamente ha piu' letteratura dietro (zona vergine + gap
            # ancora aperto = alta probabilita' di reazione al tocco).
            # Bonus forte ma additivo: non e' un requisito, solo il
            # riconoscimento che QUESTA combinazione specifica e' rara e
            # di qualita' superiore alla media. Consolida ORDER_BLOCK+FVG
            # in un'unica etichetta -- gia' impliciti, non ripetuti.
            if ob.get("status") == "FRESH" and ob.get("has_fvg"):
                score += 55.0
                if "ORDER_BLOCK" in factors:
                    factors.remove("ORDER_BLOCK")
                if "FVG" in factors:
                    factors.remove("FVG")
                factors.append("OB-FRESH+FVG")
            break  # un solo match: evita di sommare piu' OB sovrapposti

    # Sovrapposizione con una zona Reaction Map (che fonde gia' FVG,
    # Liquidity, Structure -- conferma incrociata indipendente dall'OB)
    for rz in (mie_context.get("mie_reaction_map_zones") or []):
        rzh, rzl = rz.get("zone_high"), rz.get("zone_low")
        if rzh is None or rzl is None:
            continue
        if _ranges_overlap(zone_low, zone_high, float(rzl), float(rzh)) \
           and rz.get("reaction_strength") in ("STRONG", "MODERATE"):
            score += 10.0
            factors.append("REACTION_MAP")
            break

    # Allineamento col bias di timeframe superiore -- SOLO bonus, mai
    # penalita' (coerente con "arricchisce, non blocca"): se il bias e'
    # NEUTRAL o non disponibile, nessun effetto; se e' controtrend,
    # nessuna penalita' esplicita (tensione irrisolta, vedi nota sopra
    # sulla ricorrenza -- non decido io se il controtrend sia da punire).
    mie_bias = mie_context.get("mie_market_state_bias")
    if zone_kind and mie_bias == zone_kind:
        score += 10.0
        factors.append("TREND_ALLINEATO")

    # Decelerazione in avvicinamento
    if df_m15 is not None and _is_decelerating(df_m15):
        score += 10.0
        factors.append("DECELERAZIONE")

    # Confluenza con swing storici H4/D1 (v3.12) -- se la zona coincide
    # con un vecchio swing high/low di timeframe superiore, quel livello
    # e' strutturalmente piu' importante (piu' partecipanti lo guardano).
    # D1 vale piu' di H4. Solo il match piu' alto conta (non somma
    # multipli swing sovrapposti). Coerenza direzionale: uno swing LOW
    # e' una zona di SUPPORTO (favorisce BUY), uno swing HIGH una zona
    # di RESISTENZA (favorisce SELL).
    if swing_zones:
        best_swing_bonus = 0
        best_swing_label = None
        for sw in swing_zones:
            sw_h, sw_l = sw.get("zone_high", 0), sw.get("zone_low", 0)
            if not _ranges_overlap(zone_low, zone_high, sw_l, sw_h):
                continue
            # Coerenza direzionale
            sw_type = sw.get("swing_type")
            if zone_kind == "BULLISH" and sw_type != "LOW":
                continue   # zona BUY su uno swing high (resistenza) -> non e' confluenza
            if zone_kind == "BEARISH" and sw_type != "HIGH":
                continue   # zona SELL su uno swing low (supporto) -> non e' confluenza
            tf = sw.get("timeframe", "H4")
            bonus = 15.0 if tf == "D1" else 10.0
            if bonus > best_swing_bonus:
                best_swing_bonus = bonus
                best_swing_label = f"SWING-{tf}"
        if best_swing_bonus > 0:
            score += best_swing_bonus
            factors.append(best_swing_label)

    if not refined:
        score *= 0.9  # zona non raffinata (M5 assente): lieve penalita'

    return round(min(score, 100.0), 1), factors


# ============================================================
# RICORRENZA (v3.7) -- non "quante volte ha toccato", ma "quante volte
# da qui e' REALMENTE ripartito un impulso".
#
# Macchina a stati per zona, persistita nel tempo (lo stato vive nel DB,
# questa funzione e' PURA -- riceve lo stato precedente, ritorna quello
# nuovo, nessun I/O qui dentro, cosi' resta testabile in isolamento come
# tutto il resto del motore oggi).
#
#   1. Il prezzo ENTRA nella zona (prima non c'era) -> si apre una
#      finestra di conferma (recurrence_confirmation_bars candele M15).
#   2. Dentro la finestra, si cerca un impulso VERO (stessa regola gia'
#      usata per rilevare gli impulsi originali) nella stessa direzione
#      della zona.
#   3. Se nasce -> confirmed_restarts += 1 (la zona "ha tenuto ed e'
#      ripartita" -- bonus di punteggio).
#   4. Se la finestra scade senza impulso (il prezzo attraversa senza
#      reazione) -> failed_visits += 1 (penalita' di punteggio).
#   5. Dopo troppi fallimenti consecutivi -> zona INVALIDATED, esclusa
#      dalle notifiche future.
# ============================================================

def _new_recurrence_state(zone_ref: str, asset: str, direction: str, zone_kind: str,
                          zone_high: float, zone_low: float, now_iso: str) -> dict:
    """Stato iniziale per una zona mai vista prima."""
    return {
        "zone_ref": zone_ref, "asset": asset, "direction": direction,
        "zone_kind": zone_kind, "zone_high": zone_high, "zone_low": zone_low,
        "visits": 0, "confirmed_restarts": 0, "failed_visits": 0,
        "price_inside": False, "awaiting_confirmation": False,
        "confirmation_bars_remaining": 0, "entry_ts": None,
        "status": "ACTIVE",
        # v3.10: tracciamento per la domanda "vergine batte ricorrente?"
        # -- SOLO osservazione, non influenza ancora lo score (la
        # decisione si prende quando ci sara' campione, non ora).
        "is_virgin": True,               # visits==0: mai toccata
        "restart_displacements": [],     # ampiezza (ATR) di OGNI impulso
                                          # di ritorno confermato, in ordine
        "first_seen_ts": now_iso, "last_updated_ts": now_iso,
    }


def _update_zone_recurrence(prev_state: dict, current_price: float,
                            df_m15: pd.DataFrame, min_impulse_atr: float,
                            confirmation_bars: int, invalidate_after_failures: int,
                            now_iso: str, zone_high: float = None, zone_low: float = None) -> dict:
    """
    Avanza la macchina a stati di UNA zona di un ciclo. Funzione PURA:
    nessun accesso al DB qui -- riceve lo stato precedente (dict) e i
    dati di mercato correnti, ritorna il nuovo stato. Il chiamante
    (lh_runner.py) si occupa di leggere/scrivere lo stato dal DB.

    BUG CORRETTO (09/08, trovato da segnalazione utente su riepilogo
    serale con zona 0.39$ ancora visibile dopo il fix del floor minimo
    di ieri): zone_high/zone_low venivano presi SOLO da prev_state e
    MAI aggiornati -- una zona veniva "congelata" ai confini della prima
    rilevazione per sempre, anche quando scan_restart_zones ricalcolava
    confini piu' corretti (es. dopo il fix del floor minimo) nei cicli
    successivi. Il riepilogo serale legge questi confini dalla tabella
    di ricorrenza, non dall'ultima notifica -- ecco perche' mostrava
    ancora la vecchia zona quasi puntiforme.

    Ora, se zone_high/zone_low vengono passati (il chiamante ha appena
    ricalcolato la zona in questo ciclo), sostituiscono quelli congelati.
    """
    state = dict(prev_state)  # non muto l'originale
    if zone_high is not None and zone_low is not None:
        state["zone_high"], state["zone_low"] = zone_high, zone_low
    zone_high, zone_low = state["zone_high"], state["zone_low"]
    zone_kind = state["zone_kind"]

    if state["status"] == "INVALIDATED":
        state["last_updated_ts"] = now_iso
        return state

    is_inside_now = zone_low <= current_price <= zone_high
    was_inside = state["price_inside"]

    # ── Nuovo ingresso: il prezzo NON era dentro, ora e' dentro ──
    if is_inside_now and not was_inside:
        state["visits"] += 1
        state["is_virgin"] = False  # da qui in poi, non e' piu' vergine
        state["awaiting_confirmation"] = True
        state["confirmation_bars_remaining"] = confirmation_bars
        state["entry_ts"] = now_iso

    # ── Se sto aspettando conferma, controllo se e' nato un impulso vero ──
    if state["awaiting_confirmation"]:
        recent_impulses = _detect_impulses(
            df_m15, _manual_atr(df_m15, period=14) or 1e-9,
            min_impulse_atr, lookback_bars=confirmation_bars,
            duration_ms=15*60*1000, timeframe_label="M15",
        )
        matching = [imp for imp in recent_impulses if imp["direction"] == zone_kind]
        confirmed = len(matching) > 0

        if confirmed:
            state["confirmed_restarts"] += 1
            # Registro l'ampiezza del PIU' FORTE tra gli impulsi trovati
            # in questa finestra -- per confrontare poi se le zone vergini
            # producono restart piu' forti di quelle gia' ricorrenti.
            strongest = float(max(imp["displacement_atr"] for imp in matching))
            state["restart_displacements"] = list(state.get("restart_displacements", [])) + [strongest]
            state["awaiting_confirmation"] = False
            state["confirmation_bars_remaining"] = 0
        else:
            state["confirmation_bars_remaining"] -= 1
            if state["confirmation_bars_remaining"] <= 0:
                state["failed_visits"] += 1
                state["awaiting_confirmation"] = False
                if state["failed_visits"] >= invalidate_after_failures:
                    state["status"] = "INVALIDATED"

    state["price_inside"] = is_inside_now
    state["last_updated_ts"] = now_iso
    return state


def _apply_recurrence_to_score(base_score: float, confirmations: list,
                               recurrence_state: dict) -> tuple:
    """
    Applica bonus/penalita' di ricorrenza al punteggio base. Ritorna
    (nuovo_score, nuove_confirmations). Zone INVALIDATED tornano
    score=0 esplicitamente (il chiamante decide se escluderle del tutto).
    """
    if recurrence_state is None:
        return base_score, confirmations

    confirmations = list(confirmations)

    if recurrence_state["status"] == "INVALIDATED":
        confirmations.append("INVALIDATED (attraversata senza reazione)")
        return 0.0, confirmations

    restarts = recurrence_state.get("confirmed_restarts", 0)
    failures = recurrence_state.get("failed_visits", 0)

    bonus = min(restarts * RECURRENCE_BONUS_PER_RESTART, RECURRENCE_BONUS_MAX)
    penalty = min(failures * RECURRENCE_PENALTY_PER_FAILURE, RECURRENCE_PENALTY_MAX)

    if restarts > 0:
        confirmations.append(f"RICORRENTE(x{restarts})")
    if failures > 0:
        confirmations.append(f"attraversata senza reazione (x{failures})")

    new_score = max(0.0, min(base_score + bonus - penalty, 100.0))
    return round(new_score, 1), confirmations


# ============================================================
# RIEPILOGO DI FINE GIORNATA (v3.8) -- "Overnight Trading Plan"
#
# Funzioni PURE (nessun I/O, nessuna chiamata Telegram qui dentro):
# ricevono le zone gia' lette dal DB, ritornano testo formattato.
# L'I/O (query DB, invio messaggio) vive nel runner, come sempre oggi.
# ============================================================

# ============================================================
# MEMORIA STORICA SWING H4/D1 (v3.12) -- fase 1: solo raccolta dati
#
# Niente architettura complicata, come richiesto: rileva swing high/low
# su H4 e D1, salva la zona di reazione (il range della candela stessa
# dello swing), persiste per sempre -- nessuna scadenza, nessuna
# invalidazione in questa fase. Serve a costruire memoria storica;
# l'uso di questa memoria per leggere trend/zone importanti e' un passo
# successivo, non ora.
#
# Regola di rilevamento: stessa identica logica gia' calibrata in
# order_block_engine.py (_impulse_broke_structure) per confermare uno
# swing -- k=2 candele per lato, swing STRETTO (disuguaglianza stretta,
# non "maggiore o uguale": in una zona piatta ogni barra sembrerebbe
# uno swing altrimenti). Nessuna soglia nuova inventata qui.
# ============================================================

SWING_CONFIRM_K = 2  # candele per lato per confermare uno swing -- stesso k gia' calibrato altrove


def _detect_swings(df: pd.DataFrame, asset: str, timeframe_label: str,
                   k: int = SWING_CONFIRM_K) -> list:
    """
    Scansiona TUTTE le candele disponibili in df (non solo una finestra
    recente -- usata sia per il backfill iniziale che per il refresh
    incrementale) cercando swing high e swing low confermati.

    Uno swing high in posizione i e' confermato se high[i] e' STRETTAMENTE
    maggiore degli high delle k candele prima E delle k candele dopo.
    Speculare per swing low. La zona di reazione = range high/low della
    candela stessa dello swing (stessa convenzione OB: nessuna zona
    sintetica, solo i dati grezzi della candela).

    Ritorna lista di {"swing_ref", "asset", "timeframe", "swing_type",
    "price", "zone_high", "zone_low", "formation_ts"}.
    """
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
            ts = int(timestamps[i])
            swings.append({
                "swing_ref": f"swing:{asset}:{timeframe_label}:HIGH:{ts}",
                "asset": asset, "timeframe": timeframe_label,
                "swing_type": "HIGH", "price": round(float(highs[i]), 4),
                "zone_high": round(float(highs[i]), 4),
                "zone_low": round(float(lows[i]), 4),
                "formation_ts": ts,
            })

        if lows[i] < window_l_before.min() and lows[i] < window_l_after.min():
            ts = int(timestamps[i])
            swings.append({
                "swing_ref": f"swing:{asset}:{timeframe_label}:LOW:{ts}",
                "asset": asset, "timeframe": timeframe_label,
                "swing_type": "LOW", "price": round(float(lows[i]), 4),
                "zone_high": round(float(highs[i]), 4),
                "zone_low": round(float(lows[i]), 4),
                "formation_ts": ts,
            })

    return swings


def _score_to_stars(score: float) -> str:
    """Converte il punteggio 0-100 in stelle (5 fasce, nessuna calibrata su dati)."""
    if score >= 90:
        n = 5
    elif score >= 70:
        n = 4
    elif score >= 50:
        n = 3
    elif score >= 30:
        n = 2
    else:
        n = 1
    return "\u2605" * n + "\u2606" * (5 - n)


# Mappa dalle conferme complete (quelle usate internamente per lo
# scoring) alle etichette brevi mostrate nel riepilogo -- stesso
# vocabolario del mockup (OB, FVG, LIQ, BOS). RICORRENTE e IMPULSO-*
# non compaiono qui: sono gia' impliciti nel punteggio/stelle.
_CONFIRMATION_SHORT_TAGS = {
    "ORDER_BLOCK": "OB",
    "FVG": "FVG",
    "BOS": "BOS",
    "SWEEP": "LIQ",
    "REACTION_MAP": "RM",
    "TREND_ALLINEATO": "TREND",
    "DECELERAZIONE": "DECEL",
    "OB-FRESH+FVG": "OB FRESH",
    "SWING-H4": "SWING H4",
    "SWING-D1": "SWING D1",
}


def _short_confluence_tags(confirmations: list, max_tags: int = 3) -> str:
    """
    Le prime max_tags conferme "vere" (esclude IMPULSO-*/RICORRENTE/
    attraversata senza reazione, che non sono confluenze SMC), unite
    con " + " -- stile del mockup ("OB + FVG").
    """
    tags = []
    for c in confirmations:
        short = _CONFIRMATION_SHORT_TAGS.get(c)
        if short and short not in tags:
            tags.append(short)
        if len(tags) >= max_tags:
            break
    return " + ".join(tags) if tags else "impulso puro"


def format_zone_digest(asset: str, buy_zones: list, sell_zones: list) -> dict:
    """
    Formatta le zone ancora valide in un riepilogo BUY/SELL con stelle
    e tag di confluenza brevi, piu' una riga di priorita' -- stile
    "Overnight Trading Plan". Ogni zona in buy_zones/sell_zones e' un
    dict con almeno: zone_high, zone_low, restart_score, confirmations.

    Ritorna {"buy_lines": [...], "sell_lines": [...], "focus": str}
    -- il chiamante compone il messaggio finale (Telegram/ntfy hanno
    formattazioni diverse, meglio lasciare la formattazione fisica al
    runner, qui solo il CONTENUTO).
    """
    def fp(v):
        return f"{v:,.2f}" if float(v) > 1000 else f"{v:.2f}"

    def fmt_group(zones):
        lines = []
        for z in sorted(zones, key=lambda z: z["restart_score"], reverse=True):
            stars = _score_to_stars(z["restart_score"])
            tags = _short_confluence_tags(z.get("confirmations", []))
            lines.append({
                "range": f"{fp(z['zone_low'])} \u2013 {fp(z['zone_high'])}",
                "stars": stars,
                "tags": tags,
                "score": z["restart_score"],
            })
        return lines

    buy_lines = fmt_group(buy_zones)
    sell_lines = fmt_group(sell_zones)

    best_buy = buy_lines[0]["score"] if buy_lines else 0
    best_sell = sell_lines[0]["score"] if sell_lines else 0
    if best_buy == 0 and best_sell == 0:
        focus = "Nessuna zona di qualita' sufficiente oggi."
    elif abs(best_buy - best_sell) < 5:
        focus = "Nessuna priorita' netta -- entrambi i lati validi."
    elif best_buy > best_sell:
        focus = "Priorit\u00e0 BUY."
    else:
        focus = "Priorit\u00e0 SELL."

    return {"buy_lines": buy_lines, "sell_lines": sell_lines, "focus": focus}


def _merge_nearby_zones(zones: list, asset: str) -> list:
    """
    Raggruppa Restart Zone vicine (stessa direzione) ed evita notifiche
    ridondanti: se piu' impulsi vicini producono zone che si sovrappongono
    o distano meno di zone_merge_tolerance_points, si tiene SOLO quella
    col punteggio migliore del gruppo -- le altre vengono scartate.

    Solo zone della STESSA direzione vengono fuse: una zona bullish e una
    bearish alla stessa altezza restano distinte (significano cose
    diverse). Il campo "merged_from" indica quante zone erano nel gruppo
    (1 = nessun merge avvenuto).

    Nessun dato storico dietro zone_merge_tolerance_points -- come gli
    altri parametri del Restart Zone Engine, configurabile per asset in
    ASSET_PARAMS, punto di partenza da tarare quando ci sara' campione.
    """
    P = _params(asset)
    tolerance = P.get("zone_merge_tolerance_points", 20)

    result = []
    for kind in ("BULLISH", "BEARISH"):
        subset = sorted(
            (z for z in zones if z["zone_kind"] == kind),
            key=lambda z: z["zone_low"],
        )
        clusters = []
        current = []
        for z in subset:
            if not current:
                current = [z]
                continue
            last = current[-1]
            gap = z["zone_low"] - last["zone_high"]  # negativo se sovrapposte
            if gap <= tolerance:
                current.append(z)
            else:
                clusters.append(current)
                current = [z]
        if current:
            clusters.append(current)

        for cluster in clusters:
            best = max(cluster, key=lambda z: z["restart_score"])
            best = dict(best)
            best["merged_from"] = len(cluster)
            result.append(best)

    return result


def _suggest_trade(direction: str, zone_high: float, zone_low: float,
                   atr: float, sl_buffer_atr: float) -> dict:
    """
    Idea di trade INFORMATIVA per zone di altissima qualita' -- stessa
    convenzione gia' usata dal segnale di trading vero (entry al bordo
    della zona piu' vicino a dove il prezzo arriverebbe, SL oltre la
    zona con buffer ATR). NON e' un segnale eseguibile dal sistema, non
    passa dal Decision Ledger, non ha dedup -- solo un suggerimento
    allegato alla notifica quando la zona e' eccezionale.

    TP a un multiplo fisso di rischio (2R) -- scelta semplice e
    trasparente, non tenta di indovinare un livello di liquidita'
    specifico (che richiederebbe dati non verificati qui).
    """
    buf = sl_buffer_atr * atr
    if direction == "BUY":
        entry = zone_high      # bordo piu' vicino a un prezzo che scende nella zona
        sl = zone_low - buf
    else:
        entry = zone_low
        sl = zone_high + buf
    risk = abs(entry - sl)
    if risk <= 0:
        return None
    tp = entry + 2 * risk if direction == "BUY" else entry - 2 * risk
    return {
        "entry": round(entry, 4), "stop_loss": round(sl, 4),
        "take_profit": round(tp, 4), "rr": 2.0,
    }


def scan_restart_zones(asset: str, df_m15: pd.DataFrame, now: datetime,
                       mie_context: dict = None,
                       df_m5: pd.DataFrame = None,
                       df_h1: pd.DataFrame = None,
                       df_m30: pd.DataFrame = None,
                       swing_zones: list = None) -> list:
    """
    Identifica Bullish/Bearish Restart Zone -- v3.6, ruoli distinti per
    timeframe (non tutti fanno la stessa cosa):

        H1  -> trova gli impulsi PIU' IMPORTANTI della giornata
        M30 -> trova gli impulsi MEDI
        M15 -> DEFINISCE e raffina la Restart Zone (drill-down dentro
               la finestra H1/M30 individuata)
        M5  -> raffina ULTERIORMENTE al punto preciso (dentro la M15)

    L'UNICO filtro e' la forza dell'impulso (min_impulse_atr_h1/_m30, per
    asset) -- non piu' dipendente dal registro Order Block di un altro
    engine. SMC (Order Block, FVG via Reaction Map, BOS, sweep)
    arricchisce solo il punteggio, non decide se la zona esiste.

    Se ne' df_h1 ne' df_m30 sono disponibili, nessuna zona viene trovata
    (non c'e' piu' fallback su M15 come sorgente primaria -- M15 e' solo
    lo strumento di definizione, non di ricerca, per design esplicito).

    Ritorna lista di zone (max max_zones_per_scan per ciclo, le migliori
    per punteggio dopo il merge delle vicine): [{
        "direction": "BUY"|"SELL", "zone_kind": "BULLISH"|"BEARISH",
        "zone_ref": str (stabile: asset+direzione+timeframe+timestamp),
        "zone_high": float, "zone_low": float, "zone_width": float,
        "m5_refined": bool, "source_timeframe": "H1"|"M30",
        "distance_atr": float, "distance_points": float, "is_near": bool,
        "restart_score": float, "zone_strength": "STRONG"|"MODERATE"|"WEAK",
        "confirmations": list[str], "merged_from": int,
    }, ...]
    """
    if not mie_context:
        return []

    src = df_m5 if (df_m5 is not None and len(df_m5) > 0) else df_m15
    if src is None or len(src) == 0:
        return []
    price = float(src.iloc[-1]["close"])

    atr_m15 = mie_context.get("mie_volatility_atr_m15", 0) or 0
    if atr_m15 <= 0 and df_m15 is not None and len(df_m15) >= 15:
        h = df_m15["high"].astype(float).values
        l = df_m15["low"].astype(float).values
        c = df_m15["close"].astype(float).values
        atr_m15 = sum(max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1]))
                      for i in range(-14, 0)) / 14
    if atr_m15 <= 0:
        return []

    P = _params(asset)
    max_atr = ZONE_SCAN_MAX_ATR.get(asset, ZONE_SCAN_MAX_ATR_DEFAULT)
    near_threshold = ZONE_SCAN_NEAR_POINTS.get(asset)
    body_ratio_threshold = P.get("launch_body_ratio", 0.5)
    max_zones = P.get("max_zones_per_scan", 5)

    # ── Detection: SOLO H1 e M30, mai piu' M15 come sorgente primaria ──
    impulses = []

    atr_h1 = _manual_atr(df_h1, period=14)
    if atr_h1 > 0 and df_h1 is not None:
        impulses += _detect_impulses(
            df_h1, atr_h1,
            P.get("min_impulse_atr_h1", 1.0),
            P.get("impulse_lookback_bars_h1", 12),
            duration_ms=60*60*1000, timeframe_label="H1")

    atr_m30 = _manual_atr(df_m30, period=14)
    if atr_m30 > 0 and df_m30 is not None:
        impulses += _detect_impulses(
            df_m30, atr_m30,
            P.get("min_impulse_atr_m30", 0.9),
            P.get("impulse_lookback_bars_m30", 16),
            duration_ms=30*60*1000, timeframe_label="M30")

    zones = []
    min_zone_width = P.get("min_zone_width_points", 0)
    for impulse in impulses:
        zh, zl, refined = _zone_from_impulse(impulse, df_m15, df_m5, body_ratio_threshold)
        if zh is None:
            continue

        # v3.9: se il raffinamento ha prodotto una zona quasi puntiforme
        # (candela di lancio con corpo minuscolo), la allarghiamo al
        # minimo utilizzabile -- simmetrica attorno al centro, cosi' il
        # punto di lancio individuato resta al centro della zona finale.
        width = zh - zl
        if min_zone_width > 0 and width < min_zone_width:
            pad = (min_zone_width - width) / 2
            zh += pad
            zl -= pad

        mid = (zh + zl) / 2
        distance_atr = abs(price - mid) / atr_m15
        if distance_atr > max_atr:
            continue

        score, confirmations = _score_restart_zone(
            zh, zl, impulse["displacement_atr"], mie_context, refined,
            zone_kind=impulse["direction"], df_m15=df_m15,
            swing_zones=swing_zones)
        confirmations = [f"IMPULSO-{impulse['timeframe']}({impulse['displacement_atr']:.1f}ATR)"] + confirmations[1:]

        distance_points = abs(price - mid)
        is_near = near_threshold is not None and distance_points <= near_threshold
        if score >= 70:
            strength = "STRONG"
        elif score >= 40:
            strength = "MODERATE"
        else:
            strength = "WEAK"

        direction_raw = impulse["direction"]
        direction = "BUY" if direction_raw == "BULLISH" else "SELL"

        # Trade suggerito -- solo per zone eccezionali (score>=90, tipico
        # del bonus OB fresco+FVG appena aggiunto). Informativo, non
        # eseguibile dal sistema.
        suggested_trade = None
        if score >= 90:
            suggested_trade = _suggest_trade(
                direction, zh, zl, atr_m15, P.get("sl_buffer_atr", 0.5))

        zones.append({
            "direction": direction,
            "zone_kind": direction_raw,
            "zone_ref": f"impulse:{asset}:{direction_raw}:{impulse['timeframe']}:{impulse['timestamp']}",
            "zone_high": zh,
            "zone_low": zl,
            "zone_width": round(zh - zl, 4),
            "m5_refined": refined,
            "source_timeframe": impulse["timeframe"],
            "distance_atr": round(distance_atr, 2),
            "distance_points": round(distance_points, 2),
            "is_near": is_near,
            "restart_score": score,
            "zone_strength": strength,
            "confirmations": confirmations,
            "suggested_trade": suggested_trade,
        })

    zones = _merge_nearby_zones(zones, asset)
    zones.sort(key=lambda z: z["restart_score"], reverse=True)
    zones = zones[:max_zones]           # tetto anti-inondazione, non di qualita'
    zones.sort(key=lambda z: z["distance_atr"])  # per la notifica: piu' vicine prima
    return zones


# ============================================================

def generate_lh_signal(asset: str, df_m15: pd.DataFrame, now: datetime,
                       mie_context: dict = None,
                       df_m5: pd.DataFrame = None,
                       restart_zones: list = None,
                       swing_zones: list = None) -> dict:
    """
    LH v4.0 — Restart Zone Based.

    CAMBIO RISPETTO A v3.2: il segnale di trading si basa SOLO sulle
    Restart Zone STRONG (score >= 70) quando disponibili, invece di
    cercare OB in modo indipendente. I dati lo giustificano: le zone
    STRONG hanno 75% di successo nella ricorrenza, il vecchio segnale
    OB-based aveva 9% win rate sullo stesso mercato.

    Quando nessuna Restart Zone STRONG e' vicina, NON entra -- non
    ricade piu' sulla vecchia logica OB (che ha dimostrato di non
    convertire). Meglio nessun segnale che un segnale perdente.

    Entry: al bordo della zona (zone_high per BUY, zone_low per SELL)
    SL: oltre la zona (punto di invalidazione strutturale) + buffer ATR
    TP: al prossimo swing storico H4/D1 nella direzione del trade, oppure
        al target strutturale piu' vicino se nessuno swing e' disponibile
    """
    if not mie_context:
        return _reject("NO_MIE_CONTEXT")

    P = _params(asset)
    session = _get_session(now)

    if mie_context.get("mie_macro_is_blackout"):
        return _reject("MACRO_BLACKOUT")

    src = df_m5 if (df_m5 is not None and len(df_m5) > 0) else df_m15
    if src is None or len(src) == 0:
        return _reject("NO_DATA")
    price = float(src.iloc[-1]["close"])

    atr = mie_context.get("mie_volatility_atr_m15", 0) or 0
    if atr <= 0:
        return _reject("NO_ATR")

    # ── Cerca la migliore Restart Zone vicina ──────────────────
    if not restart_zones:
        return _reject("NO_RESTART_ZONES")

    # Qualunque zona vicina (NEAR o entro 1.5 ATR) puo' generare un
    # segnale -- il punteggio modula il RR minimo richiesto, non decide
    # se entrare o no. Coerente con "arricchimento, non gate".
    candidates = [
        z for z in restart_zones
        if z.get("is_near", False) or z.get("distance_atr", 99) <= 1.5
    ]

    if not candidates:
        return _reject("NO_ZONE_NEARBY")

    # Prendi la migliore per punteggio
    best_zone = max(candidates, key=lambda z: z.get("restart_score", 0))
    direction = best_zone["direction"]  # "BUY" / "SELL"
    zh = best_zone["zone_high"]
    zl = best_zone["zone_low"]

    # ── Sessione valida? ──────────────────────────────────────
    allowed = ALLOWED_SESSIONS.get(asset, set())
    if session not in allowed:
        return _reject(f"SESSION_{session}_NOT_ALLOWED", zone=best_zone)

    # ── Entry / SL / TP ──────────────────────────────────────
    sl_buffer = P.get("sl_buffer_atr", 0.5) * atr

    if direction == "BUY":
        entry = zh                      # bordo superiore della zona
        sl = zl - sl_buffer             # sotto la zona = invalidazione
        # TP: prossimo swing HIGH sopra l'ENTRY (non sopra il prezzo
        # attuale) -- coerenza con l'Entry quando il segnale viene
        # anticipato: l'Entry guarda gia' avanti (il bordo della zona,
        # non il prezzo di oggi), il TP deve ragionare allo stesso modo,
        # altrimenti con il prezzo ancora lontano dalla zona il target
        # puo' cadere sotto l'Entry stesso (bug trovato il 18/08).
        tp = _find_next_swing_target(swing_zones, entry, "BUY", atr, P)
    else:  # SELL
        entry = zl                      # bordo inferiore della zona
        sl = zh + sl_buffer             # sopra la zona = invalidazione
        tp = _find_next_swing_target(swing_zones, entry, "SELL", atr, P)

    risk = abs(entry - sl)
    if risk <= 0:
        return _reject("ZERO_RISK", zone=best_zone)
    reward = abs(tp - entry)
    rr = round(reward / risk, 2)

    # RR minimo DINAMICO in base alla forza della zona: zone forti
    # possono entrare con RR piu' basso (75% successo), zone deboli
    # servono un setup con reward piu' alto per compensare la
    # probabilita' inferiore (50% successo).
    zone_score = best_zone.get("restart_score", 0)
    if zone_score >= 70:      # STRONG: 75% successo
        min_rr = P.get("min_rr", 1.0)
    elif zone_score >= 40:    # MODERATE: 58% successo
        min_rr = P.get("min_rr", 1.0) * 1.5
    else:                     # WEAK: 50% successo
        min_rr = P.get("min_rr", 1.0) * 2.0

    if rr < min_rr:
        return _reject(f"RR_TOO_LOW ({rr:.1f} < {min_rr:.1f} per {best_zone.get('zone_strength','?')})", zone=best_zone)

    # ── Costruisci il segnale ─────────────────────────────────
    signal = {
        "asset": asset,
        "direction": direction,
        "entry": round(entry, 5),
        "stop_loss": round(sl, 5),
        "tp": round(tp, 5),
        "rr": rr,
        "quality_score": best_zone.get("restart_score", 0),
        "quality_label": best_zone.get("zone_strength", "STRONG"),
        "trigger_type": "RESTART_ZONE",
        "swept_level_label": best_zone.get("zone_ref", ""),
        "swept_level_priority": best_zone.get("zone_strength", "STRONG"),
        "sweep_direction": direction,
        "tp_label": "SWING_TARGET",
        "session": session,
        "timestamp_setup": now.isoformat(),
        "source_timeframe": best_zone.get("source_timeframe", "M30"),
        "confirmations": best_zone.get("confirmations", []),
    }

    score = best_zone.get("restart_score", 0) / 10.0  # 0-100 -> 0-10
    factors = best_zone.get("confirmations", [])

    return {"signal": signal, "diagnostics": {
        "status": "SIGNAL_GENERATED", "score": score,
        "factors": factors, "zone": best_zone}}


def _find_next_swing_target(swing_zones: list, price: float,
                            direction: str, atr: float, params: dict) -> float:
    """
    Trova il TP al prossimo swing storico nella direzione del trade.
    BUY -> prossimo swing HIGH sopra il prezzo (resistenza)
    SELL -> prossimo swing LOW sotto il prezzo (supporto)
    Fallback: entry + 2R (come prima) se nessuno swing disponibile.
    """
    if not swing_zones:
        tp_max_atr = params.get("tp1_max_atr", 3.0)
        if direction == "BUY":
            return price + tp_max_atr * atr
        else:
            return price - tp_max_atr * atr

    if direction == "BUY":
        targets = [
            sw for sw in swing_zones
            if sw.get("swing_type") == "HIGH" and sw.get("price", 0) > price
        ]
        if targets:
            nearest = min(targets, key=lambda s: s["price"] - price)
            return nearest["price"]
    else:
        targets = [
            sw for sw in swing_zones
            if sw.get("swing_type") == "LOW" and sw.get("price", 0) < price
        ]
        if targets:
            nearest = min(targets, key=lambda s: price - s["price"])
            return nearest["price"]

    # Fallback: nessuno swing nella direzione giusta
    tp_max_atr = params.get("tp1_max_atr", 3.0)
    if direction == "BUY":
        return price + tp_max_atr * atr
    else:
        return price - tp_max_atr * atr
