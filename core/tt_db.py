"""
core/tt_db.py
TT (ex OTE-SC) — Layer accesso dati

Schema pensato per il framework:
    DIRECTION(4H) -> LOCATION(1H) -> LIQUIDITY(1H) -> PREMIUM/DISCOUNT
    -> 15M CONTEXT -> PROXIMITY -> EARLY SIGNAL -> TOUCH -> 5M SWEEP
    -> 5M REACTION -> 5M STRUCTURE -> ENTRY -> DYNAMIC TARGET -> RESULT

Differenza fondamentale rispetto a edge_lab_signals (il vecchio schema
OTE-SC): qui esiste uno stato WAITING_CONFIRMATION persistente, e i
campi Planned (al momento del SIGNAL) sono SEPARATI dai campi Actual
(al momento dell'ENTRY) -- mai sovrascritti, cosi' il backtest puo'
sempre rispondere "cosa avrebbe detto l'algoritmo in quel momento".
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Optional

import pandas as pd

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS tt_signals (
    signal_id                  TEXT PRIMARY KEY,
    asset                      TEXT NOT NULL,
    direction                  TEXT NOT NULL CHECK(direction IN ('BUY','SELL')),

    -- ── Stato: il cuore della macchina a stati ──────────────
    status TEXT NOT NULL DEFAULT 'WAITING_CONFIRMATION' CHECK(status IN (
        'WAITING_CONFIRMATION',
        'ENTRY',
        'INVALIDATED',
        'TP', 'SL', 'EXPIRED'
    )),
    invalidation_reason         TEXT,

    -- ── 4H Direction ─────────────────────────────────────────
    direction_4h                TEXT,
    swing_range_low             REAL,
    swing_range_high            REAL,
    last_bos_price              REAL,
    last_bos_ts                 INTEGER,

    -- ── 1H Location (POI) ────────────────────────────────────
    poi_type                    TEXT,
    poi_high                    REAL,
    poi_low                     REAL,
    poi_quality                 INTEGER,
    poi_ref                     TEXT,

    -- ── 1H Liquidity ──────────────────────────────────────────
    liquidity_type               TEXT,
    liquidity_level              REAL,
    liquidity_direction          TEXT,
    liquidity_distance_pct       REAL,
    sweep_target_level           REAL,

    -- ── Premium / Discount ────────────────────────────────────
    pd_zone                      TEXT,
    pd_pct                       REAL,

    -- ── 15M Intermediate Context ──────────────────────────────
    ctx_15m_structure            TEXT,
    ctx_15m_momentum             TEXT,
    ctx_15m_note                 TEXT,

    -- ── Proximity / Early Signal ──────────────────────────────
    proximity_points             REAL,
    signal_created_at            DATETIME NOT NULL,

    -- ── PLANNED (fissati al momento del SIGNAL, MAI sovrascritti) ──
    planned_entry                REAL NOT NULL,
    planned_sl                   REAL NOT NULL,
    planned_tp                   REAL NOT NULL,
    planned_rr                   REAL NOT NULL,
    planned_tp_type              TEXT,
    planned_tp_ref               TEXT,

    -- ── ACTUAL (popolati al momento dell'ENTRY, se diversi) ────
    actual_entry                 REAL,
    actual_sl                    REAL,
    actual_tp                    REAL,
    actual_rr                    REAL,

    -- ── 5M Execution ────────────────────────────────────────────
    touch_ts                     DATETIME,
    sweep_5m_confirmed           BOOLEAN DEFAULT 0,
    sweep_5m_level                REAL,
    reaction_5m_type              TEXT,
    structure_5m_confirmed        BOOLEAN DEFAULT 0,
    structure_5m_broken_level     REAL,
    entry_ts                      DATETIME,
    setup_type                    TEXT DEFAULT 'CONSERVATIVE' CHECK(setup_type IN ('AGGRESSIVE','CONSERVATIVE')),

    -- ── Quality / Score ─────────────────────────────────────────
    quality_score                 INTEGER,
    quality_label                  TEXT CHECK(quality_label IN ('HIGH','MEDIUM','LOW')),

    -- ── Esito ────────────────────────────────────────────────────
    result                        TEXT,
    result_r                      REAL,
    closed_at                     DATETIME,
    mae                           REAL,
    mfe                           REAL,
    bars_waiting                  INTEGER DEFAULT 0,
    bars_open                     INTEGER DEFAULT 0,
    expiry_bars_waiting           INTEGER DEFAULT 24,
    expiry_bars_open              INTEGER DEFAULT 96,

    -- ── Snapshot completo per debug/backtest ─────────────────────
    context_snapshot               TEXT
);

CREATE INDEX IF NOT EXISTS idx_tt_asset_status
    ON tt_signals(asset, status);
CREATE INDEX IF NOT EXISTS idx_tt_poi_ref
    ON tt_signals(asset, direction, poi_ref);
CREATE INDEX IF NOT EXISTS idx_tt_created
    ON tt_signals(signal_created_at);
"""


def init_tt_schema(conn: sqlite3.Connection):
    conn.executescript(SCHEMA_SQL)
    conn.commit()


# ============================================================
# Insert — crea un nuovo Early Signal (status WAITING_CONFIRMATION)
# ============================================================

def insert_tt_signal(conn: sqlite3.Connection, signal: dict) -> str:
    signal_id = signal.get("signal_id") or str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO tt_signals (
            signal_id, asset, direction, status,
            direction_4h, swing_range_low, swing_range_high, last_bos_price, last_bos_ts,
            poi_type, poi_high, poi_low, poi_quality, poi_ref,
            liquidity_type, liquidity_level, liquidity_direction, liquidity_distance_pct, sweep_target_level,
            pd_zone, pd_pct,
            ctx_15m_structure, ctx_15m_momentum, ctx_15m_note,
            proximity_points, signal_created_at,
            planned_entry, planned_sl, planned_tp, planned_rr, planned_tp_type, planned_tp_ref,
            setup_type, quality_score, quality_label,
            expiry_bars_waiting, expiry_bars_open,
            context_snapshot
        ) VALUES (
            ?,?,?,?, ?,?,?,?,?, ?,?,?,?,?, ?,?,?,?,?, ?,?, ?,?,?, ?,?, ?,?,?,?,?,?, ?,?,?, ?,?, ?
        )
        """,
        (
            signal_id, signal["asset"], signal["direction"], "WAITING_CONFIRMATION",
            signal.get("direction_4h"), signal.get("swing_range_low"), signal.get("swing_range_high"),
            signal.get("last_bos_price"), signal.get("last_bos_ts"),
            signal.get("poi_type"), signal.get("poi_high"), signal.get("poi_low"),
            signal.get("poi_quality"), signal.get("poi_ref"),
            signal.get("liquidity_type"), signal.get("liquidity_level"),
            signal.get("liquidity_direction"), signal.get("liquidity_distance_pct"),
            signal.get("sweep_target_level"),
            signal.get("pd_zone"), signal.get("pd_pct"),
            signal.get("ctx_15m_structure"), signal.get("ctx_15m_momentum"), signal.get("ctx_15m_note"),
            signal.get("proximity_points"), signal["signal_created_at"],
            signal["planned_entry"], signal["planned_sl"], signal["planned_tp"], signal["planned_rr"],
            signal.get("planned_tp_type"), signal.get("planned_tp_ref"),
            signal.get("setup_type", "CONSERVATIVE"),
            signal.get("quality_score"), signal.get("quality_label"),
            signal.get("expiry_bars_waiting", 24), signal.get("expiry_bars_open", 96),
            json.dumps(signal.get("context_snapshot", {}), default=str),
        ),
    )
    conn.commit()
    return signal_id


# ============================================================
# Query: setup WAITING_CONFIRMATION attivi per asset/direzione/POI
# ============================================================

def get_waiting_signals(conn: sqlite3.Connection, asset: str = None) -> list[dict]:
    q = "SELECT * FROM tt_signals WHERE status = 'WAITING_CONFIRMATION'"
    params = ()
    if asset:
        q += " AND asset = ?"
        params = (asset,)
    rows = conn.execute(q, params).fetchall()
    if not rows:
        return []
    cols = [d[0] for d in conn.execute(
        "SELECT * FROM tt_signals WHERE 1=0").description]
    return [dict(zip(cols, r)) for r in rows]


def has_active_setup_for_poi(conn: sqlite3.Connection, asset: str,
                             direction: str, poi_ref: str) -> bool:
    """
    Una POI = un solo active setup (sez.27, no duplicati). Attivo =
    WAITING_CONFIRMATION o ENTRY (non ancora chiuso).
    """
    row = conn.execute(
        """
        SELECT 1 FROM tt_signals
        WHERE asset=? AND direction=? AND poi_ref=?
          AND status IN ('WAITING_CONFIRMATION', 'ENTRY')
        LIMIT 1
        """,
        (asset, direction, poi_ref),
    ).fetchone()
    return row is not None


# ============================================================
# Update: transizioni di stato
# ============================================================

def invalidate_signal(conn: sqlite3.Connection, signal_id: str, reason: str):
    conn.execute(
        """
        UPDATE tt_signals SET status='INVALIDATED', invalidation_reason=?,
               closed_at=?
        WHERE signal_id=?
        """,
        (reason, datetime.now(timezone.utc).isoformat(), signal_id),
    )
    conn.commit()


def confirm_entry(conn: sqlite3.Connection, signal_id: str,
                  actual_entry: float, actual_sl: float, actual_tp: float,
                  touch_ts: str = None, sweep_level: float = None,
                  reaction_type: str = None, structure_level: float = None):
    actual_rr = None
    row = conn.execute(
        "SELECT direction FROM tt_signals WHERE signal_id=?", (signal_id,)
    ).fetchone()
    if row:
        direction = row[0]
        risk = abs(actual_entry - actual_sl)
        reward = abs(actual_tp - actual_entry)
        actual_rr = round(reward / risk, 4) if risk > 0 else None

    conn.execute(
        """
        UPDATE tt_signals SET
            status='ENTRY', entry_ts=?,
            actual_entry=?, actual_sl=?, actual_tp=?, actual_rr=?,
            touch_ts=COALESCE(?, touch_ts),
            sweep_5m_confirmed=1, sweep_5m_level=?,
            reaction_5m_type=?, structure_5m_confirmed=1, structure_5m_broken_level=?
        WHERE signal_id=?
        """,
        (datetime.now(timezone.utc).isoformat(), actual_entry, actual_sl, actual_tp,
         actual_rr, touch_ts, sweep_level, reaction_type, structure_level, signal_id),
    )
    conn.commit()


def close_signal(conn: sqlite3.Connection, signal_id: str, result: str,
                 result_r: float, mae: float = None, mfe: float = None,
                 bars_open: int = None):
    conn.execute(
        """
        UPDATE tt_signals SET status=?, result=?, result_r=?,
               closed_at=?, mae=COALESCE(?,mae), mfe=COALESCE(?,mfe),
               bars_open=COALESCE(?,bars_open)
        WHERE signal_id=?
        """,
        (result, "WIN" if result == "TP" else ("LOSS" if result == "SL" else result),
         result_r, datetime.now(timezone.utc).isoformat(), mae, mfe, bars_open, signal_id),
    )
    conn.commit()


def increment_bars_waiting(conn: sqlite3.Connection, signal_id: str) -> int:
    row = conn.execute(
        "SELECT bars_waiting FROM tt_signals WHERE signal_id=?", (signal_id,)
    ).fetchone()
    new_val = (row[0] or 0) + 1 if row else 1
    conn.execute(
        "UPDATE tt_signals SET bars_waiting=? WHERE signal_id=?",
        (new_val, signal_id),
    )
    conn.commit()
    return new_val
