"""
core/market_radar_db.py
Persistenza del Market Radar V1.

Tre tabelle in signals.db:
  radar_zones        — le Entry Zone emesse + outcome grezzo (MAE/MFE)
  radar_state        — stato corrente della macchina a stati per asset
  radar_transitions  — storico di OGNI transizione (funnel di analisi)

Nessuna soglia qui: si registra tutto grezzo. Le feature sono salvate in
JSON cosi' l'analisi futura puo' usarle senza migrazioni di schema.
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
        direction      TEXT,               -- BUY/SELL (rimbalzo atteso)
        emit_ts        TEXT NOT NULL,
        price          REAL,
        zone_ref       TEXT,               -- ob:xxx / fvg:xxx (dedup)
        features_json  TEXT,               -- tutte le feature grezze
        -- outcome grezzo (aggiornato dal monitor, no simulazione trade)
        status         TEXT DEFAULT 'OPEN',-- OPEN / CLOSED
        mae            REAL,               -- max escursione avversa (dir. impulso)
        mfe            REAL,               -- max escursione favorevole (rimbalzo)
        bars_open      INTEGER DEFAULT 0,
        time_to_mfe    INTEGER,            -- candele al picco favorevole
        time_to_mae    INTEGER,            -- candele al picco avverso
        stop_loss      REAL,               -- livello stop equilibrato (registrato)
        stop_hit       INTEGER DEFAULT 0,  -- 1 se lo stop e' stato toccato
        time_to_stop   INTEGER,            -- candele al tocco dello stop
        mfe_after_stop REAL,               -- respiro DOPO il tocco (il guadagno vero)
        tp_scalp       REAL,               -- TP scalp suggerito (1 ATR)
        tp_hit         INTEGER DEFAULT 0,  -- 1 se il respiro ha raggiunto il TP scalp
        time_to_tp     INTEGER,            -- candele al TP scalp
        be_trigger     REAL,               -- livello BE suggerito (1 ATR)
        be_reached     INTEGER DEFAULT 0,  -- 1 se il respiro ha raggiunto il BE
        mfe_beyond_tp  REAL,               -- quanto il respiro va OLTRE il TP scalp
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
    conn.commit()


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
    """
    Timestamp ISO dell'ULTIMA transizione VERSO to_state. Serve alla state
    machine per sapere da quanto tempo e' nello stato corrente (senza
    questo, OSSERVAZIONE non ha modo di scadere).
    """
    row = conn.execute(
        "SELECT ts FROM radar_transitions WHERE asset=? AND to_state=? "
        "ORDER BY id DESC LIMIT 1", (asset, to_state)).fetchone()
    return row[0] if row else None


def get_last_impulse_features(conn, asset: str) -> dict:
    """
    Feature registrate alla transizione RIPOSO → MERCATO_ESTESO, cioe' quelle
    dell'IMPULSO che ha attivato la macchina.

    Perche' serve: le feature salvate con la Entry Zone sono calcolate al
    momento dell'emissione, durante l'esaurimento — quindi la loro velocity
    e' per costruzione bassa (misurata sui dati reali: 0.046-0.269, mentre il
    gate d'ingresso richiede >= 0.6). Testare "impulso veloce → rimbalzo
    maggiore" su quel numero e' impossibile: e' la velocita' sbagliata.
    Qui recuperiamo quella vera, gia' salvata nel funnel.
    """
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
    Aggiorna MAE/MFE delle zone OPEN col prezzo corrente. Chiude la zona
    quando ha accumulato `window_bars` candele. NON simula un trade: misura
    il comportamento grezzo del prezzo dopo la zona.

    MFE = massimo movimento nella direzione ATTESA del rimbalzo (il respiro).
    MAE = massimo movimento nella direzione dell'impulso (avverso).

    STOP LOSS: se il prezzo tocca il livello di stop, lo REGISTRA (stop_hit=1
    + candela) ma NON chiude la zona e NON ferma la misura del respiro. Il
    rimbalzo che da' il guadagno puo' arrivare DOPO un tocco dello stop:
    per questo tracciamo mfe_after_stop separatamente. Cosi' i dati dicono
    sia 'lo stop sarebbe scattato' sia 'il respiro e' arrivato comunque'.
    """
    updated = []
    rows = conn.execute("""
        SELECT zone_id, direction, price, mae, mfe, bars_open, time_to_mfe,
               time_to_mae, stop_loss, stop_hit, time_to_stop, mfe_after_stop,
               tp_scalp, tp_hit, time_to_tp, be_trigger, be_reached, mfe_beyond_tp
        FROM radar_zones WHERE asset=? AND status='OPEN'
    """, (asset,)).fetchall()

    for (zid, direction, p0, mae, mfe, bars, t_mfe, t_mae,
         stop, stop_hit, t_stop, mfe_after,
         tp_scalp, tp_hit, t_tp, be_trigger, be_reached, mfe_beyond) in rows:
        bars = (bars or 0) + 1
        mae = mae or 0.0
        mfe = mfe or 0.0
        stop_hit = stop_hit or 0
        mfe_after = mfe_after or 0.0
        tp_hit = tp_hit or 0
        be_reached = be_reached or 0
        mfe_beyond = mfe_beyond or 0.0

        if direction == "BUY":
            fav = current_high - p0        # respiro (su)
            adv = p0 - current_low         # avverso (giu)
            stop_touched = (stop is not None) and (current_low <= stop)
            tp_touched   = (tp_scalp is not None) and (current_high >= tp_scalp)
            be_touched   = (be_trigger is not None) and (current_high >= be_trigger)
            beyond = (current_high - tp_scalp) if tp_scalp is not None else 0.0
        else:  # SELL — respiro atteso giu
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
        # quanto il respiro va OLTRE il TP scalp (guadagno lasciato col trailing)
        if tp_hit and beyond > mfe_beyond:
            mfe_beyond = beyond

        closed = bars >= window_bars
        conn.execute("""
            UPDATE radar_zones SET mae=?, mfe=?, bars_open=?, time_to_mfe=?,
                   time_to_mae=?, stop_hit=?, time_to_stop=?, mfe_after_stop=?,
                   tp_hit=?, time_to_tp=?, be_reached=?, mfe_beyond_tp=?,
                   status=?, close_ts=?
            WHERE zone_id=?
        """, (mae, mfe, bars, t_mfe, t_mae, stop_hit, t_stop, mfe_after,
              tp_hit, t_tp, be_reached, mfe_beyond,
              "CLOSED" if closed else "OPEN",
              now_iso if closed else None, zid))
        updated.append({"zone_id": zid, "mae": mae, "mfe": mfe, "bars": bars,
                        "stop_hit": bool(stop_hit), "tp_hit": bool(tp_hit),
                        "be_reached": bool(be_reached), "closed": closed})
    conn.commit()
    return updated
