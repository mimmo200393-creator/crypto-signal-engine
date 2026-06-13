"""
db.py
Gestione connessione SQLite, init schema, operazioni su candles_cache e trades.
"""

import sqlite3
import os
import pandas as pd


def get_connection(db_path: str) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


def init_db(conn: sqlite3.Connection, schema_path: str = "storage/schema.sql"):
    with open(schema_path, "r") as f:
        schema_sql = f.read()
    conn.executescript(schema_sql)
    conn.commit()


# ---------------------------------------------------------------------------
# candles_cache
# ---------------------------------------------------------------------------

def upsert_candles(conn: sqlite3.Connection, asset: str, timeframe: str, candles: list):
    """
    Inserisce/aggiorna candele nella cache. `candles` e' una lista di dict
    con chiavi: timestamp, open, high, low, close, volume.
    """
    if not candles:
        return

    rows = [
        (asset, timeframe, c["timestamp"], c["open"], c["high"], c["low"], c["close"], c["volume"])
        for c in candles
    ]

    conn.executemany(
        """
        INSERT INTO candles_cache (asset, timeframe, timestamp, open, high, low, close, volume)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(asset, timeframe, timestamp) DO UPDATE SET
            open=excluded.open,
            high=excluded.high,
            low=excluded.low,
            close=excluded.close,
            volume=excluded.volume
        """,
        rows,
    )
    conn.commit()


def get_candles_df(conn: sqlite3.Connection, asset: str, timeframe: str, limit: int = None) -> pd.DataFrame:
    """
    Ritorna le candele come DataFrame ordinato per timestamp crescente.
    Se `limit` e' specificato, ritorna solo le ultime `limit` candele.
    """
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
    """
    Ritorna il timestamp (ms) della candela piu' recente in cache,
    oppure None se non c'e' nessuna candela.
    """
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


# ---------------------------------------------------------------------------
# trades
# ---------------------------------------------------------------------------

def insert_trade(conn: sqlite3.Connection, trade: dict) -> int:
    """
    Inserisce un nuovo trade (setup validato) con stato ACTIVE.
    `trade` deve contenere tutte le colonne richieste dallo schema
    (timestamp_alert puo' essere None se score < TELEGRAM_SCORE_THRESHOLD).
    Ritorna l'id del trade inserito.
    """
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
        f"INSERT INTO trades ({col_str}) VALUES ({placeholders})",
        values,
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
    """
    Ritorna l'ultimo trade (per timestamp_setup) per asset+direzione+setup,
    indipendentemente dallo stato. Usato per il controllo cooldown.
    """
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
        updates.append("timestamp_closed = ?")
        params.append(timestamp_closed)
    if mae is not None:
        updates.append("mae = ?")
        params.append(mae)
    if mfe is not None:
        updates.append("mfe = ?")
        params.append(mfe)
    if bars_open is not None:
        updates.append("bars_open = ?")
        params.append(bars_open)

    params.append(trade_id)
    conn.execute(f"UPDATE trades SET {', '.join(updates)} WHERE id = ?", params)
    conn.commit()


def update_trade_alert_timestamp(conn: sqlite3.Connection, trade_id: int, timestamp_alert: str):
    conn.execute("UPDATE trades SET timestamp_alert = ? WHERE id = ?", (timestamp_alert, trade_id))
    conn.commit()
