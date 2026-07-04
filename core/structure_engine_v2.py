"""
core/structure_engine_v2.py
Structure Engine V2.0 — Sprint 3

Sprint 1: Swing, Struttura, BOS, CHOCH, Pullback, Confidence, Event History
Sprint 2: Trend Health (impulse counting, phase detection)
Sprint 3: Displacement detection integrato ← QUESTO

Fonte unica di verita' per la struttura di mercato dell'intero MIE.
Dipendenze: solo pandas, numpy, sqlite3, logging + core.structure_db.
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

SNAPSHOT_VERSION = "2.0.0-sprint3"

DEFAULT_CONFIG = {
    # BOS
    "bos_lookback": 3,
    "bos_persistence": 3,
    "bos_min_penetration_pct": 0.0003,
    # CHOCH
    "choch_lookback": 3,
    "choch_min_pivots": 2,
    # H4
    "h4_pivot_lookback": 3,
    # Volume
    "volume_avg_period": 20,
    # Event History
    "event_history_max": 20,
    # Trend Health (Sprint 2 — recalibrato Sprint 15)
    "max_impulses_tracked": 10,
    "amplitude_similar_pct": 15,      # era 20 → più sensibile a ACCELERATING/EXHAUSTING
    "neutral_reset_scans": 25,        # era 10 → più tollerante, non resetta il trend subito
    "min_impulse_atr": 0.3,           # era 0.5 → rileva impulsi più piccoli su M15
    # Displacement (Sprint 3 — recalibrato Sprint 15)
    "disp_body_pct": 0.35,
    "disp_min_candles": 1,            # era 2 → singola candela impulsiva conta come displacement
    "disp_atr_mult": 0.6,            # confermato (era già 1.0 nel DEFAULT, 1.5 nel fallback)
    "disp_lookback": 5,
}


# ============================================================
# Swing Points
# ============================================================

def _find_pivots(df: pd.DataFrame, lookback: int, max_pivots: int = 10) -> dict:
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
    pivots = _find_pivots(df, lookback)
    items = pivots["highs"] if swing_type == "high" else pivots["lows"]
    return items[-1] if items else None


# ============================================================
# Classificazione struttura
# ============================================================

def _classify_structure(pivots: dict, min_pivots: int = 2) -> dict:
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
                "volume_ratio": 0.0,
                "timestamp": str(candle.get("timestamp", "")),
            }

    return None


# ============================================================
# CHOCH Detection
# ============================================================

def _detect_choch(df: pd.DataFrame, prev_structure: str,
                   current_structure: dict, cfg: dict) -> Optional[dict]:
    if prev_structure == "NEUTRAL":
        return None

    if len(df) < cfg["choch_lookback"] * 2 + 3:
        return None

    pivots_before = _find_pivots(df.iloc[:-1], cfg["choch_lookback"])
    struct_before = _classify_structure(pivots_before, cfg["choch_min_pivots"])

    if struct_before["classification"] != prev_structure:
        return None

    last = df.iloc[-1]
    close = float(last["close"])
    open_ = float(last["open"])
    high = float(last["high"])
    low = float(last["low"])

    event = None

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
    """
    Sprint 15: usa i pivot H4 direttamente come riferimento quando
    la struttura è NEUTRAL e last_hl/last_lh sono None.
    Prima: buy_valid=True al 100% perché last_hl era sempre None
    quando H4 era NEUTRAL. Ora usa il pivot low/high più recente.
    """
    result = {
        "buy_valid": True,
        "sell_valid": True,
        "buy_ref_level": None,
        "sell_ref_level": None,
    }

    if len(df_h4) < 4:
        return result

    price = float(df_h4.iloc[-1]["close"])

    # Priorità 1: usa last_hl / last_lh dalla struttura classificata
    hl = structure_h4.get("last_hl")
    lh = structure_h4.get("last_lh")

    # Priorità 2: se la struttura è NEUTRAL e non ha hl/lh,
    # usa i pivot H4 più recenti come riferimento
    if hl is None or lh is None:
        pivots = _find_pivots(df_h4, 3)
        if hl is None and len(pivots["lows"]) >= 1:
            hl = pivots["lows"][-1]["price"]
        if lh is None and len(pivots["highs"]) >= 1:
            lh = pivots["highs"][-1]["price"]

    if hl is not None:
        result["buy_ref_level"] = hl
        if price < hl:
            result["buy_valid"] = False

    if lh is not None:
        result["sell_ref_level"] = lh
        if price > lh:
            result["sell_valid"] = False

    return result


# ============================================================
# Structure Confidence
# ============================================================

def _compute_confidence(structure_h4: dict, structure_m15: dict,
                         pullback: dict, volume_ratio: float,
                         displacement_confirmed: bool) -> int:
    score = 0

    h4_cls = structure_h4.get("classification", "NEUTRAL")
    m15_cls = structure_m15.get("classification", "NEUTRAL")

    if h4_cls != "NEUTRAL":
        score += 25
    if m15_cls != "NEUTRAL":
        score += 25

    if h4_cls != "NEUTRAL" and m15_cls != "NEUTRAL":
        if h4_cls == m15_cls:
            score += 15
        else:
            score -= 20

    if pullback.get("buy_valid", True) or pullback.get("sell_valid", True):
        score += 10
    if not pullback.get("buy_valid", True) and not pullback.get("sell_valid", True):
        score -= 10

    if structure_h4.get("pivot_count", 0) >= 4:
        score += 10
    if structure_m15.get("pivot_count", 0) >= 4:
        score += 10

    if volume_ratio > 1.0:
        score += 5

    # Sprint 3: displacement bonus
    if displacement_confirmed:
        score += 5

    return max(0, min(100, score))


# ============================================================
# Volume Ratio
# ============================================================

def _compute_volume_ratio(df: pd.DataFrame, avg_period: int = 20) -> dict:
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
# Event History
# ============================================================

def _update_event_history(history: list, new_events: list,
                           max_events: int) -> list:
    updated = history + new_events
    return updated[-max_events:]


# ============================================================
# SPRINT 2 — Trend Health
# ============================================================

def _update_trend_health(prev_state: dict, structure_m15: dict,
                          atr_m15: float, scan_idx: int,
                          now_iso: str, cfg: dict) -> dict:
    current_cls = structure_m15.get("classification", "NEUTRAL")
    prev_trend = prev_state.get("current_trend", "NEUTRAL")
    prev_impulses = prev_state.get("impulses", [])
    prev_impulse_count = prev_state.get("impulse_count", 0)
    prev_trend_start = prev_state.get("trend_start_timestamp")
    prev_neutral_count = prev_state.get("neutral_consecutive_scans", 0)
    max_impulses = cfg.get("max_impulses_tracked", 10)
    similar_pct = cfg.get("amplitude_similar_pct", 20)
    neutral_reset = cfg.get("neutral_reset_scans", 10)
    min_impulse_atr = cfg.get("min_impulse_atr", 0.5)

    result = {
        "current_trend": prev_trend,
        "trend_start_timestamp": prev_trend_start,
        "impulse_count": prev_impulse_count,
        "impulses": list(prev_impulses),
        "phase": "NEUTRAL",
        "avg_impulse_amplitude": 0.0,
        "last_impulse_amplitude": 0.0,
        "last_impulse_duration": 0,
        "trend_duration_bars": 0,
        "neutral_consecutive_scans": 0,
    }

    if current_cls in ("BULLISH", "BEARISH") and current_cls != prev_trend:
        result["current_trend"] = current_cls
        result["trend_start_timestamp"] = now_iso
        result["impulse_count"] = 0
        result["impulses"] = []
        result["neutral_consecutive_scans"] = 0
        logger.info("Trend Health: RESET trend %s -> %s", prev_trend, current_cls)
        return result

    if current_cls == "NEUTRAL":
        neutral_count = prev_neutral_count + 1
        result["neutral_consecutive_scans"] = neutral_count
        if neutral_count >= neutral_reset and prev_trend != "NEUTRAL":
            result["current_trend"] = "NEUTRAL"
            result["trend_start_timestamp"] = None
            result["impulse_count"] = 0
            result["impulses"] = []
            logger.info("Trend Health: RESET dopo %d scan NEUTRAL", neutral_count)
        return result

    result["neutral_consecutive_scans"] = 0

    prev_hh = prev_state.get("m15_last_hh")
    prev_ll = prev_state.get("m15_last_ll")
    curr_hh = structure_m15.get("last_hh")
    curr_hl = structure_m15.get("last_hl")
    curr_lh = structure_m15.get("last_lh")
    curr_ll = structure_m15.get("last_ll")

    new_impulse = None

    if current_cls == "BULLISH" and curr_hh is not None and curr_hl is not None:
        if prev_hh is not None and curr_hh > prev_hh:
            amplitude = abs(curr_hh - curr_hl)
            amplitude_atr = amplitude / atr_m15 if atr_m15 > 0 else 0

            if amplitude_atr >= min_impulse_atr:
                new_impulse = {
                    "direction": "UP",
                    "start_price": curr_hl,
                    "end_price": curr_hh,
                    "amplitude_pct": round(amplitude / curr_hl * 100, 4) if curr_hl > 0 else 0,
                    "amplitude_atr": round(amplitude_atr, 3),
                    "duration_bars": 0,
                    "timestamp_start": "",
                    "timestamp_end": now_iso,
                }

    elif current_cls == "BEARISH" and curr_ll is not None and curr_lh is not None:
        if prev_ll is not None and curr_ll < prev_ll:
            amplitude = abs(curr_lh - curr_ll)
            amplitude_atr = amplitude / atr_m15 if atr_m15 > 0 else 0

            if amplitude_atr >= min_impulse_atr:
                new_impulse = {
                    "direction": "DOWN",
                    "start_price": curr_lh,
                    "end_price": curr_ll,
                    "amplitude_pct": round(amplitude / curr_lh * 100, 4) if curr_lh > 0 else 0,
                    "amplitude_atr": round(amplitude_atr, 3),
                    "duration_bars": 0,
                    "timestamp_start": "",
                    "timestamp_end": now_iso,
                }

    if new_impulse is not None:
        result["impulses"].append(new_impulse)
        result["impulses"] = result["impulses"][-max_impulses:]
        result["impulse_count"] = prev_impulse_count + 1
        logger.info(
            "Trend Health: nuovo impulso #%d %s amp=%.3f ATR",
            result["impulse_count"],
            new_impulse["direction"],
            new_impulse["amplitude_atr"],
        )

    impulses = result["impulses"]

    if len(impulses) >= 2:
        last_amp = impulses[-1]["amplitude_atr"]
        prev_amp = impulses[-2]["amplitude_atr"]
        result["last_impulse_amplitude"] = last_amp

        if prev_amp > 0:
            ratio = last_amp / prev_amp
            if ratio > 1.0 + similar_pct / 100:
                result["phase"] = "ACCELERATING"
            elif ratio < 1.0 - similar_pct / 100:
                result["phase"] = "EXHAUSTING"
            else:
                result["phase"] = "MATURE"

    if impulses:
        amps = [imp["amplitude_atr"] for imp in impulses]
        result["avg_impulse_amplitude"] = round(sum(amps) / len(amps), 3)
        result["last_impulse_amplitude"] = amps[-1]
        if impulses[-1].get("duration_bars"):
            result["last_impulse_duration"] = impulses[-1]["duration_bars"]

    if prev_trend_start:
        result["trend_duration_bars"] = scan_idx - prev_state.get("trend_start_scan_idx", scan_idx)

    return result


# ============================================================
# SPRINT 3 — Displacement Detection
# ============================================================

def _detect_displacement(df: pd.DataFrame, atr: float, cfg: dict) -> dict:
    """
    Rileva un displacement (movimento impulsivo) nelle ultime candele.

    Un displacement e' una sequenza di candele consecutive con:
        - corpo > body_pct del range (candela impulsiva)
        - stessa direzione (tutte bullish o tutte bearish)
        - ampiezza totale > atr_mult x ATR

    Cerca sia displacement bullish che bearish e ritorna il piu' forte.
    """
    result = {
        "confirmed": False,
        "direction": None,
        "magnitude": 0.0,
        "magnitude_atr": 0.0,
        "candle_count": 0,
        "timestamp": None,
    }

    lookback = cfg.get("disp_lookback", 5)
    body_pct = cfg.get("disp_body_pct", 0.60)
    min_candles = cfg.get("disp_min_candles", 2)
    atr_mult = cfg.get("disp_atr_mult", 1.0)

    if len(df) < lookback + 1 or atr <= 0:
        return result

    recent = df.iloc[-lookback:]

    # Cerca sequenze bullish e bearish separatamente
    best = {"count": 0, "move": 0.0, "dir": None, "ts": None}

    for target_dir in ("BULLISH", "BEARISH"):
        consecutive = 0
        total_move = 0.0
        start_ts = None

        for idx, (_, candle) in enumerate(recent.iterrows()):
            open_ = float(candle["open"])
            close = float(candle["close"])
            high = float(candle["high"])
            low = float(candle["low"])
            body = abs(close - open_)
            range_ = high - low

            if range_ <= 0:
                consecutive = 0
                total_move = 0.0
                start_ts = None
                continue

            is_impulsive = body / range_ > body_pct
            is_bullish = close > open_
            is_bearish = close < open_

            matches = (target_dir == "BULLISH" and is_bullish and is_impulsive) or \
                      (target_dir == "BEARISH" and is_bearish and is_impulsive)

            if matches:
                if consecutive == 0:
                    start_ts = str(candle.get("timestamp", ""))
                consecutive += 1
                total_move += body
            else:
                consecutive = 0
                total_move = 0.0
                start_ts = None

        if consecutive > best["count"] or (consecutive == best["count"] and total_move > best["move"]):
            best = {"count": consecutive, "move": total_move, "dir": target_dir, "ts": start_ts}

    if best["count"] >= min_candles and best["move"] >= atr_mult * atr:
        result["confirmed"] = True
        result["direction"] = best["dir"]
        result["magnitude"] = round(best["move"], 4)
        result["magnitude_atr"] = round(best["move"] / atr, 3) if atr > 0 else 0
        result["candle_count"] = best["count"]
        result["timestamp"] = best["ts"]

    return result


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
            "neutral_consecutive_scans": 0,
            "trend_start_scan_idx": 0,
        }

    scan_idx = prev_state.get("scan_counter", 0) + 1

    # ── 2. Calcola struttura corrente ────────────────────────
    h4_pivots = _find_pivots(df_h4, cfg["h4_pivot_lookback"])
    m15_pivots = _find_pivots(df_m15, cfg["choch_lookback"])

    structure_h4 = _classify_structure(h4_pivots, cfg["choch_min_pivots"])
    structure_m15 = _classify_structure(m15_pivots, cfg["choch_min_pivots"])

    # ── 3. Rileva eventi ─────────────────────────────────────
    events = []

    bos_event = None
    if structure_m15["classification"] in ("BULLISH", "BEARISH"):
        bos_dir = "BUY" if structure_m15["classification"] == "BULLISH" else "SELL"
        bos_event = _detect_bos(df_m15, structure_m15, bos_dir, cfg)
        if bos_event:
            events.append(bos_event)

    prev_m15_structure = prev_state.get("structure_m15", "NEUTRAL")
    choch_event = _detect_choch(df_m15, prev_m15_structure, structure_m15, cfg)
    if choch_event:
        events.append(choch_event)

    pullback = _check_pullback_status(df_h4, structure_h4)

    if not pullback["buy_valid"] and prev_state.get("structure_h4") == "BULLISH":
        events.append({
            "type": "PULLBACK_INVALIDATION",
            "direction": "BEARISH", "timeframe": "H4",
            "ref_level": pullback["buy_ref_level"],
            "penetration_pct": 0, "displacement": False,
            "volume_ratio": 0, "timestamp": now_iso,
        })

    if not pullback["sell_valid"] and prev_state.get("structure_h4") == "BEARISH":
        events.append({
            "type": "PULLBACK_INVALIDATION",
            "direction": "BULLISH", "timeframe": "H4",
            "ref_level": pullback["sell_ref_level"],
            "penetration_pct": 0, "displacement": False,
            "volume_ratio": 0, "timestamp": now_iso,
        })

    if structure_m15["classification"] != prev_m15_structure and prev_m15_structure != "NEUTRAL":
        events.append({
            "type": "STRUCTURE_CHANGE",
            "direction": structure_m15["classification"],
            "timeframe": "M15", "ref_level": None,
            "penetration_pct": 0, "displacement": False,
            "volume_ratio": 0, "timestamp": now_iso,
            "prev_structure": prev_m15_structure,
        })

    # ── 4. Volume ────────────────────────────────────────────
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

    # ── 8. Trend Health (Sprint 2) ───────────────────────────
    trend_health = _update_trend_health(
        prev_state, structure_m15, atr_m15, scan_idx, now_iso, cfg
    )

    # ── 9. Displacement (Sprint 3) ───────────────────────────
    displacement = _detect_displacement(df_m15, atr_m15, cfg)

    # Aggiunge evento DISPLACEMENT se confermato
    if displacement["confirmed"]:
        events.append({
            "type": "DISPLACEMENT",
            "direction": displacement["direction"],
            "timeframe": "M15",
            "ref_level": None,
            "penetration_pct": 0,
            "displacement": True,
            "volume_ratio": vol["ratio"],
            "timestamp": displacement["timestamp"],
            "magnitude_atr": displacement["magnitude_atr"],
            "candle_count": displacement["candle_count"],
        })
        # Aggiorna event_history con il displacement
        event_history = _update_event_history(
            event_history,
            [events[-1]],
            cfg["event_history_max"],
        )

    # ── 10. Confidence (Sprint 3: include displacement) ──────
    confidence = _compute_confidence(
        structure_h4, structure_m15, pullback,
        vol["ratio"], displacement["confirmed"]
    )

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

        "trend_health": {
            "current_trend": trend_health["current_trend"],
            "trend_start_timestamp": trend_health["trend_start_timestamp"],
            "impulse_count": trend_health["impulse_count"],
            "impulses": trend_health["impulses"],
            "phase": trend_health["phase"],
            "avg_impulse_amplitude": trend_health["avg_impulse_amplitude"],
            "last_impulse_amplitude": trend_health["last_impulse_amplitude"],
            "last_impulse_duration": trend_health["last_impulse_duration"],
            "trend_duration_bars": trend_health["trend_duration_bars"],
        },

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
        "neutral_consecutive_scans": trend_health.get("neutral_consecutive_scans", 0),
        "trend_start_scan_idx": prev_state.get("trend_start_scan_idx", scan_idx) if trend_health["trend_start_timestamp"] == prev_state.get("trend_start_timestamp") else scan_idx,
    }

    upsert_state(conn, new_state)

    try:
        insert_snapshot(conn, snapshot)
    except Exception as e:
        logger.warning("Structure Engine: errore salvataggio snapshot: %s", e)

    # ── Log ──────────────────────────────────────────────────
    event_summary = ", ".join(
        f"{e['type']}({e['direction']})" for e in events
    ) if events else "none"

    disp_str = f"disp={displacement['direction']}({displacement['magnitude_atr']:.1f}ATR)" \
               if displacement["confirmed"] else "disp=none"

    logger.info(
        "Structure [%s]: H4=%s M15=%s(prev=%s) confidence=%d "
        "events=[%s] bars_bos=%s bars_choch=%s vol=%.2f(%s) pd=%s(%.2f) "
        "trend=%s phase=%s impulses=%d %s",
        asset,
        structure_h4["classification"],
        structure_m15["classification"],
        prev_m15_structure,
        confidence,
        event_summary,
        bars_since_bos, bars_since_choch,
        vol["ratio"], vol["classification"],
        pd_zone["zone"], pd_zone["position"],
        trend_health["current_trend"],
        trend_health["phase"],
        trend_health["impulse_count"],
        disp_str,
    )

    return snapshot
