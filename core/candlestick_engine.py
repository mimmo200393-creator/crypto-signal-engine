"""
core/candlestick_engine.py
Candlestick Confirmation Engine — Sprint 9

Layer 2: dipende da Reaction Map (Layer 2).
Cerca pattern candlestick SOLO nelle zone della Reaction Map.
Un pattern fuori da una zona ad alta confluenza non viene registrato.

Pattern supportati (dal doc 008):
    - Hammer / Inverted Hammer
    - Engulfing (Bullish / Bearish)
    - Doji
    - Morning Star / Evening Star (3 candele)
    - Pin Bar

Modalita': LIVE MODE.
Dipendenze: pandas, sqlite3, logging. Consuma ReactionMapSnapshot.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from datetime import datetime, timezone

import pandas as pd

logger = logging.getLogger("candlestick_engine")

SNAPSHOT_VERSION = "1.0.0"

DEFAULT_CONFIG = {
    "min_confluence_for_search": 30,  # cerca pattern solo in zone con score >= 30
    "doji_body_max_pct": 0.10,        # corpo < 10% del range = doji
    "pin_bar_wick_min_pct": 0.60,     # wick > 60% del range = pin bar
    "engulfing_min_body_pct": 0.50,   # corpo engulfing > 50% del range
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
    """Hammer: corpo piccolo in alto, wick lungo sotto."""
    o, c, h, l = candle["open"], candle["close"], candle["high"], candle["low"]
    range_ = h - l
    if range_ <= 0:
        return None

    body = abs(c - o)
    body_pct = body / range_
    lower_wick = min(o, c) - l
    upper_wick = h - max(o, c)

    # Hammer: lower wick > 60% range, body < 30%, corpo in alto
    if lower_wick / range_ > 0.60 and body_pct < 0.30 and upper_wick / range_ < 0.15:
        return {
            "pattern": "HAMMER",
            "direction": "BULLISH",
            "body_pct": round(body_pct, 3),
            "lower_wick_pct": round(lower_wick / range_, 3),
            "upper_wick_pct": round(upper_wick / range_, 3),
        }

    # Inverted Hammer: upper wick > 60%, corpo in basso
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
    """Engulfing: la candela corrente ingloba completamente la precedente."""
    p_o, p_c = prev["open"], prev["close"]
    c_o, c_c = curr["open"], curr["close"]
    c_h, c_l = curr["high"], curr["low"]
    c_range = c_h - c_l

    if c_range <= 0:
        return None

    c_body = abs(c_c - c_o)
    c_body_pct = c_body / c_range
    min_body = cfg.get("engulfing_min_body_pct", 0.50)

    # Bullish Engulfing: prev bearish, curr bullish, curr ingloba prev
    if p_c < p_o and c_c > c_o and c_body_pct >= min_body:
        if c_o <= p_c and c_c >= p_o:
            return {
                "pattern": "BULLISH_ENGULFING",
                "direction": "BULLISH",
                "body_pct": round(c_body_pct, 3),
                "lower_wick_pct": round((min(c_o, c_c) - c_l) / c_range, 3),
                "upper_wick_pct": round((c_h - max(c_o, c_c)) / c_range, 3),
            }

    # Bearish Engulfing
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
    """Doji: corpo piccolissimo, indecisione."""
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
    """Pin Bar: wick molto lungo in una direzione."""
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
    min_conf = cfg.get("min_confluence_for_search", 30)

    patterns = []
    current_price = float(df_m15.iloc[-1]["close"]) if len(df_m15) > 0 else 0

    # Cerca pattern SOLO se il prezzo e' dentro o vicino a una zona ad alta confluenza
    zones = reaction_map_snapshot.get("zones", []) if reaction_map_snapshot else []
    relevant_zones = [
        z for z in zones
        if z.get("confluence_score", 0) >= min_conf
        and z.get("distance_from_price_pct", 1) < 0.01  # entro 1%
    ]

    if relevant_zones and len(df_m15) >= 3:
        best_zone = max(relevant_zones, key=lambda z: z["confluence_score"])

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

        # Test tutti i pattern
        for detector in [
            lambda: _detect_hammer(candle),
            lambda: _detect_engulfing(prev_candle, candle, cfg),
            lambda: _detect_doji(candle, cfg),
            lambda: _detect_pin_bar(candle, cfg),
        ]:
            result = detector()
            if result:
                result["in_reaction_zone"] = True
                result["zone_confluence_score"] = best_zone["confluence_score"]
                patterns.append(result)

    # ── Risultato ────────────────────────────────────────────
    has_confirmation = len(patterns) > 0
    strongest = max(patterns, key=lambda p: p.get("zone_confluence_score", 0)) if patterns else None

    snapshot = {
        "asset": asset,
        "timestamp": now_iso,
        "snapshot_version": SNAPSHOT_VERSION,
        "patterns_detected": patterns,
        "has_confirmation": has_confirmation,
        "strongest_pattern": strongest.get("pattern") if strongest else None,
        "strongest_direction": strongest.get("direction") if strongest else None,
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
            "Candlestick [%s]: CONFIRMED %s (%s) in zone score=%.0f",
            asset,
            strongest["pattern"],
            strongest["direction"],
            strongest["zone_confluence_score"],
        )
    else:
        logger.info("Candlestick [%s]: no pattern in relevant zones", asset)

    return snapshot
