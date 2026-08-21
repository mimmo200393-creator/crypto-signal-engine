"""
core/ote_db.py
OTE Fase A — Schema DB

Due tabelle:
    ote_candidates — ogni situazione osservata, NEUTRALE (senza direzione),
                     registrata anche quando non diventa mai un trade.
    ote_signals    — solo quando il mercato MOSTRA la direzione (sweep+reaction
                     su M5). Planned/Actual separati, lifecycle completo.

Principio fondamentale: la direzione viene assegnata SOLO in ote_signals,
MAI in ote_candidates. Il Candidate dice "zona calda, preparati" — il
Signal dice "il mercato ha mostrato BUY/SELL, ecco il trade".

Dal 18/08/2026 — registro nuovo, zero dati pregressi mischiati.
"""

import sqlite3
import logging
import uuid
from datetime import datetime, timezone

logger = logging.getLogger("ote.db")

STRATEGY_NAME = "OTE"


def init_ote_schema(conn: sqlite3.Connection):
    conn.executescript("""

    CREATE TABLE IF NOT EXISTS ote_candidates (
        candidate_id    TEXT PRIMARY KEY,
        asset           TEXT NOT NULL,
        -- NESSUNA DIREZIONE QUI — il candidate e' neutrale
        status          TEXT NOT NULL DEFAULT 'WATCHING'
                        CHECK(status IN ('WATCHING','TOUCHED','SIGNAL_CREATED','EXPIRED','INVALIDATED')),

        -- Zona (da LH Restart Zone, riusata senza duplicare)
        zone_ref        TEXT,
        zone_high       REAL,
        zone_low        REAL,
        zone_score      REAL,
        zone_strength   TEXT,
        zone_recurrence INTEGER,
        zone_failed     INTEGER,

        -- Liquidity Map (sopra E sotto — neutrale, non sceglie una direzione)
        liq_above_type  TEXT,
        liq_above_price REAL,
        liq_above_dist  REAL,
        liq_below_type  TEXT,
        liq_below_price REAL,
        liq_below_dist  REAL,

        -- Reaction Map score (l'unico engine con edge positivo confermato)
        reaction_map_score REAL,

        -- Proximity
        proximity_points REAL,

        -- Contesto registrato ma MAI usato come gate
        ctx_regime      TEXT,
        ctx_session     TEXT,

        -- Timestamps
        created_at      TEXT NOT NULL,
        touched_at      TEXT,
        expired_at      TEXT,

        -- Classificazione post-evento (Fase B, per ora solo sweep yes/no, reaction yes/no)
        sweep_detected      INTEGER DEFAULT 0,
        sweep_direction     TEXT,
        sweep_timestamp     TEXT,
        reaction_detected   INTEGER DEFAULT 0,
        reaction_direction  TEXT,
        reaction_timestamp  TEXT,

        -- Collegamento al segnale se ne nasce uno
        signal_id       TEXT
    );

    CREATE TABLE IF NOT EXISTS ote_signals (
        signal_id       TEXT PRIMARY KEY,
        candidate_id    TEXT NOT NULL,
        asset           TEXT NOT NULL,
        -- DIREZIONE ASSEGNATA QUI — solo dopo sweep+reaction su M5
        direction       TEXT NOT NULL CHECK(direction IN ('BUY','SELL')),

        status          TEXT NOT NULL DEFAULT 'ENTRY'
                        CHECK(status IN ('ENTRY','TP','SL','EXPIRED','INVALIDATED')),

        -- Planned (calcolati DOPO aver visto la direzione, mai sovrascritti)
        planned_entry   REAL NOT NULL,
        planned_sl      REAL NOT NULL,
        planned_tp      REAL NOT NULL,
        planned_rr      REAL NOT NULL,

        -- Actual (compilati a runtime, separati da Planned)
        actual_entry    REAL,
        actual_sl       REAL,
        actual_tp       REAL,
        actual_rr       REAL,

        -- Da dove viene la direzione (prova oggettiva, non previsione)
        trigger_type    TEXT,
        sweep_level     REAL,
        reaction_type   TEXT,

        -- Zona e liquidity (copiati dal candidate per tracciabilita')
        zone_ref        TEXT,
        zone_score      REAL,
        zone_strength   TEXT,

        -- Quality score (calcolato DOPO la conferma direzionale)
        quality_score   INTEGER,
        quality_label   TEXT,

        -- Target info
        tp_type         TEXT,
        tp_ref          TEXT,

        -- Outcome
        result          TEXT CHECK(result IN ('WIN','LOSS',NULL)),
        result_r        REAL,
        closed_at       TEXT,

        -- MFE/MAE
        mae             REAL,
        mfe             REAL,

        -- Timing
        bars_open       INTEGER DEFAULT 0,
        expiry_bars     INTEGER DEFAULT 96,
        signal_created_at TEXT NOT NULL,
        entry_ts        TEXT,

        -- Context snapshot (JSON, per analisi futura)
        context_snapshot TEXT
    );

    CREATE INDEX IF NOT EXISTS idx_ote_cand_asset_status ON ote_candidates(asset, status);
    CREATE INDEX IF NOT EXISTS idx_ote_cand_zone ON ote_candidates(zone_ref);
    CREATE INDEX IF NOT EXISTS idx_ote_sig_asset_status ON ote_signals(asset, status);
    """)
    conn.commit()


# ============================================================
# Candidates — neutrale, senza direzione
# ============================================================

def insert_candidate(conn, asset: str, zone: dict, liq_above: dict,
                     liq_below: dict, proximity_points: float,
                     reaction_map_score: float = None,
                     ctx_regime: str = None, ctx_session: str = None) -> str:
    """Registra una situazione osservata — neutra, senza direzione."""
    cid = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    conn.execute("""
        INSERT INTO ote_candidates (
            candidate_id, asset, status, zone_ref, zone_high, zone_low,
            zone_score, zone_strength, zone_recurrence, zone_failed,
            liq_above_type, liq_above_price, liq_above_dist,
            liq_below_type, liq_below_price, liq_below_dist,
            reaction_map_score, proximity_points,
            ctx_regime, ctx_session, created_at
        ) VALUES (?,?,'WATCHING',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (cid, asset, zone.get("zone_ref"),
          zone.get("zone_high"), zone.get("zone_low"),
          zone.get("restart_score"), zone.get("zone_strength"),
          zone.get("confirmed_restarts", 0), zone.get("failed_visits", 0),
          (liq_above or {}).get("type"), (liq_above or {}).get("price"), (liq_above or {}).get("distance"),
          (liq_below or {}).get("type"), (liq_below or {}).get("price"), (liq_below or {}).get("distance"),
          reaction_map_score, proximity_points,
          ctx_regime, ctx_session, now))
    conn.commit()
    return cid


def has_active_candidate(conn, asset: str, zone_ref: str) -> bool:
    """Una zona puo' avere UN solo candidate/signal attivo — dedup completo."""
    # Candidate ancora in osservazione
    row = conn.execute(
        "SELECT 1 FROM ote_candidates WHERE asset=? AND zone_ref=? AND status IN ('WATCHING','TOUCHED','SIGNAL_CREATED')",
        (asset, zone_ref)).fetchone()
    if row:
        return True
    # Signal ancora aperto (il candidate e' gia' SIGNAL_CREATED ma il trade non e' chiuso)
    row2 = conn.execute(
        "SELECT 1 FROM ote_signals WHERE asset=? AND zone_ref=? AND status='ENTRY'",
        (asset, zone_ref)).fetchone()
    return row2 is not None


def get_watching_candidates(conn, asset: str) -> list:
    """Tutti i candidate attivi per un asset."""
    rows = conn.execute("""
        SELECT candidate_id, zone_ref, zone_high, zone_low, zone_score, zone_strength,
               liq_above_type, liq_above_price, liq_below_type, liq_below_price,
               proximity_points, created_at
        FROM ote_candidates WHERE asset=? AND status IN ('WATCHING','TOUCHED')
        ORDER BY created_at
    """, (asset,)).fetchall()
    return [dict(zip([
        "candidate_id","zone_ref","zone_high","zone_low","zone_score","zone_strength",
        "liq_above_type","liq_above_price","liq_below_type","liq_below_price",
        "proximity_points","created_at"], r)) for r in rows]


def update_candidate_touch(conn, candidate_id: str):
    now = datetime.now(timezone.utc).isoformat()
    conn.execute("UPDATE ote_candidates SET status='TOUCHED', touched_at=? WHERE candidate_id=?",
                 (now, candidate_id))
    conn.commit()


def update_candidate_sweep(conn, candidate_id: str, direction: str, timestamp: str = None):
    ts = timestamp or datetime.now(timezone.utc).isoformat()
    conn.execute("""UPDATE ote_candidates SET sweep_detected=1, sweep_direction=?, sweep_timestamp=?
                    WHERE candidate_id=?""", (direction, ts, candidate_id))
    conn.commit()


def update_candidate_reaction(conn, candidate_id: str, direction: str, timestamp: str = None):
    ts = timestamp or datetime.now(timezone.utc).isoformat()
    conn.execute("""UPDATE ote_candidates SET reaction_detected=1, reaction_direction=?, reaction_timestamp=?
                    WHERE candidate_id=?""", (direction, ts, candidate_id))
    conn.commit()


def expire_candidate(conn, candidate_id: str):
    now = datetime.now(timezone.utc).isoformat()
    conn.execute("UPDATE ote_candidates SET status='EXPIRED', expired_at=? WHERE candidate_id=?",
                 (now, candidate_id))
    conn.commit()


def link_candidate_to_signal(conn, candidate_id: str, signal_id: str):
    conn.execute("UPDATE ote_candidates SET status='SIGNAL_CREATED', signal_id=? WHERE candidate_id=?",
                 (signal_id, candidate_id))
    conn.commit()


# ============================================================
# Signals — direzione assegnata SOLO dopo sweep+reaction
# ============================================================

def insert_signal(conn, candidate_id: str, asset: str, direction: str,
                  signal_data: dict) -> str:
    sid = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    conn.execute("""
        INSERT INTO ote_signals (
            signal_id, candidate_id, asset, direction, status,
            planned_entry, planned_sl, planned_tp, planned_rr,
            trigger_type, sweep_level, reaction_type,
            zone_ref, zone_score, zone_strength,
            quality_score, quality_label, tp_type, tp_ref,
            signal_created_at, context_snapshot
        ) VALUES (?,?,?,?,'ENTRY',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (sid, candidate_id, asset, direction,
          signal_data["planned_entry"], signal_data["planned_sl"],
          signal_data["planned_tp"], signal_data["planned_rr"],
          signal_data.get("trigger_type"), signal_data.get("sweep_level"),
          signal_data.get("reaction_type"),
          signal_data.get("zone_ref"), signal_data.get("zone_score"),
          signal_data.get("zone_strength"),
          signal_data.get("quality_score"), signal_data.get("quality_label"),
          signal_data.get("tp_type"), signal_data.get("tp_ref"),
          now, signal_data.get("context_snapshot")))
    conn.commit()
    return sid


def close_signal(conn, signal_id: str, status: str, result_r: float = None,
                 mae: float = None, mfe: float = None, bars_open: int = None):
    result = "WIN" if status == "TP" else ("LOSS" if status == "SL" else None)
    now = datetime.now(timezone.utc).isoformat()
    conn.execute("""UPDATE ote_signals SET status=?, result=?, result_r=?, mae=?, mfe=?,
                    bars_open=?, closed_at=? WHERE signal_id=?""",
                 (status, result, result_r, mae, mfe, bars_open, now, signal_id))
    conn.commit()


def get_open_signals(conn, asset: str) -> list:
    rows = conn.execute("""
        SELECT signal_id, candidate_id, direction, planned_entry, planned_sl, planned_tp,
               planned_rr, actual_entry, actual_sl, actual_tp, mae, mfe, bars_open, expiry_bars
        FROM ote_signals WHERE asset=? AND status='ENTRY'
    """, (asset,)).fetchall()
    return [dict(zip([
        "signal_id","candidate_id","direction","planned_entry","planned_sl","planned_tp",
        "planned_rr","actual_entry","actual_sl","actual_tp","mae","mfe","bars_open","expiry_bars"], r)) for r in rows]
