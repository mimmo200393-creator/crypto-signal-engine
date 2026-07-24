"""
core/lh_db.py
Liquidity Hunter — Layer accesso dati

Tabella: lh_signals
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS lh_signals (
    signal_id               TEXT PRIMARY KEY,
    strategy_name           TEXT NOT NULL DEFAULT 'LH',
    strategy_version        TEXT NOT NULL DEFAULT 'v1.0',
    asset                   TEXT NOT NULL,
    direction               TEXT NOT NULL CHECK(direction IN ('BUY','SELL')),
    timestamp_setup         DATETIME NOT NULL,
    timestamp_closed        DATETIME,

    entry                   REAL NOT NULL,
    stop_loss               REAL NOT NULL,
    tp                      REAL,
    risk                    REAL,
    rr                      REAL,

    swept_level_label       TEXT,
    swept_level_price       REAL,
    swept_level_priority    TEXT,
    swept_level_touches     INTEGER DEFAULT 0,

    sweep_direction         TEXT,
    sweep_peak_price        REAL,
    sweep_penetration       REAL,
    sweep_penetration_pct   REAL,
    flag_bos_present        BOOLEAN DEFAULT 0,
    flag_choch_present      BOOLEAN DEFAULT 0,
    flag_trigger_present    BOOLEAN DEFAULT 0,
    flag_near_order_block   BOOLEAN DEFAULT 0,
    flag_near_fvg           BOOLEAN DEFAULT 0,
    ob_quality              INTEGER,
    pool_type               TEXT,
    flag_htf_pool           BOOLEAN DEFAULT 0,
    confluence_count        INTEGER DEFAULT 0,

    trigger_type            TEXT,
    trigger_ref_level       REAL,

    tp_label                TEXT,
    tp_priority             TEXT,

    quality_score           INTEGER,
    quality_label           TEXT CHECK(quality_label IN ('LOW','MEDIUM','HIGH')),

    final_outcome           TEXT DEFAULT 'OPEN'
        CHECK(final_outcome IN ('OPEN','TP','SL','EXPIRED')),
    mae                     REAL DEFAULT 0,
    mfe                     REAL DEFAULT 0,
    bars_open               INTEGER DEFAULT 0,
    expiry_bars             INTEGER DEFAULT 96
);

CREATE INDEX IF NOT EXISTS idx_lh_asset_outcome
    ON lh_signals(asset, final_outcome);
CREATE INDEX IF NOT EXISTS idx_lh_timestamp
    ON lh_signals(timestamp_setup);
CREATE INDEX IF NOT EXISTS idx_lh_level
    ON lh_signals(swept_level_label, swept_level_priority);
"""


def _migrate_lh_flags(conn: sqlite3.Connection):
    """Aggiunge le colonne nuove ai DB gia' esistenti (idempotente)."""
    cols = [r[1] for r in conn.execute("PRAGMA table_info(lh_signals)")]
    for col, typ in [("sweep_penetration_pct", "REAL"),
                     ("flag_bos_present", "BOOLEAN DEFAULT 0"),
                     ("flag_choch_present", "BOOLEAN DEFAULT 0"),
                     ("flag_trigger_present", "BOOLEAN DEFAULT 0"),
                     ("flag_near_order_block", "BOOLEAN DEFAULT 0"),
                     ("flag_near_fvg", "BOOLEAN DEFAULT 0"),
                     ("ob_quality", "INTEGER"),
                     ("pool_type", "TEXT"),
                     ("flag_htf_pool", "BOOLEAN DEFAULT 0"),
                     ("confluence_count", "INTEGER DEFAULT 0"),
                     # --- LH v3.1 ---
                     ("setup_state", "TEXT DEFAULT 'TRIGGERED'"),
                     ("order_type", "TEXT DEFAULT 'MARKET'"),
                     ("distance_atr", "REAL DEFAULT 0"),
                     ("tp1", "REAL"), ("tp1_label", "TEXT"),
                     ("tp2", "REAL"), ("tp2_label", "TEXT"),
                     ("tp3", "REAL"), ("tp3_label", "TEXT"),
                     ("confluence_factors", "TEXT"),
                     ("pending_bars", "INTEGER DEFAULT 0"),
                     ("filled_ts", "TEXT"),
                     # Stato dell'ORDINE, separato dall'esito del trade.
                     # Non si tocca final_outcome perche' ha un CHECK
                     # constraint (OPEN/TP/SL/EXPIRED) che SQLite non
                     # permette di modificare senza ricostruire la tabella.
                     ("order_status", "TEXT DEFAULT 'FILLED'"),
                     ("ob_match_type", "TEXT"),
                     ("session", "TEXT")]:
        if col not in cols:
            conn.execute(f"ALTER TABLE lh_signals ADD COLUMN {col} {typ}")
    conn.commit()


def init_lh_schema(conn: sqlite3.Connection):
    conn.executescript(SCHEMA_SQL)
    _migrate_lh_flags(conn)
    conn.commit()


def insert_lh_signal(conn: sqlite3.Connection, signal: dict) -> str:
    signal_id = signal.get("signal_id") or str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO lh_signals (
            signal_id, strategy_name, strategy_version,
            asset, direction, timestamp_setup,
            entry, stop_loss, tp, risk, rr,
            swept_level_label, swept_level_price,
            swept_level_priority, swept_level_touches,
            sweep_direction, sweep_peak_price, sweep_penetration,
            sweep_penetration_pct, flag_bos_present, flag_choch_present, flag_trigger_present,
            flag_near_order_block, flag_near_fvg, ob_quality, pool_type, flag_htf_pool, confluence_count,
            trigger_type, trigger_ref_level,
            tp_label, tp_priority,
            quality_score, quality_label,
            final_outcome, expiry_bars,
            setup_state, order_type, distance_atr,
            tp1, tp1_label, tp2, tp2_label, tp3, tp3_label,
            confluence_factors, order_status, ob_match_type, session
        ) VALUES (
            ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,
            ?,?,?,?,?,?,?,?,?,?,?,?,?
        )
        """,
        (
            signal_id,
            signal.get("strategy_name", "LH"),
            signal.get("strategy_version", "v1.0"),
            signal["asset"],
            signal["direction"],
            signal["timestamp_setup"],
            signal["entry"],
            signal["stop_loss"],
            signal.get("tp"),
            signal.get("risk"),
            signal.get("rr"),
            signal.get("swept_level_label"),
            signal.get("swept_level_price"),
            signal.get("swept_level_priority"),
            signal.get("swept_level_touches", 0),
            signal.get("sweep_direction"),
            signal.get("sweep_peak_price"),
            signal.get("sweep_penetration"),
            signal.get("sweep_penetration_pct"),
            bool(signal.get("flag_bos_present", False)),
            bool(signal.get("flag_choch_present", False)),
            bool(signal.get("flag_trigger_present", False)),
            bool(signal.get("flag_near_order_block", False)),
            bool(signal.get("flag_near_fvg", False)),
            signal.get("ob_quality"),
            signal.get("pool_type"),
            bool(signal.get("flag_htf_pool", False)),
            signal.get("confluence_count", 0),
            signal.get("trigger_type"),
            signal.get("trigger_ref_level"),
            signal.get("tp_label"),
            signal.get("tp_priority"),
            signal.get("quality_score"),
            signal.get("quality_label"),
            "OPEN",
            signal.get("expiry_bars", 96),
            signal.get("setup_state", "TRIGGERED"),
            signal.get("order_type", "MARKET"),
            signal.get("distance_atr", 0),
            signal.get("tp1"), signal.get("tp1_label"),
            signal.get("tp2"), signal.get("tp2_label"),
            signal.get("tp3"), signal.get("tp3_label"),
            signal.get("confluence_factors"),
            # LH v3.1 — un ordine PENDENTE non e' un trade aperto: diventa
            # FILLED solo quando il prezzo raggiunge l'entry. Senza questa
            # distinzione il monitor calcolerebbe MAE/MFE e colpi di TP/SL
            # da un prezzo mai scambiato.
            "PENDING" if signal.get("setup_state") == "WATCHING" else "FILLED",
            signal.get("ob_match_type"),
            signal.get("session"),
        ),
    )
    conn.commit()
    return signal_id


def has_recent_lh_signal(
    conn: sqlite3.Connection,
    asset: str,
    direction: str,
    swept_level_label: str,
    hours: int = 4,
) -> bool:
    """
    Evita duplicati: stesso asset + direzione + livello sweepato nelle ultime N ore.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    row = conn.execute(
        """
        SELECT 1 FROM lh_signals
        WHERE asset=? AND direction=? AND swept_level_label=?
        AND timestamp_setup >= ?
        LIMIT 1
        """,
        (asset, direction, swept_level_label, cutoff),
    ).fetchone()
    return row is not None


def has_open_lh_signal(
    conn: sqlite3.Connection,
    asset: str,
    direction: str,
) -> bool:
    row = conn.execute(
        """
        SELECT 1 FROM lh_signals
        WHERE asset=? AND direction=? AND final_outcome='OPEN'
        LIMIT 1
        """,
        (asset, direction),
    ).fetchone()
    return row is not None


def monitor_pending_lh_signals(
    conn: sqlite3.Connection,
    asset: str,
    current_high: float,
    current_low: float,
    now_iso: str,
    max_pending_bars: int = 24,
) -> list[dict]:
    """
    Gestisce gli ordini PENDENTI di LH v3.1 (setup_state = WATCHING).

    Un pendente NON e' un trade: e' un ordine in attesa al bordo della zona
    Order Block. Va promosso a OPEN solo quando il prezzo raggiunge davvero
    l'entry — altrimenti il monitor calcolerebbe MAE/MFE e colpi di TP/SL
    da un prezzo mai scambiato, inquinando il Ledger con esiti inventati.

    - prezzo raggiunge l'entry -> final_outcome = 'OPEN', il trade parte ORA
      (bars_open azzerato: la vita del trade comincia dal riempimento)
    - troppo tempo senza riempimento -> final_outcome = 'CANCELLED'

    Da chiamare PRIMA di monitor_open_lh_signals nel runner.
    """
    rows = conn.execute(
        """
        SELECT signal_id, direction, entry, pending_bars
        FROM lh_signals
        WHERE order_status = 'PENDING' AND final_outcome = 'OPEN' AND asset = ?
        """,
        (asset,),
    ).fetchall()

    updated = []
    for sid, direction, entry, pending_bars in rows:
        if entry is None:
            continue
        pending_bars = (pending_bars or 0) + 1
        entry = float(entry)

        # riempimento: il prezzo ha raggiunto il livello dell'ordine
        if direction == "BUY":
            filled = current_low <= entry
        else:
            filled = current_high >= entry

        if filled:
            conn.execute(
                "UPDATE lh_signals SET order_status='FILLED', pending_bars=?, "
                "filled_ts=?, bars_open=0, mae=0, mfe=0 WHERE signal_id=?",
                (pending_bars, now_iso, sid),
            )
            updated.append({"signal_id": sid, "event": "FILLED",
                            "pending_bars": pending_bars})
        elif pending_bars >= max_pending_bars:
            conn.execute(
                "UPDATE lh_signals SET order_status='CANCELLED', "
                "final_outcome='EXPIRED', pending_bars=?, "
                "timestamp_closed=? WHERE signal_id=?",
                (pending_bars, now_iso, sid),
            )
            updated.append({"signal_id": sid, "event": "CANCELLED",
                            "pending_bars": pending_bars})
        else:
            conn.execute(
                "UPDATE lh_signals SET pending_bars=? WHERE signal_id=?",
                (pending_bars, sid),
            )

    conn.commit()
    return updated


def monitor_open_lh_signals(
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
        FROM lh_signals
        WHERE final_outcome = 'OPEN' AND asset = ?
          AND COALESCE(order_status, 'FILLED') = 'FILLED' 
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
            sl_hit    = current_low  <= float(sl)
            tp_hit    = tp is not None and current_high >= float(tp)
        else:
            adverse   = max(current_high - float(entry), 0.0)
            favorable = max(float(entry) - current_low,  0.0)
            sl_hit    = current_high >= float(sl)
            tp_hit    = tp is not None and current_low  <= float(tp)

        new_mae = max(float(mae or 0), adverse)
        new_mfe = max(float(mfe or 0), favorable)

        if sl_hit:
            outcome = "SL"
        elif tp_hit:
            outcome = "TP"
        elif bars_open >= (expiry_bars or 96):
            outcome = "EXPIRED"
        else:
            outcome = None

        if outcome:
            conn.execute(
                """
                UPDATE lh_signals
                SET final_outcome=?, timestamp_closed=?,
                    mae=?, mfe=?, bars_open=?
                WHERE signal_id=?
                """,
                (outcome, now_iso, new_mae, new_mfe, bars_open, sid),
            )
            updated.append({
                "signal_id": sid, "outcome": outcome,
                "mae": new_mae, "mfe": new_mfe, "bars_open": bars_open,
            })
        else:
            conn.execute(
                "UPDATE lh_signals SET mae=?, mfe=?, bars_open=? WHERE signal_id=?",
                (new_mae, new_mfe, bars_open, sid),
            )

    conn.commit()
    return updated
