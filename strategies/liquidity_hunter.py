"""
strategies/liquidity_hunter.py
Liquidity Hunter v2.0 — Confluence Sniper

Entry su Order Block con bias allineato e confluenza multi-sensore.
M15 per contesto, M5 per entry precisa (solo XAU).

5 condizioni obbligatorie + 1 trigger (tutte devono essere vere):
    1. Bias BULLISH o BEARISH (no NEUTRAL)
    2. OB FRESH o BREAKER vicino nella direzione del bias
    3. Premium/Discount corretto (BUY in DISCOUNT, SELL in PREMIUM)
    4. Sessione attiva (Asia+London+NY per XAU, London+NY per BTC)
    5. No blackout macro
    6. Candlestick confirmation (pattern di reazione sulla zona OB)

Entry: prezzo tocca/entra nella zona OB
SL:    oltre zona OB + buffer ATR
TP:    primo target raggiungibile (OB opposto o livello liquidita')
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

import pandas as pd

logger = logging.getLogger("liquidity_hunter")

STRATEGY_NAME    = "LH"
STRATEGY_VERSION = "v2.0"

# ── Configurazione ──────────────────────────────────────────
# CALIBRAZIONE 23/07/2026 — su geometria REALE delle zone OB (2.876 zone
# FRESH/BREAKER misurate negli snapshot).
#
# DIAGNOSI del blocco (LH v2 = 0 segnali dal 19/07):
#   larghezza mediana zona OB:  BTC 0.44 ATR  |  XAU 1.84 ATR
#   Su XAU lo SL va oltre TUTTA la zona (+buffer) -> rischio ~1.3 ATR,
#   ma il TP era FISSO a 0.8 ATR -> RR = 0.61 < MIN_RR 1.0
#   -> il 97% dei segnali XAU moriva all'ultimo controllo.
#
# FIX: il TP deve SCALARE col rischio, non essere fisso. Su BTC (zone
# strette) lo scalping funzionava gia' (76% passava) e resta invariato.
OB_PROXIMITY_PCT      = 0.010    # 1.0% — distanza max OB dal prezzo

# Parametri per asset: la geometria delle zone e' troppo diversa per
# usare gli stessi numeri (BTC zone strette = scalping; XAU zone larghe
# = trade strutturale piu' lento).
ASSET_PARAMS = {
    "BTC_USDT": {
        "sl_buffer_atr":    0.3,
        "min_rr":           1.0,
        "expiry_bars":      6,     # 30 min su M5 — scalping
        "scalp_tp_atr":     0.8,   # TP fisso: su BTC e' raggiungibile
        "max_zone_atr":     2.0,   # scarta zone abnormi (bassa precisione entry)
        "tp_mode":          "fixed",
    },
    "XAU_USD": {
        "sl_buffer_atr":    0.3,
        "min_rr":           1.5,
        "expiry_bars":      18,    # 90 min su M5 — NON e' piu' scalping:
                                   # il rischio e' ~1 ATR, serve tempo al TP
        "scalp_tp_atr":     0.8,   # usato solo come pavimento minimo
        "max_zone_atr":     2.0,   # tiene il 61% delle zone, scarta le peggiori
        "tp_mode":          "proportional",   # TP = min_rr * rischio
    },
}
DEFAULT_PARAMS = ASSET_PARAMS["BTC_USDT"]

# Retrocompatibilita' (alcuni runner leggono questi nomi)
SL_BUFFER_ATR_MULT    = 0.3
MIN_RR                = 1.0
EXPIRY_BARS           = 6
SCALP_TP_ATR_MULT     = 0.8


def _params(asset: str) -> dict:
    return ASSET_PARAMS.get(asset, DEFAULT_PARAMS)

# Sessioni ammesse per asset
ALLOWED_SESSIONS = {
    "XAU_USD":  ("ASIA", "LONDON", "NEW_YORK"),
    "BTC_USDT": ("LONDON", "NEW_YORK"),
}


def _get_session(now: datetime) -> str:
    """Sessione di mercato in UTC."""
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


# ============================================================
# Core: trova OB candidato dal MIE context
# ============================================================

def _find_best_ob(mie_context: dict, bias: str, current_price: float,
                  max_zone_atr: float = 99, atr_m15: float = 0) -> Optional[dict]:
    """
    Cerca l'OB migliore (FRESH o BREAKER) nella direzione del bias,
    entro OB_PROXIMITY_PCT dal prezzo corrente.
    Priorita': FRESH > BREAKER, poi per distanza piu' vicina.

    NUOVO: scarta le zone troppo larghe (> max_zone_atr * ATR). Una zona
    molto ampia da' un entry impreciso e un rischio enorme: su XAU il p75
    e' 3.15 ATR, zone del genere rendono il trade ingestibile.
    """
    want_dir = bias  # "BULLISH" o "BEARISH"
    ob_list = mie_context.get("mie_order_block_order_blocks") or []

    best, best_dist, best_priority = None, None, 99

    for ob in ob_list:
        if ob.get("direction") != want_dir:
            continue
        status = ob.get("status")
        if status not in ("FRESH", "BREAKER"):
            continue

        zh = ob.get("zone_high")
        zl = ob.get("zone_low")
        if zh is None or zl is None:
            continue

        # Filtro larghezza zona (nuovo)
        if atr_m15 and atr_m15 > 0:
            zone_w = abs(float(zh) - float(zl))
            if zone_w / atr_m15 > max_zone_atr:
                continue

        mid = (float(zh) + float(zl)) / 2
        dist_pct = abs(current_price - mid) / current_price if current_price > 0 else 1

        if dist_pct > OB_PROXIMITY_PCT:
            continue

        # Priorita': FRESH=0, BREAKER=1
        priority = 0 if status == "FRESH" else 1

        if priority < best_priority or (priority == best_priority and
                                         (best_dist is None or dist_pct < best_dist)):
            best = ob
            best_dist = dist_pct
            best_priority = priority

    return best


def _price_in_ob_zone(ob: dict, current_high: float, current_low: float) -> bool:
    """La candela corrente tocca/entra nella zona OB?"""
    zh = float(ob["zone_high"])
    zl = float(ob["zone_low"])
    return current_low <= zh and current_high >= zl


# ============================================================
# Target: primo livello raggiungibile
# ============================================================

def _find_tp_target(mie_context: dict, direction: str,
                    entry: float, current_price: float,
                    atr_m15: float, risk: float = 0,
                    params: dict = None) -> tuple:
    """
    TP: primo target STRUTTURALE raggiungibile che soddisfi l'RR minimo.

    Ordine di preferenza (principio MIE: il TP punta a struttura/liquidita',
    non a una formula):
      1. OB opposto o livello di liquidita' vicino CHE DIA RR >= min_rr
      2. fallback proporzionale: TP = min_rr * rischio  (tp_mode
         "proportional") oppure TP fisso 0.8 ATR (tp_mode "fixed")

    FIX 23/07: prima la funzione restituiva None implicitamente quando
    atr_m15 <= 0 (nessun ramo else) -> reject NO_TP_TARGET. Ora c'e'
    sempre un valore di ritorno.

    Il tp_label distingue i target strutturali da quelli scalati, cosi'
    si potra' misurare a posteriori quali rendono di piu'.
    """
    if params is None:
        params = DEFAULT_PARAMS
    min_rr = params.get("min_rr", 1.0)
    max_structural_dist = 1.5 * atr_m15 if atr_m15 > 0 else 0
    targets = []

    # ── OB opposti vicini ────────────────────────────────────
    opposite_dir = "BEARISH" if direction == "BUY" else "BULLISH"
    ob_list = mie_context.get("mie_order_block_order_blocks") or []
    for ob in ob_list:
        if ob.get("direction") != opposite_dir:
            continue
        if ob.get("status") in ("INVALIDATED",):
            continue
        mid = ob.get("zone_midpoint")
        if mid is None:
            zh, zl = ob.get("zone_high"), ob.get("zone_low")
            if zh is None or zl is None:
                continue
            mid = (float(zh) + float(zl)) / 2
        mid = float(mid)
        dist = abs(mid - entry)
        if max_structural_dist and dist > max_structural_dist:
            continue
        if direction == "BUY" and mid > entry:
            targets.append((mid, f"OB_{ob.get('id', '?')[:4]}"))
        elif direction == "SELL" and mid < entry:
            targets.append((mid, f"OB_{ob.get('id', '?')[:4]}"))

    # ── Livelli di liquidita' vicini ─────────────────────────
    if direction == "BUY":
        liq_targets = mie_context.get("mie_liquidity_buy_targets") or []
    else:
        liq_targets = mie_context.get("mie_liquidity_sell_targets") or []

    for lv in liq_targets:
        price = lv.get("price", 0)
        if price <= 0:
            continue
        dist = abs(price - entry)
        if max_structural_dist and dist > max_structural_dist:
            continue
        if direction == "BUY" and price > entry:
            targets.append((price, lv.get("label", "LIQ")))
        elif direction == "SELL" and price < entry:
            targets.append((price, lv.get("label", "LIQ")))

    # Target strutturale piu' vicino CHE SODDISFI L'RR
    if targets and risk > 0:
        targets.sort(key=lambda t: abs(t[0] - entry))
        for tp, label in targets:
            if abs(tp - entry) / risk >= min_rr:
                return (round(tp, 4), label)
    elif targets:
        targets.sort(key=lambda t: abs(t[0] - entry))
        return targets[0]

    # ── Fallback ─────────────────────────────────────────────
    tp_mode = params.get("tp_mode", "fixed")
    scalp_mult = params.get("scalp_tp_atr", 0.8)

    if tp_mode == "proportional" and risk > 0:
        # TP scalato sul rischio: garantisce RR >= min_rr per costruzione.
        # Necessario dove le zone OB sono larghe (XAU: mediana 1.84 ATR),
        # perche' un TP fisso da' RR 0.61 e il segnale viene sempre scartato.
        tp_dist = max(min_rr * risk * 1.002, scalp_mult * atr_m15 if atr_m15 > 0 else 0)
        label = "RR_SCALED"
    elif atr_m15 > 0:
        tp_dist = scalp_mult * atr_m15
        label = "SCALP_ATR"
    elif risk > 0:
        tp_dist = min_rr * risk        # ultimo fallback: niente ATR
        label = "RR_SCALED_NOATR"
    else:
        return (None, None)            # non piu' None implicito

    tp = entry + tp_dist if direction == "BUY" else entry - tp_dist
    return (round(tp, 4), label)


# ============================================================
# Quality Score
# ============================================================

def _compute_quality(ob: dict, mie_context: dict, bias_confidence: int) -> tuple:
    """
    Quality score 0-7 (informativo, non gate).
    Ritorna (score, label).
    """
    score = 0

    # +2  OB quality_score >= 5
    if ob.get("quality_score", 0) >= 5:
        score += 2

    # +1  OB ha FVG associata
    if ob.get("has_fvg"):
        score += 1

    # +1  OB ha sweep before
    if ob.get("has_sweep_before"):
        score += 1

    # +1  bias_confidence >= 50
    if bias_confidence >= 50:
        score += 1

    # +1  displacement confermato
    disp = mie_context.get("mie_structure_displacement", {})
    if isinstance(disp, dict) and disp.get("confirmed"):
        score += 1

    # +1  candlestick confirmation
    if mie_context.get("mie_candlestick_has_confirmation"):
        score += 1

    if score >= 5:
        label = "HIGH"
    elif score >= 3:
        label = "MEDIUM"
    else:
        label = "LOW"

    return score, label


# ============================================================
# Entry Point
# ============================================================

def generate_lh_signal(
    asset: str,
    df_m15: pd.DataFrame,
    now: datetime,
    mie_context: dict = None,
    df_m5: pd.DataFrame = None,
) -> dict:
    """
    LH v2.0 — Confluence Sniper.

    Ritorna {"signal": dict | None, "diagnostics": dict}.
    """
    if not mie_context:
        return _reject("NO_MIE_CONTEXT")

    # Parametri calibrati per asset (geometria zone OB molto diversa)
    P = _params(asset)

    # ── 1. SESSIONE ──────────────────────────────────────────
    session = _get_session(now)
    allowed = ALLOWED_SESSIONS.get(asset, ("LONDON", "NEW_YORK"))
    if session not in allowed:
        return _reject(f"SESSION_{session}_NOT_ALLOWED")

    # ── 2. BIAS ──────────────────────────────────────────────
    bias = mie_context.get("mie_market_state_bias", "NEUTRAL")
    bias_confidence = mie_context.get("mie_market_state_bias_confidence", 0)
    if bias == "NEUTRAL":
        return _reject("BIAS_NEUTRAL")

    # ── 3. MACRO BLACKOUT ────────────────────────────────────
    if mie_context.get("mie_macro_is_blackout"):
        return _reject("MACRO_BLACKOUT")

    # ── 4. PREMIUM/DISCOUNT ──────────────────────────────────
    pd_zone = mie_context.get("mie_structure_premium_discount", {})
    if isinstance(pd_zone, dict):
        zone = pd_zone.get("zone", "EQUILIBRIUM")
    else:
        zone = "EQUILIBRIUM"

    direction = "BUY" if bias == "BULLISH" else "SELL"

    if direction == "BUY" and zone == "PREMIUM":
        return _reject(f"BUY_IN_PREMIUM (zone={zone})")
    if direction == "SELL" and zone == "DISCOUNT":
        return _reject(f"SELL_IN_DISCOUNT (zone={zone})")

    # ── 5. TROVA OB ──────────────────────────────────────────
    # Prezzo corrente dalla candela piu' recente disponibile
    if df_m5 is not None and len(df_m5) > 0:
        last = df_m5.iloc[-1]
    else:
        last = df_m15.iloc[-1]

    current_price = float(last["close"])
    current_high  = float(last["high"])
    current_low   = float(last["low"])

    # ── ATR M15 (serve PRIMA, per il filtro larghezza zona) ──
    atr_m15 = mie_context.get("mie_volatility_atr_m15", 0)
    if not atr_m15 or atr_m15 <= 0:
        if len(df_m15) >= 15:
            highs = df_m15["high"].astype(float).values
            lows  = df_m15["low"].astype(float).values
            closes = df_m15["close"].astype(float).values
            trs = []
            for i in range(-14, 0):
                tr = max(highs[i]-lows[i], abs(highs[i]-closes[i-1]),
                         abs(lows[i]-closes[i-1]))
                trs.append(tr)
            atr_m15 = sum(trs) / len(trs)
        else:
            atr_m15 = 0

    ob = _find_best_ob(mie_context, bias, current_price,
                       max_zone_atr=P["max_zone_atr"], atr_m15=atr_m15)
    if ob is None:
        return _reject("NO_OB_NEARBY")

    # ── 6. PREZZO TOCCA LA ZONA OB ───────────────────────────
    if not _price_in_ob_zone(ob, current_high, current_low):
        ob_mid = (float(ob["zone_high"]) + float(ob["zone_low"])) / 2
        dist = abs(current_price - ob_mid) / current_price
        return _reject(f"PRICE_NOT_AT_OB (dist={dist:.4f})")

    # ── 7. CANDLESTICK CONFIRMATION ──────────────────────────
    if not mie_context.get("mie_candlestick_has_confirmation"):
        return _reject("NO_CANDLESTICK_CONFIRMATION")

    # ══════════════════════════════════════════════════════════
    # TUTTE LE CONDIZIONI SODDISFATTE — calcola Entry/SL/TP
    # ══════════════════════════════════════════════════════════

    zh = float(ob["zone_high"])
    zl = float(ob["zone_low"])

    if atr_m15 <= 0:
        atr_m15 = abs(zh - zl) * 2  # fallback estremo

    buffer = P["sl_buffer_atr"] * atr_m15

    # Entry / SL — lo SL resta al punto di invalidazione STRUTTURALE
    # (oltre la zona OB), come da metodologia. Non lo stringiamo:
    # e' il TP che si adatta al rischio, non il contrario.
    entry = current_price
    if direction == "BUY":
        sl = zl - buffer
    else:
        sl = zh + buffer

    risk = abs(entry - sl)
    if risk <= 0:
        return _reject("ZERO_RISK")

    # TP — ora riceve il rischio: preferisce target strutturali che
    # soddisfano l'RR, altrimenti scala (tp_label distingue i due casi)
    tp_price, tp_label = _find_tp_target(mie_context, direction, entry,
                                          current_price, atr_m15,
                                          risk=risk, params=P)
    if tp_price is None:
        return _reject("NO_TP_TARGET")

    reward = abs(tp_price - entry)
    rr = reward / risk if risk > 0 else 0

    if rr < P["min_rr"] - 1e-6:   # tolleranza: il round() del TP puo' limare l'RR
        return _reject(f"RR_TOO_LOW ({rr:.2f} < {P['min_rr']})")

    # Quality
    quality_score, quality_label = _compute_quality(ob, mie_context, bias_confidence)

    # ── Costruisci segnale ───────────────────────────────────
    signal = {
        "signal_id":            str(uuid.uuid4()),
        "strategy_name":        STRATEGY_NAME,
        "strategy_version":     STRATEGY_VERSION,
        "asset":                asset,
        "direction":            direction,
        "timestamp_setup":      now.isoformat(),

        "entry":                round(entry, 4),
        "stop_loss":            round(sl, 4),
        "tp":                   round(tp_price, 4),
        "risk":                 round(risk, 4),
        "rr":                   round(rr, 2),

        # Campi legacy LH DB — riutilizzati per OB context
        "swept_level_label":    ob.get("id", "?"),           # OB id (per dedup)
        "swept_level_price":    round((zh + zl) / 2, 4),     # OB midpoint
        "swept_level_priority": ob.get("status", "FRESH"),   # FRESH/BREAKER
        "swept_level_touches":  ob.get("test_count", 0),
        "sweep_direction":      bias,                        # bias direction
        "sweep_peak_price":     zh if direction == "BUY" else zl,
        "sweep_penetration":    0,
        "sweep_penetration_pct": 0,

        "flag_bos_present":     False,
        "flag_choch_present":   False,
        "flag_trigger_present": True,
        "flag_near_order_block": True,
        "flag_near_fvg":        bool(ob.get("has_fvg")),
        "ob_quality":           ob.get("quality_score"),
        "ob_match_type":        ob.get("status"),
        "pool_type":            f"OB_{ob.get('status', 'FRESH')}",
        "flag_htf_pool":        False,
        "confluence_count":     6,  # tutte e 6 le condizioni soddisfatte

        "trigger_type":         "OB_TOUCH",
        "trigger_ref_level":    round((zh + zl) / 2, 4),

        "tp_label":             tp_label,
        "tp_priority":          "FIRST_REACHABLE",

        "quality_score":        quality_score,
        "quality_label":        quality_label,

        "session":              session,
        "expiry_bars":          P["expiry_bars"],
    }

    return {"signal": signal, "diagnostics": {"status": "SIGNAL_GENERATED"}}
