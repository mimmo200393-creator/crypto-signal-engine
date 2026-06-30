"""
core/session_sweep_engine.py
Session Sweep Engine — Sprint 7

Layer 1: dipende da Structure Engine (Layer 0).
Rileva il pattern ICT "London sweeps Asia" e varianti.

Logica:
    1. Alla fine di ASIA, registra il range (Asia High, Asia Low)
    2. Durante LONDON, verifica se il prezzo ha sweepato un estremo
    3. Se ha sweepato E invertito → pattern ICT confermato
    4. Traccia anche NY vs London

Modalita': LIVE MODE.
Dipendenze: pandas, sqlite3, logging.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from datetime import datetime, timezone

import pandas as pd

logger = logging.getLogger("session_sweep_engine")

SNAPSHOT_VERSION = "1.0.0"

# Session boundaries in UTC minutes
SESSIONS = {
    "ASIA":     (0, 8 * 60 - 1),         # 00:00 - 07:59
    "LONDON":   (8 * 60, 13 * 60 + 29),   # 08:00 - 13:29
    "OVERLAP":  (13 * 60 + 30, 16 * 60 + 30),  # 13:30 - 16:30
    "NEW_YORK": (16 * 60 + 31, 22 * 60),  # 16:31 - 22:00
}

SWEEP_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS session_sweep_snapshots (
    snapshot_id         TEXT PRIMARY KEY,
    asset               TEXT NOT NULL,
    timestamp_snapshot  DATETIME NOT NULL,
    current_session     TEXT,
    asia_swept          BOOLEAN DEFAULT 0,
    sweep_direction     TEXT,
    sweep_reversed      BOOLEAN DEFAULT 0,
    snapshot_json       TEXT
);
CREATE INDEX IF NOT EXISTS idx_ss_sweep_asset_ts
    ON session_sweep_snapshots(asset, timestamp_snapshot);
"""


def init_session_sweep_schema(conn: sqlite3.Connection):
    conn.executescript(SWEEP_SCHEMA_SQL)
    conn.commit()


def _get_current_session(now: datetime) -> str:
    t = now.hour * 60 + now.minute
    for name, (start, end) in SESSIONS.items():
        if start <= t <= end:
            return name
    return "ASIA"


def _get_session_candles(df_m15: pd.DataFrame, session_start_min: int,
                          session_end_min: int) -> pd.DataFrame:
    """Filtra le candele che appartengono a una sessione specifica (oggi)."""
    if len(df_m15) == 0 or "timestamp" not in df_m15.columns:
        return pd.DataFrame()

    # Usa le ultime 96 candele (24h) per trovare quelle della sessione
    recent = df_m15.iloc[-96:]
    mask = []
    for _, row in recent.iterrows():
        try:
            ts = int(row["timestamp"])
            dt = datetime.fromtimestamp(ts / 1000 if ts > 1e12 else ts, tz=timezone.utc)
            t = dt.hour * 60 + dt.minute
            mask.append(session_start_min <= t <= session_end_min)
        except (ValueError, OSError):
            mask.append(False)

    return recent[mask] if any(mask) else pd.DataFrame()


def produce_session_sweep_snapshot(
    asset: str,
    df_m15: pd.DataFrame,
    conn: sqlite3.Connection,
    now: datetime = None,
    config: dict = None,
) -> dict:
    if now is None:
        now = datetime.now(timezone.utc)

    current_session = _get_current_session(now)

    # ── Asia range ───────────────────────────────────────────
    asia_start, asia_end = SESSIONS["ASIA"]
    asia_candles = _get_session_candles(df_m15, asia_start, asia_end)

    asia_high = float(asia_candles["high"].max()) if len(asia_candles) > 0 else 0
    asia_low = float(asia_candles["low"].min()) if len(asia_candles) > 0 else 0
    asia_range = asia_high - asia_low
    current_price = float(df_m15.iloc[-1]["close"]) if len(df_m15) > 0 else 0
    asia_range_pct = round(asia_range / current_price * 100, 4) if current_price > 0 else 0

    # ── London action vs Asia ────────────────────────────────
    london_start, london_end = SESSIONS["LONDON"]
    london_candles = _get_session_candles(df_m15, london_start, london_end)

    swept_asia_high = False
    swept_asia_low = False
    sweep_direction = None
    sweep_reversed = False
    true_direction = None

    if len(london_candles) > 0 and asia_high > 0 and asia_low > 0:
        london_high = float(london_candles["high"].max())
        london_low = float(london_candles["low"].min())
        london_close = float(london_candles.iloc[-1]["close"])

        if london_high > asia_high:
            swept_asia_high = True
            sweep_direction = "UP"
            if london_close < asia_high:
                sweep_reversed = True
                true_direction = "BEARISH"

        if london_low < asia_low:
            swept_asia_low = True
            sweep_direction = "DOWN"
            if london_close > asia_low:
                sweep_reversed = True
                true_direction = "BULLISH"

    # ── NY action vs London ──────────────────────────────────
    ny_start, ny_end = SESSIONS["NEW_YORK"]
    ny_candles = _get_session_candles(df_m15, ny_start, ny_end)

    london_high_val = float(london_candles["high"].max()) if len(london_candles) > 0 else 0
    london_low_val = float(london_candles["low"].min()) if len(london_candles) > 0 else 0

    swept_london_high = False
    swept_london_low = False
    continues_london = False

    if len(ny_candles) > 0 and london_high_val > 0:
        ny_high = float(ny_candles["high"].max())
        ny_low = float(ny_candles["low"].min())

        if ny_high > london_high_val:
            swept_london_high = True
        if ny_low < london_low_val:
            swept_london_low = True

        if true_direction == "BULLISH" and ny_high > london_high_val:
            continues_london = True
        elif true_direction == "BEARISH" and ny_low < london_low_val:
            continues_london = True

    # ── Snapshot ─────────────────────────────────────────────
    snapshot = {
        "asset": asset,
        "timestamp": now.isoformat(),
        "snapshot_version": SNAPSHOT_VERSION,
        "current_session": current_session,

        "asia_range": {
            "high": asia_high,
            "low": asia_low,
            "range_size": round(asia_range, 4),
            "range_pct": asia_range_pct,
        },

        "london_action": {
            "swept_asia_high": swept_asia_high,
            "swept_asia_low": swept_asia_low,
            "sweep_direction": sweep_direction,
            "sweep_reversed": sweep_reversed,
            "true_direction": true_direction,
        },

        "ny_action": {
            "swept_london_high": swept_london_high,
            "swept_london_low": swept_london_low,
            "continues_london": continues_london,
        },
    }

    # ── Salva ────────────────────────────────────────────────
    try:
        conn.execute("""
            INSERT INTO session_sweep_snapshots (
                snapshot_id, asset, timestamp_snapshot, current_session,
                asia_swept, sweep_direction, sweep_reversed, snapshot_json
            ) VALUES (?,?,?,?,?,?,?,?)
        """, (
            str(uuid.uuid4()), asset, now.isoformat(), current_session,
            swept_asia_high or swept_asia_low,
            sweep_direction, sweep_reversed,
            json.dumps(snapshot, default=str),
        ))
        conn.commit()
    except Exception as e:
        logger.warning("Session Sweep [%s]: errore salvataggio: %s", asset, e)

    # ── Log ──────────────────────────────────────────────────
    logger.info(
        "Session Sweep [%s]: session=%s asia=%.2f-%.2f(%.2f%%) "
        "london_swept=%s dir=%s reversed=%s true_dir=%s",
        asset, current_session,
        asia_low, asia_high, asia_range_pct,
        swept_asia_high or swept_asia_low,
        sweep_direction or "none",
        sweep_reversed,
        true_direction or "none",
    )

    return snapshot
