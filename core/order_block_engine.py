"""
core/order_block_engine.py
Order Block Engine — Sprint 4 V2

Fix V2:
    - OB creati su TUTTI gli eventi BOS/CHOCH, non solo displacement
    - Scansione storico: analizza ultime N candele per trovare OB esistenti
    - Displacement e' un campo di qualita', non un gate
    - has_displacement: True/False per futura calibrazione

Layer 1: dipende da Structure Engine (Layer 0).
Modalita': LIVE MODE.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger("order_block_engine")

SNAPSHOT_VERSION = "2.0.0"

DEFAULT_CONFIG = {
    "ob_lookback": 20,
    "ob_max_tracked": 30,
    "ob_max_age_bars": 300,
    "ob_body_min_pct": 0.30,
    "mitigation_touch_pct": 0.002,
    "historical_scan_bars": 200,
}

OB_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS order_block_snapshots (
    snapshot_id         TEXT PRIMARY KEY,
    asset               TEXT NOT NULL,
    timestamp_snapshot  DATETIME NOT NULL,
    snapshot_version    TEXT DEFAULT '2.0.0',
    fresh_bullish       INTEGER DEFAULT 0,
    fresh_bearish       INTEGER DEFAULT 0,
    total_tracked       INTEGER DEFAULT 0,
    snapshot_json       TEXT
);
CREATE INDEX IF NOT EXISTS idx_ob_asset_ts
    ON order_block_snapshots(asset, timestamp_snapshot);

CREATE TABLE IF NOT EXISTS order_blocks (
    ob_id               TEXT PRIMARY KEY,
    asset               TEXT NOT NULL,
    direction           TEXT NOT NULL,
    timeframe           TEXT DEFAULT 'M15',
    zone_high           REAL NOT NULL,
    zone_low            REAL NOT NULL,
    formation_ts        DATETIME NOT NULL,
    status              TEXT DEFAULT 'FRESH',
    quality_score       INTEGER DEFAULT 0,
    has_fvg             BOOLEAN DEFAULT 0,
    has_sweep_before    BOOLEAN DEFAULT 0,
    has_displacement    BOOLEAN DEFAULT 0,
    is_last_ob          BOOLEAN DEFAULT 0,
    session_quality     TEXT,
    displacement_atr    REAL DEFAULT 0,
    mitigation_count    INTEGER DEFAULT 0,
    first_mitigation_ts DATETIME,
    invalidation_ts     DATETIME,
    age_bars            INTEGER DEFAULT 0,
    trend_at_formation  TEXT,
    in_discount         BOOLEAN DEFAULT 0,
    in_premium          BOOLEAN DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_ob_asset_status
    ON order_blocks(asset, status);
"""


def init_ob_schema(conn: sqlite3.Connection):
    conn.executescript(OB_SCHEMA_SQL)
    # Aggiungi colonna has_displacement se non esiste (migrazione)
    try:
        conn.execute("ALTER TABLE order_blocks ADD COLUMN has_displacement BOOLEAN DEFAULT 0")
        conn.commit()
    except Exception:
        pass  # colonna gia' presente
    conn.commit()


# ============================================================
# OB Detection — su QUALSIASI evento strutturale
# ============================================================

def _find_contrary_candle(df_m15: pd.DataFrame, event_direction: str,
                           event_idx: int, max_search: int = 6) -> dict | None:
    """
    Cerca l'ultima candela contraria PRIMA di un evento strutturale.

    Per un evento BULLISH (BOS up / CHOCH up): cerca candela bearish
    Per un evento BEARISH (BOS down / CHOCH down): cerca candela bullish
    """
    for i in range(event_idx - 1, max(event_idx - max_search - 1, -1), -1):
        if i < 0 or i >= len(df_m15):
            continue

        candle = df_m15.iloc[i]
        open_ = float(candle["open"])
        close = float(candle["close"])

        if event_direction == "BULLISH" and close < open_:
            return {"candle": candle, "index": i}
        if event_direction == "BEARISH" and close > open_:
            return {"candle": candle, "index": i}

    return None


def _find_new_obs(df_m15: pd.DataFrame, structure_snapshot: dict,
                   session: str, cfg: dict) -> list:
    """
    Cerca OB basandosi su TUTTI gli eventi strutturali (BOS, CHOCH),
    non solo sul displacement.
    """
    obs = []
    events = structure_snapshot.get("events", [])
    structural_events = [e for e in events if e.get("type") in ("BOS", "CHOCH")]

    if not structural_events:
        return obs

    disp = structure_snapshot.get("displacement", {})
    disp_confirmed = disp.get("confirmed", False)
    disp_atr = disp.get("magnitude_atr", 0) if disp_confirmed else 0

    for event in structural_events:
        direction = event.get("direction")
        if not direction:
            continue

        # Cerca la candela contraria nelle ultime candele
        search_from = len(df_m15) - 1
        result = _find_contrary_candle(df_m15, direction, search_from)

        if result:
            ob = _build_ob(
                result["candle"], direction, structure_snapshot,
                session, disp_atr, disp_confirmed, event.get("type", "BOS"), cfg
            )
            obs.append(ob)

    return obs


def _scan_historical_obs(df_m15: pd.DataFrame, cfg: dict,
                          session: str) -> list:
    """
    Scansione storico: cerca OB nelle candele passate.
    Identifica movimenti impulsivi (3+ candele nella stessa direzione
    con corpo significativo) e trova la candela contraria precedente.
    """
    obs = []
    scan_bars = cfg.get("historical_scan_bars", 200)
    body_min = cfg.get("ob_body_min_pct", 0.30)

    if len(df_m15) < 20:
        return obs

    n = min(scan_bars, len(df_m15))
    df = df_m15.iloc[-n:]

    i = 3
    while i < len(df) - 1:
        # Cerca sequenze impulsive: 3+ candele nella stessa direzione
        c0 = df.iloc[i]
        o0, c0_close, h0, l0 = float(c0["open"]), float(c0["close"]), float(c0["high"]), float(c0["low"])
        range0 = h0 - l0

        if range0 <= 0:
            i += 1
            continue

        body_pct = abs(c0_close - o0) / range0
        is_bull = c0_close > o0 and body_pct >= body_min
        is_bear = c0_close < o0 and body_pct >= body_min

        if not is_bull and not is_bear:
            i += 1
            continue

        # Conta candele consecutive nella stessa direzione
        consecutive = 1
        for j in range(i - 1, max(i - 4, -1), -1):
            cj = df.iloc[j]
            oj, cj_close = float(cj["open"]), float(cj["close"])
            if is_bull and cj_close > oj:
                consecutive += 1
            elif is_bear and cj_close < oj:
                consecutive += 1
            else:
                break

        if consecutive >= 2:
            # Movimento impulsivo trovato — cerca candela contraria PRIMA
            search_start = i - consecutive
            ob_direction = "BULLISH" if is_bull else "BEARISH"

            for k in range(search_start, max(search_start - 6, -1), -1):
                if k < 0:
                    break
                ck = df.iloc[k]
                ok, ck_close = float(ck["open"]), float(ck["close"])

                found = (ob_direction == "BULLISH" and ck_close < ok) or \
                        (ob_direction == "BEARISH" and ck_close > ok)

                if found:
                    high = float(ck["high"])
                    low = float(ck["low"])
                    ts = str(ck.get("timestamp", ""))

                    obs.append({
                        "id": str(uuid.uuid4())[:8],
                        "direction": ob_direction,
                        "timeframe": "M15",
                        "zone_high": high,
                        "zone_low": low,
                        "zone_midpoint": round((high + low) / 2, 4),
                        "formation_timestamp": ts,
                        "status": "FRESH",
                        "quality_score": 2,
                        "has_fvg": False,
                        "has_sweep_before": False,
                        "has_displacement": consecutive >= 3,
                        "is_last_ob_of_move": True,
                        "session_quality": session,
                        "displacement_atr": 0,
                        "mitigation_count": 0,
                        "first_mitigation_ts": None,
                        "age_bars": len(df) - k,
                        "trend_at_formation": "UNKNOWN",
                        "in_discount": False,
                        "in_premium": False,
                    })
                    break

            i += consecutive
        else:
            i += 1

    return obs


def _build_ob(candle, direction: str, snapshot: dict, session: str,
              disp_atr: float, has_displacement: bool,
              event_type: str, cfg: dict) -> dict:
    high = float(candle["high"])
    low = float(candle["low"])
    ts = str(candle.get("timestamp", ""))

    pd_info = snapshot.get("premium_discount", {})
    in_discount = pd_info.get("zone") == "DISCOUNT"
    in_premium = pd_info.get("zone") == "PREMIUM"
    session_quality = session if session in ("LONDON", "NEW_YORK") else "OTHER"
    trend = snapshot.get("trend_health", {}).get("current_trend", "NEUTRAL")

    # Quality Score (0-5)
    quality = 0
    events = snapshot.get("event_history", [])
    has_sweep = any(
        e.get("type") in ("BOS", "CHOCH") and e.get("displacement", False)
        for e in events[-5:]
    )
    if has_sweep:
        quality += 1
    quality += 1  # ultimo OB (singolo)
    quality += 1  # fresco
    if session_quality in ("LONDON", "NEW_YORK"):
        quality += 1
    if has_displacement:
        quality += 1

    return {
        "id": str(uuid.uuid4())[:8],
        "direction": direction,
        "timeframe": "M15",
        "zone_high": high,
        "zone_low": low,
        "zone_midpoint": round((high + low) / 2, 4),
        "formation_timestamp": ts,
        "status": "FRESH",
        "quality_score": min(quality, 5),
        "has_fvg": False,
        "has_sweep_before": has_sweep,
        "has_displacement": has_displacement,
        "is_last_ob_of_move": True,
        "session_quality": session_quality,
        "displacement_atr": round(disp_atr, 3),
        "mitigation_count": 0,
        "first_mitigation_ts": None,
        "age_bars": 0,
        "trend_at_formation": trend,
        "in_discount": in_discount,
        "in_premium": in_premium,
    }


# ============================================================
# State Management
# ============================================================

def _load_active_obs(conn: sqlite3.Connection, asset: str) -> list:
    rows = conn.execute(
        "SELECT * FROM order_blocks WHERE asset = ? AND status IN ('FRESH', 'MITIGATED') "
        "ORDER BY formation_ts DESC",
        (asset,)
    ).fetchall()

    if not rows:
        return []

    cols = [d[0] for d in conn.execute("SELECT * FROM order_blocks WHERE 1=0").description]
    return [dict(zip(cols, row)) for row in rows]


def _save_ob(conn: sqlite3.Connection, asset: str, ob: dict):
    conn.execute("""
        INSERT OR REPLACE INTO order_blocks (
            ob_id, asset, direction, timeframe,
            zone_high, zone_low, formation_ts,
            status, quality_score,
            has_fvg, has_sweep_before, has_displacement, is_last_ob,
            session_quality, displacement_atr,
            mitigation_count, first_mitigation_ts, invalidation_ts,
            age_bars, trend_at_formation,
            in_discount, in_premium
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        ob.get("id") or ob.get("ob_id"),
        asset,
        ob["direction"],
        ob.get("timeframe", "M15"),
        ob["zone_high"],
        ob["zone_low"],
        ob.get("formation_timestamp") or ob.get("formation_ts"),
        ob["status"],
        ob.get("quality_score", 0),
        ob.get("has_fvg", False),
        ob.get("has_sweep_before", False),
        ob.get("has_displacement", False),
        ob.get("is_last_ob_of_move") or ob.get("is_last_ob", False),
        ob.get("session_quality"),
        ob.get("displacement_atr", 0),
        ob.get("mitigation_count", 0),
        ob.get("first_mitigation_ts"),
        ob.get("invalidation_ts"),
        ob.get("age_bars", 0),
        ob.get("trend_at_formation"),
        ob.get("in_discount", False),
        ob.get("in_premium", False),
    ))
    conn.commit()


def _update_ob_states(active_obs: list, current_price: float,
                       cfg: dict) -> list:
    touch_pct = cfg.get("mitigation_touch_pct", 0.002)
    max_age = cfg.get("ob_max_age_bars", 300)
    updated = []

    for ob in active_obs:
        ob["age_bars"] = ob.get("age_bars", 0) + 1

        if ob["age_bars"] > max_age:
            ob["status"] = "EXPIRED"
            updated.append(ob)
            continue

        zone_high = ob["zone_high"]
        zone_low = ob["zone_low"]

        in_zone = zone_low <= current_price <= zone_high
        near_zone = False
        if not in_zone and zone_high > 0:
            dist = min(abs(current_price - zone_high), abs(current_price - zone_low))
            near_zone = dist / zone_high <= touch_pct

        if in_zone or near_zone:
            ob["mitigation_count"] = ob.get("mitigation_count", 0) + 1
            if ob["mitigation_count"] == 1:
                ob["first_mitigation_ts"] = datetime.now(timezone.utc).isoformat()
            if ob["mitigation_count"] >= 3:
                ob["status"] = "INVALIDATED"
                ob["invalidation_ts"] = datetime.now(timezone.utc).isoformat()
            elif ob["status"] == "FRESH":
                ob["status"] = "MITIGATED"

        updated.append(ob)

    return updated


def _is_duplicate(new_ob: dict, existing_obs: list, tolerance: float = 0.003) -> bool:
    mid = new_ob.get("zone_midpoint", (new_ob["zone_high"] + new_ob["zone_low"]) / 2)
    for ex in existing_obs:
        ex_mid = ex.get("zone_midpoint") or ex.get("zone_high", 0)
        if ex_mid > 0 and abs(mid - ex_mid) / ex_mid < tolerance:
            return True
    return False


# ============================================================
# Entry Point
# ============================================================

def produce_ob_snapshot(
    asset: str,
    df_m15: pd.DataFrame,
    structure_snapshot: dict,
    conn: sqlite3.Connection,
    session: str = "ASIA",
    now: datetime = None,
    config: dict = None,
) -> dict:
    if now is None:
        now = datetime.now(timezone.utc)
    if config is None:
        config = {}

    cfg = {**DEFAULT_CONFIG, **config}
    now_iso = now.isoformat()
    current_price = float(df_m15.iloc[-1]["close"]) if len(df_m15) > 0 else 0

    # ── 1. Carica OB attivi dal DB ───────────────────────────
    active_obs = _load_active_obs(conn, asset)

    # ── 2. Scansione storico (solo se DB vuoto per questo asset) ─
    if not active_obs and len(df_m15) >= 50:
        historical = _scan_historical_obs(df_m15, cfg, session)
        for ob in historical:
            if not _is_duplicate(ob, active_obs):
                active_obs.append(ob)
                _save_ob(conn, asset, ob)
        if historical:
            logger.info("OB Engine [%s]: scansione storico → %d OB trovati", asset, len(historical))

    # ── 3. Cerca nuovi OB dagli eventi correnti ──────────────
    if structure_snapshot:
        new_obs = _find_new_obs(df_m15, structure_snapshot, session, cfg)
        for ob in new_obs:
            if not _is_duplicate(ob, active_obs):
                active_obs.append(ob)
                _save_ob(conn, asset, ob)
                logger.info(
                    "OB Engine [%s]: NUOVO %s OB @ %.2f-%.2f q=%d disp=%s",
                    asset, ob["direction"],
                    ob["zone_low"], ob["zone_high"],
                    ob["quality_score"], ob["has_displacement"],
                )

    # ── 4. Aggiorna stato ────────────────────────────────────
    updated_obs = _update_ob_states(active_obs, current_price, cfg)
    for ob in updated_obs:
        _save_ob(conn, asset, ob)

    # ── 5. Filtra per snapshot ───────────────────────────────
    max_tracked = cfg.get("ob_max_tracked", 30)
    all_obs = [ob for ob in updated_obs if ob["status"] not in ("EXPIRED",)]
    all_obs = all_obs[-max_tracked:]

    fresh_bullish = [ob for ob in all_obs if ob["status"] == "FRESH" and ob["direction"] == "BULLISH"]
    fresh_bearish = [ob for ob in all_obs if ob["status"] == "FRESH" and ob["direction"] == "BEARISH"]

    nearest_bull = None
    nearest_bear = None
    if fresh_bullish and current_price > 0:
        below = [ob for ob in fresh_bullish if ob["zone_high"] < current_price]
        if below:
            nearest_bull = min(below, key=lambda ob: current_price - ob["zone_high"])
    if fresh_bearish and current_price > 0:
        above = [ob for ob in fresh_bearish if ob["zone_low"] > current_price]
        if above:
            nearest_bear = min(above, key=lambda ob: ob["zone_low"] - current_price)

    snapshot = {
        "asset": asset,
        "timestamp": now_iso,
        "snapshot_version": SNAPSHOT_VERSION,
        "order_blocks": all_obs,
        "fresh_bullish_count": len(fresh_bullish),
        "fresh_bearish_count": len(fresh_bearish),
        "nearest_fresh_bullish": nearest_bull,
        "nearest_fresh_bearish": nearest_bear,
        "total_tracked": len(all_obs),
    }

    try:
        conn.execute("""
            INSERT INTO order_block_snapshots (
                snapshot_id, asset, timestamp_snapshot, snapshot_version,
                fresh_bullish, fresh_bearish, total_tracked, snapshot_json
            ) VALUES (?,?,?,?,?,?,?,?)
        """, (
            str(uuid.uuid4()), asset, now_iso, SNAPSHOT_VERSION,
            len(fresh_bullish), len(fresh_bearish),
            len(all_obs), json.dumps(snapshot, default=str),
        ))
        conn.commit()
    except Exception as e:
        logger.warning("OB Engine [%s]: errore salvataggio: %s", asset, e)

    nb = nearest_bull
    nbe = nearest_bear
    logger.info(
        "OB Engine [%s]: fresh_bull=%d fresh_bear=%d total=%d "
        "nearest_bull=%s nearest_bear=%s",
        asset,
        len(fresh_bullish), len(fresh_bearish), len(all_obs),
        f"{nb['zone_low']:.2f}-{nb['zone_high']:.2f}(q{nb['quality_score']})" if nb else "none",
        f"{nbe['zone_low']:.2f}-{nbe['zone_high']:.2f}(q{nbe['quality_score']})" if nbe else "none",
    )

    return snapshot
