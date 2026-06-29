"""
core/structure_engine_v2.py
Structure Engine V2.0 — Sprint 1

Fonte unica di verita' per la struttura di mercato dell'intero MIE.
Nessun altro modulo deve calcolare swing, BOS, CHOCH, o classificare
la struttura. Tutti consumano lo StructureSnapshot prodotto da qui.

Sprint 1 include:
    - Swing Points con lookback configurabile
    - Classificazione struttura (HH/HL/LH/LL → BULLISH/BEARISH/NEUTRAL)
    - BOS V2 (lookback=3, persistenza, penetrazione minima)
    - CHOCH V2 (struttura M15 reale, non confronto con H4)
    - Pullback Invalidation
    - Structure Confidence (0-100)
    - Event History con bars_since_bos, bars_since_choch
    - Volume Ratio e Premium/Discount (informativi)
    - Snapshot versioning + config per riproducibilita'

Sprint 2 aggiungerà: Trend Health (impulse counting, phase detection)
Sprint 3 aggiungerà: Displacement detection integrato

Dipendenze: solo pandas, numpy, sqlite3, logging.
Non importa NULLA dal progetto. I consumatori importano da qui.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Optional

import numpy as np
import pandas as pd

from core.structure_db import (
    init_structure_schema,
    get_state,
    upsert_state,
    insert_snapshot,
)

logger = logging.getLogger("structure_engine_v2")

# ============================================================
# Versione e configurazione
# ============================================================

SNAPSHOT_VERSION = "2.0.0-sprint1"

DEFAULT_CONFIG = {
    "bos_lookback": 3,
    "bos_persistence": 3,
    "bos_min_penetration_pct": 0.0003,
    "choch_lookback": 3,
    "choch_min_pivots": 2,
    "h4_pivot_lookback": 3,
    "volume_avg_period": 20,
    "event_history_max": 20,
}


# ============================================================
# Swing Points
# ============================================================

def _find_pivots(df: pd.DataFrame, lookback: int, max_pivots: int = 10) -> dict:
    """
    Trova pivot highs e lows. Ritorna liste di dict ordinate per
    timestamp crescente (i piu' recenti alla fine).
    """
    if len(df) < lookback * 2 + 1:
        return {"highs": [], "lows": []}

    highs_arr = df["high"].values
    lows_arr = df["low"].values
    timestamps = df["timestamp"].values
    n = len(df)

    pivot_highs = []
    pivot_lows = []

    for i in range(lookback, n - lookback):
        before_h = highs_arr[i - lookback:i]
        after_h = highs_arr[i + 1:i + 1 + lookback]
        if highs_arr[i] > before_h.max() and highs_arr[i] > after_h.max():
            pivot_highs.append({
                "price": float(highs_arr[i]),
                "index": i,
                "timestamp": int(timestamps[i]),
            })

        before_l = lows_arr[i - lookback:i]
        after_l = lows_arr[i + 1:i + 1 + lookback]
        if lows_arr[i] < before_l.min() and lows_arr[i] < after_l.min():
            pivot_lows.append({
                "price": float(lows_arr[i]),
                "index": i,
                "timestamp": int(timestamps[i]),
            })

    return {
        "highs": pivot_highs[-max_pivots:],
        "lows": pivot_lows[-max_pivots:],
    }


def _find_latest_swing(df: pd.DataFrame, swing_type: str,
                        lookback: int) -> Optional[dict]:
    """Trova lo swing piu' recente (high o low)."""
    pivots = _find_pivots(df, lookback)
    items = pivots["highs"] if swing_type == "high" else pivots["lows"]
    return items[-1] if items else None


# ============================================================
# Classificazione struttura
# ============================================================

def _classify_structure(pivots: dict, min_pivots: int = 2) -> dict:
    """
    Classifica la struttura di mercato dalla sequenza di pivot.

    BULLISH: ultimi 2 highs sono HH E ultimi 2 lows sono HL
    BEARISH: ultimi 2 highs sono LH E ultimi 2 lows sono LL
    NEUTRAL: pivot insufficienti o sequenza mista

    Ritorna anche i livelli chiave (ultimo HH, HL, LH, LL).
    """
    highs = pivots["highs"]
    lows = pivots["lows"]

    result = {
        "classification": "NEUTRAL",
        "last_hh": None, "last_hl": None,
        "last_lh": None, "last_ll": None,
        "pivot_count": len(highs) + len(lows),
    }

    if len(highs) < min_pivots or len(lows) < min_pivots:
        return result

    last_highs = highs[-2:]
    last_lows = lows[-2:]

    hh = last_highs[1]["price"] > last_highs[0]["price"]
    hl = last_lows[1]["price"] > last_lows[0]["price"]
    lh = last_highs[1]["price"] < last_highs[0]["price"]
    ll = last_lows[1]["price"] < last_lows[0]["price"]

    if hh and hl:
        result["classification"] = "BULLISH"
        result["last_hh"] = last_highs[1]["price"]
        result["last_hl"] = last_lows[1]["price"]
    elif lh and ll:
        result["classification"] = "BEARISH"
        result["last_lh"] = last_highs[1]["price"]
        result["last_ll"] = last_lows[1]["price"]

    return result


# ============================================================
# BOS Detection
# ============================================================

def _detect_bos(df: pd.DataFrame, structure: dict, direction: str,
                cfg: dict) -> Optional[dict]:
    """
    BOS V2: Break of Structure nella direzione del trend.

    Cerca nelle ultime `persistence` candele se il close ha rotto
    lo swing di riferimento nella direzione attesa.
    """
    lookback = cfg["bos_lookback"]
    persistence = cfg["bos_persistence"]
    min_pen = cfg["bos_min_penetration_pct"]

    if len(df) < lookback * 2 + persistence + 3:
        return None

    df_for_swing = df.iloc[:-persistence]
    swing_type = "high" if direction == "BUY" else "low"
    swing = _find_latest_swing(df_for_swing, swing_type, lookback)

    if swing is None:
        return None

    for offset in range(-persistence, 0):
        candle = df.iloc[offset]
        close = float(candle["close"])
        open_ = float(candle["open"])
        high = float(candle["high"])
        low = float(candle["low"])

        broke = False
        pen = 0.0

        if direction == "BUY" and close > swing["price"] and swing["price"] != 0:
            pen = (close - swing["price"]) / swing["price"]
            broke = pen >= min_pen
        elif direction == "SELL" and close < swing["price"] and swing["price"] != 0:
            pen = (swing["price"] - close) / swing["price"]
            broke = pen >= min_pen

        if broke:
            body = abs(close - open_)
            range_ = high - low
            return {
                "type": "BOS",
                "direction": "BULLISH" if direction == "BUY" else "BEARISH",
                "timeframe": "M15",
                "ref_level": swing["price"],
                "penetration_pct": round(pen, 6),
                "displacement": (body / range_ > 0.6) if range_ > 0 else False,
                "volume_ratio": 0.0,  # sarà popolato dal chiamante
                "timestamp": str(candle.get("timestamp", "")),
            }

    return None


# ============================================================
# CHOCH Detection — struttura M15 reale
# ============================================================

def _detect_choch(df: pd.DataFrame, prev_structure: str,
                   current_structure: dict, cfg: dict) -> Optional[dict]:
    """
    CHOCH V2: Change of Character basato sulla struttura M15 REALE.

    Il CHOCH scatta quando:
    - La struttura M15 precedente (dallo scan precedente) era definita
    - L'ultima candela chiude oltre il livello che definiva quella struttura
    - La struttura e' effettivamente CAMBIATA (non era gia' nella nuova direzione)

    Nota: prev_structure viene dallo stato persistente (DB), non da H4.
    """
    if prev_structure == "NEUTRAL":
        return None

    if len(df) < cfg["choch_lookback"] * 2 + 3:
        return None

    # Ricalcola la struttura sulle candele PRECEDENTI all'ultima
    pivots_before = _find_pivots(df.iloc[:-1], cfg["choch_lookback"])
    struct_before = _classify_structure(pivots_before, cfg["choch_min_pivots"])

    # La struttura prima dell'ultima candela deve essere quella attesa
    if struct_before["classification"] != prev_structure:
        return None

    last = df.iloc[-1]
    close = float(last["close"])
    open_ = float(last["open"])
    high = float(last["high"])
    low = float(last["low"])

    event = None

    # Bullish CHOCH: struttura era BEARISH, close > ultimo LH
    if prev_structure == "BEARISH" and struct_before["last_lh"] is not None:
        ref = struct_before["last_lh"]
        if close > ref and ref != 0:
            pen = (close - ref) / ref
            body = abs(close - open_)
            range_ = high - low
            event = {
                "type": "CHOCH",
                "direction": "BULLISH",
                "timeframe": "M15",
                "ref_level": ref,
                "penetration_pct": round(pen, 6),
                "displacement": (body / range_ > 0.6) if range_ > 0 else False,
                "volume_ratio": 0.0,
                "timestamp": str(last.get("timestamp", "")),
                "prev_structure": prev_structure,
            }

    # Bearish CHOCH: struttura era BULLISH, close < ultimo HL
    elif prev_structure == "BULLISH" and struct_before["last_hl"] is not None:
        ref = struct_before["last_hl"]
        if close < ref and ref != 0:
            pen = (ref - close) / ref
            body = abs(close - open_)
            range_ = high - low
            event = {
                "type": "CHOCH",
                "direction": "BEARISH",
                "timeframe": "M15",
                "ref_level": ref,
                "penetration_pct": round(pen, 6),
                "displacement": (body / range_ > 0.6) if range_ > 0 else False,
                "volume_ratio": 0.0,
                "timestamp": str(last.get("timestamp", "")),
                "prev_structure": prev_structure,
            }

    return event


# ============================================================
# Pullback Invalidation
# ============================================================

def _check_pullback_status(df_h4: pd.DataFrame, structure_h4: dict) -> dict:
    """Verifica se i pullback sono ancora validi per entrambe le direzioni."""
    result = {
        "buy_valid": True,
        "sell_valid": True,
        "buy_ref_level": None,
        "sell_ref_level": None,
    }

    if len(df_h4) < 2:
        return result

    price = float(df_h4.iloc[-1]["close"])

    # BUY: valido se il prezzo non ha chiuso sotto l'ultimo HL
    hl = structure_h4.get("last_hl")
    if hl is not None:
        result["buy_ref_level"] = hl
        if price < hl:
            result["buy_valid"] = False

    # SELL: valido se il prezzo non ha chiuso sopra l'ultimo LH
    lh = structure_h4.get("last_lh")
    if lh is not None:
        result["sell_ref_level"] = lh
        if price > lh:
            result["sell_valid"] = False

    return result


# ============================================================
# Structure Confidence (0-100)
# ============================================================

def _compute_confidence(structure_h4: dict, structure_m15: dict,
                         pullback: dict, volume_ratio: float,
                         events: list) -> int:
    """
    Punteggio di confidenza sulla qualita' della struttura corrente.
    Calcolato SOLO dallo Structure Engine.

    Fattori:
        +25  H4 structure definita (BULLISH o BEARISH, non NEUTRAL)
        +25  M15 structure definita
        +15  H4 e M15 concordi nella stessa direzione
        +10  Pullback valido per almeno una direzione
        +10  Pivot count H4 >= 4 (struttura solida)
        +10  Pivot count M15 >= 4
        +5   Volume ratio > 1.0 (partecipazione)

    Penalita':
        -20  H4 e M15 in conflitto (uno BULLISH e l'altro BEARISH)
        -10  Pullback invalidato per entrambe le direzioni
    """
    score = 0

    h4_cls = structure_h4.get("classification", "NEUTRAL")
    m15_cls = structure_m15.get("classification", "NEUTRAL")

    # Struttura definita
    if h4_cls != "NEUTRAL":
        score += 25
    if m15_cls != "NEUTRAL":
        score += 25

    # Concordanza
    if h4_cls != "NEUTRAL" and m15_cls != "NEUTRAL":
        if h4_cls == m15_cls:
            score += 15
        else:
            score -= 20  # conflitto

    # Pullback
    if pullback.get("buy_valid", True) or pullback.get("sell_valid", True):
        score += 10
    if not pullback.get("buy_valid", True) and not pullback.get("sell_valid", True):
        score -= 10

    # Pivot count (solidita')
    if structure_h4.get("pivot_count", 0) >= 4:
        score += 10
    if structure_m15.get("pivot_count", 0) >= 4:
        score += 10

    # Volume
    if volume_ratio > 1.0:
        score += 5

    return max(0, min(100, score))


# ============================================================
# Volume Ratio
# ============================================================

def _compute_volume_ratio(df: pd.DataFrame, avg_period: int = 20) -> dict:
    """Volume dell'ultima candela / media delle ultime avg_period."""
    result = {
        "ratio": 1.0,
        "classification": "NORMAL",
        "current_volume": 0.0,
        "avg_volume": 0.0,
    }

    if "volume" not in df.columns or len(df) < avg_period + 1:
        return result

    current = float(df.iloc[-1]["volume"])
    avg = float(df.iloc[-(avg_period + 1):-1]["volume"].mean())

    result["current_volume"] = current
    result["avg_volume"] = avg

    if avg <= 0:
        return result

    ratio = current / avg
    result["ratio"] = round(ratio, 3)

    if ratio > 3.0:
        result["classification"] = "CLIMAX"
    elif ratio > 1.5:
        result["classification"] = "HIGH"
    elif ratio < 0.7:
        result["classification"] = "LOW"

    return result


# ============================================================
# Premium / Discount
# ============================================================

def _compute_premium_discount(price: float, high: float, low: float) -> dict:
    """Posizione del prezzo nel range."""
    result = {
        "zone": "EQUILIBRIUM",
        "position": 0.5,
        "range_high": high,
        "range_low": low,
    }

    range_size = high - low
    if range_size <= 0:
        return result

    pos = (price - low) / range_size
    pos = max(0.0, min(1.0, pos))
    result["position"] = round(pos, 4)

    if pos < 0.45:
        result["zone"] = "DISCOUNT"
    elif pos > 0.55:
        result["zone"] = "PREMIUM"

    return result


# ============================================================
# Event History Management
# ============================================================

def _update_event_history(history: list, new_events: list,
                           max_events: int) -> list:
    """Aggiunge i nuovi eventi alla history, mantiene solo gli ultimi N."""
    updated = history + new_events
    return updated[-max_events:]


# ============================================================
# Entry Point Principale
# ============================================================

def produce_structure_snapshot(
    asset: str,
    df_h4: pd.DataFrame,
    df_m15: pd.DataFrame,
    conn,
    atr_m15: float = 0.0,
    session_high: float = 0.0,
    session_low: float = 0.0,
    now: datetime = None,
    config: dict = None,
) -> dict:
    """
    Produce lo StructureSnapshot per un asset.

    1. Legge lo stato precedente dal DB
    2. Calcola la struttura corrente
    3. Rileva eventi (BOS, CHOCH, pullback invalidation)
    4. Calcola confidence, volume, premium/discount
    5. Aggiorna lo stato nel DB
    6. Salva lo snapshot nel DB
    7. Ritorna lo snapshot (dict immutabile)
    """
    if now is None:
        now = datetime.now(timezone.utc)
    if config is None:
        config = dict(DEFAULT_CONFIG)

    cfg = {**DEFAULT_CONFIG, **config}
    now_iso = now.isoformat()

    # ── 1. Leggi stato precedente ────────────────────────────
    prev_state = get_state(conn, asset)
    if prev_state is None:
        prev_state = {
            "asset": asset,
            "structure_m15": "NEUTRAL",
            "structure_m15_prev": "NEUTRAL",
            "structure_h4": "NEUTRAL",
            "scan_counter": 0,
            "last_bos_scan_idx": 0,
            "last_choch_scan_idx": 0,
            "event_history": [],
            "impulses": [],
            "impulse_count": 0,
            "trend_phase": "NEUTRAL",
            "current_trend": "NEUTRAL",
        }

    scan_idx = prev_state.get("scan_counter", 0) + 1

    # ── 2. Calcola struttura corrente ────────────────────────
    h4_pivots = _find_pivots(df_h4, cfg["h4_pivot_lookback"])
    m15_pivots = _find_pivots(df_m15, cfg["choch_lookback"])

    structure_h4 = _classify_structure(h4_pivots, cfg["choch_min_pivots"])
    structure_m15 = _classify_structure(m15_pivots, cfg["choch_min_pivots"])

    # ── 3. Rileva eventi ─────────────────────────────────────
    events = []

    # BOS: cerca nella direzione della struttura M15 corrente
    bos_event = None
    if structure_m15["classification"] in ("BULLISH", "BEARISH"):
        bos_dir = "BUY" if structure_m15["classification"] == "BULLISH" else "SELL"
        bos_event = _detect_bos(df_m15, structure_m15, bos_dir, cfg)
        if bos_event:
            events.append(bos_event)

    # CHOCH: confronta con la struttura M15 dello scan PRECEDENTE
    prev_m15_structure = prev_state.get("structure_m15", "NEUTRAL")
    choch_event = _detect_choch(df_m15, prev_m15_structure, structure_m15, cfg)
    if choch_event:
        events.append(choch_event)

    # Pullback Invalidation
    pullback = _check_pullback_status(df_h4, structure_h4)

    if not pullback["buy_valid"] and prev_state.get("structure_h4") == "BULLISH":
        events.append({
            "type": "PULLBACK_INVALIDATION",
            "direction": "BEARISH",
            "timeframe": "H4",
            "ref_level": pullback["buy_ref_level"],
            "penetration_pct": 0,
            "displacement": False,
            "volume_ratio": 0,
            "timestamp": now_iso,
        })

    if not pullback["sell_valid"] and prev_state.get("structure_h4") == "BEARISH":
        events.append({
            "type": "PULLBACK_INVALIDATION",
            "direction": "BULLISH",
            "timeframe": "H4",
            "ref_level": pullback["sell_ref_level"],
            "penetration_pct": 0,
            "displacement": False,
            "volume_ratio": 0,
            "timestamp": now_iso,
        })

    # Structure Change (transizione della classificazione M15)
    if structure_m15["classification"] != prev_m15_structure and prev_m15_structure != "NEUTRAL":
        events.append({
            "type": "STRUCTURE_CHANGE",
            "direction": structure_m15["classification"],
            "timeframe": "M15",
            "ref_level": None,
            "penetration_pct": 0,
            "displacement": False,
            "volume_ratio": 0,
            "timestamp": now_iso,
            "prev_structure": prev_m15_structure,
        })

    # ── 4. Arricchisci eventi con volume ─────────────────────
    vol = _compute_volume_ratio(df_m15, cfg["volume_avg_period"])
    for ev in events:
        ev["volume_ratio"] = vol["ratio"]

    # ── 5. Bars since BOS / CHOCH ────────────────────────────
    last_bos_idx = prev_state.get("last_bos_scan_idx", 0)
    last_choch_idx = prev_state.get("last_choch_scan_idx", 0)

    if bos_event:
        last_bos_idx = scan_idx
    if choch_event:
        last_choch_idx = scan_idx

    bars_since_bos = (scan_idx - last_bos_idx) if last_bos_idx > 0 else None
    bars_since_choch = (scan_idx - last_choch_idx) if last_choch_idx > 0 else None

    # ── 6. Premium / Discount ────────────────────────────────
    current_price = float(df_m15.iloc[-1]["close"]) if len(df_m15) > 0 else 0
    pd_zone = _compute_premium_discount(current_price, session_high, session_low)

    # ── 7. Event History ─────────────────────────────────────
    event_history = _update_event_history(
        prev_state.get("event_history", []),
        events,
        cfg["event_history_max"],
    )

    # ── 8. Confidence ────────────────────────────────────────
    confidence = _compute_confidence(
        structure_h4, structure_m15, pullback, vol["ratio"], events
    )

    # ── 9. Trend Health (placeholder Sprint 2) ───────────────
    trend_health = {
        "current_trend": prev_state.get("current_trend", "NEUTRAL"),
        "trend_start_timestamp": prev_state.get("trend_start_timestamp"),
        "impulse_count": prev_state.get("impulse_count", 0),
        "impulses": prev_state.get("impulses", []),
        "phase": prev_state.get("trend_phase", "NEUTRAL"),
        "avg_impulse_amplitude": 0.0,
        "last_impulse_amplitude": 0.0,
    }

    # ── 10. Displacement (placeholder Sprint 3) ──────────────
    displacement = {
        "confirmed": False,
        "direction": None,
        "magnitude_atr": 0.0,
        "candle_count": 0,
        "timestamp": None,
    }

    # ── Costruisci lo snapshot ───────────────────────────────
    snapshot = {
        "asset": asset,
        "timestamp": now_iso,
        "scan_id": f"{asset}_{scan_idx}",
        "snapshot_version": SNAPSHOT_VERSION,

        "config": cfg,

        "structure_h4": structure_h4,
        "structure_m15": structure_m15,

        "events": events,
        "event_history": event_history,

        "trend_health": trend_health,
        "displacement": displacement,

        "pullback_status": pullback,

        "structure_confidence": confidence,

        "volume_ratio_m15": vol["ratio"],
        "volume_classification": vol["classification"],

        "premium_discount": pd_zone,

        "bars_since_bos": bars_since_bos,
        "bars_since_choch": bars_since_choch,

        "current_price": current_price,
    }

    # ── Aggiorna stato persistente ───────────────────────────
    new_state = {
        "asset": asset,
        "updated_at": now_iso,
        "structure_h4": structure_h4["classification"],
        "structure_m15": structure_m15["classification"],
        "structure_m15_prev": prev_m15_structure,
        "h4_last_hh": structure_h4.get("last_hh"),
        "h4_last_hl": structure_h4.get("last_hl"),
        "h4_last_lh": structure_h4.get("last_lh"),
        "h4_last_ll": structure_h4.get("last_ll"),
        "m15_last_hh": structure_m15.get("last_hh"),
        "m15_last_hl": structure_m15.get("last_hl"),
        "m15_last_lh": structure_m15.get("last_lh"),
        "m15_last_ll": structure_m15.get("last_ll"),
        "current_trend": trend_health["current_trend"],
        "trend_start_timestamp": trend_health["trend_start_timestamp"],
        "impulse_count": trend_health["impulse_count"],
        "impulses": trend_health["impulses"],
        "trend_phase": trend_health["phase"],
        "last_displacement_ts": displacement.get("timestamp"),
        "last_displacement_dir": displacement.get("direction"),
        "last_displacement_atr": displacement.get("magnitude_atr", 0),
        "event_history": event_history,
        "last_bos_timestamp": now_iso if bos_event else prev_state.get("last_bos_timestamp"),
        "last_choch_timestamp": now_iso if choch_event else prev_state.get("last_choch_timestamp"),
        "last_bos_scan_idx": last_bos_idx,
        "last_choch_scan_idx": last_choch_idx,
        "scan_counter": scan_idx,
    }

    upsert_state(conn, new_state)

    # ── Salva snapshot ───────────────────────────────────────
    try:
        insert_snapshot(conn, snapshot)
    except Exception as e:
        logger.warning("Structure Engine: errore salvataggio snapshot: %s", e)

    # ── Log ──────────────────────────────────────────────────
    event_summary = ", ".join(
        f"{e['type']}({e['direction']})" for e in events
    ) if events else "none"

    logger.info(
        "Structure [%s]: H4=%s M15=%s(prev=%s) confidence=%d "
        "events=[%s] bars_bos=%s bars_choch=%s vol=%.2f(%s) pd=%s(%.2f)",
        asset,
        structure_h4["classification"],
        structure_m15["classification"],
        prev_m15_structure,
        confidence,
        event_summary,
        bars_since_bos, bars_since_choch,
        vol["ratio"], vol["classification"],
        pd_zone["zone"], pd_zone["position"],
    )

    return snapshot
