"""
core/trend_rider_db.py
NMC Trend Rider Balanced — Layer accesso dati

Tabella: trb_signals
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional

import pandas as pd


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS trb_signals (
    signal_id            TEXT PRIMARY KEY,
    strategy_name        TEXT NOT NULL DEFAULT 'TRB',
    strategy_version     TEXT NOT NULL DEFAULT 'v1.0',
    asset                TEXT NOT NULL,
    direction            TEXT NOT NULL CHECK(direction IN ('BUY','SELL')),
    timestamp_setup      DATETIME NOT NULL,
    timestamp_closed     DATETIME,

    -- Prezzi
    entry                REAL NOT NULL,
    stop_loss            REAL NOT NULL,
    tp1                  REAL,
    tp2                  REAL,
    risk                 REAL,
    rr1                  REAL DEFAULT 1.0,
    rr2                  REAL,

    -- Contesto
    trend_h1             TEXT,
    trend_h4             TEXT,
    adx                  REAL,
    atr_m15              REAL,
    atr_h1               REAL,
    pullback_valid       BOOLEAN DEFAULT 0,
    new_24h_extreme      BOOLEAN DEFAULT 0,
    session              TEXT,

    -- Liquidità
    liquidity_target       TEXT,
    liquidity_target_price REAL,
    liquidity_priority     TEXT,

    -- Quality
    quality_score        INTEGER,
    quality_label        TEXT CHECK(quality_label IN ('LOW','MEDIUM','HIGH','PREMIUM')),

    -- Tracking
    final_outcome        TEXT DEFAULT 'OPEN'
        CHECK(final_outcome IN ('OPEN','TP1_HIT','TP2_HIT','SL_HIT','EXPIRED')),
    tp1_hit              BOOLEAN DEFAULT 0,
    tp2_hit              BOOLEAN DEFAULT 0,
    mae                  REAL DEFAULT 0,
    mfe                  REAL DEFAULT 0,
    bars_open            INTEGER DEFAULT 0,
    expiry_bars          INTEGER DEFAULT 96,
    timestamp_tp1        DATETIME,
    timestamp_tp2        DATETIME,
    timestamp_sl         DATETIME
);

CREATE INDEX IF NOT EXISTS idx_trb_asset_outcome
    ON trb_signals(asset, final_outcome);
CREATE INDEX IF NOT EXISTS idx_trb_timestamp
    ON trb_signals(timestamp_setup);
CREATE INDEX IF NOT EXISTS idx_trb_quality
    ON trb_signals(quality_label);
"""


def init_trb_schema(conn: sqlite3.Connection):
    conn.executescript(SCHEMA_SQL)
    conn.commit()


def insert_trb_signal(conn: sqlite3.Connection, signal: dict) -> str:
    signal_id = signal.get("signal_id") or str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO trb_signals (
            signal_id, strategy_name, strategy_version,
            asset, direction, timestamp_setup,
            entry, stop_loss, tp1, tp2, risk, rr1, rr2,
            trend_h1, trend_h4, adx, atr_m15, atr_h1,
            pullback_valid, new_24h_extreme, session,
            liquidity_target, liquidity_target_price, liquidity_priority,
            quality_score, quality_label,
            final_outcome, expiry_bars
        ) VALUES (
            ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
        )
        """,
        (
            signal_id,
            signal.get("strategy_name", "TRB"),
            signal.get("strategy_version", "v1.0"),
            signal["asset"],
            signal["direction"],
            signal["timestamp_setup"],
            signal["entry"],
            signal["stop_loss"],
            signal.get("tp1"),
            signal.get("tp2"),
            signal.get("risk"),
            signal.get("rr1", 1.0),
            signal.get("rr2"),
            signal.get("trend_h1"),
            signal.get("trend_h4"),
            signal.get("adx"),
            signal.get("atr_m15"),
            signal.get("atr_h1"),
            bool(signal.get("pullback_valid", False)),
            bool(signal.get("new_24h_extreme", False)),
            signal.get("session"),
            signal.get("liquidity_target"),
            signal.get("liquidity_target_price"),
            signal.get("liquidity_priority"),
            signal.get("quality_score"),
            signal.get("quality_label"),
            "OPEN",
            signal.get("expiry_bars", 96),
        ),
    )
    conn.commit()
    return signal_id


def has_recent_trb_signal(
    conn: sqlite3.Connection,
    asset: str,
    direction: str,
    hours: int = 2,
) -> bool:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    row = conn.execute(
        """
        SELECT 1 FROM trb_signals
        WHERE asset=? AND direction=? AND timestamp_setup >= ?
        LIMIT 1
        """,
        (asset, direction, cutoff),
    ).fetchone()
    return row is not None


def has_open_trb_signal(
    conn: sqlite3.Connection,
    asset: str,
    direction: str,
) -> bool:
    row = conn.execute(
        """
        SELECT 1 FROM trb_signals
        WHERE asset=? AND direction=? AND final_outcome='OPEN'
        LIMIT 1
        """,
        (asset, direction),
    ).fetchone()
    return row is not None


def monitor_open_trb_signals(
    conn: sqlite3.Connection,
    asset: str,
    current_high: float,
    current_low: float,
    now_iso: str,
) -> list[dict]:
    rows = conn.execute(
        """
        SELECT signal_id, direction, entry, stop_loss, tp1, tp2,
               mae, mfe, bars_open, expiry_bars, tp1_hit, tp2_hit
        FROM trb_signals
        WHERE final_outcome = 'OPEN' AND asset = ?
        """,
        (asset,),
    ).fetchall()

    updated = []

    for row in rows:
        sid, direction, entry, sl, tp1, tp2, mae, mfe, bars_open, expiry_bars, tp1_hit, tp2_hit = row

        if entry is None or sl is None:
            continue

        bars_open = (bars_open or 0) + 1

        # MAE / MFE
        if direction == "BUY":
            adverse   = max(float(entry) - current_low,  0.0)
            favorable = max(current_high - float(entry), 0.0)
        else:
            adverse   = max(current_high - float(entry), 0.0)
            favorable = max(float(entry) - current_low,  0.0)

        new_mae = max(float(mae or 0), adverse)
        new_mfe = max(float(mfe or 0), favorable)

        # TP1 / TP2 / SL hit
        if direction == "BUY":
            sl_hit  = current_low  <= float(sl)
            tp1_hit_now = tp1 is not None and current_high >= float(tp1)
            tp2_hit_now = tp2 is not None and current_high >= float(tp2)
        else:
            sl_hit  = current_high >= float(sl)
            tp1_hit_now = tp1 is not None and current_low  <= float(tp1)
            tp2_hit_now = tp2 is not None and current_low  <= float(tp2)

        new_tp1_hit = bool(tp1_hit) or tp1_hit_now
        new_tp2_hit = bool(tp2_hit) or tp2_hit_now

        # SL priorità su TP
        if sl_hit:
            outcome = "SL_HIT"
        elif new_tp2_hit:
            outcome = "TP2_HIT"
        elif new_tp1_hit:
            outcome = "TP1_HIT"
        elif bars_open >= (expiry_bars or 96):
            outcome = "EXPIRED"
        else:
            outcome = None

        updates = [
            "mae = ?", "mfe = ?", "bars_open = ?",
            "tp1_hit = ?", "tp2_hit = ?",
        ]
        params = [new_mae, new_mfe, bars_open, new_tp1_hit, new_tp2_hit]

        if outcome and outcome != "TP1_HIT":
            # Chiude il trade
            updates += ["final_outcome = ?", "timestamp_closed = ?"]
            params  += [outcome, now_iso]
        elif outcome == "TP1_HIT" and not bool(tp1_hit):
            # Registra TP1 ma non chiude
            updates += ["timestamp_tp1 = ?"]
            params  += [now_iso]

        params.append(sid)
        conn.execute(
            f"UPDATE trb_signals SET {', '.join(updates)} WHERE signal_id = ?",
            params,
        )

    conn.commit()

    # Ritorna segnali aggiornati con outcome finale
    for row in rows:
        sid = row[0]
        updated_row = conn.execute(
            "SELECT signal_id, final_outcome, mae, mfe, bars_open FROM trb_signals WHERE signal_id=?",
            (sid,)
        ).fetchone()
        if updated_row and updated_row[1] != "OPEN":
            updated.append({
                "signal_id": updated_row[0],
                "outcome":   updated_row[1],
                "mae":       updated_row[2],
                "mfe":       updated_row[3],
                "bars_open": updated_row[4],
            })

    return updated
