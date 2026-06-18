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
from typing import Optional
import pandas as pd


def _migrate_v41_signals_columns(conn: sqlite3.Connection):
    """
    Migrazione leggera e idempotente: se la tabella v41_signals esiste
    già (da un'installazione precedente) ma le manca una colonna
    introdotta in seguito, la aggiunge con ALTER TABLE. Necessaria
    perché CREATE TABLE IF NOT EXISTS non aggiorna lo schema di una
    tabella già esistente, anche se lo script SQL cambia.
    """
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(v41_signals)").fetchall()}
    if not existing_cols:
        return  # la tabella non esiste ancora: la creerà lo script SQL

    required_columns = {
        "ote_entry_low": "REAL",
        "ote_entry_high": "REAL",
        "ote_in_zone_now": "BOOLEAN DEFAULT 0",
        "expected_move_points": "REAL",
        "expected_move_pct": "REAL",
        "expected_move_barrier": "TEXT",
        "expected_move_barrier_price": "REAL",
        "tp1": "REAL",
        "tp2": "REAL",
        "tp1_hit": "BOOLEAN DEFAULT 0",
        "tp2_hit": "BOOLEAN DEFAULT 0",
    }
    for col_name, col_type in required_columns.items():
        if col_name not in existing_cols:
            conn.execute(f"ALTER TABLE v41_signals ADD COLUMN {col_name} {col_type}")
    conn.commit()


def init_v41_schema(conn: sqlite3.Connection, schema_path: str = "storage/v41_schema.sql"):
    with open(schema_path, "r") as f:
        conn.executescript(f.read())
    conn.commit()
    _migrate_v41_signals_columns(conn)


def insert_v41_signal(conn: sqlite3.Connection, signal_dict: dict) -> str:
    signal_id = signal_dict.get("signal_id") or str(uuid.uuid4())
    snapshot_json = json.dumps(signal_dict.get("market_snapshot")) if signal_dict.get("market_snapshot") else None
    trigger_types_json = json.dumps(signal_dict.get("trigger_types", []))

    conn.execute(
        """
        INSERT INTO v41_signals (
            signal_id, timestamp_setup, asset, direction,
            entry, stop_loss, take_profit, tp1, tp2, rr,
            trigger_types, sweep_direction, bos_direction, choch_direction,
            quality_score, quality_label,
            ema_h4, ema_h1, dow_theory_h4, momentum,
            in_h4_zone, sr_reaction, ote_present, session,
            liquidity_source, liquidity_target, liquidity_target_price,
            ote_entry_low, ote_entry_high, ote_in_zone_now,
            expected_move_points, expected_move_pct,
            expected_move_barrier, expected_move_barrier_price,
            trader_decision, final_outcome, market_snapshot
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            signal_id,
            signal_dict["timestamp_setup"],
            signal_dict["asset"],
            signal_dict["direction"],
            signal_dict["entry"],
            signal_dict["stop_loss"],
            signal_dict.get("take_profit"),   # TP2 per compatibilità
            signal_dict.get("tp1"),
            signal_dict.get("tp2"),
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
            signal_dict.get("ote_entry_low"),
            signal_dict.get("ote_entry_high"),
            signal_dict.get("ote_in_zone_now", False),
            signal_dict.get("expected_move_points"),
            signal_dict.get("expected_move_pct"),
            signal_dict.get("expected_move_barrier"),
            signal_dict.get("expected_move_barrier_price"),
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
                               mae: float = None, mfe: float = None,
                               tp1_hit: bool = None, tp2_hit: bool = None):
    updates = ["final_outcome = ?"]
    params = [final_outcome]
    if timestamp_closed:
        updates.append("timestamp_closed = ?"); params.append(timestamp_closed)
    if mae is not None:
        updates.append("mae = ?"); params.append(mae)
    if mfe is not None:
        updates.append("mfe = ?"); params.append(mfe)
    if tp1_hit is not None:
        updates.append("tp1_hit = ?"); params.append(tp1_hit)
    if tp2_hit is not None:
        updates.append("tp2_hit = ?"); params.append(tp2_hit)
    params.append(signal_id)
    conn.execute(f"UPDATE v41_signals SET {', '.join(updates)} WHERE signal_id = ?", params)
    conn.commit()


def monitor_open_signals(conn: sqlite3.Connection, asset: str,
                          current_high: float, current_low: float,
                          now_iso: str, expiry_hours: int = 24) -> list:
    """
    Monitora i segnali aperti per un asset usando high/low dell'ultima
    candela M15 disponibile nel DB (nessun fetch aggiuntivo).

    Per ciascun segnale aperto verifica nell'ordine:
        1. SL raggiunto (high/low tocca o supera stop_loss)
        2. TP2 raggiunto (high/low tocca o supera tp2)
        3. TP1 raggiunto (high/low tocca o supera tp1) — aggiorna solo tp1_hit
        4. Scadenza dopo expiry_hours: chiude come EXPIRED

    Aggiorna MAE e MFE ad ogni scan, indipendentemente dall'outcome.
    SL ha priorità su TP (caso raro di candela con wick che tocca entrambi).

    Ritorna lista di dict {"signal_id", "outcome", "tp1_hit", "tp2_hit"}
    per ogni segnale aggiornato in questo ciclo.
    """
    from datetime import datetime, timezone, timedelta

    rows = conn.execute(
        """SELECT signal_id, direction, entry, stop_loss, tp1, tp2,
                  mae, mfe, tp1_hit, timestamp_setup
           FROM v41_signals
           WHERE final_outcome = 'OPEN' AND asset = ?""",
        (asset,)
    ).fetchall()

    updated = []

    for row in rows:
        sid, direction, entry, sl, tp1, tp2, mae, mfe, tp1_hit_db, ts_setup = row

        if entry is None or sl is None:
            continue

        # Aggiorna MAE e MFE (peggiore e migliore escursione avversa/favorevole)
        if direction == "BUY":
            adverse = entry - current_low     # quanto è sceso contro di noi
            favorable = current_high - entry  # quanto è salito a favore
        else:
            adverse = current_high - entry
            favorable = entry - current_low

        new_mae = max(mae or 0, adverse)
        new_mfe = max(mfe or 0, favorable)

        # Verifica scadenza (priorità massima dopo SL)
        try:
            setup_dt = datetime.fromisoformat(ts_setup)
            if setup_dt.tzinfo is None:
                setup_dt = setup_dt.replace(tzinfo=timezone.utc)
            elapsed = datetime.now(timezone.utc) - setup_dt
            expired = elapsed > timedelta(hours=expiry_hours)
        except Exception:
            expired = False

        # Verifica SL
        sl_hit = (direction == "BUY" and current_low <= sl) or \
                 (direction == "SELL" and current_high >= sl)

        # Verifica TP2
        tp2_hit_now = tp2 is not None and (
            (direction == "BUY" and current_high >= tp2) or
            (direction == "SELL" and current_low <= tp2)
        )

        # Verifica TP1
        tp1_hit_now = tp1 is not None and (
            (direction == "BUY" and current_high >= tp1) or
            (direction == "SELL" and current_low <= tp1)
        )

        # SL ha sempre priorità su TP (candela con spike in entrambe le direzioni)
        if sl_hit:
            update_v41_signal_outcome(
                conn, sid, "SL", now_iso,
                mae=new_mae, mfe=new_mfe,
                tp1_hit=bool(tp1_hit_db or tp1_hit_now),
                tp2_hit=False,
            )
            updated.append({"signal_id": sid, "outcome": "SL",
                             "tp1_hit": bool(tp1_hit_now), "tp2_hit": False})
        elif tp2_hit_now:
            update_v41_signal_outcome(
                conn, sid, "TP", now_iso,
                mae=new_mae, mfe=new_mfe,
                tp1_hit=True, tp2_hit=True,
            )
            updated.append({"signal_id": sid, "outcome": "TP2",
                             "tp1_hit": True, "tp2_hit": True})
        elif expired:
            update_v41_signal_outcome(
                conn, sid, "EXPIRED", now_iso,
                mae=new_mae, mfe=new_mfe,
                tp1_hit=bool(tp1_hit_db or tp1_hit_now),
                tp2_hit=False,
            )
            updated.append({"signal_id": sid, "outcome": "EXPIRED",
                             "tp1_hit": bool(tp1_hit_now), "tp2_hit": False})
        else:
            # Segnale ancora aperto: aggiorna MAE/MFE e tp1_hit se raggiunto
            new_tp1_hit = bool(tp1_hit_db or tp1_hit_now)
            conn.execute(
                "UPDATE v41_signals SET mae=?, mfe=?, tp1_hit=? WHERE signal_id=?",
                (new_mae, new_mfe, new_tp1_hit, sid)
            )
            conn.commit()

    return updated


# ============================================================
# Watchlist state (transizioni dentro/fuori fascia di prossimità)
# ============================================================

def get_watchlist_state(conn: sqlite3.Connection, asset: str, level_label: str) -> bool:
    """
    Ritorna True se l'ultimo stato noto per asset+livello era
    "dentro la fascia di prossimità", False altrimenti (incluso il
    caso in cui non esiste ancora uno stato salvato, default False).
    """
    row = conn.execute(
        "SELECT is_inside_proximity FROM v41_watchlist_state WHERE asset = ? AND level_label = ?",
        (asset, level_label)
    ).fetchone()
    return bool(row[0]) if row else False


def set_watchlist_state(conn: sqlite3.Connection, asset: str, level_label: str,
                         is_inside_proximity: bool, timestamp: str):
    conn.execute(
        """
        INSERT INTO v41_watchlist_state (asset, level_label, is_inside_proximity, last_updated)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(asset, level_label) DO UPDATE SET
            is_inside_proximity = excluded.is_inside_proximity,
            last_updated = excluded.last_updated
        """,
        (asset, level_label, is_inside_proximity, timestamp)
    )
    conn.commit()


def insert_watchlist_alert(conn: sqlite3.Connection, asset: str, proximity: dict, timestamp: str) -> str:
    alert_id = str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO v41_watchlist_alerts (
            alert_id, timestamp_alert, asset, level_label, level_price,
            distance_pct, potential_direction
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            alert_id, timestamp, asset, proximity["label"], proximity["price"],
            proximity["distance_pct"], proximity["potential_direction"],
        )
    )
    conn.commit()
    return alert_id


# ============================================================
# Duplicate Signal Protection (stato ultimo alert operativo per asset)
# ============================================================

def get_last_alert_state(conn: sqlite3.Connection, asset: str) -> Optional[dict]:
    """
    Ritorna l'ultimo alert operativo registrato per l'asset, come dict
    {"direction", "trigger_type", "liquidity_source"}, oppure None se
    non è ancora stato inviato alcun alert per quell'asset.
    """
    row = conn.execute(
        "SELECT direction, trigger_type, liquidity_source FROM v41_last_alert_state WHERE asset = ?",
        (asset,)
    ).fetchone()
    if row is None:
        return None
    return {"direction": row[0], "trigger_type": row[1], "liquidity_source": row[2]}


def set_last_alert_state(conn: sqlite3.Connection, asset: str, direction: str,
                          trigger_type: str, liquidity_source, timestamp: str):
    conn.execute(
        """
        INSERT INTO v41_last_alert_state (asset, direction, trigger_type, liquidity_source, last_updated)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(asset) DO UPDATE SET
            direction = excluded.direction,
            trigger_type = excluded.trigger_type,
            liquidity_source = excluded.liquidity_source,
            last_updated = excluded.last_updated
        """,
        (asset, direction, trigger_type, liquidity_source, timestamp)
    )
    conn.commit()
