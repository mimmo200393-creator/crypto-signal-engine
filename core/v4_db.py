"""
core/v4_db.py
Funzioni di accesso dati isolate per Institutional Scanner V4.0 Daily Edition.

Opera esclusivamente sulla tabella v4_signals, separata da v3_signals
per garantire tracking statistico indipendente tra le due strategie.
Le candele D1/M30/M15 sono condivise con V3.2 tramite v3_candles_cache
(stessi dati di mercato, stesso asset, nessun motivo di duplicarle).
"""

import json
import uuid
import sqlite3
import pandas as pd


def init_v4_schema(conn: sqlite3.Connection, schema_path: str = "storage/v4_schema.sql"):
    with open(schema_path, "r") as f:
        conn.executescript(f.read())
    conn.commit()


def insert_v4_signal(conn: sqlite3.Connection, signal_dict: dict) -> str:
    signal_id = signal_dict.get("signal_id") or str(uuid.uuid4())
    snapshot_json = json.dumps(signal_dict.get("market_snapshot")) if signal_dict.get("market_snapshot") else None

    conn.execute(
        """
        INSERT INTO v4_signals (
            signal_id, timestamp_setup, asset, direction,
            entry, stop_loss, tp1, tp2, tp3, rr, signal_quality, quality_label,
            daily_context_status, h4_structure_status, h4_zone_status,
            ote_present, pullback_type, pullback_invalidated,
            m30_transition_status, m15_bos_confirmed, session,
            trader_decision, final_outcome, market_snapshot
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            signal_id,
            signal_dict["timestamp_setup"],
            signal_dict["asset"],
            signal_dict["direction"],
            signal_dict["entry"],
            signal_dict["stop_loss"],
            signal_dict.get("tp1"),
            signal_dict.get("tp2"),
            signal_dict.get("tp3"),
            signal_dict["rr"],
            signal_dict["signal_quality"],
            signal_dict.get("quality_label"),
            signal_dict.get("daily_context_status"),
            signal_dict.get("h4_structure_status"),
            signal_dict.get("h4_zone_status"),
            signal_dict.get("ote_present", False),
            signal_dict.get("pullback_type"),
            signal_dict.get("pullback_invalidated", False),
            signal_dict.get("m30_transition_status"),
            signal_dict.get("m15_bos_confirmed", False),
            signal_dict.get("session"),
            "unknown",
            "OPEN",
            snapshot_json,
        )
    )
    conn.commit()
    return signal_id


def get_open_v4_signals(conn: sqlite3.Connection, asset: str = None) -> pd.DataFrame:
    if asset:
        return pd.read_sql_query(
            "SELECT * FROM v4_signals WHERE final_outcome = 'OPEN' AND asset = ?",
            conn, params=(asset,)
        )
    return pd.read_sql_query(
        "SELECT * FROM v4_signals WHERE final_outcome = 'OPEN'", conn
    )


def update_v4_signal_outcome(conn: sqlite3.Connection, signal_id: str,
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
    conn.execute(f"UPDATE v4_signals SET {', '.join(updates)} WHERE signal_id = ?", params)
    conn.commit()
