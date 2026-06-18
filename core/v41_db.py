"""
core/v41_db.py
Funzioni di accesso dati isolate per Institutional Scanner V4.1
Intraday Wave Edition.

Opera esclusivamente sulla tabella v41_signals, separata da
v3_signals e v4_signals per garantire tracking statistico
indipendente. Le candele M15 sono condivise con V3.2/V4.0 tramite
v3_candles_cache (stessi dati di mercato, stesso asset).
"""

import json
import uuid
import sqlite3
import pandas as pd


def init_v41_schema(conn: sqlite3.Connection, schema_path: str = "storage/v41_schema.sql"):
    with open(schema_path, "r") as f:
        conn.executescript(f.read())
    conn.commit()


def insert_v41_signal(conn: sqlite3.Connection, signal_dict: dict) -> str:
    signal_id = signal_dict.get("signal_id") or str(uuid.uuid4())
    snapshot_json = json.dumps(signal_dict.get("market_snapshot")) if signal_dict.get("market_snapshot") else None
    trigger_types_json = json.dumps(signal_dict.get("trigger_types", []))

    conn.execute(
        """
        INSERT INTO v41_signals (
            signal_id, timestamp_setup, asset, direction,
            entry, stop_loss, take_profit, rr,
            trigger_types, sweep_direction, bos_direction, choch_direction,
            quality_score, quality_label,
            ema_h4, ema_h1, dow_theory_h4, momentum,
            in_h4_zone, sr_reaction, ote_present, session,
            liquidity_source, liquidity_target, liquidity_target_price,
            trader_decision, final_outcome, market_snapshot
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            signal_id,
            signal_dict["timestamp_setup"],
            signal_dict["asset"],
            signal_dict["direction"],
            signal_dict["entry"],
            signal_dict["stop_loss"],
            signal_dict.get("take_profit"),
            signal_dict.get("rr"),
            trigger_types_json,
            signal_dict.get("sweep_direction"),
            signal_dict.get("bos_direction"),
            signal_dict.get("choch_direction"),
            signal_dict["quality_score"],
            signal_dict["quality_label"],
            signal_dict.get("ema_h4"),
            signal_dict.get("ema_h1"),
            signal_dict.get("dow_theory_h4"),
            signal_dict.get("momentum"),
            signal_dict.get("in_h4_zone", False),
            signal_dict.get("sr_reaction", False),
            signal_dict.get("ote_present", False),
            signal_dict.get("session"),
            signal_dict.get("liquidity_source"),
            signal_dict.get("liquidity_target"),
            signal_dict.get("liquidity_target_price"),
            "unknown",
            "OPEN",
            snapshot_json,
        )
    )
    conn.commit()
    return signal_id


def get_open_v41_signals(conn: sqlite3.Connection, asset: str = None) -> pd.DataFrame:
    if asset:
        return pd.read_sql_query(
            "SELECT * FROM v41_signals WHERE final_outcome = 'OPEN' AND asset = ?",
            conn, params=(asset,)
        )
    return pd.read_sql_query(
        "SELECT * FROM v41_signals WHERE final_outcome = 'OPEN'", conn
    )


def update_v41_signal_outcome(conn: sqlite3.Connection, signal_id: str,
                               final_outcome: str, timestamp_closed: str = None,
                               mae: float = None, mfe: float = None):
    updates = ["final_outcome = ?"]
    params = [final_outcome]
    if timestamp_closed:
        updates.append("timestamp_closed = ?"); params.append(timestamp_closed)
    if mae is not None:
        updates.append("mae = ?"); params.append(mae)
    if mfe is not None:
        updates.append("mfe = ?"); params.append(mfe)
    params.append(signal_id)
    conn.execute(f"UPDATE v41_signals SET {', '.join(updates)} WHERE signal_id = ?", params)
    conn.commit()
