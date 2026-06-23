"""
core/edge_lab_db.py
Edge Lab — Layer accesso dati (Step 1 + Step 10)

Opera su due tabelle isolate:
    market_context_snapshots  → una riga per asset per scan (Layer 1)
    edge_lab_signals          → tutti i segnali con strategy_name (Layer 3)
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional

import pandas as pd


# ============================================================
# Init schema
# ============================================================

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS market_context_snapshots (
    snapshot_id          TEXT PRIMARY KEY,
    asset                TEXT NOT NULL,
    timestamp_snapshot   DATETIME NOT NULL,
    current_price        REAL,
    trend_h4             TEXT,
    trend_h1             TEXT,
    trend_combined       TEXT,
    ema_slope_h4         TEXT,
    ema_slope_h1         TEXT,
    current_session      TEXT,
    reference_session    TEXT,
    ref_high             REAL,
    ref_low              REAL,
    ref_range            REAL,
    ote_low              REAL,
    ote_high             REAL,
    fib_618              REAL,
    fib_786              REAL,
    nearest_above_label  TEXT,
    nearest_above_price  REAL,
    nearest_below_label  TEXT,
    nearest_below_price  REAL,
    sr_score_buy         REAL,
    sr_score_sell        REAL,
    sr_reaction_buy      BOOLEAN DEFAULT 0,
    sr_reaction_sell     BOOLEAN DEFAULT 0,
    vol_regime_m15       TEXT,
    vol_regime_h1        TEXT,
    atr_m15              REAL,
    atr_h1               REAL,
    is_high_vol_window   BOOLEAN DEFAULT 0,
    macro_risk           TEXT,
    macro_is_blackout    BOOLEAN DEFAULT 0,
    macro_event_type     TEXT,
    macro_minutes_to_event INTEGER,
    is_tradeable         BOOLEAN DEFAULT 1,
    block_reasons        TEXT
);

CREATE INDEX IF NOT EXISTS idx_el_ctx_asset_ts
    ON market_context_snapshots(asset, timestamp_snapshot);

CREATE TABLE IF NOT EXISTS edge_lab_signals (
    signal_id            TEXT PRIMARY KEY,
    strategy_name        TEXT NOT NULL,
    strategy_version     TEXT NOT NULL,
    asset                TEXT NOT NULL,
    direction            TEXT NOT NULL CHECK(direction IN ('BUY','SELL')),
    timestamp_setup      DATETIME NOT NULL,
    timestamp_closed     DATETIME,
    entry                REAL NOT NULL,
    stop_loss            REAL NOT NULL,
    tp                   REAL,
    rr                   REAL,
    ote_low              REAL,
    ote_high             REAL,
    liquidity_target          TEXT,
    liquidity_target_price    REAL,
    liquidity_target_priority TEXT,
    liquidity_target_score    REAL,
    session              TEXT,
    ref_session          TEXT,
    trend_h4             TEXT,
    trend_h1             TEXT,
    trend_combined       TEXT,
    vol_regime_m15       TEXT,
    sr_reaction          BOOLEAN DEFAULT 0,
    sr_score             REAL,
    quality_score        INTEGER,
    quality_label        TEXT CHECK(quality_label IN ('HIGH','MEDIUM','LOW')),
    tradeability_flags   TEXT,
    final_outcome        TEXT DEFAULT 'OPEN'
        CHECK(final_outcome IN ('OPEN','TP','SL','EXPIRED')),
    mae                  REAL,
    mfe                  REAL,
    bars_open            INTEGER DEFAULT 0,
    expiry_bars          INTEGER DEFAULT 96
);

CREATE INDEX IF NOT EXISTS idx_el_signals_asset_outcome
    ON edge_lab_signals(asset, final_outcome);
CREATE INDEX IF NOT EXISTS idx_el_signals_strategy
    ON edge_lab_signals(strategy_name);
CREATE INDEX IF NOT EXISTS idx_el_signals_timestamp
    ON edge_lab_signals(timestamp_setup);
"""


def init_edge_lab_schema(conn: sqlite3.Connection):
    conn.executescript(SCHEMA_SQL)
    conn.commit()


# ============================================================
# market_context_snapshots
# ============================================================

def insert_market_context(conn: sqlite3.Connection, snapshot: dict) -> str:
    snapshot_id = str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO market_context_snapshots (
            snapshot_id, asset, timestamp_snapshot, current_price,
            trend_h4, trend_h1, trend_combined, ema_slope_h4, ema_slope_h1,
            current_session, reference_session, ref_high, ref_low, ref_range,
            ote_low, ote_high, fib_618, fib_786,
            nearest_above_label, nearest_above_price,
            nearest_below_label, nearest_below_price,
            sr_score_buy, sr_score_sell, sr_reaction_buy, sr_reaction_sell,
            vol_regime_m15, vol_regime_h1, atr_m15, atr_h1, is_high_vol_window,
            macro_risk, macro_is_blackout, macro_event_type, macro_minutes_to_event,
            is_tradeable, block_reasons
        ) VALUES (
            ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
        )
        """,
        (
            snapshot_id,
            snapshot["asset"],
            snapshot["timestamp_snapshot"],
            snapshot.get("current_price"),
            snapshot.get("trend_h4"),
            snapshot.get("trend_h1"),
            snapshot.get("trend_combined"),
            snapshot.get("ema_slope_h4"),
            snapshot.get("ema_slope_h1"),
            snapshot.get("current_session"),
            snapshot.get("reference_session"),
            snapshot.get("ref_high"),
            snapshot.get("ref_low"),
            snapshot.get("ref_range"),
            snapshot.get("ote_low"),
            snapshot.get("ote_high"),
            snapshot.get("fib_618"),
            snapshot.get("fib_786"),
            snapshot.get("nearest_above_label"),
            snapshot.get("nearest_above_price"),
            snapshot.get("nearest_below_label"),
            snapshot.get("nearest_below_price"),
            snapshot.get("sr_score_buy", 0.0),
            snapshot.get("sr_score_sell", 0.0),
            bool(snapshot.get("sr_reaction_buy", False)),
            bool(snapshot.get("sr_reaction_sell", False)),
            snapshot.get("vol_regime_m15"),
            snapshot.get("vol_regime_h1"),
            snapshot.get("atr_m15"),
            snapshot.get("atr_h1"),
            bool(snapshot.get("is_high_vol_window", False)),
            snapshot.get("macro_risk", "LOW"),
            bool(snapshot.get("macro_is_blackout", False)),
            snapshot.get("macro_event_type"),
            snapshot.get("macro_minutes_to_event"),
            bool(snapshot.get("is_tradeable", True)),
            snapshot.get("block_reasons", "[]"),
        ),
    )
    conn.commit()
    return snapshot_id


# ============================================================
# edge_lab_signals — insert
# ============================================================

def insert_el_signal(conn: sqlite3.Connection, signal: dict) -> str:
    signal_id  = signal.get("signal_id") or str(uuid.uuid4())
    flags_json = json.dumps(signal.get("tradeability_flags", []))

    conn.execute(
        """
        INSERT INTO edge_lab_signals (
            signal_id, strategy_name, strategy_version,
            asset, direction, timestamp_setup,
            entry, stop_loss, tp, rr,
            ote_low, ote_high,
            liquidity_target, liquidity_target_price,
            liquidity_target_priority, liquidity_target_score,
            session, ref_session,
            trend_h4, trend_h1, trend_combined,
            vol_regime_m15, sr_reaction, sr_score,
            quality_score, quality_label,
            tradeability_flags, final_outcome, expiry_bars
        ) VALUES (
            ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
        )
        """,
        (
            signal_id,
            signal.get("strategy_name", "OTE-SC"),
            signal.get("strategy_version", "Phase1A"),
            signal["asset"],
            signal["direction"],
            signal["timestamp_setup"],
            signal["entry"],
            signal["stop_loss"],
            signal.get("tp"),
            signal.get("rr"),
            signal.get("ote_low"),
            signal.get("ote_high"),
            signal.get("liquidity_target"),
            signal.get("liquidity_target_price"),
            signal.get("liquidity_target_priority"),
            signal.get("liquidity_target_score"),
            signal.get("session"),
            signal.get("ref_session"),
            signal.get("trend_h4"),
            signal.get("trend_h1"),
            signal.get("trend_combined"),
            signal.get("vol_regime_m15"),
            bool(signal.get("sr_reaction", False)),
            signal.get("sr_score", 0.0),
            signal.get("quality_score"),
            signal.get("quality_label"),
            flags_json,
            "OPEN",
            signal.get("expiry_bars", 96),
        ),
    )
    conn.commit()
    return signal_id


# ============================================================
# edge_lab_signals — monitoring
# ============================================================

def get_open_el_signals(
    conn: sqlite3.Connection,
    asset: Optional[str] = None,
) -> pd.DataFrame:
    if asset:
        return pd.read_sql_query(
            "SELECT * FROM edge_lab_signals WHERE final_outcome='OPEN' AND asset=?",
            conn, params=(asset,)
        )
    return pd.read_sql_query(
        "SELECT * FROM edge_lab_signals WHERE final_outcome='OPEN'", conn
    )


def update_el_signal_outcome(
    conn: sqlite3.Connection,
    signal_id: str,
    final_outcome: str,
    timestamp_closed: Optional[str] = None,
    mae: Optional[float] = None,
    mfe: Optional[float] = None,
    bars_open: Optional[int] = None,
):
    updates = ["final_outcome = ?"]
    params: list = [final_outcome]
    if timestamp_closed:
        updates.append("timestamp_closed = ?"); params.append(timestamp_closed)
    if mae is not None:
        updates.append("mae = ?"); params.append(mae)
    if mfe is not None:
        updates.append("mfe = ?"); params.append(mfe)
    if bars_open is not None:
        updates.append("bars_open = ?"); params.append(bars_open)
    params.append(signal_id)
    conn.execute(
        f"UPDATE edge_lab_signals SET {', '.join(updates)} WHERE signal_id = ?",
        params,
    )
    conn.commit()


def monitor_open_el_signals(
    conn: sqlite3.Connection,
    asset: str,
    current_high: float,
    current_low: float,
    now_iso: str,
) -> list[dict]:
    rows = conn.execute(
        """
        SELECT signal_id, direction, entry, stop_loss, tp,
               mae, mfe, bars_open, expiry_bars
        FROM edge_lab_signals
        WHERE final_outcome = 'OPEN' AND asset = ?
        """,
        (asset,),
    ).fetchall()

    updated = []

    for row in rows:
        sid, direction, entry, sl, tp, mae, mfe, bars_open, expiry_bars = row

        if entry is None or sl is None:
            continue

        bars_open = (bars_open or 0) + 1

        if direction == "BUY":
            adverse   = max(float(entry) - current_low,  0.0)
            favorable = max(current_high - float(entry), 0.0)
        else:
            adverse   = max(current_high - float(entry), 0.0)
            favorable = max(float(entry) - current_low,  0.0)

        new_mae = max(float(mae or 0), adverse)
        new_mfe = max(float(mfe or 0), favorable)

        if direction == "BUY":
            sl_hit = current_low  <= float(sl)
            tp_hit = tp is not None and current_high >= float(tp)
        else:
            sl_hit = current_high >= float(sl)
            tp_hit = tp is not None and current_low  <= float(tp)

        # SL priorità su TP
        if sl_hit:
            outcome = "SL"
        elif tp_hit:
            outcome = "TP"
        elif bars_open >= (expiry_bars or 96):
            outcome = "EXPIRED"
        else:
            outcome = None

        if outcome:
            update_el_signal_outcome(
                conn, sid, outcome,
                timestamp_closed=now_iso,
                mae=new_mae, mfe=new_mfe, bars_open=bars_open,
            )
            updated.append({
                "signal_id": sid, "outcome": outcome,
                "mae": new_mae, "mfe": new_mfe, "bars_open": bars_open,
            })
        else:
            update_el_signal_outcome(
                conn, sid, "OPEN",
                mae=new_mae, mfe=new_mfe, bars_open=bars_open,
            )

    return updated


def has_open_el_signal(
    conn: sqlite3.Connection,
    asset: str,
    direction: str,
    strategy_name: str = "OTE-SC",
) -> bool:
    row = conn.execute(
        """
        SELECT 1 FROM edge_lab_signals
        WHERE asset=? AND direction=? AND strategy_name=? AND final_outcome='OPEN'
        LIMIT 1
        """,
        (asset, direction, strategy_name),
    ).fetchone()
    return row is not None


def has_recent_el_signal(
    conn: sqlite3.Connection,
    asset: str,
    direction: str,
    strategy_name: str = "OTE-SC",
    hours: int = 2,
) -> bool:
    """
    Ritorna True se esiste già un segnale (OPEN o chiuso) generato
    nelle ultime N ore con stesso asset, direzione e strategia.
    Previene la creazione di segnali duplicati quando il setup
    non è ancora cambiato tra un scan e l'altro.
    """
    cutoff = (
        datetime.now(timezone.utc) - timedelta(hours=hours)
    ).isoformat()
    row = conn.execute(
        """
        SELECT 1 FROM edge_lab_signals
        WHERE asset=? AND direction=? AND strategy_name=?
        AND timestamp_setup >= ?
        LIMIT 1
        """,
        (asset, direction, strategy_name, cutoff),
    ).fetchone()
    return row is not None
