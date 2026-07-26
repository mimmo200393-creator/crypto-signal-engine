"""
core/market_radar_db.py
Persistenza del Market Radar V1.1.

Tre tabelle in signals.db:
  radar_zones        — le Entry Zone emesse + outcome grezzo (MAE/MFE)
  radar_state        — stato corrente della macchina a stati per asset
  radar_transitions  — storico di OGNI transizione (funnel di analisi)

V1.1: aggiunto first_hit (TP/SL/None) e first_hit_bar per sapere
chi arriva primo tra stop e target. TP alzato a 2 ATR (1:2 RR).
"""
from __future__ import annotations
import json
import uuid
import logging

logger = logging.getLogger("market_radar.db")


def init_radar_schema(conn):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS radar_zones (
        zone_id        TEXT PRIMARY KEY,
        asset          TEXT NOT NULL,
        direction      TEXT,
        emit_ts        TEXT NOT NULL,
        price          REAL,
        zone_ref       TEXT,
        features_json  TEXT,
        status         TEXT DEFAULT 'OPEN',
        mae            REAL,
        mfe            REAL,
        bars_open      INTEGER DEFAULT 0,
        time_to_mfe    INTEGER,
        time_to_mae    INTEGER,
        stop_loss      REAL,
        stop_hit       INTEGER DEFAULT 0,
        time_to_stop   INTEGER,
        mfe_after_stop REAL,
        tp_scalp       REAL,
        tp_hit         INTEGER DEFAULT 0,
        time_to_tp     INTEGER,
        be_trigger     REAL,
        be_reached     INTEGER DEFAULT 0,
        mfe_beyond_tp  REAL,
        close_ts       TEXT
    );
    CREATE TABLE IF NOT EXISTS radar_state (
        asset      TEXT PRIMARY KEY,
        state      TEXT NOT NULL,
        updated_ts TEXT
    );
    CREATE TABLE IF NOT EXISTS radar_transitions (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        asset         TEXT NOT NULL,
        from_state    TEXT,
        to_state      TEXT,
        ts            TEXT,
        features_json TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_radar_zones_open
        ON radar_zones(asset, status);
    """)
    _migrate_radar(conn)
    conn.commit()


def _migrate_radar(conn):
    """Aggiunge colonne V1.1. Idempotente."""
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(radar_zones)").fetchall()]
    except Exception:
        return
    for col, typ in [
        ("first_hit", "TEXT"),           # 'TP' o 'SL' — chi arriva primo
        ("first_hit_bar", "INTEGER"),    # a quale barra
    ]:
        if col not in cols:
            try:
                conn.execute(f"ALTER TABLE radar_zones ADD COLUMN {col} {typ}")
                logger.info("Radar DB: colonna %s aggiunta", col)
            except Exception as e:
                logger.warning("Radar DB migrate %s: %s", col, e)


# ── stato macchina ────────────────────────────────────────────────
def get_state(conn, asset: str):
    row = conn.execute("SELECT state FROM radar_state WHERE asset=?", (asset,)).fetchone()
    return row[0] if row else None

def set_state(conn, asset: str, state: str, now_iso: str = None):
    conn.execute("""
        INSERT INTO radar_state(asset, state, updated_ts) VALUES(?,?,?)
        ON CONFLICT(asset) DO UPDATE SET state=excluded.state, updated_ts=excluded.updated_ts
    """, (asset, state, now_iso))
    conn.commit()

def get_last_transition_ts(conn, asset: str, to_state: str):
    row = conn.execute(
        "SELECT ts FROM radar_transitions WHERE asset=? AND to_state=? "
        "ORDER BY id DESC LIMIT 1", (asset, to_state)).fetchone()
    return row[0] if row else None


def get_last_impulse_features(conn, asset: str) -> dict:
    row = conn.execute(
        "SELECT features_json FROM radar_transitions "
        "WHERE asset=? AND from_state='RIPOSO' AND to_state='MERCATO_ESTESO' "
        "ORDER BY id DESC LIMIT 1", (asset,)).fetchone()
    if not row or not row[0]:
        return {}
    try:
        return json.loads(row[0])
    except Exception:
        return {}


def log_transition(conn, asset, from_state, to_state, features, now_iso):
    conn.execute("""
        INSERT INTO radar_transitions(asset, from_state, to_state, ts, features_json)
        VALUES(?,?,?,?,?)
    """, (asset, from_state, to_state, now_iso, json.dumps(features, default=str)))
    conn.commit()


# ── zone ──────────────────────────────────────────────────────────
def get_open_zone_refs(conn, asset: str) -> set:
    rows = conn.execute(
        "SELECT zone_ref FROM radar_zones WHERE asset=? AND status='OPEN' AND zone_ref IS NOT NULL",
        (asset,)).fetchall()
    return {r[0] for r in rows}

def insert_zone(conn, asset, direction, price, features, zone_ref, now_iso) -> str:
    zid = uuid.uuid4().hex
    conn.execute("""
        INSERT INTO radar_zones(zone_id, asset, direction, emit_ts, price,
                                zone_ref, features_json, status, stop_loss,
                                tp_scalp, be_trigger)
        VALUES(?,?,?,?,?,?,?,'OPEN',?,?,?)
    """, (zid, asset, direction, now_iso, price, zone_ref,
          json.dumps(features, default=str), features.get("stop_loss"),
          features.get("tp_scalp"), features.get("be_trigger")))
    conn.commit()
    return zid

def monitor_open_zones(conn, asset, current_high, current_low, now_iso, window_bars) -> list:
    """
    Aggiorna MAE/MFE delle zone OPEN. NON interrompe la misura del respiro
    quando SL/TP viene toccato (V1: registra tutto grezzo).

    V1.1: traccia first_hit — chi tra TP e SL viene toccato PER PRIMO.
    Questo e' il dato che serve per calcolare il WR reale di un sistema
    con SL 1 ATR e TP 2 ATR: se first_hit='TP' il trade sarebbe vincente,
    se first_hit='SL' perdente. Il monitor continua comunque a registrare
    il comportamento completo (MFE dopo SL, respiro oltre TP, ecc.).
    """
    updated = []
    rows = conn.execute("""
        SELECT zone_id, direction, price, mae, mfe, bars_open, time_to_mfe,
               time_to_mae, stop_loss, stop_hit, time_to_stop, mfe_after_stop,
               tp_scalp, tp_hit, time_to_tp, be_trigger, be_reached, mfe_beyond_tp,
               first_hit, first_hit_bar
        FROM radar_zones WHERE asset=? AND status='OPEN'
    """, (asset,)).fetchall()

    for (zid, direction, p0, mae, mfe, bars, t_mfe, t_mae,
         stop, stop_hit, t_stop, mfe_after,
         tp_scalp, tp_hit, t_tp, be_trigger, be_reached, mfe_beyond,
         first_hit, first_hit_bar) in rows:
        bars = (bars or 0) + 1
        mae = mae or 0.0
        mfe = mfe or 0.0
        stop_hit = stop_hit or 0
        mfe_after = mfe_after or 0.0
        tp_hit = tp_hit or 0
        be_reached = be_reached or 0
        mfe_beyond = mfe_beyond or 0.0

        if direction == "BUY":
            fav = current_high - p0
            adv = p0 - current_low
            stop_touched = (stop is not None) and (current_low <= stop)
            tp_touched   = (tp_scalp is not None) and (current_high >= tp_scalp)
            be_touched   = (be_trigger is not None) and (current_high >= be_trigger)
            beyond = (current_high - tp_scalp) if tp_scalp is not None else 0.0
        else:
            fav = p0 - current_low
            adv = current_high - p0
            stop_touched = (stop is not None) and (current_high >= stop)
            tp_touched   = (tp_scalp is not None) and (current_low <= tp_scalp)
            be_touched   = (be_trigger is not None) and (current_low <= be_trigger)
            beyond = (tp_scalp - current_low) if tp_scalp is not None else 0.0

        if fav > mfe:
            mfe = fav; t_mfe = bars
        if adv > mae:
            mae = adv; t_mae = bars

        # stop: registra il PRIMO tocco, non interrompe
        if stop_touched and not stop_hit:
            stop_hit = 1; t_stop = bars
        if stop_hit and fav > mfe_after:
            mfe_after = fav

        # TP scalp e BE: registra il PRIMO raggiungimento
        if tp_touched and not tp_hit:
            tp_hit = 1; t_tp = bars
        if be_touched and not be_reached:
            be_reached = 1
        if tp_hit and beyond > mfe_beyond:
            mfe_beyond = beyond

        # V1.1: chi arriva PRIMO tra TP e SL
        # Si registra UNA SOLA VOLTA (first_hit e' immutabile dopo il primo tocco)
        if first_hit is None:
            if tp_touched and stop_touched:
                # Entrambi toccati nella stessa barra: conservativamente SL
                # (il prezzo potrebbe aver toccato SL prima nella barra)
                first_hit = "SL"
                first_hit_bar = bars
            elif tp_touched:
                first_hit = "TP"
                first_hit_bar = bars
            elif stop_touched:
                first_hit = "SL"
                first_hit_bar = bars

        closed = bars >= window_bars
        conn.execute("""
            UPDATE radar_zones SET mae=?, mfe=?, bars_open=?, time_to_mfe=?,
                   time_to_mae=?, stop_hit=?, time_to_stop=?, mfe_after_stop=?,
                   tp_hit=?, time_to_tp=?, be_reached=?, mfe_beyond_tp=?,
                   first_hit=?, first_hit_bar=?,
                   status=?, close_ts=?
            WHERE zone_id=?
        """, (mae, mfe, bars, t_mfe, t_mae, stop_hit, t_stop, mfe_after,
              tp_hit, t_tp, be_reached, mfe_beyond,
              first_hit, first_hit_bar,
              "CLOSED" if closed else "OPEN",
              now_iso if closed else None, zid))
        updated.append({"zone_id": zid, "mae": mae, "mfe": mfe, "bars": bars,
                        "stop_hit": bool(stop_hit), "tp_hit": bool(tp_hit),
                        "be_reached": bool(be_reached), "closed": closed,
                        "first_hit": first_hit})
    conn.commit()
    return updated
