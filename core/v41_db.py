"""
core/v41p1_db.py
Funzioni di accesso dati per Institutional Scanner V4.1 Phase 1.
Opera su v41p1_signals, v41p1_watchlist_alerts, v41p1_watchlist_state,
v41p1_last_alert_state, v41p1_mfm_snapshots.

Sprint 13: Breakeven a +1R in monitor_open_signals.
"""

import json
import uuid
import sqlite3
from datetime import datetime, timezone, timedelta
from typing import Optional

# Sprint 0 — Decision Ledger (collegamento esito, raccolta passiva)
try:
    from core.decision_ledger import v41p1_integration as _ledger_link
except Exception:
    _ledger_link = None


def init_v41p1_schema(conn: sqlite3.Connection,
                       schema_path: str = "storage/v41p1_schema.sql"):
    for col, col_type in [
        ("mfm_sweep_confirmed", "BOOLEAN DEFAULT 0"),
        ("mfm_sweep_level",     "TEXT"),
        ("mfm_sweep_price",     "REAL"),
        ("mfm_sweep_priority",  "TEXT"),
        ("adx_m15",             "REAL"),
    ]:
        try:
            conn.execute(f"ALTER TABLE v41p1_signals ADD COLUMN {col} {col_type}")
            conn.commit()
        except sqlite3.OperationalError:
            pass

    with open(schema_path, "r") as f:
        conn.executescript(f.read())

    conn.commit()


# ============================================================
# Segnali
# ============================================================

def insert_v41p1_signal(conn: sqlite3.Connection, signal: dict) -> str:
    signal_id = signal.get("signal_id") or str(uuid.uuid4())
    trigger_json = json.dumps(signal.get("trigger_types", []))
    snapshot_json = signal.get("market_snapshot") if signal.get("market_snapshot") else None

    conn.execute("""
        INSERT INTO v41p1_signals (
            signal_id, timestamp_setup, asset, direction,
            entry, stop_loss, take_profit, tp1, tp2, rr,
            trigger_types, sweep_direction, bos_direction, choch_direction,
            nearest_above_label, nearest_above_price,
            nearest_above_priority, nearest_above_score,
            nearest_below_label, nearest_below_price,
            nearest_below_priority, nearest_below_score,
            liquidity_source, liquidity_source_price,
            liquidity_source_priority, liquidity_source_score,
            liquidity_target, liquidity_target_price,
            liquidity_target_priority, liquidity_target_score,
            expected_move_points, expected_move_pct, expected_move_barrier,
            distance_to_nearest_above_pct, distance_to_nearest_below_pct,
            quality_score, quality_label,
            ema_h4, ema_h1, dow_theory_h4, momentum, session,
            in_h4_zone, sr_reaction, ote_present,
            ote_entry_low, ote_entry_high,
            mfm_sweep_confirmed, mfm_sweep_level,
            mfm_sweep_price, mfm_sweep_priority,
            adx_m15,
            trader_decision, final_outcome, market_snapshot
        ) VALUES (
            ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,
            ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'unknown','OPEN',?
        )
    """, (
        signal_id,
        signal["timestamp_setup"],
        signal["asset"],
        signal["direction"],
        signal["entry"],
        signal["stop_loss"],
        signal.get("take_profit"),
        signal.get("tp1"),
        signal.get("tp2"),
        signal.get("rr"),
        trigger_json,
        signal.get("sweep_direction"),
        signal.get("bos_direction"),
        signal.get("choch_direction"),
        signal.get("nearest_above_label"),
        signal.get("nearest_above_price"),
        signal.get("nearest_above_priority"),
        signal.get("nearest_above_score"),
        signal.get("nearest_below_label"),
        signal.get("nearest_below_price"),
        signal.get("nearest_below_priority"),
        signal.get("nearest_below_score"),
        signal.get("liquidity_source"),
        signal.get("liquidity_source_price"),
        signal.get("liquidity_source_priority"),
        signal.get("liquidity_source_score"),
        signal.get("liquidity_target"),
        signal.get("liquidity_target_price"),
        signal.get("liquidity_target_priority"),
        signal.get("liquidity_target_score"),
        signal.get("expected_move_points"),
        signal.get("expected_move_pct"),
        signal.get("expected_move_barrier"),
        signal.get("distance_to_nearest_above_pct"),
        signal.get("distance_to_nearest_below_pct"),
        signal["quality_score"],
        signal["quality_label"],
        signal.get("ema_h4"),
        signal.get("ema_h1"),
        signal.get("dow_theory_h4"),
        signal.get("momentum"),
        signal.get("session"),
        signal.get("in_h4_zone", False),
        signal.get("sr_reaction", False),
        signal.get("ote_present", False),
        signal.get("ote_entry_low"),
        signal.get("ote_entry_high"),
        bool(signal.get("mfm_sweep_confirmed", False)),
        signal.get("mfm_sweep_level"),
        signal.get("mfm_sweep_price"),
        signal.get("mfm_sweep_priority"),
        signal.get("adx_m15"),
        snapshot_json,
    ))
    conn.commit()
    return signal_id


def update_v41p1_signal_outcome(conn: sqlite3.Connection, signal_id: str,
                                 final_outcome: str, timestamp_closed: str = None,
                                 mae: float = None, mfe: float = None,
                                 tp1_hit: bool = None, tp2_hit: bool = None,
                                 time_to_tp_minutes: int = None,
                                 time_to_sl_minutes: int = None):
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
    if time_to_tp_minutes is not None:
        updates.append("time_to_tp_minutes = ?"); params.append(time_to_tp_minutes)
    if time_to_sl_minutes is not None:
        updates.append("time_to_sl_minutes = ?"); params.append(time_to_sl_minutes)
    params.append(signal_id)
    conn.execute(
        f"UPDATE v41p1_signals SET {', '.join(updates)} WHERE signal_id = ?",
        params
    )
    conn.commit()


def monitor_open_signals(conn: sqlite3.Connection, asset: str,
                          current_high: float, current_low: float,
                          now_iso: str, expiry_hours: int = 24) -> list:
    rows = conn.execute("""
        SELECT signal_id, direction, entry, stop_loss, tp1, tp2,
               mae, mfe, tp1_hit, timestamp_setup
        FROM v41p1_signals
        WHERE final_outcome = 'OPEN' AND asset = ?
    """, (asset,)).fetchall()

    updated = []

    for row in rows:
        sid, direction, entry, sl, tp1, tp2, mae, mfe, tp1_hit_db, ts_setup = row

        if entry is None or sl is None:
            continue

        if direction == "BUY":
            adverse   = entry - current_low
            favorable = current_high - entry
        else:
            adverse   = current_high - entry
            favorable = entry - current_low

        new_mae = max(mae or 0, adverse)
        new_mfe = max(mfe or 0, favorable)

        try:
            setup_dt = datetime.fromisoformat(ts_setup)
            if setup_dt.tzinfo is None:
                setup_dt = setup_dt.replace(tzinfo=timezone.utc)
            now_dt = datetime.now(timezone.utc)
            elapsed = now_dt - setup_dt
            expired = elapsed > timedelta(hours=expiry_hours)
            elapsed_minutes = int(elapsed.total_seconds() / 60)
        except Exception:
            expired = False
            elapsed_minutes = 0

        # ══════════════════════════════════════════════════════
        # ── Breakeven a +1R (Sprint 13) ──────────────────────
        # ══════════════════════════════════════════════════════
        # Se il trade ha raggiunto +1R di profitto (mfe >= risk),
        # sposta lo SL a entry (breakeven). Dall'audit: 18% dei
        # trade SL raggiunge 0.75-1.5R prima di invertire.
        # Questo salva ~8/58 SL trasformandoli in breakeven.
        #
        # NOTA: modifica la variabile locale `sl` che viene poi
        # usata nel check sl_hit sotto. MAE è calcolato PRIMA
        # quindi registra comunque l'escursione completa.
        risk = abs(entry - sl)
        if risk > 0 and new_mfe >= risk:
            if direction == "BUY" and sl < entry:
                sl = entry
                conn.execute(
                    "UPDATE v41p1_signals SET stop_loss=? WHERE signal_id=?",
                    (sl, sid)
                )
                conn.commit()
            elif direction == "SELL" and sl > entry:
                sl = entry
                conn.execute(
                    "UPDATE v41p1_signals SET stop_loss=? WHERE signal_id=?",
                    (sl, sid)
                )
                conn.commit()
        # ══════════════════════════════════════════════════════

        sl_hit = (
            (direction == "BUY"  and current_low  <= sl) or
            (direction == "SELL" and current_high >= sl)
        )
        tp2_hit_now = tp2 is not None and (
            (direction == "BUY"  and current_high >= tp2) or
            (direction == "SELL" and current_low  <= tp2)
        )
        tp1_hit_now = tp1 is not None and (
            (direction == "BUY"  and current_high >= tp1) or
            (direction == "SELL" and current_low  <= tp1)
        )

        # Sprint 0: durata in barre M15 (elapsed_minutes / 15)
        _dur_bars = int(elapsed_minutes / 15) if elapsed_minutes else None

        if sl_hit:
            update_v41p1_signal_outcome(
                conn, sid, "SL", now_iso,
                mae=new_mae, mfe=new_mfe,
                tp1_hit=bool(tp1_hit_db or tp1_hit_now),
                tp2_hit=False,
                time_to_sl_minutes=elapsed_minutes,
            )
            updated.append({"signal_id": sid, "outcome": "SL",
                             "tp1_hit": bool(tp1_hit_now), "tp2_hit": False})
            if _ledger_link:
                # Se il breakeven era scattato (sl spostato a entry), l'esito
                # reale è BE, non SL. Il Ledger lo distingue per l'analisi.
                _outcome_ledger = "BE" if abs(sl - entry) < 1e-9 else "SL"
                # Per il calcolo R usa lo SL originale se disponibile
                _ledger_link.link_outcome(sid, _outcome_ledger, entry, sl,
                                          mae=new_mae, mfe=new_mfe, duration_bars=_dur_bars)
        elif tp2_hit_now:
            update_v41p1_signal_outcome(
                conn, sid, "TP", now_iso,
                mae=new_mae, mfe=new_mfe,
                tp1_hit=True, tp2_hit=True,
                time_to_tp_minutes=elapsed_minutes,
            )
            updated.append({"signal_id": sid, "outcome": "TP2",
                             "tp1_hit": True, "tp2_hit": True})
            if _ledger_link:
                _ledger_link.link_outcome(sid, "TP", entry, sl,
                                          mae=new_mae, mfe=new_mfe, duration_bars=_dur_bars)
        elif expired:
            update_v41p1_signal_outcome(
                conn, sid, "EXPIRED", now_iso,
                mae=new_mae, mfe=new_mfe,
                tp1_hit=bool(tp1_hit_db or tp1_hit_now),
                tp2_hit=False,
            )
            updated.append({"signal_id": sid, "outcome": "EXPIRED",
                             "tp1_hit": bool(tp1_hit_now), "tp2_hit": False})
            if _ledger_link:
                _ledger_link.link_outcome(sid, "EXPIRED", entry, sl,
                                          mae=new_mae, mfe=new_mfe, duration_bars=_dur_bars)
        else:
            new_tp1_hit = bool(tp1_hit_db or tp1_hit_now)
            conn.execute(
                "UPDATE v41p1_signals SET mae=?, mfe=?, tp1_hit=? WHERE signal_id=?",
                (new_mae, new_mfe, new_tp1_hit, sid)
            )
            conn.commit()

    return updated


# ============================================================
# Money Flow Map Snapshots
# ============================================================

def insert_mfm_snapshot(conn: sqlite3.Connection, asset: str,
                         mfm: dict, now_iso: str) -> str:
    snapshot_id = str(uuid.uuid4())
    above = mfm.get("nearest_above")
    below = mfm.get("nearest_below")
    levels_json = json.dumps(mfm.get("levels", []))

    conn.execute("""
        INSERT INTO v41p1_mfm_snapshots (
            snapshot_id, timestamp_snapshot, asset, current_price,
            nearest_above_label, nearest_above_price,
            nearest_above_priority, nearest_above_score,
            nearest_below_label, nearest_below_price,
            nearest_below_priority, nearest_below_score,
            levels_json
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        snapshot_id, now_iso, asset, mfm["current_price"],
        above["label"]          if above else None,
        above["price"]          if above else None,
        above["priority_label"] if above else None,
        above["priority_score"] if above else None,
        below["label"]          if below else None,
        below["price"]          if below else None,
        below["priority_label"] if below else None,
        below["priority_score"] if below else None,
        levels_json,
    ))
    conn.commit()
    return snapshot_id


# ============================================================
# Watchlist
# ============================================================

def get_watchlist_state(conn: sqlite3.Connection,
                         asset: str, level_label: str) -> bool:
    row = conn.execute(
        "SELECT is_inside_proximity FROM v41p1_watchlist_state "
        "WHERE asset = ? AND level_label = ?",
        (asset, level_label)
    ).fetchone()
    return bool(row[0]) if row else False


def set_watchlist_state(conn: sqlite3.Connection, asset: str,
                         level_label: str, is_inside: bool, timestamp: str):
    conn.execute("""
        INSERT INTO v41p1_watchlist_state
            (asset, level_label, is_inside_proximity, last_updated)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(asset, level_label) DO UPDATE SET
            is_inside_proximity = excluded.is_inside_proximity,
            last_updated = excluded.last_updated
    """, (asset, level_label, is_inside, timestamp))
    conn.commit()


def insert_watchlist_alert(conn: sqlite3.Connection, asset: str,
                            level: dict, timestamp: str) -> str:
    alert_id = str(uuid.uuid4())
    conn.execute("""
        INSERT INTO v41p1_watchlist_alerts (
            alert_id, timestamp_alert, asset, level_label, level_price,
            level_priority, level_score, distance_pct,
            potential_direction, historical_touches
        ) VALUES (?,?,?,?,?,?,?,?,?,?)
    """, (
        alert_id, timestamp, asset,
        level["label"], level["price"],
        level["priority_label"], level["priority_score"],
        level["distance_pct"],
        "SELL" if level["kind"] == "high" else "BUY",
        level["historical_touches"],
    ))
    conn.commit()
    return alert_id


# ============================================================
# Duplicate Signal Protection
# ============================================================

def get_last_alert_state(conn: sqlite3.Connection,
                          asset: str) -> Optional[dict]:
    row = conn.execute(
        "SELECT direction, trigger_type, liquidity_source "
        "FROM v41p1_last_alert_state WHERE asset = ?",
        (asset,)
    ).fetchone()
    if row is None:
        return None
    return {"direction": row[0], "trigger_type": row[1],
            "liquidity_source": row[2]}


def set_last_alert_state(conn: sqlite3.Connection, asset: str,
                          direction: str, trigger_type: str,
                          liquidity_source, timestamp: str):
    conn.execute("""
        INSERT INTO v41p1_last_alert_state
            (asset, direction, trigger_type, liquidity_source, last_updated)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(asset) DO UPDATE SET
            direction = excluded.direction,
            trigger_type = excluded.trigger_type,
            liquidity_source = excluded.liquidity_source,
            last_updated = excluded.last_updated
    """, (asset, direction, trigger_type, liquidity_source, timestamp))
    conn.commit()
