"""
core/market_state_model.py
Market State Model — Sprint 11

Layer 3: dipende da TUTTO.
Produce un unico MarketStateSnapshot che risponde alle 7 domande:

    1. DOVE?       → premium/discount + reaction map zone
    2. PERCHE'?    → displacement + volume
    3. CHI?        → volume ratio + session sweep
    4. DOVE VA?    → liquidity target + structural score
    5. QUANTO?     → trend health + impulse count
    6. QUANDO?     → sessione + macro context
    7. VALIDO?     → pullback invalidation + OB mitigation

Modalita': LIVE MODE.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from datetime import datetime, timezone

logger = logging.getLogger("market_state_model")

SNAPSHOT_VERSION = "1.0.0"

MS_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS market_state_snapshots (
    snapshot_id         TEXT PRIMARY KEY,
    asset               TEXT NOT NULL,
    timestamp_snapshot  DATETIME NOT NULL,
    market_quality      INTEGER DEFAULT 0,
    bias                TEXT DEFAULT 'NEUTRAL',
    bias_confidence     INTEGER DEFAULT 0,
    tradeable           BOOLEAN DEFAULT 1,
    snapshot_json       TEXT
);
CREATE INDEX IF NOT EXISTS idx_ms_asset_ts
    ON market_state_snapshots(asset, timestamp_snapshot);
"""


def init_market_state_schema(conn: sqlite3.Connection):
    conn.executescript(MS_SCHEMA_SQL)
    conn.commit()


def _answer_where(structure: dict, rm: dict) -> dict:
    pd_info = structure.get("premium_discount", {})
    in_zone = rm.get("in_high_confluence_zone", False) if rm else False
    zone_score = 0
    if rm:
        sa = rm.get("strongest_above")
        sb = rm.get("strongest_below")
        zone_score = max(
            sa.get("confluence_score", 0) if sa else 0,
            sb.get("confluence_score", 0) if sb else 0,
        )

    return {
        "zone": pd_info.get("zone", "EQUILIBRIUM"),
        "position": pd_info.get("position", 0.5),
        "in_reaction_zone": in_zone,
        "reaction_zone_score": zone_score,
    }


def _answer_why(structure: dict) -> dict:
    disp = structure.get("displacement", {})
    return {
        "displacement_confirmed": disp.get("confirmed", False),
        "displacement_atr": disp.get("magnitude_atr", 0),
        "volume_classification": structure.get("volume_classification", "NORMAL"),
    }


def _answer_who(structure: dict, ss: dict) -> dict:
    la = ss.get("london_action", {}) if ss else {}
    return {
        "volume_ratio": structure.get("volume_ratio_m15", 1.0),
        "session_sweep_detected": la.get("sweep_reversed", False),
        "session_sweep_direction": la.get("true_direction"),
    }


def _answer_where_going(liq: dict, direction: str) -> dict:
    result = {"target_label": None, "target_price": 0, "target_structural_score": 0}
    if not liq:
        return result

    if direction == "BULLISH":
        targets = liq.get("buy_targets", [])
    else:
        targets = liq.get("sell_targets", [])

    if targets:
        best = targets[0]
        result["target_label"] = best.get("label")
        result["target_price"] = best.get("price", 0)
        result["target_structural_score"] = best.get("structural_score", 0)

    return result


def _answer_how_strong(structure: dict) -> dict:
    th = structure.get("trend_health", {})
    return {
        "trend_phase": th.get("phase", "NEUTRAL"),
        "impulse_count": th.get("impulse_count", 0),
        "structure_confidence": structure.get("structure_confidence", 0),
    }


def _answer_when(session: str, macro: dict) -> dict:
    return {
        "session": session,
        "macro_blackout": macro.get("is_blackout", False) if macro else False,
        "macro_event_near": macro.get("next_event", {}).get("type") is not None if macro else False,
    }


def _answer_still_valid(structure: dict, ob: dict) -> dict:
    pb = structure.get("pullback_status", {})
    ob_fresh = False
    if ob:
        ob_fresh = ob.get("fresh_bullish_count", 0) + ob.get("fresh_bearish_count", 0) > 0

    return {
        "pullback_buy_valid": pb.get("buy_valid", True),
        "pullback_sell_valid": pb.get("sell_valid", True),
        "ob_fresh_exists": ob_fresh,
    }


def _compute_bias(structure: dict, rm: dict, ss: dict) -> tuple:
    """Calcola il bias (BULLISH/BEARISH/NEUTRAL) e la confidenza (0-100)."""
    signals = []

    # Structure M15
    m15 = structure.get("structure_m15", {}).get("classification", "NEUTRAL")
    if m15 == "BULLISH":
        signals.append(1)
    elif m15 == "BEARISH":
        signals.append(-1)

    # Structure H4
    h4 = structure.get("structure_h4", {}).get("classification", "NEUTRAL")
    if h4 == "BULLISH":
        signals.append(1)
    elif h4 == "BEARISH":
        signals.append(-1)

    # Displacement direction
    disp_dir = structure.get("displacement", {}).get("direction")
    if disp_dir == "BULLISH":
        signals.append(1)
    elif disp_dir == "BEARISH":
        signals.append(-1)

    # Session sweep true direction
    if ss:
        td = ss.get("london_action", {}).get("true_direction")
        if td == "BULLISH":
            signals.append(1)
        elif td == "BEARISH":
            signals.append(-1)

    # Reaction map strongest zone
    if rm:
        for side in ("strongest_above", "strongest_below"):
            zone = rm.get(side)
            if zone and zone.get("confluence_score", 0) >= 50:
                exp = zone.get("expected_reaction", "")
                if exp == "BOUNCE_UP":
                    signals.append(1)
                elif exp == "BOUNCE_DOWN":
                    signals.append(-1)

    if not signals:
        return "NEUTRAL", 0

    avg = sum(signals) / len(signals)
    confidence = int(abs(avg) * 100)

    if avg > 0.2:
        bias = "BULLISH"
    elif avg < -0.2:
        bias = "BEARISH"
    else:
        bias = "NEUTRAL"

    return bias, confidence


def _compute_quality(answers: dict, vol: dict) -> int:
    """Market Quality Score 0-100."""
    score = 0

    # Structure confidence
    score += min(answers["how_strong"]["structure_confidence"], 30)

    # Reaction zone
    if answers["where"]["in_reaction_zone"]:
        score += 15

    # Displacement
    if answers["why"]["displacement_confirmed"]:
        score += 15

    # Session
    session = answers["when"]["session"]
    if session in ("LONDON", "NEW_YORK"):
        score += 10
    elif session == "OVERLAP":
        score -= 5

    # Not in blackout
    if not answers["when"]["macro_blackout"]:
        score += 5

    # Pullback valid
    if answers["still_valid"]["pullback_buy_valid"] or answers["still_valid"]["pullback_sell_valid"]:
        score += 5

    # OB fresh
    if answers["still_valid"]["ob_fresh_exists"]:
        score += 10

    # Volume
    vol_cls = answers["why"].get("volume_classification", "NORMAL")
    if vol_cls in ("HIGH", "CLIMAX"):
        score += 5

    # Volatility not extreme
    if vol:
        regime = vol.get("regime", "NORMAL")
        if regime == "EXTREME":
            score -= 10
        elif regime == "LOW":
            score -= 5

    return max(0, min(100, score))


# ============================================================
# Entry Point
# ============================================================

def produce_market_state(
    asset: str,
    structure_snapshot: dict,
    ob_snapshot: dict = None,
    fvg_snapshot: dict = None,
    liq_snapshot: dict = None,
    ss_snapshot: dict = None,
    rm_snapshot: dict = None,
    cs_snapshot: dict = None,
    macro_snapshot: dict = None,
    vol_snapshot: dict = None,
    session: str = "ASIA",
    conn: sqlite3.Connection = None,
    now: datetime = None,
) -> dict:
    if now is None:
        now = datetime.now(timezone.utc)
    now_iso = now.isoformat()

    if not structure_snapshot:
        structure_snapshot = {}

    # ── Le 7 risposte ────────────────────────────────────────
    answers = {
        "where": _answer_where(structure_snapshot, rm_snapshot),
        "why": _answer_why(structure_snapshot),
        "who": _answer_who(structure_snapshot, ss_snapshot),
        "where_going": _answer_where_going(liq_snapshot,
            structure_snapshot.get("structure_m15", {}).get("classification", "NEUTRAL")),
        "how_strong": _answer_how_strong(structure_snapshot),
        "when": _answer_when(session, macro_snapshot),
        "still_valid": _answer_still_valid(structure_snapshot, ob_snapshot),
    }

    # ── Bias e quality ───────────────────────────────────────
    bias, bias_confidence = _compute_bias(structure_snapshot, rm_snapshot, ss_snapshot)
    quality = _compute_quality(answers, vol_snapshot)

    tradeable = (
        not answers["when"]["macro_blackout"]
        and quality >= 20
    )

    # ── Snapshot ─────────────────────────────────────────────
    snapshot = {
        "asset": asset,
        "timestamp": now_iso,
        "state_version": SNAPSHOT_VERSION,

        "answers": answers,

        "market_quality_score": quality,
        "tradeable": tradeable,
        "bias": bias,
        "bias_confidence": bias_confidence,

        "candlestick_confirmation": cs_snapshot.get("has_confirmation", False) if cs_snapshot else False,
        "candlestick_pattern": cs_snapshot.get("strongest_pattern") if cs_snapshot else None,
    }

    # ── Salva ────────────────────────────────────────────────
    if conn:
        try:
            conn.execute("""
                INSERT INTO market_state_snapshots (
                    snapshot_id, asset, timestamp_snapshot,
                    market_quality, bias, bias_confidence,
                    tradeable, snapshot_json
                ) VALUES (?,?,?,?,?,?,?,?)
            """, (
                str(uuid.uuid4()), asset, now_iso,
                quality, bias, bias_confidence, tradeable,
                json.dumps(snapshot, default=str),
            ))
            conn.commit()
        except Exception as e:
            logger.warning("Market State [%s]: errore salvataggio: %s", asset, e)

    logger.info(
        "Market State [%s]: quality=%d bias=%s(conf=%d) tradeable=%s "
        "where=%s why_disp=%s who_vol=%.2f how=%s/%d when=%s valid=%s",
        asset, quality, bias, bias_confidence, tradeable,
        answers["where"]["zone"],
        answers["why"]["displacement_confirmed"],
        answers["who"]["volume_ratio"],
        answers["how_strong"]["trend_phase"],
        answers["how_strong"]["impulse_count"],
        answers["when"]["session"],
        answers["still_valid"]["pullback_buy_valid"] or answers["still_valid"]["pullback_sell_valid"],
    )

    return snapshot
