"""
strategies/liquidity_hunter.py
Liquidity Hunter v3.1 — Confluence Engine

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

TAKE PROFIT: scala a 3 livelli.
    TP1 = primo target strutturale vicino (OB opposto / FVG / zona RM)
    TP2 = prima area di liquidita'
    TP3 = seconda area di liquidita'
    (i target di liquidita' sono lontani — mediana 12.6 ATR su XAU, 29.4 su
    BTC — quindi non possono fare da TP1: servirebbe un orizzonte di giorni)

TRACCIAMENTO: `tp` resta TP1, cosi' lh_db e il Decision Ledger continuano a
    funzionare e la serie storica degli esiti non si rompe. TP2/TP3 sono
    osservazione fino a quando i dati non diranno se vengono raggiunti.

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
STRATEGY_VERSION = "v3.1"

OB_PROXIMITY_PCT = 0.010     # distanza max dell'OB dal prezzo

# CALIBRAZIONE 23/07/2026 — soglie misurate sulla distribuzione REALE dei
# punteggi (133 setup ricostruiti dal DB), non scelte a priori.
#   distribuzione: min 1.90, mediana 3.15, max 6.30
#   con le soglie iniziali (MED 5.0 / HIGH 6.5): HIGH 0%, MEDIUM 13%, LOW 87%
#   -> HIGH era irraggiungibile e quasi tutto finiva LOW (quindi non notificato)
# Il punteggio massimo teorico e' 9, ma nella pratica diversi fattori
# contribuiscono di rado (candlestick ~0, reaction_map basso su XAU), quindi
# la scala utile si ferma intorno a 6.3. Le soglie seguono quella scala.
MIN_SCORE        = 3.5       # su 9 — sotto, il setup e' debole
QUALITY_HIGH_MIN = 5.0
QUALITY_MED_MIN  = 4.2

ASSET_PARAMS = {
    "BTC_USDT": {
        "sl_buffer_atr": 0.3, "min_rr": 1.0, "expiry_bars": 12,
        "max_zone_atr": 2.0, "tp1_max_atr": 3.0,
        "watch_max_atr": 1.5,      # entro quanto il prezzo e' "in avvicinamento"
        "liq_tight_atr": 3.0,      # sotto questo, poco spazio davanti
        "liq_ample_atr": 10.0,     # sopra questo, molto spazio
    },
    "XAU_USD": {
        "sl_buffer_atr": 0.3, "min_rr": 1.2, "expiry_bars": 18,
        "max_zone_atr": 2.0, "tp1_max_atr": 3.0,
        "watch_max_atr": 1.5,
        "liq_tight_atr": 3.0,
        "liq_ample_atr": 10.0,
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
                  max_zone_atr: float, atr: float) -> Optional[dict]:
    """OB piu' vicino nella direzione, entro OB_PROXIMITY_PCT.
    Priorita' FRESH > TESTED > MITIGATED > BREAKER, poi distanza.
    Zone troppo larghe scartate: entry impreciso e rischio ingestibile."""
    prio = {"FRESH": 0, "TESTED": 1, "MITIGATED": 2, "BREAKER": 3}
    best, best_d, best_p = None, None, 99
    for ob in (mie_context.get("mie_order_block_order_blocks") or []):
        if ob.get("direction") != want_dir or ob.get("status") not in prio:
            continue
        zh, zl = ob.get("zone_high"), ob.get("zone_low")
        if zh is None or zl is None:
            continue
        zh, zl = float(zh), float(zl)
        if atr > 0 and abs(zh - zl) / atr > max_zone_atr:
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

    # 5. FVG — qualita' della zona (proposta: sovrapposizione, distanza, purezza)
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
    #    (su XAU active_sweeps e' non vuoto nell'86% dei casi: troppo comune)
    sw = 0.0
    for lv in (mie_context.get("mie_liquidity_levels") or []):
        if not lv.get("swept"):
            continue
        ba = lv.get("swept_bars_ago")
        if ba is None:
            sw = max(sw, 0.3)
        else:
            sw = max(sw, _clamp(1 - float(ba) / 20.0))   # 0 barre->1, 20->0
    f["liquidity_sweep"] = sw

    # 7. Candlestick — non solo direzione: qualita', zona, e se nasce sull'OB
    cs_dir = mie_context.get("mie_candlestick_strongest_direction")
    cs = 0.0
    if mie_context.get("mie_candlestick_has_confirmation"):
        if (up and cs_dir == "BULLISH") or (not up and cs_dir == "BEARISH"):
            cs += 0.45                                # direzione contestuale ok
            pq = float(mie_context.get("mie_candlestick_pattern_quality_score") or 0)
            cs += 0.30 * _clamp(pq / 100.0)           # qualita' del pattern
            if mie_context.get("mie_candlestick_in_reaction_zone"):
                cs += 0.10
            # il pattern nasce DENTRO la zona OB?
            zsc = mie_context.get("mie_candlestick_zone_confluence_score")
            if zsc is not None and float(zsc) >= 70:
                cs += 0.15
        elif cs_dir:
            cs = -0.25                                # pattern CONTRO il trade
    f["candlestick"] = round(cs, 3)

    # 8. Spazio davanti al trade (proposta 4): poco spazio penalizza.
    #    Misurato: il 63-70% dei casi ha molto spazio, quindi premiare
    #    l'abbondanza discrimina poco — e' la strettezza che informa.
    targets = mie_context.get("mie_liquidity_buy_targets" if up
                              else "mie_liquidity_sell_targets") or []
    dists = [abs(float(t["price"]) - entry) / atr
             for t in targets if t.get("price") and atr > 0]
    if not dists:
        f["liquidity_space"] = 0.3                    # nessun target noto
    else:
        nearest = min(dists)
        tight = params.get("liq_tight_atr", 3.0)
        ample = params.get("liq_ample_atr", 10.0)
        if nearest < tight:
            f["liquidity_space"] = 0.0                # muro davanti
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
# Take Profit — scala strutturale
# ============================================================

def _build_tp_ladder(direction: str, entry: float, risk: float, atr: float,
                     mie_context: dict, params: dict) -> list:
    """TP1 vicino (OB opposto -> FVG -> zona RM -> fallback su rischio),
    TP2/TP3 dalle aree di liquidita'. Ritorna [(price,label), ...]."""
    up = direction == "BUY"
    tp1_max = params.get("tp1_max_atr", 3.0) * atr if atr > 0 else 0

    def ahead(p): return p > entry if up else p < entry
    def near_ok(p): return not tp1_max or abs(p - entry) <= tp1_max

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

    ladder = []
    if near:
        near.sort(key=lambda t: abs(t[0] - entry))
        ladder.append(near[0])
    else:
        d = params.get("min_rr", 1.0) * risk * 1.002
        ladder.append((entry + d if up else entry - d, "RR_SCALED"))

    liq = mie_context.get("mie_liquidity_buy_targets" if up
                          else "mie_liquidity_sell_targets") or []
    pts = sorted([(float(t["price"]), t.get("label", "LIQ")) for t in liq
                  if t.get("price") and ahead(float(t["price"]))],
                 key=lambda t: abs(t[0] - entry))
    for price, label in pts:
        if abs(price - entry) <= abs(ladder[-1][0] - entry) * 1.05:
            continue
        ladder.append((price, label))
        if len(ladder) >= 3:
            break
    return [(round(p, 4), l) for p, l in ladder]


# ============================================================
# Entry Point
# ============================================================

def generate_lh_signal(asset: str, df_m15: pd.DataFrame, now: datetime,
                       mie_context: dict = None,
                       df_m5: pd.DataFrame = None) -> dict:
    """LH v3.1 — Confluence Engine. Ritorna {"signal", "diagnostics"}."""
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
        cand = [(d, _find_best_ob(mie_context, d, price, P["max_zone_atr"], atr))
                for d in ("BULLISH", "BEARISH")]
        cand = [(d, o) for d, o in cand if o]
        if not cand:
            return _reject("NO_OB_NEARBY (bias neutro)")
        want_dir = min(cand, key=lambda x: x[1].get("distance_from_price_pct", 1))[0]
    direction = "BUY" if want_dir == "BULLISH" else "SELL"

    ob = _find_best_ob(mie_context, want_dir, price, P["max_zone_atr"], atr)
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
        entry = price                       # ingresso a mercato
        order_type = "MARKET"
    else:
        # ordine PENDENTE al bordo della zona: mantiene il rischio corretto
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

    ladder = _build_tp_ladder(direction, entry, risk, atr, mie_context, P)
    tp1, tp1_label = ladder[0]
    rr = abs(tp1 - entry) / risk if risk > 0 else 0
    if rr < P["min_rr"] - 1e-6:
        return {"signal": None, "diagnostics": {
            "rejection": f"RR_TOO_LOW ({rr:.2f} < {P['min_rr']})",
            "score": score, "factors": factors, "setup_state": state}}

    signal = {
        "signal_id":        str(uuid.uuid4()),
        "strategy_name":    STRATEGY_NAME,
        "strategy_version": STRATEGY_VERSION,
        "asset":            asset,
        "direction":        direction,
        "timestamp_setup":  now.isoformat(),

        "entry":     round(entry, 4),
        "stop_loss": round(sl, 4),
        "tp":        tp1,               # TP1: usato da lh_db e dal Ledger
        "risk":      round(risk, 4),
        "rr":        round(rr, 2),

        # anticipazione
        "setup_state":  state,          # TRIGGERED | WATCHING
        "order_type":   order_type,     # MARKET | PENDING
        "distance_atr": round(dist_atr, 2),

        # scala TP (osservazione, non ancora uscite parziali)
        "tp1": tp1, "tp1_label": tp1_label,
        "tp2": ladder[1][0] if len(ladder) > 1 else None,
        "tp2_label": ladder[1][1] if len(ladder) > 1 else None,
        "tp3": ladder[2][0] if len(ladder) > 2 else None,
        "tp3_label": ladder[2][1] if len(ladder) > 2 else None,

        # zona OB in PREZZO (per notifica e analisi): l'id interno non dice
        # nulla a chi legge, i bordi della zona si'
        "ob_zone_low":  round(zl, 4),
        "ob_zone_high": round(zh, 4),

        # campi legacy LH DB — riusati per il contesto OB
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
        # serializzato: un dict non e' inseribile in una colonna SQLite.
        # Il dict resta disponibile in diagnostics["factors"].
        "confluence_factors": json.dumps(factors),

        "session":     session,
        "expiry_bars": P["expiry_bars"],
    }

    return {"signal": signal, "diagnostics": {
        "status": "SIGNAL_GENERATED", "score": score,
        "factors": factors, "ladder": ladder, "setup_state": state}}
