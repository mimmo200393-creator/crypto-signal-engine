"""
core/reaction_map.py
Reaction Map — Sprint 8

Layer 2: dipende da TUTTI i Layer 0 e 1.
Unifica tutte le zone dei moduli precedenti in una singola mappa.
Per ogni zona calcola un punteggio di confluenza.

La Reaction Map NON inventa zone — unifica quelle gia' identificate
da OB Engine, FVG Engine, Liquidity Engine, Structure Engine.
Due zone di moduli diversi che si sovrappongono diventano una
singola zona con confluenza piu' alta.

Modalita': LIVE MODE.
Dipendenze: pandas, sqlite3, logging. Consuma tutti gli snapshot L0+L1.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from datetime import datetime, timezone

logger = logging.getLogger("reaction_map")

SNAPSHOT_VERSION = "1.0.0"

DEFAULT_CONFIG = {
    "zone_merge_tolerance_pct": 0.003,  # zone entro 0.3% vengono fuse
    "max_zones": 20,
}

# Pesi per il calcolo del confluence score
WEIGHTS = {
    "order_block": 25,
    "fvg": 15,
    "liquidity": 20,
    "support_resistance": 10,
    "session_extreme": 10,
    "premium_discount": 10,
    "displacement": 10,
}

RM_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS reaction_map_snapshots (
    snapshot_id         TEXT PRIMARY KEY,
    asset               TEXT NOT NULL,
    timestamp_snapshot  DATETIME NOT NULL,
    total_zones         INTEGER DEFAULT 0,
    strongest_score     REAL DEFAULT 0,
    snapshot_json       TEXT
);
CREATE INDEX IF NOT EXISTS idx_rm_asset_ts
    ON reaction_map_snapshots(asset, timestamp_snapshot);
"""


def init_reaction_map_schema(conn: sqlite3.Connection):
    conn.executescript(RM_SCHEMA_SQL)
    conn.commit()


# ============================================================
# Zone Collection
# ============================================================

def _collect_zones(ob_snapshot: dict, fvg_snapshot: dict,
                    liq_snapshot: dict, structure_snapshot: dict,
                    current_price: float) -> list:
    """Raccoglie tutte le zone da tutti i moduli in un formato unificato."""
    raw_zones = []

    # ── Order Blocks ─────────────────────────────────────────
    if ob_snapshot:
        for ob in ob_snapshot.get("order_blocks", []):
            if ob.get("status") == "EXPIRED":
                continue
            raw_zones.append({
                "source": "ORDER_BLOCK",
                "zone_high": ob["zone_high"],
                "zone_low": ob["zone_low"],
                "direction": ob.get("direction"),
                "strength": ob.get("quality_score", 0) / 5.0,
                "status": ob.get("status", "FRESH"),
                "metadata": {
                    "ob_quality": ob.get("quality_score", 0),
                    "ob_has_fvg": ob.get("has_fvg", False),
                    "ob_displacement_atr": ob.get("displacement_atr", 0),
                },
            })

    # ── Fair Value Gaps ──────────────────────────────────────
    if fvg_snapshot:
        for fvg in fvg_snapshot.get("fvgs", []):
            if fvg.get("status") == "EXPIRED":
                continue
            raw_zones.append({
                "source": "FVG",
                "zone_high": fvg["zone_high"],
                "zone_low": fvg["zone_low"],
                "direction": fvg.get("direction"),
                "strength": 0.7 if fvg.get("during_displacement") else 0.4,
                "status": fvg.get("status", "OPEN"),
                "metadata": {
                    "fvg_fill_pct": fvg.get("fill_percentage", 0),
                    "fvg_during_displacement": fvg.get("during_displacement", False),
                    "fvg_ifvg_active": fvg.get("ifvg_active", False),
                },
            })

    # ── Liquidity Levels ─────────────────────────────────────
    if liq_snapshot:
        for lv in liq_snapshot.get("levels", []):
            price = lv.get("price", 0)
            if price <= 0:
                continue
            # Crea una zona sottile attorno al livello (±0.1%)
            zone_size = price * 0.001
            raw_zones.append({
                "source": "LIQUIDITY",
                "zone_high": price + zone_size,
                "zone_low": price - zone_size,
                "direction": "BEARISH" if lv.get("kind") == "high" else "BULLISH",
                "strength": lv.get("structural_score", 0.5),
                "status": "SWEPT" if lv.get("swept") else "ACTIVE",
                "metadata": {
                    "liq_label": lv.get("label"),
                    "liq_type": lv.get("type"),
                    "liq_structural_score": lv.get("structural_score", 0),
                },
            })

    # ── Structure levels (swing points come S/R) ─────────────
    if structure_snapshot:
        struct_h4 = structure_snapshot.get("structure_h4", {})
        for key in ("last_hh", "last_hl", "last_lh", "last_ll"):
            val = struct_h4.get(key)
            if val is not None and val > 0:
                zone_size = val * 0.001
                raw_zones.append({
                    "source": "STRUCTURE_SR",
                    "zone_high": val + zone_size,
                    "zone_low": val - zone_size,
                    "direction": "BULLISH" if "l" in key else "BEARISH",
                    "strength": 0.5,
                    "status": "ACTIVE",
                    "metadata": {"level_type": key},
                })

    return raw_zones


# ============================================================
# Zone Merging
# ============================================================

def _merge_overlapping_zones(raw_zones: list, tolerance_pct: float,
                               current_price: float) -> list:
    """
    Fonde zone che si sovrappongono. Ogni zona fusa accumula le
    sorgenti dei moduli che la compongono → confluenza.
    """
    if not raw_zones:
        return []

    # Ordina per midpoint
    for z in raw_zones:
        z["midpoint"] = (z["zone_high"] + z["zone_low"]) / 2
    raw_zones.sort(key=lambda z: z["midpoint"])

    merged = []
    current_group = [raw_zones[0]]

    for i in range(1, len(raw_zones)):
        prev_mid = current_group[-1]["midpoint"]
        curr_mid = raw_zones[i]["midpoint"]

        if prev_mid > 0 and abs(curr_mid - prev_mid) / prev_mid <= tolerance_pct:
            current_group.append(raw_zones[i])
        else:
            merged.append(_fuse_group(current_group, current_price))
            current_group = [raw_zones[i]]

    if current_group:
        merged.append(_fuse_group(current_group, current_price))

    return merged


def _fuse_group(group: list, current_price: float) -> dict:
    """Fonde un gruppo di zone sovrapposte in una singola zona."""
    zone_high = max(z["zone_high"] for z in group)
    zone_low = min(z["zone_low"] for z in group)
    midpoint = (zone_high + zone_low) / 2

    sources = set(z["source"] for z in group)
    directions = [z["direction"] for z in group if z["direction"]]

    # Confluenza: quanti moduli diversi convergono
    has_ob = "ORDER_BLOCK" in sources
    has_fvg = "FVG" in sources
    has_liq = "LIQUIDITY" in sources
    has_sr = "STRUCTURE_SR" in sources

    confluence_count = len(sources)

    # Confluence score (pesato)
    score = 0
    if has_ob:
        ob_quality = max(
            (z["metadata"].get("ob_quality", 0) for z in group if z["source"] == "ORDER_BLOCK"),
            default=0
        )
        score += WEIGHTS["order_block"] * (ob_quality / 5.0)
    if has_fvg:
        fvg_disp = any(
            z["metadata"].get("fvg_during_displacement", False)
            for z in group if z["source"] == "FVG"
        )
        score += WEIGHTS["fvg"] * (1.0 if fvg_disp else 0.5)
    if has_liq:
        liq_score = max(
            (z["metadata"].get("liq_structural_score", 0) for z in group if z["source"] == "LIQUIDITY"),
            default=0
        )
        score += WEIGHTS["liquidity"] * liq_score
    if has_sr:
        score += WEIGHTS["support_resistance"]

    # Premium/Discount context
    in_premium = current_price > 0 and midpoint > current_price
    in_discount = current_price > 0 and midpoint < current_price

    if in_discount and any(d == "BULLISH" for d in directions):
        score += WEIGHTS["premium_discount"]
    elif in_premium and any(d == "BEARISH" for d in directions):
        score += WEIGHTS["premium_discount"]

    score = min(score, 100)

    # Expected reaction
    bull_count = sum(1 for d in directions if d == "BULLISH")
    bear_count = sum(1 for d in directions if d == "BEARISH")

    if bull_count > bear_count:
        expected = "BOUNCE_UP"
    elif bear_count > bull_count:
        expected = "BOUNCE_DOWN"
    else:
        expected = "UNKNOWN"

    # Reaction strength
    if score >= 70:
        strength = "STRONG"
    elif score >= 40:
        strength = "MODERATE"
    else:
        strength = "WEAK"

    distance = abs(midpoint - current_price) / current_price if current_price > 0 else 0

    return {
        "zone_id": str(uuid.uuid4())[:8],
        "zone_high": round(zone_high, 4),
        "zone_low": round(zone_low, 4),
        "zone_midpoint": round(midpoint, 4),
        "has_order_block": has_ob,
        "ob_quality": max((z["metadata"].get("ob_quality", 0) for z in group if z["source"] == "ORDER_BLOCK"), default=0),
        "has_fvg": has_fvg,
        "fvg_status": next((z["status"] for z in group if z["source"] == "FVG"), None),
        "has_liquidity_pool": has_liq,
        "liq_structural_score": max((z["metadata"].get("liq_structural_score", 0) for z in group if z["source"] == "LIQUIDITY"), default=0),
        "has_support_resistance": has_sr,
        "is_session_extreme": False,
        "in_premium": in_premium,
        "in_discount": in_discount,
        "confluence_count": confluence_count,
        "confluence_score": round(score, 1),
        "expected_reaction": expected,
        "reaction_strength": strength,
        "distance_from_price_pct": round(distance, 6),
        "sources": list(sources),
    }


# ============================================================
# Entry Point
# ============================================================

def produce_reaction_map(
    asset: str,
    structure_snapshot: dict,
    ob_snapshot: dict = None,
    fvg_snapshot: dict = None,
    liq_snapshot: dict = None,
    conn: sqlite3.Connection = None,
    now: datetime = None,
    config: dict = None,
) -> dict:
    if now is None:
        now = datetime.now(timezone.utc)
    if config is None:
        config = {}

    cfg = {**DEFAULT_CONFIG, **config}
    now_iso = now.isoformat()
    current_price = structure_snapshot.get("current_price", 0) if structure_snapshot else 0

    # ── 1. Raccogli zone ─────────────────────────────────────
    raw = _collect_zones(ob_snapshot, fvg_snapshot, liq_snapshot,
                          structure_snapshot, current_price)

    # ── 2. Fondi zone sovrapposte ────────────────────────────
    tolerance = cfg.get("zone_merge_tolerance_pct", 0.003)
    merged = _merge_overlapping_zones(raw, tolerance, current_price)

    # ── 3. Ordina per confluence score ───────────────────────
    merged.sort(key=lambda z: z["confluence_score"], reverse=True)
    max_zones = cfg.get("max_zones", 20)
    merged = merged[:max_zones]

    # ── 4. Strongest above/below ─────────────────────────────
    above = [z for z in merged if z["zone_midpoint"] > current_price]
    below = [z for z in merged if z["zone_midpoint"] < current_price]

    strongest_above = above[0] if above else None
    strongest_below = below[0] if below else None

    # ── 5. In high confluence zone? ──────────────────────────
    in_high_conf = any(
        z["zone_low"] <= current_price <= z["zone_high"] and z["confluence_score"] >= 70
        for z in merged
    )

    # ── Snapshot ─────────────────────────────────────────────
    snapshot = {
        "asset": asset,
        "timestamp": now_iso,
        "snapshot_version": SNAPSHOT_VERSION,
        "zones": merged,
        "strongest_above": strongest_above,
        "strongest_below": strongest_below,
        "in_high_confluence_zone": in_high_conf,
        "total_zones": len(merged),
    }

    # ── Salva ────────────────────────────────────────────────
    if conn:
        try:
            conn.execute("""
                INSERT INTO reaction_map_snapshots (
                    snapshot_id, asset, timestamp_snapshot,
                    total_zones, strongest_score, snapshot_json
                ) VALUES (?,?,?,?,?,?)
            """, (
                str(uuid.uuid4()), asset, now_iso,
                len(merged),
                merged[0]["confluence_score"] if merged else 0,
                json.dumps(snapshot, default=str),
            ))
            conn.commit()
        except Exception as e:
            logger.warning("Reaction Map [%s]: errore salvataggio: %s", asset, e)

    # ── Log ──────────────────────────────────────────────────
    logger.info(
        "Reaction Map [%s]: zones=%d in_high_conf=%s "
        "strongest_above=%s(%s) strongest_below=%s(%s)",
        asset, len(merged), in_high_conf,
        f"{strongest_above['zone_midpoint']:.2f}" if strongest_above else "none",
        f"score={strongest_above['confluence_score']}" if strongest_above else "",
        f"{strongest_below['zone_midpoint']:.2f}" if strongest_below else "none",
        f"score={strongest_below['confluence_score']}" if strongest_below else "",
    )

    return snapshot
