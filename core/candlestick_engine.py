"""
core/candlestick_engine.py
Candlestick Confirmation Engine — Sprint 9

Sprint 16: Fix detection — cerca pattern SEMPRE, poi valuta
se il pattern è in una zona della Reaction Map.
Prima: cercava pattern SOLO in zone ad alta confluenza, ma il
campo distance_from_price_pct non esisteva nelle zone → sempre False.

Ora produce:
    has_confirmation: True se un pattern è stato rilevato (qualsiasi)
    in_reaction_zone: True se il pattern è in una zona RM vicina
    confirmation_quality: HIGH (in zona) / LOW (fuori zona)

Pattern supportati (dal doc 008):
    - Hammer / Inverted Hammer
    - Engulfing (Bullish / Bearish)
    - Doji
    - Pin Bar
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from datetime import datetime, timezone

import pandas as pd

logger = logging.getLogger("candlestick_engine")

SNAPSHOT_VERSION = "2.0.0"

DEFAULT_CONFIG = {
    "zone_proximity_pct": 0.01,       # zona RM entro 1% del prezzo = "vicina"
    "min_confluence_for_high": 25,    # score >= 25 per confirmation_quality HIGH
    "doji_body_max_pct": 0.10,
    "pin_bar_wick_min_pct": 0.60,
    "engulfing_min_body_pct": 0.50,
}

CS_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS candlestick_snapshots (
    snapshot_id         TEXT PRIMARY KEY,
    asset               TEXT NOT NULL,
    timestamp_snapshot  DATETIME NOT NULL,
    has_confirmation    BOOLEAN DEFAULT 0,
    pattern_name        TEXT,
    pattern_direction   TEXT,
    zone_score          REAL DEFAULT 0,
    snapshot_json       TEXT
);
CREATE INDEX IF NOT EXISTS idx_cs_asset_ts
    ON candlestick_snapshots(asset, timestamp_snapshot);
"""


def init_candlestick_schema(conn: sqlite3.Connection):
    conn.executescript(CS_SCHEMA_SQL)
    conn.commit()


# ============================================================
# Pattern Detection
# ============================================================

def _detect_hammer(candle: dict) -> dict | None:
    o, c, h, l = candle["open"], candle["close"], candle["high"], candle["low"]
    range_ = h - l
    if range_ <= 0:
        return None

    body = abs(c - o)
    body_pct = body / range_
    lower_wick = min(o, c) - l
    upper_wick = h - max(o, c)

    if lower_wick / range_ > 0.60 and body_pct < 0.30 and upper_wick / range_ < 0.15:
        return {
            "pattern": "HAMMER",
            "direction": "BULLISH",
            "body_pct": round(body_pct, 3),
            "lower_wick_pct": round(lower_wick / range_, 3),
            "upper_wick_pct": round(upper_wick / range_, 3),
        }

    if upper_wick / range_ > 0.60 and body_pct < 0.30 and lower_wick / range_ < 0.15:
        return {
            "pattern": "INVERTED_HAMMER",
            "direction": "BEARISH",
            "body_pct": round(body_pct, 3),
            "lower_wick_pct": round(lower_wick / range_, 3),
            "upper_wick_pct": round(upper_wick / range_, 3),
        }

    return None


def _detect_engulfing(prev: dict, curr: dict, cfg: dict) -> dict | None:
    p_o, p_c = prev["open"], prev["close"]
    c_o, c_c = curr["open"], curr["close"]
    c_h, c_l = curr["high"], curr["low"]
    c_range = c_h - c_l

    if c_range <= 0:
        return None

    c_body = abs(c_c - c_o)
    c_body_pct = c_body / c_range
    min_body = cfg.get("engulfing_min_body_pct", 0.50)

    if p_c < p_o and c_c > c_o and c_body_pct >= min_body:
        if c_o <= p_c and c_c >= p_o:
            return {
                "pattern": "BULLISH_ENGULFING",
                "direction": "BULLISH",
                "body_pct": round(c_body_pct, 3),
                "lower_wick_pct": round((min(c_o, c_c) - c_l) / c_range, 3),
                "upper_wick_pct": round((c_h - max(c_o, c_c)) / c_range, 3),
            }

    if p_c > p_o and c_c < c_o and c_body_pct >= min_body:
        if c_o >= p_c and c_c <= p_o:
            return {
                "pattern": "BEARISH_ENGULFING",
                "direction": "BEARISH",
                "body_pct": round(c_body_pct, 3),
                "lower_wick_pct": round((min(c_o, c_c) - c_l) / c_range, 3),
                "upper_wick_pct": round((c_h - max(c_o, c_c)) / c_range, 3),
            }

    return None


def _detect_doji(candle: dict, cfg: dict) -> dict | None:
    o, c, h, l = candle["open"], candle["close"], candle["high"], candle["low"]
    range_ = h - l
    if range_ <= 0:
        return None

    body_pct = abs(c - o) / range_
    max_body = cfg.get("doji_body_max_pct", 0.10)

    if body_pct <= max_body:
        return {
            "pattern": "DOJI",
            "direction": "NEUTRAL",
            "body_pct": round(body_pct, 3),
            "lower_wick_pct": round((min(o, c) - l) / range_, 3),
            "upper_wick_pct": round((h - max(o, c)) / range_, 3),
        }

    return None


def _detect_pin_bar(candle: dict, cfg: dict) -> dict | None:
    o, c, h, l = candle["open"], candle["close"], candle["high"], candle["low"]
    range_ = h - l
    if range_ <= 0:
        return None

    lower_wick = min(o, c) - l
    upper_wick = h - max(o, c)
    min_wick = cfg.get("pin_bar_wick_min_pct", 0.60)

    if lower_wick / range_ >= min_wick:
        return {
            "pattern": "PIN_BAR",
            "direction": "BULLISH",
            "body_pct": round(abs(c - o) / range_, 3),
            "lower_wick_pct": round(lower_wick / range_, 3),
            "upper_wick_pct": round(upper_wick / range_, 3),
        }

    if upper_wick / range_ >= min_wick:
        return {
            "pattern": "PIN_BAR",
            "direction": "BEARISH",
            "body_pct": round(abs(c - o) / range_, 3),
            "lower_wick_pct": round(lower_wick / range_, 3),
            "upper_wick_pct": round(upper_wick / range_, 3),
        }

    return None


# ============================================================
# Zone proximity check
# ============================================================

def _find_nearest_zone(current_price: float, zones: list,
                        proximity_pct: float, min_confluence: float) -> dict | None:
    """
    Cerca la zona RM più vicina al prezzo corrente.
    Calcola la distanza direttamente da zone_high/zone_low
    invece di affidarsi a un campo pre-calcolato.
    """
    best = None
    best_dist = float('inf')

    for z in zones:
        zh = z.get("zone_high", z.get("high", 0))
        zl = z.get("zone_low", z.get("low", 0))
        score = z.get("confluence_score", 0)

        if zh <= 0 or zl <= 0 or current_price <= 0:
            continue

        # Distanza: 0 se dentro la zona, altrimenti distanza dal bordo più vicino
        if zl <= current_price <= zh:
            dist = 0
        else:
            dist = min(abs(current_price - zh), abs(current_price - zl)) / current_price

        if dist <= proximity_pct and score >= min_confluence:
            if dist < best_dist:
                best_dist = dist
                best = {
                    "zone_high": zh,
                    "zone_low": zl,
                    "confluence_score": score,
                    "distance_pct": round(dist, 6),
                }

    return best


# ============================================================
# Entry Point
# ============================================================

def produce_candlestick_snapshot(
    asset: str,
    df_m15: pd.DataFrame,
    reaction_map_snapshot: dict,
    conn: sqlite3.Connection,
    now: datetime = None,
    config: dict = None,
) -> dict:
    if now is None:
        now = datetime.now(timezone.utc)
    if config is None:
        config = {}

    cfg = {**DEFAULT_CONFIG, **config}
    now_iso = now.isoformat()

    patterns = []
    current_price = float(df_m15.iloc[-1]["close"]) if len(df_m15) > 0 else 0

    # ── Step 1: Cerca pattern SEMPRE (indipendente dalle zone) ─
    if len(df_m15) >= 3:
        last = df_m15.iloc[-1]
        prev = df_m15.iloc[-2]

        candle = {
            "open": float(last["open"]),
            "close": float(last["close"]),
            "high": float(last["high"]),
            "low": float(last["low"]),
        }
        prev_candle = {
            "open": float(prev["open"]),
            "close": float(prev["close"]),
            "high": float(prev["high"]),
            "low": float(prev["low"]),
        }

        for detector in [
            lambda: _detect_hammer(candle),
            lambda: _detect_engulfing(prev_candle, candle, cfg),
            lambda: _detect_doji(candle, cfg),
            lambda: _detect_pin_bar(candle, cfg),
        ]:
            result = detector()
            if result:
                patterns.append(result)

    # ── Step 2: Valuta se il pattern è in una zona RM ────────
    zones = reaction_map_snapshot.get("zones", []) if reaction_map_snapshot else []
    proximity_pct = cfg.get("zone_proximity_pct", 0.01)
    min_confluence = cfg.get("min_confluence_for_high", 25)

    nearest_zone = _find_nearest_zone(current_price, zones, proximity_pct, min_confluence)

    for p in patterns:
        if nearest_zone:
            p["in_reaction_zone"] = True
            p["zone_confluence_score"] = nearest_zone["confluence_score"]
            p["zone_distance_pct"] = nearest_zone["distance_pct"]
        else:
            p["in_reaction_zone"] = False
            p["zone_confluence_score"] = 0
            p["zone_distance_pct"] = None

    # ── Step 3: Risultato ────────────────────────────────────
    has_confirmation = len(patterns) > 0

    # Priorità: pattern in zona > pattern fuori zona
    # Tra pattern nella stessa categoria: prendi quello con zone_confluence più alto
    if patterns:
        in_zone = [p for p in patterns if p.get("in_reaction_zone")]
        strongest = max(in_zone, key=lambda p: p["zone_confluence_score"]) if in_zone else patterns[0]
    else:
        strongest = None

    confirmation_quality = "NONE"
    if strongest:
        if strongest.get("in_reaction_zone"):
            confirmation_quality = "HIGH"
        else:
            confirmation_quality = "LOW"

    snapshot = {
        "asset": asset,
        "timestamp": now_iso,
        "snapshot_version": SNAPSHOT_VERSION,
        "patterns_detected": patterns,
        "has_confirmation": has_confirmation,
        "confirmation_quality": confirmation_quality,
        "strongest_pattern": strongest.get("pattern") if strongest else None,
        "strongest_direction": strongest.get("direction") if strongest else None,
        "in_reaction_zone": strongest.get("in_reaction_zone", False) if strongest else False,
        "zone_confluence_score": strongest.get("zone_confluence_score", 0) if strongest else 0,
        "total_patterns": len(patterns),
    }

    # ── Salva ────────────────────────────────────────────────
    if conn:
        try:
            conn.execute("""
                INSERT INTO candlestick_snapshots (
                    snapshot_id, asset, timestamp_snapshot,
                    has_confirmation, pattern_name, pattern_direction,
                    zone_score, snapshot_json
                ) VALUES (?,?,?,?,?,?,?,?)
            """, (
                str(uuid.uuid4()), asset, now_iso,
                has_confirmation,
                strongest["pattern"] if strongest else None,
                strongest["direction"] if strongest else None,
                strongest["zone_confluence_score"] if strongest else 0,
                json.dumps(snapshot, default=str),
            ))
            conn.commit()
        except Exception as e:
            logger.warning("Candlestick [%s]: errore salvataggio: %s", asset, e)

    if has_confirmation:
        logger.info(
            "Candlestick [%s]: %s %s (%s) zone=%s score=%.0f",
            asset,
            strongest["pattern"],
            strongest["direction"],
            confirmation_quality,
            "YES" if strongest.get("in_reaction_zone") else "NO",
            strongest.get("zone_confluence_score", 0),
        )
    else:
        logger.info("Candlestick [%s]: no pattern detected", asset)

    return snapshot
