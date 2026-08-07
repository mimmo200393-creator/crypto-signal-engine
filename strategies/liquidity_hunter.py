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
        # Restart Zone Engine (v3.4) -- configurabili, da ottimizzare
        # quando ci sara' campione reale. Nessuno di questi due e' stato
        # validato su dati storici, sono punti di partenza ragionevoli.
        "launch_body_ratio": 0.5,          # soglia "candela decisa" (M5)
        "zone_merge_tolerance_points": 50, # sotto questa distanza, due
                                            # Restart Zone della stessa
                                            # direzione vengono fuse
    },
    "XAU_USD": {
        "sl_buffer_atr": 0.3, "min_rr": 1.2, "expiry_bars": 18,
        "max_zone_atr": 2.0, "min_zone_atr": 0.25, "tp1_max_atr": 3.0,
        "watch_max_atr": 1.5,
        "liq_tight_atr": 3.0,
        "liq_ample_atr": 10.0,
        "launch_body_ratio": 0.5,
        "zone_merge_tolerance_points": 10,
    },
}
DEFAULT_PARAMS = ASSET_PARAMS["BTC_USDT"]

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


def _reject(reason: str) -> dict:
    logger.info("LH: REJECT %s", reason)
    return {"signal": None, "diagnostics": {"rejection": reason}}


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

RESTART_ZONE_STATUSES = {"FRESH", "TESTED"}  # MITIGATED/BREAKER/INVALIDATED esclusi:
                                              # non sono piu' un punto di "ripartenza" pulito

ZONE_SCAN_MAX_ATR = {
    "BTC_USDT": 3.0,
    "XAU_USD":  6.0,   # vedi nota v3.3: finestra allargata perche' l'ATR
                       # M15 di XAU (~3.5-4pt) renderebbe 3.0 ATR piu'
                       # stretto della soglia NEAR (15pt), collassando i
                       # due livelli di avviso in uno solo.
}
ZONE_SCAN_MAX_ATR_DEFAULT = 3.0

# Punteggio minimo per notificare -- sotto, la zona ha al massimo una
# conferma debole, troppo rumore. Vedi _score_restart_zone per la formula.
RESTART_SCORE_MIN = 25

# Secondo avviso, piu' urgente: quando il prezzo e' a pochi punti dalla
# zona (punti assoluti, non ATR -- qui conta la precisione di entrata).
ZONE_SCAN_NEAR_POINTS = {
    "BTC_USDT": 75,   # punto medio del range 50-100 indicato dall'utente
    "XAU_USD":  15,   # punto medio del range 10-20 indicato dall'utente
}


def _m5_window_for_m15_candle(df_m5: pd.DataFrame, formation_ts, lookback_extra_ms: int = 5*60*1000) -> pd.DataFrame:
    """
    Ritorna le candele M5 che compongono la candela M15 iniziata a
    formation_ts (tipicamente 3 candele M5), allargata di una M5 PRIMA
    dell'inizio -- il vero punto di svolta puo' cadere esattamente al
    bordo tra due candele M15. df_m5 deve avere 'timestamp' in
    millisecondi, stessa convenzione usata altrove nel sistema.
    """
    try:
        ts0 = int(float(formation_ts))
    except (TypeError, ValueError):
        return df_m5.iloc[0:0]
    ts_start = ts0 - lookback_extra_ms
    ts_end = ts0 + 15 * 60 * 1000  # +15 minuti in ms
    mask = (df_m5["timestamp"] >= ts_start) & (df_m5["timestamp"] < ts_end)
    return df_m5[mask].sort_values("timestamp")


def _is_accelerating_candle(row, direction: str, body_ratio_threshold: float) -> bool:
    """
    Definizione OGGETTIVA di "candela in accelerazione": deterministica,
    stessa regola applicata sempre, nessuna interpretazione caso per caso.

    Vera SOLO se ENTRAMBE le condizioni sono soddisfatte:
      1. Direzione: bullish per zona BULLISH, bearish per zona BEARISH.
      2. Corpo deciso: |close-open| / (high-low) >= body_ratio_threshold
         (configurabile per asset in ASSET_PARAMS["launch_body_ratio"];
         default 0.5, la stessa soglia che order_block_engine.py usa
         gia' per definire una candela di impulso a livello M15).

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
    solo uno spike/stop-hunt: scende in wick e rimbalza subito dentro la
    STESSA candela, senza che il movimento sostenuto sia partito li'),
    e non necessariamente la prima candela di colore giusto incontrata
    (che puo' essere solo un piccolo rimbalzo insignificante, dominato
    da wick, prima della VERA accelerazione).

    REGOLA OGGETTIVA (sempre la stessa, nessuna interpretazione):
    si cammina all'INDIETRO dalla fine della finestra M5 e si prende la
    PRIMA candela incontrata che NON e' "in accelerazione" secondo
    _is_accelerating_candle -- cioe' l'ultima, in ordine cronologico,
    prima che il movimento diventi DECISO (non solo "nella direzione
    giusta", ma con un corpo che domina sul wick).

    Se OGNI candela nella finestra risulta "in accelerazione" (raro:
    significa che gia' a M5 il movimento era deciso fin dall'inizio della
    finestra), fallback sull'estremo assoluto -- meglio quello che
    l'intera candela M15 originale.
    """
    rows = list(window.iterrows())
    for _, row in reversed(rows):
        if not _is_accelerating_candle(row, direction, body_ratio_threshold):
            return row

    # Fallback: tutte le candele erano gia' "in accelerazione"
    if direction == "BULLISH":
        idx = window["low"].astype(float).idxmin()
    else:
        idx = window["high"].astype(float).idxmax()
    return window.loc[idx]


def _refine_zone_with_m5(ob: dict, df_m5, body_ratio_threshold: float) -> tuple:
    """
    Raffina zone_high/zone_low di un OB usando le candele M5 al suo
    interno. Ritorna (zone_high, zone_low, refined: bool).

    Usa _find_launch_candle: l'ULTIMA candela M5 di colore opposto prima
    del movimento deciso -- non la candela con l'estremo assoluto, che
    puo' essere solo uno spike/stop-hunt rientrato nella stessa candela
    e quindi NON il vero punto da cui e' partita l'accelerazione.

    BULLISH: zone_low = low della candela di lancio, zone_high = il TOP
        del suo corpo (esclude l'eventuale wick sopra: rumore, non parte
        del lancio).
    BEARISH: speculare.
    """
    zh_wide, zl_wide = float(ob["zone_high"]), float(ob["zone_low"])

    if df_m5 is None or len(df_m5) == 0:
        return zh_wide, zl_wide, False

    window = _m5_window_for_m15_candle(df_m5, ob.get("formation_timestamp"))
    if len(window) == 0:
        return zh_wide, zl_wide, False

    direction = ob.get("direction")
    if direction not in ("BULLISH", "BEARISH"):
        return zh_wide, zl_wide, False

    core = _find_launch_candle(window, direction, body_ratio_threshold)

    if direction == "BULLISH":
        zone_low = float(core["low"])
        zone_high = max(float(core["open"]), float(core["close"]))
        if zone_high <= zone_low:
            zone_high = zone_low + (zh_wide - zl_wide) * 0.1  # fallback minimo
    else:
        zone_high = float(core["high"])
        zone_low = min(float(core["open"]), float(core["close"]))
        if zone_low >= zone_high:
            zone_low = zone_high - (zh_wide - zl_wide) * 0.1

    return round(zone_high, 4), round(zone_low, 4), True


def _score_restart_zone(ob: dict, mie_context: dict, refined: bool) -> tuple:
    """
    Punteggio di CONFERMA (0-100) sulla zona gia' individuata e raffinata.
    Ogni fattore SMC aggiunge o non aggiunge -- nessuno di questi crea la
    zona, la zona esiste gia' dall'impulso trovato dall'Order Block Engine.
    """
    factors = []
    score = 0.0

    if ob.get("has_sweep_before"):
        score += 25.0
        factors.append("SWEEP")
    if ob.get("has_bos"):
        score += 25.0
        factors.append("BOS")
    if ob.get("has_fvg"):
        score += 20.0
        factors.append("FVG")

    disp = float(ob.get("displacement_atr") or 0)
    disp_score = min(disp / 2.0, 1.0) * 20.0  # 2+ ATR di impulso = punteggio pieno
    if disp_score > 0:
        score += disp_score
        factors.append(f"IMPULSO({disp:.1f}ATR)")

    # Reaction Map come conferma esterna (non come sorgente della zona)
    mid = (float(ob["zone_high"]) + float(ob["zone_low"])) / 2
    for rz in (mie_context.get("mie_reaction_map_zones") or []):
        rzh, rzl = rz.get("zone_high"), rz.get("zone_low")
        if rzh is None or rzl is None:
            continue
        if float(rzl) <= mid <= float(rzh) and rz.get("reaction_strength") in ("STRONG", "MODERATE"):
            score += 10.0
            factors.append("REACTION_MAP")
            break

    if not refined:
        score *= 0.7  # zona non raffinata (M5 assente): meno affidabile, penalizzata

    return round(min(score, 100.0), 1), factors


def _merge_nearby_zones(zones: list, asset: str) -> list:
    """
    Raggruppa Restart Zone vicine (stessa direzione) ed evita notifiche
    ridondanti: se piu' impulsi vicini producono zone che si sovrappongono
    o distano meno di zone_merge_tolerance_points, si tiene SOLO quella
    col punteggio migliore del gruppo -- le altre vengono scartate.

    Solo zone della STESSA direzione vengono fuse: una zona bullish e una
    bearish alla stessa altezza restano distinte (significano cose
    diverse). Il campo "merged_from" indica quante zone erano nel gruppo
    (1 = nessun merge avvenuto), utile per capire quanta conferma
    incrociata c'e' dietro la zona notificata.

    Nessun dato storico dietro zone_merge_tolerance_points -- come
    launch_body_ratio, e' configurabile per asset in ASSET_PARAMS,
    punto di partenza da tarare quando ci sara' campione.
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


def scan_restart_zones(asset: str, df_m15: pd.DataFrame, now: datetime,
                       mie_context: dict = None,
                       df_m5: pd.DataFrame = None) -> list:
    """
    Identifica Bullish/Bearish Restart Zone: zone precise (idealmente
    4-10$ su XAU, proporzionale su BTC) da cui il prezzo e' GIA' partito
    con forza in passato -- non zone SMC generiche.

    Fonte della zona: gli Order Block gia' calcolati da order_block_engine
    (che e' gia' impulso-first), raffinati con le candele M5 per tagliare
    il rumore della candela M15 intera. SMC (BOS, sweep, FVG, Reaction Map)
    contribuisce SOLO al punteggio di conferma, mai alla creazione.

    Ritorna lista di zone: [{
        "direction": "BUY"|"SELL", "zone_kind": "BULLISH"|"BEARISH",
        "zone_ref": str (ob_id, stabile), "zone_high": float, "zone_low": float,
        "zone_width": float, "m5_refined": bool,
        "distance_atr": float, "distance_points": float, "is_near": bool,
        "restart_score": float, "zone_strength": "STRONG"|"MODERATE",
        "confirmations": list[str],
    }, ...]
    """
    if not mie_context:
        return []

    obs = mie_context.get("mie_order_block_order_blocks") or []
    if not obs:
        return []

    src = df_m5 if (df_m5 is not None and len(df_m5) > 0) else df_m15
    if src is None or len(src) == 0:
        return []
    price = float(src.iloc[-1]["close"])

    atr = mie_context.get("mie_volatility_atr_m15", 0) or 0
    if atr <= 0 and df_m15 is not None and len(df_m15) >= 15:
        h = df_m15["high"].astype(float).values
        l = df_m15["low"].astype(float).values
        c = df_m15["close"].astype(float).values
        atr = sum(max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1]))
                  for i in range(-14, 0)) / 14
    if atr <= 0:
        return []

    max_atr = ZONE_SCAN_MAX_ATR.get(asset, ZONE_SCAN_MAX_ATR_DEFAULT)
    near_threshold = ZONE_SCAN_NEAR_POINTS.get(asset)
    P = _params(asset)
    body_ratio_threshold = P.get("launch_body_ratio", 0.5)

    zones = []
    for ob in obs:
        if ob.get("status") not in RESTART_ZONE_STATUSES:
            continue
        direction_raw = ob.get("direction")
        if direction_raw not in ("BULLISH", "BEARISH"):
            continue

        zh, zl, refined = _refine_zone_with_m5(ob, df_m5, body_ratio_threshold)
        mid = (zh + zl) / 2
        distance_atr = abs(price - mid) / atr
        if distance_atr > max_atr:
            continue

        score, confirmations = _score_restart_zone(ob, mie_context, refined)
        if score < RESTART_SCORE_MIN:
            continue

        distance_points = abs(price - mid)
        is_near = near_threshold is not None and distance_points <= near_threshold
        strength = "STRONG" if score >= 70 else "MODERATE"

        zones.append({
            "direction": "BUY" if direction_raw == "BULLISH" else "SELL",
            "zone_kind": direction_raw,
            "zone_ref": str(ob.get("id", "?")),
            "zone_high": zh,
            "zone_low": zl,
            "zone_width": round(zh - zl, 4),
            "m5_refined": refined,
            "distance_atr": round(distance_atr, 2),
            "distance_points": round(distance_points, 2),
            "is_near": is_near,
            "restart_score": score,
            "zone_strength": strength,
            "confirmations": confirmations,
        })

    zones = _merge_nearby_zones(zones, asset)
    zones.sort(key=lambda z: z["distance_atr"])
    return zones


# ============================================================
# Entry Point
# ============================================================


# ============================================================
# Entry Point
# ============================================================

def generate_lh_signal(asset: str, df_m15: pd.DataFrame, now: datetime,
                       mie_context: dict = None,
                       df_m5: pd.DataFrame = None) -> dict:
    """LH v3.2 — Confluence Engine. Ritorna {"signal", "diagnostics"}."""
    if not mie_context:
        return _reject("NO_MIE_CONTEXT")

    P = _params(asset)
    session = _get_session(now)

    if mie_context.get("mie_macro_is_blackout"):
        return _reject("MACRO_BLACKOUT")

    src = df_m5 if (df_m5 is not None and len(df_m5) > 0) else df_m15
    if src is None or len(src) == 0:
        return _reject("NO_CANDLES")
    last = src.iloc[-1]
    price = float(last["close"])
    hi_c  = float(last["high"])
    lo_c  = float(last["low"])

    atr = mie_context.get("mie_volatility_atr_m15", 0) or 0
    if atr <= 0 and df_m15 is not None and len(df_m15) >= 15:
        h = df_m15["high"].astype(float).values
        l = df_m15["low"].astype(float).values
        c = df_m15["close"].astype(float).values
        atr = sum(max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1]))
                  for i in range(-14, 0)) / 14

    # Direzione: dal bias se c'e', altrimenti dall'OB piu' vicino
    bias = mie_context.get("mie_market_state_bias", "NEUTRAL")
    if bias in ("BULLISH", "BEARISH"):
        want_dir = bias
    else:
        cand = [(d, _find_best_ob(mie_context, d, price, P["max_zone_atr"], P["min_zone_atr"], atr))
                for d in ("BULLISH", "BEARISH")]
        cand = [(d, o) for d, o in cand if o]
        if not cand:
            return _reject("NO_OB_NEARBY (bias neutro)")
        want_dir = min(cand, key=lambda x: x[1].get("distance_from_price_pct", 1))[0]
    direction = "BUY" if want_dir == "BULLISH" else "SELL"

    ob = _find_best_ob(mie_context, want_dir, price, P["max_zone_atr"], P["min_zone_atr"], atr)
    if ob is None:
        return _reject("NO_OB_NEARBY")

    zh, zl = float(ob["zone_high"]), float(ob["zone_low"])
    if atr <= 0:
        atr = abs(zh - zl) * 2

    # ── Anticipazione: TRIGGERED / WATCHING / FAR ────────────
    state, dist_atr = _ob_position(ob, hi_c, lo_c, price, atr, P["watch_max_atr"])
    if state == "FAR":
        return _reject(f"PRICE_FAR_FROM_OB ({dist_atr:.1f} ATR)")

    if state == "TRIGGERED":
        entry = price
        order_type = "MARKET"
    else:
        entry = zh if direction == "BUY" else zl
        order_type = "PENDING"

    # Stop STRUTTURALE oltre l'Order Block
    buf = P["sl_buffer_atr"] * atr
    sl = zl - buf if direction == "BUY" else zh + buf
    risk = abs(entry - sl)
    if risk <= 0:
        return _reject("ZERO_RISK")

    score, factors = _score_confluence(direction, want_dir, ob, mie_context,
                                        entry, atr, session, asset, P)
    if score < MIN_SCORE:
        return {"signal": None, "diagnostics": {
            "rejection": f"SCORE_TOO_LOW ({score}/9 < {MIN_SCORE})",
            "score": score, "factors": factors, "setup_state": state}}

    tp_result = _build_tp_ladder(direction, entry, risk, atr, mie_context, P)
    structural_targets = tp_result["structural"]
    liquidity_levels = tp_result["liquidity"]

    # v3.2: NESSUN segnale senza target strutturale reale.
    # Un "RR_SCALED" significa che non esiste nessun OB opposto, FVG o zona RM
    # entro 3 ATR — il trade non ha una tesi di uscita strutturale.
    if not structural_targets or structural_targets[0][1] == "RR_SCALED":
        return {"signal": None, "diagnostics": {
            "rejection": "NO_STRUCTURAL_TP1 (nessun target strutturale entro tp1_max_atr)",
            "score": score, "factors": factors, "setup_state": state}}

    tp1, tp1_label = structural_targets[0]
    rr = abs(tp1 - entry) / risk if risk > 0 else 0
    if rr < P["min_rr"] - 1e-6:
        return {"signal": None, "diagnostics": {
            "rejection": f"RR_TOO_LOW ({rr:.2f} < {P['min_rr']})",
            "score": score, "factors": factors, "setup_state": state}}

    tp2 = structural_targets[1][0] if len(structural_targets) > 1 else None
    tp2_label = structural_targets[1][1] if len(structural_targets) > 1 else None

    signal = {
        "signal_id":        str(uuid.uuid4()),
        "strategy_name":    STRATEGY_NAME,
        "strategy_version": STRATEGY_VERSION,
        "asset":            asset,
        "direction":        direction,
        "timestamp_setup":  now.isoformat(),

        "entry":     round(entry, 4),
        "stop_loss": round(sl, 4),
        "tp":        tp1,
        "risk":      round(risk, 4),
        "rr":        round(rr, 2),

        # anticipazione
        "setup_state":  state,
        "order_type":   order_type,
        "distance_atr": round(dist_atr, 2),

        # TP operativi (strutturali)
        "tp1": tp1, "tp1_label": tp1_label,
        "tp2": tp2, "tp2_label": tp2_label,
        # TP3 rimosso: i livelli di liquidita' non sono target operativi
        "tp3": None, "tp3_label": None,

        # Livelli di liquidita' informativi (Equal Lows/Highs)
        "liquidity_levels": json.dumps(
            [{"price": p, "label": l} for p, l in liquidity_levels[:3]]
        ),

        # zona OB
        "ob_zone_low":  round(zl, 4),
        "ob_zone_high": round(zh, 4),

        # campi legacy LH DB
        "swept_level_label":     ob.get("id", "?"),
        "swept_level_price":     round((zh + zl) / 2, 4),
        "swept_level_priority":  ob.get("status", "FRESH"),
        "swept_level_touches":   ob.get("test_count", 0),
        "sweep_direction":       want_dir,
        "sweep_peak_price":      zh if direction == "BUY" else zl,
        "sweep_penetration":     0,
        "sweep_penetration_pct": 0,

        "flag_bos_present":      bool(ob.get("has_bos")),
        "flag_choch_present":    False,
        "flag_trigger_present":  state == "TRIGGERED",
        "flag_near_order_block": True,
        "flag_near_fvg":         factors.get("fvg_quality", 0) > 0,
        "ob_quality":            ob.get("quality_score"),
        "ob_match_type":         ob.get("status"),
        "pool_type":             f"OB_{ob.get('status','FRESH')}",
        "flag_htf_pool":         False,
        "confluence_count":      score,

        "trigger_type":      "OB_TOUCH" if state == "TRIGGERED" else "OB_PENDING",
        "trigger_ref_level": round((zh + zl) / 2, 4),
        "tp_label":          tp1_label,
        "tp_priority":       "STRUCTURAL_LADDER",

        "quality_score":      score,
        "quality_label":      _quality_label(score),
        "confluence_factors": json.dumps(factors),

        "session":     session,
        "expiry_bars": P["expiry_bars"],
    }

    return {"signal": signal, "diagnostics": {
        "status": "SIGNAL_GENERATED", "score": score,
        "factors": factors, "structural_targets": structural_targets,
        "liquidity_levels": liquidity_levels, "setup_state": state}}
