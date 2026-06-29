"""
core/structure_db.py
Structure Engine V2.0 — Database Layer (Sprint 1)

Due tabelle:
    structure_state      → 1 riga per asset, mutable (stato operativo)
    structure_snapshots   → append-only (storico per analytics)

Nessuna dipendenza da altri moduli del progetto.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional


# ============================================================
# Schema
# ============================================================

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS structure_state (
    asset                    TEXT PRIMARY KEY,
    updated_at               DATETIME NOT NULL,

    -- Struttura corrente per timeframe
    structure_h4             TEXT DEFAULT 'NEUTRAL',
    structure_m15            TEXT DEFAULT 'NEUTRAL',
    structure_m15_prev       TEXT DEFAULT 'NEUTRAL',

    -- Pivot di riferimento H4
    h4_last_hh               REAL,
    h4_last_hl               REAL,
    h4_last_lh               REAL,
    h4_last_ll               REAL,

    -- Pivot di riferimento M15
    m15_last_hh              REAL,
    m15_last_hl              REAL,
    m15_last_lh              REAL,
    m15_last_ll              REAL,

    -- Trend tracking
    current_trend            TEXT DEFAULT 'NEUTRAL',
    trend_start_timestamp    DATETIME,
    impulse_count            INTEGER DEFAULT 0,
    impulses_json            TEXT DEFAULT '[]',
    trend_phase              TEXT DEFAULT 'NEUTRAL',

    -- Ultimo displacement
    last_displacement_ts     DATETIME,
    last_displacement_dir    TEXT,
    last_displacement_atr    REAL DEFAULT 0,

    -- Event history
    event_history_json       TEXT DEFAULT '[]',
    last_bos_timestamp       DATETIME,
    last_choch_timestamp     DATETIME,
    last_bos_scan_idx        INTEGER DEFAULT 0,
    last_choch_scan_idx      INTEGER DEFAULT 0,
    scan_counter             INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS structure_snapshots (
    snapshot_id              TEXT PRIMARY KEY,
    asset                    TEXT NOT NULL,
    timestamp_snapshot       DATETIME NOT NULL,
    snapshot_version         TEXT NOT NULL DEFAULT '2.0.0',

    -- Campi denormalizzati per query dirette
    structure_h4             TEXT,
    structure_m15            TEXT,
    structure_confidence     INTEGER DEFAULT 0,
    trend_phase              TEXT,
    impulse_count            INTEGER DEFAULT 0,
    displacement_confirmed   BOOLEAN DEFAULT 0,
    pullback_buy_valid       BOOLEAN DEFAULT 1,
    pullback_sell_valid       BOOLEAN DEFAULT 1,
    volume_ratio             REAL,
    volume_classification    TEXT,
    premium_discount_zone    TEXT,
    premium_discount_pos     REAL,
    bars_since_bos           INTEGER,
    bars_since_choch         INTEGER,
    event_count              INTEGER DEFAULT 0,

    -- Snapshot completo
    snapshot_json            TEXT NOT NULL,

    -- Config usata (per riproducibilità)
    config_json              TEXT
);

CREATE INDEX IF NOT EXISTS idx_ss_asset_ts
    ON structure_snapshots(asset, timestamp_snapshot);
CREATE INDEX IF NOT EXISTS idx_ss_confidence
    ON structure_snapshots(asset, structure_confidence);
CREATE INDEX IF NOT EXISTS idx_ss_phase
    ON structure_snapshots(asset, trend_phase);
"""


def init_structure_schema(conn: sqlite3.Connection):
    """Crea le tabelle structure_state e structure_snapshots."""
    conn.executescript(SCHEMA_SQL)
    conn.commit()


# ============================================================
# structure_state — CRUD
# ============================================================

def get_state(conn: sqlite3.Connection, asset: str) -> Optional[dict]:
    """Legge lo stato corrente per un asset. Ritorna None se non esiste."""
    row = conn.execute(
        "SELECT * FROM structure_state WHERE asset = ?", (asset,)
    ).fetchone()
    if row is None:
        return None
    cols = [d[0] for d in conn.execute(
        "SELECT * FROM structure_state WHERE asset = ?", (asset,)
    ).description]
    state = dict(zip(cols, row))

    # Deserializza JSON
    try:
        state["impulses"] = json.loads(state.get("impulses_json") or "[]")
    except (json.JSONDecodeError, TypeError):
        state["impulses"] = []
    try:
        state["event_history"] = json.loads(state.get("event_history_json") or "[]")
    except (json.JSONDecodeError, TypeError):
        state["event_history"] = []

    return state


def upsert_state(conn: sqlite3.Connection, state: dict):
    """Salva lo stato corrente (insert or update)."""
    conn.execute("""
        INSERT INTO structure_state (
            asset, updated_at,
            structure_h4, structure_m15, structure_m15_prev,
            h4_last_hh, h4_last_hl, h4_last_lh, h4_last_ll,
            m15_last_hh, m15_last_hl, m15_last_lh, m15_last_ll,
            current_trend, trend_start_timestamp,
            impulse_count, impulses_json, trend_phase,
            last_displacement_ts, last_displacement_dir, last_displacement_atr,
            event_history_json,
            last_bos_timestamp, last_choch_timestamp,
            last_bos_scan_idx, last_choch_scan_idx,
            scan_counter
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(asset) DO UPDATE SET
            updated_at = excluded.updated_at,
            structure_h4 = excluded.structure_h4,
            structure_m15 = excluded.structure_m15,
            structure_m15_prev = excluded.structure_m15_prev,
            h4_last_hh = excluded.h4_last_hh,
            h4_last_hl = excluded.h4_last_hl,
            h4_last_lh = excluded.h4_last_lh,
            h4_last_ll = excluded.h4_last_ll,
            m15_last_hh = excluded.m15_last_hh,
            m15_last_hl = excluded.m15_last_hl,
            m15_last_lh = excluded.m15_last_lh,
            m15_last_ll = excluded.m15_last_ll,
            current_trend = excluded.current_trend,
            trend_start_timestamp = excluded.trend_start_timestamp,
            impulse_count = excluded.impulse_count,
            impulses_json = excluded.impulses_json,
            trend_phase = excluded.trend_phase,
            last_displacement_ts = excluded.last_displacement_ts,
            last_displacement_dir = excluded.last_displacement_dir,
            last_displacement_atr = excluded.last_displacement_atr,
            event_history_json = excluded.event_history_json,
            last_bos_timestamp = excluded.last_bos_timestamp,
            last_choch_timestamp = excluded.last_choch_timestamp,
            last_bos_scan_idx = excluded.last_bos_scan_idx,
            last_choch_scan_idx = excluded.last_choch_scan_idx,
            scan_counter = excluded.scan_counter
    """, (
        state["asset"],
        state.get("updated_at", datetime.now(timezone.utc).isoformat()),
        state.get("structure_h4", "NEUTRAL"),
        state.get("structure_m15", "NEUTRAL"),
        state.get("structure_m15_prev", "NEUTRAL"),
        state.get("h4_last_hh"),
        state.get("h4_last_hl"),
        state.get("h4_last_lh"),
        state.get("h4_last_ll"),
        state.get("m15_last_hh"),
        state.get("m15_last_hl"),
        state.get("m15_last_lh"),
        state.get("m15_last_ll"),
        state.get("current_trend", "NEUTRAL"),
        state.get("trend_start_timestamp"),
        state.get("impulse_count", 0),
        json.dumps(state.get("impulses", [])),
        state.get("trend_phase", "NEUTRAL"),
        state.get("last_displacement_ts"),
        state.get("last_displacement_dir"),
        state.get("last_displacement_atr", 0),
        json.dumps(state.get("event_history", [])),
        state.get("last_bos_timestamp"),
        state.get("last_choch_timestamp"),
        state.get("last_bos_scan_idx", 0),
        state.get("last_choch_scan_idx", 0),
        state.get("scan_counter", 0),
    ))
    conn.commit()


# ============================================================
# structure_snapshots — Insert & Query
# ============================================================

def insert_snapshot(conn: sqlite3.Connection, snapshot: dict) -> str:
    """Inserisce uno snapshot (append-only). Ritorna lo snapshot_id."""
    snapshot_id = str(uuid.uuid4())

    conn.execute("""
        INSERT INTO structure_snapshots (
            snapshot_id, asset, timestamp_snapshot, snapshot_version,
            structure_h4, structure_m15, structure_confidence,
            trend_phase, impulse_count,
            displacement_confirmed,
            pullback_buy_valid, pullback_sell_valid,
            volume_ratio, volume_classification,
            premium_discount_zone, premium_discount_pos,
            bars_since_bos, bars_since_choch,
            event_count,
            snapshot_json, config_json
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        snapshot_id,
        snapshot["asset"],
        snapshot["timestamp"],
        snapshot.get("snapshot_version", "2.0.0"),
        snapshot.get("structure_h4", {}).get("classification", "NEUTRAL"),
        snapshot.get("structure_m15", {}).get("classification", "NEUTRAL"),
        snapshot.get("structure_confidence", 0),
        snapshot.get("trend_health", {}).get("phase", "NEUTRAL"),
        snapshot.get("trend_health", {}).get("impulse_count", 0),
        snapshot.get("displacement", {}).get("confirmed", False),
        snapshot.get("pullback_status", {}).get("buy_valid", True),
        snapshot.get("pullback_status", {}).get("sell_valid", True),
        snapshot.get("volume_ratio_m15", 0),
        snapshot.get("volume_classification", "NORMAL"),
        snapshot.get("premium_discount", {}).get("zone", "EQUILIBRIUM"),
        snapshot.get("premium_discount", {}).get("position", 0.5),
        snapshot.get("bars_since_bos"),
        snapshot.get("bars_since_choch"),
        len(snapshot.get("events", [])),
        json.dumps(snapshot),
        json.dumps(snapshot.get("config", {})),
    ))
    conn.commit()
    return snapshot_id


def get_recent_snapshots(conn: sqlite3.Connection, asset: str,
                          hours: int = 24) -> list[dict]:
    """Ritorna gli snapshot delle ultime N ore."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    rows = conn.execute("""
        SELECT snapshot_json FROM structure_snapshots
        WHERE asset = ? AND timestamp_snapshot >= ?
        ORDER BY timestamp_snapshot DESC
    """, (asset, cutoff)).fetchall()

    results = []
    for row in rows:
        try:
            results.append(json.loads(row[0]))
        except (json.JSONDecodeError, TypeError):
            pass
    return results
