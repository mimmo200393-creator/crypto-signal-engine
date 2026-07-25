"""
core/v3_db.py
Funzioni di accesso dati isolate per l'Institutional Scanner Framework V3.2.

Opera sulle tabelle v3_candles_cache, v3_signals, v3_structure_state
definite in storage/v3_schema.sql. Non tocca mai le tabelle esistenti.
"""

import json
import uuid
import sqlite3
import pandas as pd
from datetime import datetime, timezone


def init_v3_schema(conn: sqlite3.Connection, schema_path: str = "storage/v3_schema.sql"):
    with open(schema_path, "r") as f:
        conn.executescript(f.read())
    conn.commit()
    _ensure_dedup_column(conn)


def _ensure_dedup_column(conn: sqlite3.Connection):
    """
    Migrazione difensiva: aggiunge la colonna m15_trigger_ts a v3_signals
    se non esiste ancora. Necessaria per il dedup dei segnali generati
    dalla stessa candela M15 (nessun impatto su altre tabelle/sistemi:
    ALTER TABLE additivo, nullable, su una tabella dedicata al V3).
    """
    cols = [row[1] for row in conn.execute("PRAGMA table_info(v3_signals)").fetchall()]
    if "m15_trigger_ts" not in cols:
        conn.execute("ALTER TABLE v3_signals ADD COLUMN m15_trigger_ts INTEGER")
        conn.commit()


# ============================================================
# v3_candles_cache
# ============================================================

def upsert_v3_candles(conn: sqlite3.Connection, asset: str, timeframe: str, candles: list):
    if not candles:
        return
    rows = [
        (asset, timeframe, c["timestamp"], c["open"], c["high"],
         c["low"], c["close"], c["volume"])
        for c in candles
    ]
    conn.executemany(
        """
        INSERT INTO v3_candles_cache
            (asset, timeframe, timestamp, open, high, low, close, volume)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(asset, timeframe, timestamp) DO UPDATE SET
            open=excluded.open, high=excluded.high, low=excluded.low,
            close=excluded.close, volume=excluded.volume
        """,
        rows,
    )
    conn.commit()


def get_v3_candles_df(conn: sqlite3.Connection, asset: str, timeframe: str,
                       limit: int = None) -> pd.DataFrame:
    query = """
        SELECT timestamp, open, high, low, close, volume
        FROM v3_candles_cache
        WHERE asset = ? AND timeframe = ?
        ORDER BY timestamp DESC
    """
    if limit:
        query += f" LIMIT {int(limit)}"
    df = pd.read_sql_query(query, conn, params=(asset, timeframe))
    return df.sort_values("timestamp").reset_index(drop=True)


def get_v3_latest_timestamp(conn: sqlite3.Connection, asset: str, timeframe: str):
    cur = conn.execute(
        "SELECT MAX(timestamp) FROM v3_candles_cache WHERE asset = ? AND timeframe = ?",
        (asset, timeframe),
    )
    row = cur.fetchone()
    return row[0] if row and row[0] is not None else None


def count_v3_candles(conn: sqlite3.Connection, asset: str, timeframe: str) -> int:
    cur = conn.execute(
        "SELECT COUNT(*) FROM v3_candles_cache WHERE asset = ? AND timeframe = ?",
        (asset, timeframe),
    )
    return cur.fetchone()[0]


# ============================================================
# v3_structure_state (pullback invalidation tracking)
# ============================================================

def get_structure_state(conn: sqlite3.Connection, asset: str) -> dict:
    row = conn.execute(
        "SELECT * FROM v3_structure_state WHERE asset = ?", (asset,)
    ).fetchone()
    if row is None:
        return {}
    cols = [d[0] for d in conn.execute(
        "SELECT * FROM v3_structure_state WHERE asset = ?", (asset,)
    ).description]
    return dict(zip(cols, row))


def upsert_structure_state(conn: sqlite3.Connection, asset: str, trend_direction: str,
                            last_higher_low: float = None, last_lower_high: float = None,
                            last_swing_timestamp: int = None):
    conn.execute(
        """
        INSERT INTO v3_structure_state
            (asset, trend_direction, last_higher_low, last_lower_high,
             last_swing_timestamp, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(asset) DO UPDATE SET
            trend_direction=excluded.trend_direction,
            last_higher_low=excluded.last_higher_low,
            last_lower_high=excluded.last_lower_high,
            last_swing_timestamp=excluded.last_swing_timestamp,
            updated_at=excluded.updated_at
        """,
        (asset, trend_direction, last_higher_low, last_lower_high,
         last_swing_timestamp, datetime.now(timezone.utc).isoformat())
    )
    conn.commit()


# ============================================================
# v3_signals
# ============================================================

def has_duplicate_trigger(conn: sqlite3.Connection, asset: str, direction: str,
                           m15_trigger_ts: int) -> bool:
    """
    True se esiste gia' un segnale OPEN per lo stesso asset+direction generato
    dalla stessa candela M15 (stesso m15_trigger_ts). Il runner gira ogni 5 min
    ma una candela M15 dura 15 min (3 cicli): senza questo check, lo stesso
    identico setup viene salvato 3-4 volte come se fossero eventi distinti.
    """
    if m15_trigger_ts is None:
        return False
    cur = conn.execute(
        """
        SELECT 1 FROM v3_signals
        WHERE asset = ? AND direction = ? AND final_outcome = 'OPEN'
          AND m15_trigger_ts = ?
        LIMIT 1
        """,
        (asset, direction, m15_trigger_ts),
    )
    return cur.fetchone() is not None


def insert_v3_signal(conn: sqlite3.Connection, signal_dict: dict) -> str:
    signal_id = signal_dict.get("signal_id") or str(uuid.uuid4())
    snapshot_json = json.dumps(signal_dict.get("market_snapshot")) if signal_dict.get("market_snapshot") else None

    conn.execute(
        """
        INSERT INTO v3_signals (
            signal_id, timestamp_setup, asset, direction,
            entry, stop_loss, tp1, tp2, tp3, rr, signal_quality,
            daily_context_status, h4_structure_status, h4_zone_status,
            ote_present, pullback_type, pullback_invalidated,
            m30_transition_status, m15_bos_confirmed, session,
            trader_decision, final_outcome, market_snapshot, m15_trigger_ts
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
            signal_dict.get("m15_trigger_ts"),
        )
    )
    conn.commit()
    return signal_id


def get_open_v3_signals(conn: sqlite3.Connection, asset: str = None) -> pd.DataFrame:
    if asset:
        return pd.read_sql_query(
            "SELECT * FROM v3_signals WHERE final_outcome = 'OPEN' AND asset = ?",
            conn, params=(asset,)
        )
    return pd.read_sql_query(
        "SELECT * FROM v3_signals WHERE final_outcome = 'OPEN'", conn
    )


def update_v3_signal_outcome(conn: sqlite3.Connection, signal_id: str,
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
    conn.execute(f"UPDATE v3_signals SET {', '.join(updates)} WHERE signal_id = ?", params)
    conn.commit()
