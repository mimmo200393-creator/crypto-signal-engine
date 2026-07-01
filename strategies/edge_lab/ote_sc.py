"""
strategies/edge_lab/ote_sc.py
Edge Lab — OTE Session Continuation V2

3 fix rispetto a V1:
    Fix 1: OTE calcolata sull'impulso (displacement/trend_health), non sulla sessione
    Fix 2: Entry a livello calcolato (PENDING), non a market
    Fix 3: SL strutturale max(swing, 1.5*ATR), non estremo sessione

Flusso a 2 fasi:
    Fase 1 — Setup Detection: calcola livello entry ottimale → PENDING
    Fase 2 — Fill Check: monitora se il prezzo raggiunge il livello → FILLED

Consuma market_context + structure_snapshot (dal MIE Layer 0).
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

import pandas as pd

from strategies.edge_lab.session_engine import check_ote_touch
from strategies.edge_lab.liquidity_engine import (
    find_nearest_liquidity_target,
    find_session_sl_extreme,
)
from strategies.edge_lab.market_context_engine import get_direction_context

logger = logging.getLogger("edge_lab.ote_sc")

STRATEGY_NAME    = "OTE-SC"
STRATEGY_VERSION = "V2-Pending"

# Parametri
CONFIRMATION_BODY_PCT = 0.50
EXPIRY_BARS_M15       = 96       # segnale attivo: 24h
PENDING_EXPIRY_BARS   = 24       # setup pendente: 6h max di attesa
MIN_RR_FLAG           = 1.5
MIN_RR_BLOCK          = 1.0
MAX_SL_ATR_MULT       = 3.0
MIN_QUALITY_LABEL     = "MEDIUM"
SL_ATR_MULTIPLIER     = 1.5      # SL minimo in multipli ATR


# ============================================================
# Fix 1: OTE sull'impulso
# ============================================================

def _compute_ote_from_impulse(structure_snapshot: dict, direction: str) -> dict | None:
    """
    Calcola la zona OTE sull'impulso reale, non sul range sessione.

    Priorita':
        1. Ultimo impulso dal trend_health (piu' preciso)
        2. Displacement dal structure_snapshot
        3. None (fallback alla sessione nel chiamante)
    """
    if structure_snapshot is None:
        return None

    # Prova 1: impulso dal trend_health
    impulses = structure_snapshot.get("trend_health", {}).get("impulses", [])
    if impulses:
        last = impulses[-1]
        high = max(last["start_price"], last["end_price"])
        low = min(last["start_price"], last["end_price"])
        rng = high - low
        if rng > 0:
            return _calc_fib(high, low, rng, direction, source="IMPULSE")

    # Prova 2: eventi BOS/CHOCH con ref_level
    events = structure_snapshot.get("events", [])
    struct_events = [e for e in events if e.get("type") in ("BOS", "CHOCH")]
    if struct_events:
        ev = struct_events[-1]
        ref = ev.get("ref_level")
        if ref:
            # Stima dell'impulso: dal ref_level al prezzo corrente
            current = structure_snapshot.get("current_price", 0)
            if current > 0 and ref > 0:
                high = max(ref, current)
                low = min(ref, current)
                rng = high - low
                if rng > 0:
                    return _calc_fib(high, low, rng, direction, source="BOS_EVENT")

    return None


def _calc_fib(high: float, low: float, rng: float,
              direction: str, source: str = "UNKNOWN") -> dict:
    """Calcola la zona Fibonacci OTE 61.8%-78.6%."""
    if direction == "BUY":
        fib_618 = high - 0.618 * rng
        fib_786 = high - 0.786 * rng
        ote_low = fib_786
        ote_high = fib_618
    else:
        fib_618 = low + 0.618 * rng
        fib_786 = low + 0.786 * rng
        ote_low = fib_618
        ote_high = fib_786

    return {
        "ote_low": ote_low,
        "ote_high": ote_high,
        "fib_618": fib_618,
        "fib_786": fib_786,
        "session_high": high,
        "session_low": low,
        "ote_source": source,
        "ote_midpoint": (ote_low + ote_high) / 2,
    }


# ============================================================
# Fix 3: SL strutturale
# ============================================================

def _compute_structural_sl(direction: str, entry: float, atr_m15: float,
                            structure_snapshot: dict = None,
                            session_sl: float = None) -> float:
    """
    SL = max(swing strutturale, 1.5*ATR) — il PIU' LONTANO dall'entry.
    Non piu' l'estremo sessione (spesso troppo vicino).
    """
    candidates = []

    # ATR-based minimum
    if atr_m15 > 0:
        if direction == "SELL":
            candidates.append(entry + SL_ATR_MULTIPLIER * atr_m15)
        else:
            candidates.append(entry - SL_ATR_MULTIPLIER * atr_m15)

    # Swing strutturale dallo Structure Engine
    if structure_snapshot:
        struct_m15 = structure_snapshot.get("structure_m15", {})
        struct_h4 = structure_snapshot.get("structure_h4", {})

        if direction == "SELL":
            # SL sopra l'ultimo HH o LH
            for key in ("last_hh", "last_lh"):
                val = struct_m15.get(key) or struct_h4.get(key)
                if val and val > entry:
                    candidates.append(val)
        else:
            # SL sotto l'ultimo LL o HL
            for key in ("last_ll", "last_hl"):
                val = struct_m15.get(key) or struct_h4.get(key)
                if val and val < entry:
                    candidates.append(val)

    # Session extreme come fallback
    if session_sl is not None:
        candidates.append(session_sl)

    if not candidates:
        # Ultimo fallback: 2 ATR
        if direction == "SELL":
            return entry + 2.0 * atr_m15 if atr_m15 > 0 else entry * 1.005
        else:
            return entry - 2.0 * atr_m15 if atr_m15 > 0 else entry * 0.995

    # Per SELL: SL è SOPRA → prendi il più alto (più sicuro)
    # Per BUY: SL è SOTTO → prendi il più basso (più sicuro)
    if direction == "SELL":
        return max(candidates)
    else:
        return min(candidates)


# ============================================================
# Quality Score
# ============================================================

def _compute_quality_score(dir_ctx: dict, rr: float, flags: list,
                            ote_source: str = "SESSION",
                            ob_in_zone: bool = False,
                            displacement: bool = False) -> tuple[int, str]:
    score = 0
    score += 3  # trend allineato (prerequisito)

    if dir_ctx.get("sr_reaction", False):
        score += 2
    if dir_ctx.get("sr_score", 0.0) >= 0.5:
        score += 1
    if rr >= 3.0:
        score += 3
    elif rr >= 2.0:
        score += 2
    elif rr >= 1.5:
        score += 1

    # Bonus V2
    if ote_source == "IMPULSE":
        score += 1
    if ob_in_zone:
        score += 1
    if displacement:
        score += 1

    score -= len(flags)
    score = max(0, min(score, 12))

    if score >= 8:
        label = "HIGH"
    elif score >= 5:
        label = "MEDIUM"
    else:
        label = "LOW"

    return score, label


# ============================================================
# Fase 1: Setup Detection → PENDING
# ============================================================

def detect_ote_setup(
    market_ctx: dict,
    df_m15: pd.DataFrame,
    direction: str,
    structure_snapshot: dict = None,
    ob_snapshot: dict = None,
    fvg_snapshot: dict = None,
) -> dict:
    """
    Fase 1: cerca un setup OTE valido e ritorna un PENDING setup
    con il livello di entry calcolato — non un segnale.
    """
    diag = {
        "strategy": STRATEGY_NAME,
        "direction": direction,
        "phase": "SETUP_DETECTION",
        "rejection": None,
    }

    def reject(reason):
        diag["rejection"] = reason
        logger.info("OTE-SC [%s %s]: REJECT %s",
                     market_ctx.get("asset", "?"), direction, reason)
        return {"setup": None, "diagnostics": diag}

    # ── Gate 0: tradeable ────────────────────────────────────
    dir_ctx = get_direction_context(market_ctx, direction)
    if not dir_ctx["is_tradeable"]:
        return reject(f"MARKET_NOT_TRADEABLE ({', '.join(dir_ctx['block_reasons'])})")

    # ── Gate 1: trend ────────────────────────────────────────
    if not dir_ctx["trend_ok"]:
        return reject(f"TREND_NOT_ALIGNED (combined={market_ctx['trend']['combined']})")

    # ── Gate 2: OTE zone (Fix 1: prova impulso prima) ───────
    atr_m15 = (market_ctx.get("volatility") or {}).get("atr_m15", 0.0)

    # Prova OTE sull'impulso
    ote_zone = _compute_ote_from_impulse(structure_snapshot, direction)
    ote_source = "IMPULSE" if ote_zone else "SESSION"

    # Fallback: OTE sulla sessione
    if ote_zone is None:
        session_ote = dir_ctx.get("ote_zone")
        if session_ote:
            ote_zone = dict(session_ote)
            ote_zone["ote_source"] = "SESSION"
            ote_zone["ote_midpoint"] = (session_ote["ote_low"] + session_ote["ote_high"]) / 2

    if ote_zone is None:
        return reject("OTE_ZONE_UNAVAILABLE")

    diag["ote_zone"] = ote_zone
    diag["ote_source"] = ote_source

    # ── Gate 3: OTE Touch (almeno una candela recente nella zona) ──
    touch_idx = None
    for i in range(max(0, len(df_m15) - 10), len(df_m15)):
        candle = df_m15.iloc[i]
        if check_ote_touch(candle, ote_zone):
            touch_idx = i
            break

    if touch_idx is None:
        return reject("NO_OTE_TOUCH")

    # ── Fix 2: Entry a livello calcolato ─────────────────────
    entry_level = ote_zone.get("ote_midpoint", (ote_zone["ote_low"] + ote_zone["ote_high"]) / 2)

    # Se un OB fresco coincide con la zona OTE, usa il livello OB
    ob_in_zone = False
    if ob_snapshot:
        for ob in ob_snapshot.get("order_blocks", []):
            if ob.get("status") != "FRESH":
                continue
            ob_mid = (ob["zone_high"] + ob["zone_low"]) / 2
            if ote_zone["ote_low"] <= ob_mid <= ote_zone["ote_high"]:
                if direction == "SELL":
                    entry_level = ob["zone_low"]  # entry più alto per SELL
                else:
                    entry_level = ob["zone_high"]  # entry più basso per BUY
                ob_in_zone = True
                break

    # ── Fix 3: SL strutturale ────────────────────────────────
    liq_map = dir_ctx.get("liq_map")
    session_sl = find_session_sl_extreme(liq_map, direction) if liq_map else None

    sl = _compute_structural_sl(
        direction, entry_level, atr_m15,
        structure_snapshot, session_sl
    )

    # Validazione SL
    if direction == "SELL" and sl <= entry_level:
        return reject(f"SL_INVALID (sl={sl:.4f} <= entry={entry_level:.4f})")
    if direction == "BUY" and sl >= entry_level:
        return reject(f"SL_INVALID (sl={sl:.4f} >= entry={entry_level:.4f})")

    # ── TP: nearest liquidity target ─────────────────────────
    tp_level = find_nearest_liquidity_target(liq_map, entry_level, direction) if liq_map else None
    if tp_level is None:
        return reject("NO_LIQUIDITY_TARGET")

    tp = float(tp_level["price"])

    # ── RR e flags ───────────────────────────────────────────
    risk = abs(entry_level - sl)
    reward = abs(tp - entry_level)
    rr = reward / risk if risk > 0 else 0
    flags = []
    if rr < MIN_RR_FLAG:
        flags.append(f"RR_TOO_LOW ({rr:.2f} < {MIN_RR_FLAG})")
    if atr_m15 > 0 and risk > MAX_SL_ATR_MULT * atr_m15:
        flags.append(f"STOP_TOO_WIDE ({risk:.4f} > {MAX_SL_ATR_MULT}x ATR)")

    if rr < MIN_RR_BLOCK:
        return reject(f"RR_TOO_LOW_BLOCK (rr={rr:.2f})")

    # ── Quality ──────────────────────────────────────────────
    disp_confirmed = False
    if structure_snapshot:
        disp_confirmed = structure_snapshot.get("displacement", {}).get("confirmed", False)

    quality_score, quality_label = _compute_quality_score(
        dir_ctx, rr, flags, ote_source, ob_in_zone, disp_confirmed
    )

    if quality_label == "LOW":
        return reject(f"QUALITY_TOO_LOW (score={quality_score})")

    # ── Costruisci PENDING setup ─────────────────────────────
    asset = market_ctx.get("asset", "UNKNOWN")
    setup_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)

    setup = {
        "setup_id":         setup_id,
        "strategy_name":    STRATEGY_NAME,
        "strategy_version": STRATEGY_VERSION,
        "asset":            asset,
        "direction":        direction,
        "status":           "PENDING",

        # Livelli
        "pending_entry":    round(entry_level, 4),
        "stop_loss":        round(sl, 4),
        "take_profit":      round(tp, 4),
        "rr":               round(rr, 4),

        # OTE
        "ote_source":       ote_source,
        "ote_low":          ote_zone["ote_low"],
        "ote_high":         ote_zone["ote_high"],

        # Timing
        "created_at":       now.isoformat(),
        "expiry_bars":      PENDING_EXPIRY_BARS,
        "bars_waiting":     0,

        # Contesto
        "session":          dir_ctx.get("session"),
        "ref_session":      dir_ctx.get("ref_session"),
        "trend_combined":   market_ctx["trend"]["combined"],
        "quality_score":    quality_score,
        "quality_label":    quality_label,
        "ob_in_zone":       ob_in_zone,
        "displacement":     disp_confirmed,
        "tradeability_flags": flags,

        # Target info
        "liquidity_target":       tp_level.get("label"),
        "liquidity_target_price": tp,
        "liquidity_target_priority": tp_level.get("priority_label"),
    }

    logger.info(
        "OTE-SC [%s %s]: PENDING setup @ %.4f (ote_src=%s) "
        "sl=%.4f tp=%.4f rr=%.2f quality=%d/%s ob=%s disp=%s",
        asset, direction, entry_level, ote_source,
        sl, tp, rr, quality_score, quality_label,
        ob_in_zone, disp_confirmed,
    )

    return {"setup": setup, "diagnostics": diag}


# ============================================================
# Fase 2: Fill Check → FILLED / INVALIDATED / EXPIRED
# ============================================================

def check_pending_setup(setup: dict, df_m15: pd.DataFrame) -> dict:
    """
    Controlla se un setup PENDING e' stato filled, invalidato, o scaduto.

    Ritorna il setup aggiornato con status modificato.
    """
    if setup["status"] != "PENDING":
        return setup

    setup["bars_waiting"] += 1

    if len(df_m15) < 1:
        return setup

    last = df_m15.iloc[-1]
    current_high = float(last["high"])
    current_low = float(last["low"])
    direction = setup["direction"]
    entry = setup["pending_entry"]
    sl = setup["stop_loss"]

    # Check scadenza
    if setup["bars_waiting"] >= setup["expiry_bars"]:
        setup["status"] = "EXPIRED"
        logger.info("OTE-SC [%s %s]: EXPIRED dopo %d barre",
                     setup["asset"], direction, setup["bars_waiting"])
        return setup

    # Check invalidazione (SL raggiunto prima dell'entry)
    if direction == "SELL" and current_high >= sl:
        setup["status"] = "INVALIDATED"
        setup["invalidated_at"] = datetime.now(timezone.utc).isoformat()
        logger.info("OTE-SC [%s %s]: INVALIDATED (high=%.4f >= sl=%.4f)",
                     setup["asset"], direction, current_high, sl)
        return setup
    if direction == "BUY" and current_low <= sl:
        setup["status"] = "INVALIDATED"
        setup["invalidated_at"] = datetime.now(timezone.utc).isoformat()
        logger.info("OTE-SC [%s %s]: INVALIDATED (low=%.4f <= sl=%.4f)",
                     setup["asset"], direction, current_low, sl)
        return setup

    # Check fill (prezzo raggiunge il livello di entry)
    if direction == "SELL" and current_high >= entry:
        setup["status"] = "FILLED"
        setup["filled_at"] = datetime.now(timezone.utc).isoformat()
        logger.info("OTE-SC [%s %s]: FILLED @ %.4f dopo %d barre",
                     setup["asset"], direction, entry, setup["bars_waiting"])
        return setup
    if direction == "BUY" and current_low <= entry:
        setup["status"] = "FILLED"
        setup["filled_at"] = datetime.now(timezone.utc).isoformat()
        logger.info("OTE-SC [%s %s]: FILLED @ %.4f dopo %d barre",
                     setup["asset"], direction, entry, setup["bars_waiting"])
        return setup

    return setup


def create_signal_from_setup(setup: dict) -> dict:
    """Converte un setup FILLED in un segnale tradizionale."""
    return {
        "signal_id":        str(uuid.uuid4()),
        "strategy_name":    STRATEGY_NAME,
        "strategy_version": STRATEGY_VERSION,
        "asset":            setup["asset"],
        "direction":        setup["direction"],
        "timestamp_setup":  setup.get("filled_at", setup["created_at"]),

        "entry":            setup["pending_entry"],
        "stop_loss":        setup["stop_loss"],
        "tp":               setup["take_profit"],
        "rr":               setup["rr"],

        "ote_low":          setup["ote_low"],
        "ote_high":         setup["ote_high"],

        "liquidity_target":          setup.get("liquidity_target"),
        "liquidity_target_price":    setup.get("liquidity_target_price"),
        "liquidity_target_priority": setup.get("liquidity_target_priority"),

        "sl_source":        "STRUCTURAL_V2",
        "ote_source":       setup.get("ote_source", "SESSION"),
        "pending_bars":     setup.get("bars_waiting", 0),

        "session":          setup.get("session"),
        "ref_session":      setup.get("ref_session"),
        "trend_combined":   setup.get("trend_combined"),
        "quality_score":    setup.get("quality_score"),
        "quality_label":    setup.get("quality_label"),
        "ob_in_zone":       setup.get("ob_in_zone", False),
        "displacement":     setup.get("displacement", False),
        "tradeability_flags": setup.get("tradeability_flags", []),

        "final_outcome":    "OPEN",
        "expiry_bars":      EXPIRY_BARS_M15,
    }


# ============================================================
# Compatibility: generate_ote_sc_signal (wrapper V1 → V2)
# ============================================================

def generate_ote_sc_signal(market_ctx: dict, df_m15: pd.DataFrame,
                            direction: str) -> dict:
    """
    Wrapper di compatibilita' con il runner esistente.
    Chiama detect_ote_setup e, se trova un setup, lo ritorna
    come segnale (per il flusso attuale senza pending).

    Il runner edge_lab puo' usare direttamente detect_ote_setup
    + check_pending_setup per il flusso a 2 fasi.
    """
    # Prova a ottenere structure_snapshot dal market_ctx
    structure_snapshot = market_ctx.get("structure_snapshot")

    result = detect_ote_setup(
        market_ctx, df_m15, direction,
        structure_snapshot=structure_snapshot,
    )

    if result["setup"] is None:
        return {"signal": None, "diagnostics": result["diagnostics"]}

    # Per compatibilita': converti setup in segnale immediato
    signal = create_signal_from_setup(result["setup"])
    return {"signal": signal, "diagnostics": result["diagnostics"]}


def is_signal_expired(signal: dict) -> bool:
    expiry = signal.get("expiry_bars", EXPIRY_BARS_M15)
    bars_open = signal.get("bars_open", 0)
    return bars_open >= expiry
