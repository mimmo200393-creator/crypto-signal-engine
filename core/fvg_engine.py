"""
core/fvg_engine.py
Fair Value Gap Engine — Sprint 5

Layer 1: dipende da Structure Engine (Layer 0).
Identifica, classifica e traccia Fair Value Gap su M15.

Un FVG e' il gap tra il wick della candela 1 e il wick della candela 3
in un movimento a 3 candele. Il FVG rappresenta un'area di squilibrio
dove il prezzo ha probabilita' di tornare.

Include IFVG (Inverse FVG — doc 006): un FVG che viene completamente
attraversato dal prezzo diventa una zona IFVG che inverte ruolo
(supporto diventa resistenza e viceversa).

Modalita': LIVE MODE — osserva, produce snapshot, salva nel DB.

Dipendenze: pandas, sqlite3, logging. Consuma StructureSnapshot.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Optional

import pandas as pd

logger = logging.getLogger("fvg_engine")

SNAPSHOT_VERSION = "1.0.0"

DEFAULT_CONFIG = {
    "fvg_lookback": 30,
    "fvg_max_tracked": 30,
    "fvg_max_age_bars": 200,
    "fvg_min_size_atr": 0.1,
    "fill_threshold_pct": 0.90,
}

# ============================================================
# Schema DB
# ============================================================

FVG_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS fvg_snapshots (
    snapshot_id         TEXT PRIMARY KEY,
    asset               TEXT NOT NULL,
    timestamp_snapshot  DATETIME NOT NULL,
    open_bullish        INTEGER DEFAULT 0,
    open_bearish        INTEGER DEFAULT 0,
    total_tracked       INTEGER DEFAULT 0,
    snapshot_json       TEXT
);
CREATE INDEX IF NOT EXISTS idx_fvg_snap_asset_ts
    ON fvg_snapshots(asset, timestamp_snapshot);

CREATE TABLE IF NOT EXISTS fvg_zones (
    fvg_id              TEXT PRIMARY KEY,
    asset               TEXT NOT NULL,
    direction           TEXT NOT NULL,
    zone_high           REAL NOT NULL,
    zone_low            REAL NOT NULL,
    zone_size_pct       REAL DEFAULT 0,
    formation_ts        DATETIME NOT NULL,
    status              TEXT DEFAULT 'OPEN',
    fill_percentage     REAL DEFAULT 0,
    first_touch_ts      DATETIME,
    during_displacement BOOLEAN DEFAULT 0,
    associated_bos      BOOLEAN DEFAULT 0,
    associated_choch    BOOLEAN DEFAULT 0,
    trend_at_formation  TEXT,
    age_bars            INTEGER DEFAULT 0,
    is_invalidated      BOOLEAN DEFAULT 0,
    ifvg_active         BOOLEAN DEFAULT 0,
    ifvg_retested       BOOLEAN DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_fvg_asset_status
    ON fvg_zones(asset, status);
"""


def init_fvg_schema(conn: sqlite3.Connection):
    conn.executescript(FVG_SCHEMA_SQL)
    conn.commit()


# ============================================================
# FVG Detection
# ============================================================

def _scan_for_fvgs(df_m15: pd.DataFrame, structure_snapshot: dict,
                    atr_m15: float, cfg: dict) -> list:
    """
    Cerca FVG nelle candele recenti.

    Bullish FVG: candle[i].high < candle[i+2].low (gap verso l'alto)
    Bearish FVG: candle[i].low > candle[i+2].high (gap verso il basso)
    """
    fvgs = []
    lookback = cfg.get("fvg_lookback", 30)
    min_size_atr = cfg.get("fvg_min_size_atr", 0.1)

    if len(df_m15) < lookback or atr_m15 <= 0:
        return fvgs

    recent = df_m15.iloc[-lookback:]
    disp = structure_snapshot.get("displacement", {})
    disp_confirmed = disp.get("confirmed", False)
    events = structure_snapshot.get("events", [])
    has_bos = any(e.get("type") == "BOS" for e in events)
    has_choch = any(e.get("type") == "CHOCH" for e in events)
    trend = structure_snapshot.get("trend_health", {}).get("current_trend", "NEUTRAL")
    current_price = float(df_m15.iloc[-1]["close"])

    # Solo le ultime 5 candele per evitare duplicati con scan precedenti
    search_start = max(0, len(recent) - 5)

    for i in range(search_start, len(recent) - 2):
        c1 = recent.iloc[i]
        c3 = recent.iloc[i + 2]

        c1_high = float(c1["high"])
        c1_low = float(c1["low"])
        c3_high = float(c3["high"])
        c3_low = float(c3["low"])

        # Bullish FVG: gap up
        if c1_high < c3_low:
            size = c3_low - c1_high
            if size / atr_m15 >= min_size_atr:
                fvgs.append({
                    "id": str(uuid.uuid4())[:8],
                    "direction": "BULLISH",
                    "timeframe": "M15",
                    "zone_high": c3_low,
                    "zone_low": c1_high,
                    "zone_size_pct": round(size / current_price * 100, 4) if current_price > 0 else 0,
                    "formation_timestamp": str(c3.get("timestamp", "")),
                    "status": "OPEN",
                    "fill_percentage": 0.0,
                    "first_touch_ts": None,
                    "during_displacement": disp_confirmed,
                    "associated_bos": has_bos,
                    "associated_choch": has_choch,
                    "trend_at_formation": trend,
                    "age_bars": 0,
                    "is_invalidated": False,
                    "ifvg_active": False,
                    "ifvg_retested": False,
                })

        # Bearish FVG: gap down
        if c1_low > c3_high:
            size = c1_low - c3_high
            if size / atr_m15 >= min_size_atr:
                fvgs.append({
                    "id": str(uuid.uuid4())[:8],
                    "direction": "BEARISH",
                    "timeframe": "M15",
                    "zone_high": c1_low,
                    "zone_low": c3_high,
                    "zone_size_pct": round(size / current_price * 100, 4) if current_price > 0 else 0,
                    "formation_timestamp": str(c3.get("timestamp", "")),
                    "status": "OPEN",
                    "fill_percentage": 0.0,
                    "first_touch_ts": None,
                    "during_displacement": disp_confirmed,
                    "associated_bos": has_bos,
                    "associated_choch": has_choch,
                    "trend_at_formation": trend,
                    "age_bars": 0,
                    "is_invalidated": False,
                    "ifvg_active": False,
                    "ifvg_retested": False,
                })

    return fvgs


# ============================================================
# FVG State Management
# ============================================================

def _load_active_fvgs(conn: sqlite3.Connection, asset: str) -> list:
    rows = conn.execute(
        "SELECT * FROM fvg_zones WHERE asset = ? AND status IN ('OPEN', 'PARTIALLY_FILLED') "
        "ORDER BY formation_ts DESC",
        (asset,)
    ).fetchall()

    if not rows:
        return []

    cols = [d[0] for d in conn.execute("SELECT * FROM fvg_zones WHERE 1=0").description]
    return [dict(zip(cols, row)) for row in rows]


def _save_fvg(conn: sqlite3.Connection, asset: str, fvg: dict):
    conn.execute("""
        INSERT OR REPLACE INTO fvg_zones (
            fvg_id, asset, direction, zone_high, zone_low, zone_size_pct,
            formation_ts, status, fill_percentage, first_touch_ts,
            during_displacement, associated_bos, associated_choch,
            trend_at_formation, age_bars,
            is_invalidated, ifvg_active, ifvg_retested
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        fvg.get("id") or fvg.get("fvg_id"),
        asset,
        fvg["direction"],
        fvg["zone_high"],
        fvg["zone_low"],
        fvg.get("zone_size_pct", 0),
        fvg.get("formation_timestamp") or fvg.get("formation_ts"),
        fvg["status"],
        fvg.get("fill_percentage", 0),
        fvg.get("first_touch_ts"),
        fvg.get("during_displacement", False),
        fvg.get("associated_bos", False),
        fvg.get("associated_choch", False),
        fvg.get("trend_at_formation"),
        fvg.get("age_bars", 0),
        fvg.get("is_invalidated", False),
        fvg.get("ifvg_active", False),
        fvg.get("ifvg_retested", False),
    ))
    conn.commit()


def _update_fvg_states(active_fvgs: list, current_price: float,
                        current_high: float, current_low: float,
                        cfg: dict) -> list:
    """
    Aggiorna lo stato dei FVG attivi:
    - Calcola fill_percentage
    - OPEN → PARTIALLY_FILLED → FILLED
    - FVG completamente attraversato → is_invalidated → IFVG
    """
    fill_threshold = cfg.get("fill_threshold_pct", 0.90)
    max_age = cfg.get("fvg_max_age_bars", 200)
    now_iso = datetime.now(timezone.utc).isoformat()
    updated = []

    for fvg in active_fvgs:
        fvg["age_bars"] = fvg.get("age_bars", 0) + 1

        if fvg["age_bars"] > max_age:
            fvg["status"] = "EXPIRED"
            updated.append(fvg)
            continue

        zone_high = fvg["zone_high"]
        zone_low = fvg["zone_low"]
        zone_size = zone_high - zone_low

        if zone_size <= 0:
            updated.append(fvg)
            continue

        # Calcola fill percentage
        if fvg["direction"] == "BULLISH":
            # Bullish FVG: si riempie quando il prezzo scende nella zona
            if current_low <= zone_high:
                filled = zone_high - max(current_low, zone_low)
                fill_pct = min(filled / zone_size, 1.0)
                fvg["fill_percentage"] = round(max(fvg.get("fill_percentage", 0), fill_pct) * 100, 1)
                if fvg.get("first_touch_ts") is None:
                    fvg["first_touch_ts"] = now_iso
        else:
            # Bearish FVG: si riempie quando il prezzo sale nella zona
            if current_high >= zone_low:
                filled = min(current_high, zone_high) - zone_low
                fill_pct = min(filled / zone_size, 1.0)
                fvg["fill_percentage"] = round(max(fvg.get("fill_percentage", 0), fill_pct) * 100, 1)
                if fvg.get("first_touch_ts") is None:
                    fvg["first_touch_ts"] = now_iso

        # Aggiorna stato
        fill = fvg.get("fill_percentage", 0)
        if fill >= fill_threshold * 100:
            fvg["status"] = "FILLED"
            fvg["is_invalidated"] = True
            # IFVG: un FVG riempito completamente diventa zona inversa
            fvg["ifvg_active"] = True
        elif fill > 0:
            fvg["status"] = "PARTIALLY_FILLED"

        # IFVG retest check
        if fvg.get("ifvg_active", False):
            if fvg["direction"] == "BULLISH" and current_price > zone_high:
                fvg["ifvg_retested"] = True
            elif fvg["direction"] == "BEARISH" and current_price < zone_low:
                fvg["ifvg_retested"] = True

        updated.append(fvg)

    return updated


# ============================================================
# Entry Point
# ============================================================

def produce_fvg_snapshot(
    asset: str,
    df_m15: pd.DataFrame,
    structure_snapshot: dict,
    conn: sqlite3.Connection,
    atr_m15: float = 0.0,
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
    current_high = float(df_m15.iloc[-1]["high"]) if len(df_m15) > 0 else 0
    current_low = float(df_m15.iloc[-1]["low"]) if len(df_m15) > 0 else 0

    # ── 1. Carica FVG attivi ─────────────────────────────────
    active_fvgs = _load_active_fvgs(conn, asset)

    # ── 2. Cerca nuovi FVG ───────────────────────────────────
    new_fvgs = _scan_for_fvgs(df_m15, structure_snapshot, atr_m15, cfg)

    for new_fvg in new_fvgs:
        is_dup = any(
            abs(new_fvg["zone_high"] - ex.get("zone_high", 0)) < atr_m15 * 0.1 and
            abs(new_fvg["zone_low"] - ex.get("zone_low", 0)) < atr_m15 * 0.1
            for ex in active_fvgs
        )
        if not is_dup:
            active_fvgs.append(new_fvg)
            _save_fvg(conn, asset, new_fvg)
            logger.info(
                "FVG Engine [%s]: NUOVO %s FVG @ %.2f-%.2f size=%.4f%% disp=%s",
                asset, new_fvg["direction"],
                new_fvg["zone_low"], new_fvg["zone_high"],
                new_fvg["zone_size_pct"],
                new_fvg["during_displacement"],
            )

    # ── 3. Aggiorna stato ────────────────────────────────────
    updated = _update_fvg_states(active_fvgs, current_price,
                                  current_high, current_low, cfg)

    for fvg in updated:
        _save_fvg(conn, asset, fvg)

    # ── 4. Filtra ────────────────────────────────────────────
    max_tracked = cfg.get("fvg_max_tracked", 30)
    all_fvgs = [f for f in updated if f["status"] != "EXPIRED"]
    all_fvgs = all_fvgs[-max_tracked:]

    open_bull = [f for f in all_fvgs if f["status"] in ("OPEN", "PARTIALLY_FILLED") and f["direction"] == "BULLISH"]
    open_bear = [f for f in all_fvgs if f["status"] in ("OPEN", "PARTIALLY_FILLED") and f["direction"] == "BEARISH"]

    nearest_bull = None
    nearest_bear = None
    if open_bull and current_price > 0:
        below = [f for f in open_bull if f["zone_high"] < current_price]
        if below:
            nearest_bull = min(below, key=lambda f: current_price - f["zone_high"])
    if open_bear and current_price > 0:
        above = [f for f in open_bear if f["zone_low"] > current_price]
        if above:
            nearest_bear = min(above, key=lambda f: f["zone_low"] - current_price)

    ifvg_active_list = [f for f in all_fvgs if f.get("ifvg_active", False)]

    # ── 5. Snapshot ──────────────────────────────────────────
    snapshot = {
        "asset": asset,
        "timestamp": now_iso,
        "snapshot_version": SNAPSHOT_VERSION,
        "fvgs": all_fvgs,
        "open_bullish_count": len(open_bull),
        "open_bearish_count": len(open_bear),
        "nearest_open_bullish": nearest_bull,
        "nearest_open_bearish": nearest_bear,
        "ifvg_active_count": len(ifvg_active_list),
        "total_tracked": len(all_fvgs),
    }

    try:
        conn.execute("""
            INSERT INTO fvg_snapshots (snapshot_id, asset, timestamp_snapshot,
                open_bullish, open_bearish, total_tracked, snapshot_json)
            VALUES (?,?,?,?,?,?,?)
        """, (str(uuid.uuid4()), asset, now_iso,
              len(open_bull), len(open_bear), len(all_fvgs),
              json.dumps(snapshot, default=str)))
        conn.commit()
    except Exception as e:
        logger.warning("FVG Engine [%s]: errore salvataggio: %s", asset, e)

    logger.info(
        "FVG Engine [%s]: open_bull=%d open_bear=%d ifvg=%d total=%d "
        "nearest_bull=%s nearest_bear=%s",
        asset, len(open_bull), len(open_bear), len(ifvg_active_list), len(all_fvgs),
        f"{nearest_bull['zone_low']:.2f}-{nearest_bull['zone_high']:.2f}" if nearest_bull else "none",
        f"{nearest_bear['zone_low']:.2f}-{nearest_bear['zone_high']:.2f}" if nearest_bear else "none",
    )

    return snapshot
