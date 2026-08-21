"""
strategies/ote/confluence_engine.py
OTE — Cervello di Confluenza

NON un nuovo engine — un LETTORE di tutti gli engine esistenti.
Legge i dati GIA' calcolati da LH, OB, FVG, Reaction Map, Session
levels, e cerca dove convergono. Nessun engine viene modificato.

Il principio: una zona vista da UNA fonte e' un'ipotesi. Una zona
vista da TRE fonti indipendenti e' una confluenza. Il prezzo reagisce
dove MOLTI partecipanti hanno ordini — e ogni fonte rappresenta un
tipo diverso di partecipante.

Fonti di zone precise (confini stretti, < 15 punti):
    - LH Restart Zone (impulsi forti con ricorrenza)
    - Order Block FRESH/TESTED (footprint istituzionale)
    - FVG OPEN (imbalance non colmato)

Fonti di livelli puntuali (un prezzo specifico):
    - Swing H4/D1 (struttura macro)
    - Equal High/Low (cluster di stop)
    - Previous Day High/Low
    - Asian Session High/Low

Overlay (zone larghe, bonus non zona):
    - Reaction Map confluence area (dove il prezzo ha gia' reagito)
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone, timedelta
from typing import Optional

from strategies.tt.liquidity_engine import (
    _detect_swings, _detect_equal_levels, _manual_atr,
)

logger = logging.getLogger("ote.confluence")

# Pesi per la confluenza — quante "voci" ha ogni fonte
SOURCE_WEIGHT = {
    "LH_RESTART": 3,      # impulso forte + ricorrenza, la piu testata
    "ORDER_BLOCK": 2,     # footprint istituzionale
    "FVG": 1,             # imbalance, meno affidabile da solo
    "SWING": 2,           # struttura macro
    "EQUAL_LEVEL": 2,     # cluster di stop (liquidita pura)
    "PREV_DAY": 2,        # livello universale
    "ASIAN_SESSION": 2,   # range notturno
    "REACTION_MAP": 2,    # overlay: il prezzo ha gia reagito qui
}

# Soglia per raggruppare fonti "vicine" nella stessa zona
CLUSTER_TOLERANCE_POINTS = {"XAU_USD": 5.0, "BTC_USDT": 50.0}

# Score minimo di confluenza per creare un candidate
MIN_CONFLUENCE_SCORE = 4


def find_confluence_zones(conn: sqlite3.Connection, asset: str,
                          df_h1, current_price: float, now: datetime) -> list:
    """
    Cerca zone di confluenza combinando TUTTE le fonti disponibili.
    Restituisce una lista di zone ordinate per confluence_score.
    """
    raw_levels = []

    # ── 1. LH Restart Zone (precise, testate) ──
    raw_levels.extend(_load_lh_zones(conn, asset))

    # ── 2. Order Block FRESH/TESTED ──
    raw_levels.extend(_load_order_blocks(conn, asset))

    # ── 3. FVG OPEN ──
    raw_levels.extend(_load_fvg_zones(conn, asset))

    # ── 4. Swing H4/D1 ──
    raw_levels.extend(_load_swing_levels(conn, asset))

    # ── 5. Equal High/Low (calcolati al volo su H1) ──
    raw_levels.extend(_compute_equal_levels(df_h1))

    # ── 6. Session levels ──
    raw_levels.extend(_compute_session_levels(conn, asset, now))

    if not raw_levels:
        return []

    # ── 7. Clustering: raggruppa fonti vicine ──
    tolerance = CLUSTER_TOLERANCE_POINTS.get(asset, 10.0)
    clusters = _cluster_levels(raw_levels, tolerance)

    # ── 8. Reaction Map overlay: bonus per zone in area RM ──
    rm_zones = _load_reaction_map_zones(conn, asset)
    for cluster in clusters:
        _apply_reaction_map_bonus(cluster, rm_zones)

    # ── 9. Filtra per score minimo e ordina ──
    valid = [c for c in clusters if c["confluence_score"] >= MIN_CONFLUENCE_SCORE]
    valid.sort(key=lambda c: -c["confluence_score"])

    return valid


# ============================================================
# Loader per ogni fonte
# ============================================================

def _load_lh_zones(conn, asset: str) -> list:
    """Zone LH ACTIVE con i loro bordi e score."""
    levels = []
    try:
        zones = conn.execute("""
            SELECT r.zone_ref, r.confirmed_restarts, r.failed_visits,
                   a.zone_high, a.zone_low, a.restart_score, a.zone_strength
            FROM lh_zone_recurrence r
            JOIN (
                SELECT zone_ref, zone_high, zone_low, restart_score, zone_strength,
                       ROW_NUMBER() OVER (PARTITION BY zone_ref ORDER BY created_at DESC) as rn
                FROM lh_zone_alerts WHERE asset=?
            ) a ON r.zone_ref = a.zone_ref AND a.rn = 1
            WHERE r.asset=? AND r.status='ACTIVE'
        """, (asset, asset)).fetchall()
    except Exception:
        # Fallback se la query con window function non funziona su questa versione SQLite
        try:
            zone_refs = conn.execute(
                "SELECT zone_ref, confirmed_restarts, failed_visits FROM lh_zone_recurrence WHERE asset=? AND status='ACTIVE'",
                (asset,)).fetchall()
            zones = []
            for zref, restarts, failures in zone_refs:
                alert = conn.execute(
                    "SELECT zone_high, zone_low, restart_score, zone_strength FROM lh_zone_alerts WHERE zone_ref=? ORDER BY created_at DESC LIMIT 1",
                    (zref,)).fetchone()
                if alert:
                    zones.append((zref, restarts, failures, alert[0], alert[1], alert[2], alert[3]))
        except Exception as e:
            logger.warning("confluence _load_lh_zones: %s", e)
            return []

    for zref, restarts, failures, zh, zl, score, strength in zones:
        levels.append({
            "source": "LH_RESTART", "price_high": zh, "price_low": zl,
            "midpoint": (zh + zl) / 2,
            "details": {"zone_ref": zref, "restart_score": score,
                       "zone_strength": strength, "restarts": restarts,
                       "failures": failures},
        })
    return levels


def _load_order_blocks(conn, asset: str) -> list:
    """OB FRESH o TESTED — zone istituzionali ancora valide."""
    levels = []
    try:
        rows = conn.execute("""
            SELECT ob_id, direction, zone_high, zone_low, quality_score,
                   status, has_fvg, displacement_atr, structure_confidence
            FROM order_blocks
            WHERE asset=? AND status IN ('FRESH','TESTED')
        """, (asset,)).fetchall()
    except Exception as e:
        logger.warning("confluence _load_order_blocks: %s", e)
        return []

    for ob_id, direction, zh, zl, quality, status, has_fvg, disp_atr, struct_conf in rows:
        levels.append({
            "source": "ORDER_BLOCK", "price_high": zh, "price_low": zl,
            "midpoint": (zh + zl) / 2,
            "details": {"ob_id": ob_id, "direction": direction,
                       "quality": quality, "status": status,
                       "has_fvg": has_fvg, "displacement_atr": disp_atr},
        })
    return levels


def _load_fvg_zones(conn, asset: str) -> list:
    """FVG OPEN — gap non ancora colmati."""
    levels = []
    try:
        rows = conn.execute("""
            SELECT fvg_id, direction, zone_high, zone_low, zone_size_pct,
                   during_displacement
            FROM fvg_zones
            WHERE asset=? AND status='OPEN' AND is_invalidated=0
        """, (asset,)).fetchall()
    except Exception as e:
        logger.warning("confluence _load_fvg_zones: %s", e)
        return []

    for fvg_id, direction, zh, zl, size_pct, during_disp in rows:
        levels.append({
            "source": "FVG", "price_high": zh, "price_low": zl,
            "midpoint": (zh + zl) / 2,
            "details": {"fvg_id": fvg_id, "direction": direction,
                       "size_pct": size_pct, "during_displacement": during_disp},
        })
    return levels


def _load_swing_levels(conn, asset: str) -> list:
    """Swing H4/D1 persistiti da LH."""
    levels = []
    try:
        rows = conn.execute("""
            SELECT swing_type, price, timeframe FROM lh_swing_zones
            WHERE asset=?
        """, (asset,)).fetchall()
    except Exception as e:
        logger.warning("confluence _load_swing_levels: %s", e)
        return []

    for stype, price, tf in rows:
        levels.append({
            "source": "SWING", "price_high": price, "price_low": price,
            "midpoint": price,
            "details": {"swing_type": stype, "timeframe": tf},
        })
    return levels


def _compute_equal_levels(df_h1) -> list:
    """Equal High/Low calcolati al volo."""
    swings = _detect_swings(df_h1)
    equal = _detect_equal_levels(swings)
    levels = []
    for eq in equal:
        levels.append({
            "source": "EQUAL_LEVEL", "price_high": eq["price"],
            "price_low": eq["price"], "midpoint": eq["price"],
            "details": {"type": eq["type"]},
        })
    return levels


def _compute_session_levels(conn, asset: str, now: datetime) -> list:
    """Previous Day H/L + Asian Session H/L."""
    levels = []
    try:
        yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
        today = now.strftime("%Y-%m-%d")
        row = conn.execute("""
            SELECT MAX(high), MIN(low) FROM candles_cache
            WHERE asset=? AND timeframe='1h'
            AND datetime(timestamp/1000, 'unixepoch') >= ?
            AND datetime(timestamp/1000, 'unixepoch') < ?
        """, (asset, yesterday, today)).fetchone()
        if row and row[0]:
            levels.append({"source": "PREV_DAY", "price_high": float(row[0]),
                          "price_low": float(row[0]), "midpoint": float(row[0]),
                          "details": {"type": "PREV_DAY_HIGH"}})
            levels.append({"source": "PREV_DAY", "price_high": float(row[1]),
                          "price_low": float(row[1]), "midpoint": float(row[1]),
                          "details": {"type": "PREV_DAY_LOW"}})
    except Exception as e:
        logger.warning("confluence _compute_session_levels prev_day: %s", e)

    try:
        asian_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        asian_end = now.replace(hour=8, minute=0, second=0, microsecond=0)
        if now >= asian_end:
            ts_s = int(asian_start.timestamp() * 1000)
            ts_e = int(asian_end.timestamp() * 1000)
            row2 = conn.execute("""
                SELECT MAX(high), MIN(low) FROM v3_candles_cache
                WHERE asset=? AND timeframe='5m'
                AND timestamp >= ? AND timestamp < ?
            """, (asset, ts_s, ts_e)).fetchone()
            if row2 and row2[0]:
                levels.append({"source": "ASIAN_SESSION", "price_high": float(row2[0]),
                              "price_low": float(row2[0]), "midpoint": float(row2[0]),
                              "details": {"type": "ASIAN_HIGH"}})
                levels.append({"source": "ASIAN_SESSION", "price_high": float(row2[1]),
                              "price_low": float(row2[1]), "midpoint": float(row2[1]),
                              "details": {"type": "ASIAN_LOW"}})
    except Exception as e:
        logger.warning("confluence _compute_session_levels asian: %s", e)

    return levels


def _load_reaction_map_zones(conn, asset: str) -> list:
    """Zone Reaction Map come overlay (non come fonte primaria)."""
    try:
        row = conn.execute("""
            SELECT snapshot_json FROM reaction_map_snapshots
            WHERE asset=? ORDER BY timestamp_snapshot DESC LIMIT 1
        """, (asset,)).fetchone()
        if not row:
            return []
        snap = json.loads(row[0])
        return snap.get("zones", [])
    except Exception as e:
        logger.warning("confluence _load_reaction_map_zones: %s", e)
        return []


# ============================================================
# Clustering: raggruppa fonti che puntano alla stessa area
# ============================================================

def _cluster_levels(raw_levels: list, tolerance: float) -> list:
    """
    Raggruppa livelli il cui midpoint e' entro 'tolerance' punti.
    Ogni cluster diventa una zona di confluenza con uno score pesato.
    """
    if not raw_levels:
        return []

    sorted_levels = sorted(raw_levels, key=lambda lv: lv["midpoint"])
    clusters = []
    used = [False] * len(sorted_levels)

    for i, lv in enumerate(sorted_levels):
        if used[i]:
            continue
        cluster_members = [lv]
        used[i] = True

        for j in range(i + 1, len(sorted_levels)):
            if used[j]:
                continue
            if abs(sorted_levels[j]["midpoint"] - lv["midpoint"]) <= tolerance:
                cluster_members.append(sorted_levels[j])
                used[j] = True
            elif sorted_levels[j]["midpoint"] - lv["midpoint"] > tolerance:
                break

        # Calcola i confini del cluster
        all_highs = [m["price_high"] for m in cluster_members]
        all_lows = [m["price_low"] for m in cluster_members]
        zone_high = max(all_highs)
        zone_low = min(all_lows)

        # Confluenza: fonti DIVERSE che convergono (non contare due LH come 2)
        sources_seen = {}
        for m in cluster_members:
            src = m["source"]
            weight = SOURCE_WEIGHT.get(src, 1)
            if src not in sources_seen or weight > sources_seen[src]:
                sources_seen[src] = weight

        confluence_score = sum(sources_seen.values())
        source_list = list(sources_seen.keys())

        # Best LH zone in questo cluster (se presente)
        lh_members = [m for m in cluster_members if m["source"] == "LH_RESTART"]
        best_lh = max(lh_members, key=lambda m: m["details"].get("restart_score", 0)) if lh_members else None

        clusters.append({
            "zone_high": round(zone_high, 5),
            "zone_low": round(zone_low, 5),
            "midpoint": round((zone_high + zone_low) / 2, 5),
            "confluence_score": confluence_score,
            "source_count": len(source_list),
            "sources": source_list,
            "members": cluster_members,
            "best_lh": best_lh,
            # Campi per il Candidate Registry
            "zone_ref": best_lh["details"]["zone_ref"] if best_lh else f"cluster_{round(zone_low)}_{round(zone_high)}",
            "zone_strength": _classify_strength(confluence_score),
            "restart_score": best_lh["details"]["restart_score"] if best_lh else confluence_score * 10,
            "confirmed_restarts": best_lh["details"]["restarts"] if best_lh else 0,
            "failed_visits": best_lh["details"]["failures"] if best_lh else 0,
        })

    return clusters


def _classify_strength(confluence_score: int) -> str:
    if confluence_score >= 7:
        return "STRONG"
    if confluence_score >= 4:
        return "MODERATE"
    return "WEAK"


def _apply_reaction_map_bonus(cluster: dict, rm_zones: list):
    """
    Se il cluster cade dentro una zona Reaction Map con alta confluenza,
    aggiungi un bonus. Il Reaction Map e' un overlay (conferma), non
    una fonte primaria (le sue zone sono troppo larghe per essere precise).
    """
    mid = cluster["midpoint"]
    for rmz in rm_zones:
        if rmz["zone_low"] <= mid <= rmz["zone_high"]:
            rm_conf = rmz.get("confluence_count", 0)
            rm_score = rmz.get("confluence_score", 0)
            if rm_conf >= 2:
                bonus = SOURCE_WEIGHT["REACTION_MAP"]
                cluster["confluence_score"] += bonus
                if "REACTION_MAP" not in cluster["sources"]:
                    cluster["sources"].append("REACTION_MAP")
                    cluster["source_count"] += 1
                cluster["reaction_map_score"] = rm_score
                cluster["reaction_map_strength"] = rmz.get("reaction_strength")
            break
