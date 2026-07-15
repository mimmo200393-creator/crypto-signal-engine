"""
core/candlestick_engine.py
Candlestick Confirmation Engine — Sprint 9

Sprint 16: Fix detection — cerca pattern SEMPRE, poi valuta
se il pattern è in una zona della Reaction Map.
Prima: cercava pattern SOLO in zone ad alta confluenza, ma il
campo distance_from_price_pct non esisteva nelle zone → sempre False.

Sprint 17 (Precision Audit V1): il motore risponde solo alla domanda
"che pattern di candela è questo?". Nessuna decisione di validità o
contesto (trend/OB/liquidità) — quella spetta ad altri engine.
    - Hammer / Inverted Hammer: richiesto che il body sia vicino
      rispettivamente al high/low della candela (non solo wick lunga).
    - Pin Bar: richiesto body vicino al bordo E chiusura nella stessa
      area (non basta più la sola shadow lunga).
    - Engulfing: richiesto un rapporto minimo tra body corrente e
      body precedente, per scartare engulfing marginali.
    - Doji: richieste ombre simmetriche, non solo body piccolo.
    - Pattern multipli: una sola candela produce UN SOLO pattern
      principale, secondo priorità (vedi DEFAULT_CONFIG["pattern_priority"]).

Sprint 17 (Precision Audit V2 — rifiniture finali pre-certificazione):
    - Tutte le soglie di detection (incl. 0.60/0.30/0.15 di Hammer/
      Inverted Hammer) sono ora in DEFAULT_CONFIG, zero hardcoded.
    - Pin Bar: verificato che l'INTERO body (non solo un bordo) sia
      dentro la edge zone, per eliminare le pin bar "sporche".
    - Priorità pattern configurabile via cfg["pattern_priority"].
    - Pattern Quality (quality_score 0-100 + quality_label LOW/MEDIUM/
      HIGH) calcolata SOLO dalla geometria della candela (wick, body,
      posizione, simmetria) — nessun trend/ATR/volume/Order Block.

Ora produce:
    has_confirmation: True se un pattern è stato rilevato
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

SNAPSHOT_VERSION = "2.2.0"

DEFAULT_CONFIG = {
    "zone_proximity_pct": 0.01,       # zona RM entro 1% del prezzo = "vicina"
    "min_confluence_for_high": 25,    # score >= 25 per confirmation_quality HIGH
    "doji_body_max_pct": 0.10,
    "pin_bar_wick_min_pct": 0.60,
    "engulfing_min_body_pct": 0.50,
    # --- Precision Audit V1 ---
    "hammer_body_near_high_pct": 0.20,
    "inverted_hammer_body_near_low_pct": 0.20,
    "pin_body_near_edge_pct": 0.20,
    "engulfing_body_ratio": 0.70,
    "doji_max_wick_difference_pct": 0.20,
    # --- Precision Audit V2 (rifiniture finali) ---
    # Soglie hammer/inverted hammer, prima hardcoded (0.60/0.30/0.15).
    # Condivise tra Hammer e Inverted Hammer, che sono speculari.
    "hammer_wick_min_pct": 0.60,
    "hammer_body_max_pct": 0.30,
    "hammer_opposite_wick_max_pct": 0.15,
    # Priorità di rilevamento: la prima voce che matcha vince.
    # Modificabile senza toccare la logica del motore.
    "pattern_priority": ["ENGULFING", "HAMMER", "INVERTED_HAMMER", "PIN_BAR", "DOJI"],
    # Soglie per bucket di Pattern Quality (score 0-100, solo geometria)
    "pattern_quality_high_min": 75,
    "pattern_quality_medium_min": 45,
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
# Pattern Quality — puramente geometrica, nessun trend/ATR/volume
# ============================================================

def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def _quality_label(score: float, cfg: dict) -> str:
    high_min = cfg.get("pattern_quality_high_min", 75)
    medium_min = cfg.get("pattern_quality_medium_min", 45)
    if score >= high_min:
        return "HIGH"
    if score >= medium_min:
        return "MEDIUM"
    return "LOW"


def _score_from_components(components: list, cfg: dict) -> tuple:
    """
    Media di componenti 0-1 (ognuna già normalizzata), scalata a 0-100.
    Restituisce (score, label).
    """
    score = round(100 * (sum(components) / len(components))) if components else 0
    return score, _quality_label(score, cfg)


# ============================================================
# Pattern Detection
# ============================================================

def _detect_hammer(candle: dict, cfg: dict) -> dict | None:
    o, c, h, l = candle["open"], candle["close"], candle["high"], candle["low"]
    range_ = h - l
    if range_ <= 0:
        return None

    body = abs(c - o)
    body_pct = body / range_
    lower_wick = min(o, c) - l
    upper_wick = h - max(o, c)
    lower_wick_pct = lower_wick / range_
    upper_wick_pct = upper_wick / range_
    body_top = max(o, c)

    wick_min = cfg.get("hammer_wick_min_pct", 0.60)
    body_max = cfg.get("hammer_body_max_pct", 0.30)
    opposite_wick_max = cfg.get("hammer_opposite_wick_max_pct", 0.15)
    near_high_pct = cfg.get("hammer_body_near_high_pct", 0.20)

    if lower_wick_pct > wick_min and body_pct < body_max and upper_wick_pct < opposite_wick_max:
        # Fix Precision Audit V1: il body deve stare nella parte alta della candela
        gap_pct = (h - body_top) / range_
        if gap_pct <= near_high_pct:
            wick_component = _clamp01((lower_wick_pct - wick_min) / (1 - wick_min)) if wick_min < 1 else 0.0
            position_component = _clamp01((near_high_pct - gap_pct) / near_high_pct) if near_high_pct > 0 else 0.0
            score, label = _score_from_components([wick_component, position_component], cfg)
            return {
                "pattern": "HAMMER",
                "direction": "BULLISH",
                "body_pct": round(body_pct, 3),
                "lower_wick_pct": round(lower_wick_pct, 3),
                "upper_wick_pct": round(upper_wick_pct, 3),
                "quality_score": score,
                "quality_label": label,
            }

    return None


def _detect_inverted_hammer(candle: dict, cfg: dict) -> dict | None:
    o, c, h, l = candle["open"], candle["close"], candle["high"], candle["low"]
    range_ = h - l
    if range_ <= 0:
        return None

    body = abs(c - o)
    body_pct = body / range_
    lower_wick = min(o, c) - l
    upper_wick = h - max(o, c)
    lower_wick_pct = lower_wick / range_
    upper_wick_pct = upper_wick / range_
    body_bottom = min(o, c)

    wick_min = cfg.get("hammer_wick_min_pct", 0.60)
    body_max = cfg.get("hammer_body_max_pct", 0.30)
    opposite_wick_max = cfg.get("hammer_opposite_wick_max_pct", 0.15)
    near_low_pct = cfg.get("inverted_hammer_body_near_low_pct", 0.20)

    if upper_wick_pct > wick_min and body_pct < body_max and lower_wick_pct < opposite_wick_max:
        # Fix Precision Audit V1: il body deve stare nella parte bassa della candela
        gap_pct = (body_bottom - l) / range_
        if gap_pct <= near_low_pct:
            wick_component = _clamp01((upper_wick_pct - wick_min) / (1 - wick_min)) if wick_min < 1 else 0.0
            position_component = _clamp01((near_low_pct - gap_pct) / near_low_pct) if near_low_pct > 0 else 0.0
            score, label = _score_from_components([wick_component, position_component], cfg)
            return {
                "pattern": "INVERTED_HAMMER",
                "direction": "BEARISH",
                "body_pct": round(body_pct, 3),
                "lower_wick_pct": round(lower_wick_pct, 3),
                "upper_wick_pct": round(upper_wick_pct, 3),
                "quality_score": score,
                "quality_label": label,
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

    # Fix Precision Audit V1: il body corrente deve essere significativo
    # rispetto al body precedente, per scartare engulfing marginali
    p_body = abs(p_c - p_o)
    body_ratio = cfg.get("engulfing_body_ratio", 0.70)
    if p_body <= 0 or c_body < p_body * body_ratio:
        return None

    if p_c < p_o and c_c > c_o and c_body_pct >= min_body:
        if c_o <= p_c and c_c >= p_o:
            actual_ratio = c_body / p_body
            ratio_component = _clamp01((actual_ratio - body_ratio) / (2 - body_ratio)) if body_ratio < 2 else 0.0
            fill_component = _clamp01((c_body_pct - min_body) / (1 - min_body)) if min_body < 1 else 0.0
            score, label = _score_from_components([ratio_component, fill_component], cfg)
            return {
                "pattern": "BULLISH_ENGULFING",
                "direction": "BULLISH",
                "body_pct": round(c_body_pct, 3),
                "lower_wick_pct": round((min(c_o, c_c) - c_l) / c_range, 3),
                "upper_wick_pct": round((c_h - max(c_o, c_c)) / c_range, 3),
                "quality_score": score,
                "quality_label": label,
            }

    if p_c > p_o and c_c < c_o and c_body_pct >= min_body:
        if c_o >= p_c and c_c <= p_o:
            actual_ratio = c_body / p_body
            ratio_component = _clamp01((actual_ratio - body_ratio) / (2 - body_ratio)) if body_ratio < 2 else 0.0
            fill_component = _clamp01((c_body_pct - min_body) / (1 - min_body)) if min_body < 1 else 0.0
            score, label = _score_from_components([ratio_component, fill_component], cfg)
            return {
                "pattern": "BEARISH_ENGULFING",
                "direction": "BEARISH",
                "body_pct": round(c_body_pct, 3),
                "lower_wick_pct": round((min(c_o, c_c) - c_l) / c_range, 3),
                "upper_wick_pct": round((c_h - max(c_o, c_c)) / c_range, 3),
                "quality_score": score,
                "quality_label": label,
            }

    return None


def _detect_doji(candle: dict, cfg: dict) -> dict | None:
    o, c, h, l = candle["open"], candle["close"], candle["high"], candle["low"]
    range_ = h - l
    if range_ <= 0:
        return None

    body_pct = abs(c - o) / range_
    max_body = cfg.get("doji_body_max_pct", 0.10)
    lower_wick_pct = (min(o, c) - l) / range_
    upper_wick_pct = (h - max(o, c)) / range_

    # Fix Precision Audit V1: non basta un body piccolo, servono ombre
    # ragionevolmente simmetriche altrimenti non è un vero Doji
    max_wick_diff = cfg.get("doji_max_wick_difference_pct", 0.20)
    wick_diff = abs(upper_wick_pct - lower_wick_pct)

    if body_pct <= max_body and wick_diff <= max_wick_diff:
        body_component = _clamp01((max_body - body_pct) / max_body) if max_body > 0 else 0.0
        symmetry_component = _clamp01((max_wick_diff - wick_diff) / max_wick_diff) if max_wick_diff > 0 else 0.0
        score, label = _score_from_components([body_component, symmetry_component], cfg)
        return {
            "pattern": "DOJI",
            "direction": "NEUTRAL",
            "body_pct": round(body_pct, 3),
            "lower_wick_pct": round(lower_wick_pct, 3),
            "upper_wick_pct": round(upper_wick_pct, 3),
            "quality_score": score,
            "quality_label": label,
        }

    return None


def _detect_pin_bar(candle: dict, cfg: dict) -> dict | None:
    o, c, h, l = candle["open"], candle["close"], candle["high"], candle["low"]
    range_ = h - l
    if range_ <= 0:
        return None

    lower_wick = min(o, c) - l
    upper_wick = h - max(o, c)
    lower_wick_pct = lower_wick / range_
    upper_wick_pct = upper_wick / range_
    body_top = max(o, c)
    body_bottom = min(o, c)
    min_wick = cfg.get("pin_bar_wick_min_pct", 0.60)
    edge_pct = cfg.get("pin_body_near_edge_pct", 0.20)

    edge_zone_high = h - (range_ * edge_pct)
    edge_zone_low = l + (range_ * edge_pct)

    # Fix Precision Audit V2: non basta che il top/bottom del body tocchi
    # il bordo — l'INTERO body deve stare nella edge zone. Verificare il
    # bordo "peggiore" del body (quello più lontano dall'estremo) implica
    # automaticamente anche l'altro, quindi è il vincolo sufficiente.
    if lower_wick_pct >= min_wick:
        if body_bottom >= edge_zone_high and c >= edge_zone_high:
            wick_component = _clamp01((lower_wick_pct - min_wick) / (1 - min_wick)) if min_wick < 1 else 0.0
            body_gap_pct = (h - body_bottom) / range_
            position_component = _clamp01((edge_pct - body_gap_pct) / edge_pct) if edge_pct > 0 else 0.0
            score, label = _score_from_components([wick_component, position_component], cfg)
            return {
                "pattern": "PIN_BAR",
                "direction": "BULLISH",
                "body_pct": round(abs(c - o) / range_, 3),
                "lower_wick_pct": round(lower_wick_pct, 3),
                "upper_wick_pct": round(upper_wick_pct, 3),
                "quality_score": score,
                "quality_label": label,
            }

    if upper_wick_pct >= min_wick:
        if body_top <= edge_zone_low and c <= edge_zone_low:
            wick_component = _clamp01((upper_wick_pct - min_wick) / (1 - min_wick)) if min_wick < 1 else 0.0
            body_gap_pct = (body_top - l) / range_
            position_component = _clamp01((edge_pct - body_gap_pct) / edge_pct) if edge_pct > 0 else 0.0
            score, label = _score_from_components([wick_component, position_component], cfg)
            return {
                "pattern": "PIN_BAR",
                "direction": "BEARISH",
                "body_pct": round(abs(c - o) / range_, 3),
                "lower_wick_pct": round(lower_wick_pct, 3),
                "upper_wick_pct": round(upper_wick_pct, 3),
                "quality_score": score,
                "quality_label": label,
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
    # Fix Precision Audit V2: la priorità è configurabile via
    # cfg["pattern_priority"] (default in DEFAULT_CONFIG), non più
    # scritta nella logica. Una candela produce UN SOLO pattern.
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

        detector_map = {
            "ENGULFING": lambda: _detect_engulfing(prev_candle, candle, cfg),
            "HAMMER": lambda: _detect_hammer(candle, cfg),
            "INVERTED_HAMMER": lambda: _detect_inverted_hammer(candle, cfg),
            "PIN_BAR": lambda: _detect_pin_bar(candle, cfg),
            "DOJI": lambda: _detect_doji(candle, cfg),
        }
        priority = cfg.get("pattern_priority", DEFAULT_CONFIG["pattern_priority"])

        for pattern_name in priority:
            detector = detector_map.get(pattern_name)
            if detector is None:
                logger.warning("Candlestick [%s]: pattern '%s' in pattern_priority sconosciuto, ignorato", asset, pattern_name)
                continue
            result = detector()
            if result:
                patterns.append(result)
                break  # un solo pattern principale per candela

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
        # Precision Audit V2: Pattern Quality, puramente geometrica
        # (nessun trend/ATR/volume/OB) — quanto è "pulito" il pattern.
        "pattern_quality_score": strongest.get("quality_score", 0) if strongest else 0,
        "pattern_quality_label": strongest.get("quality_label", "NONE") if strongest else "NONE",
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
            "Candlestick [%s]: %s %s quality=%s(%d) zone=%s score=%.0f",
            asset,
            strongest["pattern"],
            strongest["direction"],
            strongest.get("quality_label", "NONE"),
            strongest.get("quality_score", 0),
            "YES" if strongest.get("in_reaction_zone") else "NO",
            strongest.get("zone_confluence_score", 0),
        )
    else:
        logger.info("Candlestick [%s]: no pattern detected", asset)

    return snapshot
