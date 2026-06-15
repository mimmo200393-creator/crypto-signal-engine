"""
storage/db.py  (V2.2)
Gestione connessione SQLite.

Mantiene invariate tutte le funzioni V1 (trades, candles_cache).
Aggiunge le funzioni per la tabella signals V2 con campi V2.2.
"""

import sqlite3
import json
import os
import pandas as pd
from datetime import datetime, timezone


# ============================================================
# Connessione e init
# ============================================================

def get_connection(db_path: str) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


def init_db(conn: sqlite3.Connection, schema_path: str = "storage/schema_v2.sql"):
    with open(schema_path, "r") as f:
        schema_sql = f.read()
    conn.executescript(schema_sql)
    conn.commit()


# ============================================================
# candles_cache (invariato da V1)
# ============================================================

def upsert_candles(conn: sqlite3.Connection, asset: str, timeframe: str, candles: list):
    if not candles:
        return
    rows = [
        (asset, timeframe, c["timestamp"], c["open"], c["high"],
         c["low"], c["close"], c["volume"])
        for c in candles
    ]
    conn.executemany(
        """
        INSERT INTO candles_cache
            (asset, timeframe, timestamp, open, high, low, close, volume)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(asset, timeframe, timestamp) DO UPDATE SET
            open=excluded.open, high=excluded.high, low=excluded.low,
            close=excluded.close, volume=excluded.volume
        """,
        rows,
    )
    conn.commit()


def get_candles_df(conn: sqlite3.Connection, asset: str, timeframe: str,
                   limit: int = None) -> pd.DataFrame:
    query = """
        SELECT timestamp, open, high, low, close, volume
        FROM candles_cache
        WHERE asset = ? AND timeframe = ?
        ORDER BY timestamp DESC
    """
    if limit:
        query += f" LIMIT {int(limit)}"
    df = pd.read_sql_query(query, conn, params=(asset, timeframe))
    df = df.sort_values("timestamp").reset_index(drop=True)
    return df


def get_latest_timestamp(conn: sqlite3.Connection, asset: str, timeframe: str):
    cur = conn.execute(
        "SELECT MAX(timestamp) FROM candles_cache WHERE asset = ? AND timeframe = ?",
        (asset, timeframe),
    )
    row = cur.fetchone()
    return row[0] if row and row[0] is not None else None


def count_candles(conn: sqlite3.Connection, asset: str, timeframe: str) -> int:
    cur = conn.execute(
        "SELECT COUNT(*) FROM candles_cache WHERE asset = ? AND timeframe = ?",
        (asset, timeframe),
    )
    return cur.fetchone()[0]


# ============================================================
# trades (V1 - invariato per retrocompatibilita')
# ============================================================

def insert_trade(conn: sqlite3.Connection, trade: dict) -> int:
    columns = [
        "strategy_name", "strategy_version", "timestamp_alert", "timestamp_setup",
        "asset", "setup", "direzione", "entry", "stop_loss", "take_profit",
        "rr", "score", "stato", "atr_h1", "support_level", "resistance_level",
        "trigger_type", "macro_event_active", "macro_event_type",
        "macro_event_minutes_to_release", "bars_open",
    ]
    values = [trade.get(c) for c in columns]
    placeholders = ", ".join(["?"] * len(columns))
    col_str = ", ".join(columns)
    cur = conn.execute(
        f"INSERT INTO trades ({col_str}) VALUES ({placeholders})", values
    )
    conn.commit()
    return cur.lastrowid


def has_active_trade(conn: sqlite3.Connection, asset: str, direzione: str) -> bool:
    cur = conn.execute(
        "SELECT 1 FROM trades WHERE asset = ? AND direzione = ? AND stato = 'ACTIVE' LIMIT 1",
        (asset, direzione),
    )
    return cur.fetchone() is not None


def get_last_trade(conn: sqlite3.Connection, asset: str, direzione: str, setup: str):
    cur = conn.execute(
        """
        SELECT id, timestamp_setup, stato FROM trades
        WHERE asset = ? AND direzione = ? AND setup = ?
        ORDER BY timestamp_setup DESC LIMIT 1
        """,
        (asset, direzione, setup),
    )
    row = cur.fetchone()
    if row is None:
        return None
    return {"id": row[0], "timestamp_setup": row[1], "stato": row[2]}


def get_active_trades(conn: sqlite3.Connection) -> pd.DataFrame:
    return pd.read_sql_query("SELECT * FROM trades WHERE stato = 'ACTIVE'", conn)


def update_trade_status(conn: sqlite3.Connection, trade_id: int, stato: str,
                         timestamp_closed: str = None, mae: float = None,
                         mfe: float = None, bars_open: int = None):
    updates = ["stato = ?"]
    params = [stato]
    if timestamp_closed is not None:
        updates.append("timestamp_closed = ?"); params.append(timestamp_closed)
    if mae is not None:
        updates.append("mae = ?"); params.append(mae)
    if mfe is not None:
        updates.append("mfe = ?"); params.append(mfe)
    if bars_open is not None:
        updates.append("bars_open = ?"); params.append(bars_open)
    params.append(trade_id)
    conn.execute(f"UPDATE trades SET {', '.join(updates)} WHERE id = ?", params)
    conn.commit()


def update_trade_alert_timestamp(conn: sqlite3.Connection, trade_id: int,
                                  timestamp_alert: str):
    conn.execute(
        "UPDATE trades SET timestamp_alert = ? WHERE id = ?",
        (timestamp_alert, trade_id)
    )
    conn.commit()


# ============================================================
# signals (V2.2)
# ============================================================

def insert_signal(conn: sqlite3.Connection, signal, market_snapshot: dict = None) -> str:
    snapshot_json = json.dumps(market_snapshot) if market_snapshot else None
    ctx = signal.additional_context or {}
    macro_event = ctx.get("macro_event")

    conn.execute(
        """
        INSERT INTO signals (
            signal_id, strategy_name, strategy_version,
            asset, direction, entry, stop_loss, take_profit, rr,
            raw_score, final_score, market_regime,
            timestamp_setup, trade_status, rejection_reason,
            market_snapshot,
            macro_event_active, macro_event_type, macro_event_minutes_to_release,
            macro_risk, zone_level, zone_touches, session, momentum_direction, atr_daily
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            signal.signal_id,
            signal.strategy_name,
            signal.strategy_version,
            signal.asset,
            signal.direction,
            signal.entry,
            signal.stop_loss,
            signal.take_profit,
            signal.rr,
            signal.raw_score,
            signal.final_score,
            signal.market_regime,
            signal.timestamp.isoformat(),
            signal.trade_status,
            signal.rejection_reason,
            snapshot_json,
            bool(macro_event),
            macro_event["type"] if macro_event else None,
            macro_event["minutes_to_release"] if macro_event else None,
            ctx.get("macro_risk"),
            ctx.get("zone_level"),
            ctx.get("zone_touches"),
            ctx.get("session"),
            ctx.get("momentum"),
            ctx.get("atr_daily"),
        ),
    )
    conn.commit()
    return signal.signal_id


def update_signal_status(conn: sqlite3.Connection, signal_id: str,
                          trade_status: str, timestamp_closed: str = None,
                          mae: float = None, mfe: float = None,
                          bars_open: int = None, time_to_tp: int = None,
                          time_to_sl: int = None):
    updates = ["trade_status = ?"]
    params = [trade_status]
    if timestamp_closed:
        updates.append("timestamp_closed = ?"); params.append(timestamp_closed)
    if mae is not None:
        updates.append("mae = ?"); params.append(mae)
    if mfe is not None:
        updates.append("mfe = ?"); params.append(mfe)
    if bars_open is not None:
        updates.append("bars_open = ?"); params.append(bars_open)
    if time_to_tp is not None:
        updates.append("time_to_tp = ?"); params.append(time_to_tp)
    if time_to_sl is not None:
        updates.append("time_to_sl = ?"); params.append(time_to_sl)
    params.append(signal_id)
    conn.execute(f"UPDATE signals SET {', '.join(updates)} WHERE signal_id = ?", params)
    conn.commit()


def set_signal_notified(conn: sqlite3.Connection, signal_id: str):
    conn.execute(
        "UPDATE signals SET trade_status = 'NOTIFIED', timestamp_alert = ? WHERE signal_id = ?",
        (datetime.now(timezone.utc).isoformat(), signal_id)
    )
    conn.commit()


def get_open_signals(conn: sqlite3.Connection) -> pd.DataFrame:
    return pd.read_sql_query(
        "SELECT * FROM signals WHERE trade_status = 'OPEN'", conn
    )


def has_open_signal(conn: sqlite3.Connection, asset: str, direction: str,
                    strategy_name: str) -> bool:
    cur = conn.execute(
        """
        SELECT 1 FROM signals
        WHERE asset = ? AND direction = ? AND strategy_name = ?
          AND trade_status = 'OPEN'
        LIMIT 1
        """,
        (asset, direction, strategy_name),
    )
    return cur.fetchone() is not None


def get_last_signal_timestamp(conn: sqlite3.Connection, asset: str,
                               direction: str, strategy_name: str):
    cur = conn.execute(
        """
        SELECT timestamp_setup FROM signals
        WHERE asset = ? AND direction = ? AND strategy_name = ?
        ORDER BY timestamp_setup DESC LIMIT 1
        """,
        (asset, direction, strategy_name),
    )
    row = cur.fetchone()
    return row[0] if row else None
