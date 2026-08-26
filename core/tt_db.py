"""
core/tt_db.py
TT (ex OTE-SC) — Layer accesso dati

Schema pensato per il framework:
    DIRECTION(4H) -> LOCATION(1H) -> LIQUIDITY(1H) -> PREMIUM/DISCOUNT
    -> 15M CONTEXT -> PROXIMITY -> EARLY SIGNAL -> TOUCH -> 5M SWEEP
    -> 5M REACTION -> 5M STRUCTURE -> ENTRY -> DYNAMIC TARGET -> RESULT

Differenza fondamentale rispetto a edge_lab_signals (il vecchio schema
OTE-SC): qui esiste uno stato SETUP persistente, e i
campi Planned (al momento del SIGNAL) sono SEPARATI dai campi Actual
(al momento dell'ENTRY) -- mai sovrascritti, cosi' il backtest puo'
sempre rispondere "cosa avrebbe detto l'algoritmo in quel momento".
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Optional

import pandas as pd

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS tt_signals (
    signal_id                  TEXT PRIMARY KEY,
    asset                      TEXT NOT NULL,
    direction                  TEXT NOT NULL CHECK(direction IN ('BUY','SELL')),

    -- ── Stato: il cuore della macchina a stati ──────────────
    -- Rinominato WAITING_CONFIRMATION -> SETUP il 25/08 (nuovo concept
    -- Expansion/Pullback/Balance/HL-LH): un setup completo in attesa
    -- della sola Confirmation.
    status TEXT NOT NULL DEFAULT 'SETUP' CHECK(status IN (
        'SETUP',
        'ENTRY',
        'INVALIDATED',
        'TP', 'SL', 'EXPIRED'
    )),
    invalidation_reason         TEXT,

    -- ── 4H Direction ─────────────────────────────────────────
    direction_4h                TEXT,
    direction_1h                TEXT,
    direction_30m               TEXT,
    mtf_combination              TEXT,
    swing_range_low             REAL,
    swing_range_high            REAL,
    last_bos_price              REAL,
    last_bos_ts                 INTEGER,

    -- ── H1 Location (nuovo concept 25/08: HL/LH, non piu' Demand/Supply) ──
    poi_type                    TEXT,
    poi_high                    REAL,
    poi_low                     REAL,
    poi_quality                 INTEGER,
    poi_ref                     TEXT,
    expansion_start_price       REAL,
    expansion_end_price         REAL,
    expansion_size_atr          REAL,
    balance_detected            BOOLEAN DEFAULT 0,

    -- ── 1H Liquidity ──────────────────────────────────────────
    liquidity_type               TEXT,
    liquidity_level              REAL,
    liquidity_direction          TEXT,
    liquidity_distance_pct       REAL,
    sweep_target_level           REAL,

    -- ── Premium / Discount ────────────────────────────────────
    pd_zone                      TEXT,
    pd_pct                       REAL,

    -- ── 15M Intermediate Context ──────────────────────────────
    ctx_15m_structure            TEXT,
    ctx_15m_momentum             TEXT,
    ctx_15m_note                 TEXT,

    -- ── Proximity / Early Signal ──────────────────────────────
    proximity_points             REAL,
    signal_created_at            DATETIME NOT NULL,

    -- ── PLANNED (fissati al momento del SIGNAL, MAI sovrascritti) ──
    planned_entry                REAL NOT NULL,
    planned_sl                   REAL NOT NULL,
    planned_tp                   REAL NOT NULL,
    planned_rr                   REAL NOT NULL,
    planned_tp_type              TEXT,
    planned_tp_ref               TEXT,

    -- ── ACTUAL (popolati al momento dell'ENTRY, se diversi) ────
    actual_entry                 REAL,
    actual_sl                    REAL,
    actual_tp                    REAL,
    actual_rr                    REAL,

    -- ── 5M Execution ────────────────────────────────────────────
    touch_ts                     DATETIME,
    sweep_5m_confirmed           BOOLEAN DEFAULT 0,
    sweep_5m_level                REAL,
    reaction_5m_type              TEXT,
    structure_5m_confirmed        BOOLEAN DEFAULT 0,
    structure_5m_broken_level     REAL,
    entry_ts                      DATETIME,
    setup_type                    TEXT DEFAULT 'STRUCTURE_PULLBACK' CHECK(setup_type IN ('AGGRESSIVE','CONSERVATIVE','STRUCTURE_PULLBACK')),

    -- ── Quality / Score ─────────────────────────────────────────
    quality_score                 INTEGER,
    quality_label                  TEXT CHECK(quality_label IN ('HIGH','MEDIUM','LOW')),

    -- ── Esito ────────────────────────────────────────────────────
    result                        TEXT,
    result_r                      REAL,
    closed_at                     DATETIME,
    mae                           REAL,
    mfe                           REAL,
    bars_waiting                  INTEGER DEFAULT 0,
    bars_open                     INTEGER DEFAULT 0,
    expiry_bars_waiting           INTEGER DEFAULT 24,
    expiry_bars_open              INTEGER DEFAULT 96,

    -- ── Snapshot completo per debug/backtest ─────────────────────
    context_snapshot               TEXT
);

CREATE INDEX IF NOT EXISTS idx_tt_asset_status
    ON tt_signals(asset, status);
CREATE INDEX IF NOT EXISTS idx_tt_poi_ref
    ON tt_signals(asset, direction, poi_ref);
CREATE INDEX IF NOT EXISTS idx_tt_created
    ON tt_signals(signal_created_at);
"""


def _migrate_tt_signals_if_needed(conn: sqlite3.Connection):
    """
    Migrazione di sicurezza -- bug reale trovato in produzione il 26/08:
    "table tt_signals has no column named direction_1h". CREATE TABLE
    IF NOT EXISTS non aggiunge colonne a una tabella gia' esistente con
    lo schema vecchio (pre 25/08, senza direction_1h/direction_30m/
    mtf_combination/expansion_*/balance_detected), e SQLite non permette
    di modificare un CHECK constraint via ALTER TABLE (lo status vecchio
    accettava solo 'WAITING_CONFIRMATION', non 'SETUP' -- un secondo
    errore sarebbe scattato subito dopo aver corretto solo le colonne).

    Se la tabella esiste ma le manca anche una sola colonna nuova, la
    ricrea da zero: backup dei dati esistenti in tt_signals_pre_migration
    (mai cancellato, per sicurezza), drop, create con lo schema attuale,
    ripristino di tutte le colonne compatibili per nome. Se la tabella
    non esiste ancora, non fa nulla (la crea normalmente init_tt_schema).
    """
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='tt_signals'"
    ).fetchone()
    if row is None:
        return  # non esiste ancora, niente da migrare

    existing_cols = {r[1] for r in conn.execute("PRAGMA table_info(tt_signals)").fetchall()}
    required_new_cols = {"direction_1h", "direction_30m", "mtf_combination",
                         "expansion_start_price", "expansion_end_price",
                         "expansion_size_atr", "balance_detected"}

    if required_new_cols.issubset(existing_cols):
        return  # schema gia' aggiornato, niente da fare

    logger_migration = logging.getLogger("tt_db.migration")
    logger_migration.warning(
        "tt_signals ha lo schema vecchio (mancano: %s) -- migrazione in corso.",
        required_new_cols - existing_cols,
    )

    conn.execute("DROP TABLE IF EXISTS tt_signals_pre_migration")
    conn.execute("ALTER TABLE tt_signals RENAME TO tt_signals_pre_migration")
    conn.executescript(SCHEMA_SQL)

    new_cols = {r[1] for r in conn.execute("PRAGMA table_info(tt_signals)").fetchall()}
    common_cols = [c for c in existing_cols if c in new_cols]
    col_list = ", ".join(common_cols)
    # Traduzione inline durante la copia: WAITING_CONFIRMATION -> SETUP
    # (rinominato il 25/08, stesso significato). Fatta nella SELECT, non
    # sulla tabella vecchia -- quella ha ancora il SUO vecchio CHECK
    # constraint, che rifiuterebbe 'SETUP' prima ancora di arrivare alla
    # tabella nuova.
    select_cols = ", ".join(
        "CASE WHEN status='WAITING_CONFIRMATION' THEN 'SETUP' ELSE status END"
        if c == "status" else c
        for c in common_cols
    )
    try:
        conn.execute(
            f"INSERT INTO tt_signals ({col_list}) SELECT {select_cols} FROM tt_signals_pre_migration"
        )
        n_migrated = conn.execute("SELECT COUNT(*) FROM tt_signals").fetchone()[0]
        logger_migration.warning(
            "Migrazione completata: %d righe ripristinate. Vecchia tabella conservata in tt_signals_pre_migration.",
            n_migrated,
        )
    except sqlite3.Error as e:
        # Anche in caso di errore nel ripristino dati (es. CHECK violato
        # da un valore di status vecchio non piu' valido), la tabella
        # NUOVA con lo schema corretto resta comunque creata e utilizzabile
        # -- i dati vecchi restano al sicuro in tt_signals_pre_migration
        # per ispezione manuale, non vengono mai persi.
        logger_migration.error(
            "Ripristino dati falliti (%s) -- tabella nuova vuota ma funzionante, "
            "dati vecchi intatti in tt_signals_pre_migration.", e,
        )
    conn.commit()


def init_tt_schema(conn: sqlite3.Connection):
    _migrate_tt_signals_if_needed(conn)
    conn.executescript(SCHEMA_SQL)
    conn.commit()


# ============================================================
# Insert — crea un nuovo Early Signal (status SETUP)
# ============================================================

def insert_tt_signal(conn: sqlite3.Connection, signal: dict) -> str:
    signal_id = signal.get("signal_id") or str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO tt_signals (
            signal_id, asset, direction, status,
            direction_4h, direction_1h, direction_30m, mtf_combination,
            swing_range_low, swing_range_high, last_bos_price, last_bos_ts,
            poi_type, poi_high, poi_low, poi_quality, poi_ref,
            expansion_start_price, expansion_end_price, expansion_size_atr, balance_detected,
            liquidity_type, liquidity_level, liquidity_direction, liquidity_distance_pct, sweep_target_level,
            pd_zone, pd_pct,
            ctx_15m_structure, ctx_15m_momentum, ctx_15m_note,
            proximity_points, signal_created_at,
            planned_entry, planned_sl, planned_tp, planned_rr, planned_tp_type, planned_tp_ref,
            setup_type, quality_score, quality_label,
            expiry_bars_waiting, expiry_bars_open,
            context_snapshot
        ) VALUES (
            ?,?,?,?, ?,?,?,?, ?,?,?,?, ?,?,?,?,?, ?,?,?,?, ?,?,?,?,?, ?,?, ?,?,?, ?,?, ?,?,?,?,?,?, ?,?,?, ?,?, ?
        )
        """,
        (
            signal_id, signal["asset"], signal["direction"], "SETUP",
            signal.get("direction_4h"), signal.get("direction_1h"), signal.get("direction_30m"),
            signal.get("mtf_combination"),
            signal.get("swing_range_low"), signal.get("swing_range_high"),
            signal.get("last_bos_price"), signal.get("last_bos_ts"),
            signal.get("poi_type"), signal.get("poi_high"), signal.get("poi_low"),
            signal.get("poi_quality"), signal.get("poi_ref"),
            signal.get("expansion_start_price"), signal.get("expansion_end_price"),
            signal.get("expansion_size_atr"), signal.get("balance_detected", False),
            signal.get("liquidity_type"), signal.get("liquidity_level"),
            signal.get("liquidity_direction"), signal.get("liquidity_distance_pct"),
            signal.get("sweep_target_level"),
            signal.get("pd_zone"), signal.get("pd_pct"),
            signal.get("ctx_15m_structure"), signal.get("ctx_15m_momentum"), signal.get("ctx_15m_note"),
            signal.get("proximity_points"), signal["signal_created_at"],
            signal["planned_entry"], signal["planned_sl"], signal["planned_tp"], signal["planned_rr"],
            signal.get("planned_tp_type"), signal.get("planned_tp_ref"),
            signal.get("setup_type", "STRUCTURE_PULLBACK"),
            signal.get("quality_score"), signal.get("quality_label"),
            signal.get("expiry_bars_waiting", 24), signal.get("expiry_bars_open", 96),
            json.dumps(signal.get("context_snapshot", {}), default=str),
        ),
    )
    conn.commit()
    return signal_id


# ============================================================
# Query: setup SETUP attivi per asset/direzione/POI
# ============================================================

def get_waiting_signals(conn: sqlite3.Connection, asset: str = None) -> list[dict]:
    q = "SELECT * FROM tt_signals WHERE status = 'SETUP'"
    params = ()
    if asset:
        q += " AND asset = ?"
        params = (asset,)
    rows = conn.execute(q, params).fetchall()
    if not rows:
        return []
    cols = [d[0] for d in conn.execute(
        "SELECT * FROM tt_signals WHERE 1=0").description]
    return [dict(zip(cols, r)) for r in rows]


def has_active_setup_for_poi(conn: sqlite3.Connection, asset: str,
                             direction: str, poi_ref: str) -> bool:
    """
    Una POI = un solo active setup (sez.27, no duplicati). Attivo =
    SETUP o ENTRY (non ancora chiuso).
    """
    row = conn.execute(
        """
        SELECT 1 FROM tt_signals
        WHERE asset=? AND direction=? AND poi_ref=?
          AND status IN ('SETUP', 'ENTRY')
        LIMIT 1
        """,
        (asset, direction, poi_ref),
    ).fetchone()
    return row is not None


def has_active_overlapping_setup(conn: sqlite3.Connection, asset: str,
                                 direction: str, poi_low: float,
                                 poi_high: float, tolerance: float) -> bool:
    """
    Dedup per SOVRAPPOSIZIONE DI PREZZO su setup ancora ATTIVI (SETUP
    o ENTRY), non solo match esatto su poi_ref. Bug trovato il 25/08:
    durante un pullback esteso, ogni nuovo swing HL confermato ha un
    location_ts diverso (quindi poi_ref diverso) anche se fa parte
    della STESSA storia in evoluzione -- has_active_setup_for_poi
    (match esatto) non lo riconosceva, producendo location "nuove"
    ogni volta che il pullback avanzava di un altro swing, gonfiando
    il conteggio di opportunita' distinte (verificato: 4 location
    "diverse" erano in realta' 2 storie, ognuna vista due volte).
    """
    rows = conn.execute(
        """
        SELECT poi_low, poi_high FROM tt_signals
        WHERE asset=? AND direction=? AND status IN ('SETUP', 'ENTRY')
        """,
        (asset, direction),
    ).fetchall()
    for prev_low, prev_high in rows:
        if prev_low is None or prev_high is None:
            continue
        # Sovrapposizione allargata dalla tolleranza (non solo overlap
        # esatto -- due HL a pochi punti di distanza sulla stessa
        # gamba sono la stessa storia, non due storie diverse)
        if (poi_low - tolerance) <= prev_high and prev_low <= (poi_high + tolerance):
            return True
    return False


# ============================================================
# Update: transizioni di stato
# ============================================================

def invalidate_signal(conn: sqlite3.Connection, signal_id: str, reason: str):
    conn.execute(
        """
        UPDATE tt_signals SET status='INVALIDATED', invalidation_reason=?,
               closed_at=?
        WHERE signal_id=?
        """,
        (reason, datetime.now(timezone.utc).isoformat(), signal_id),
    )
    conn.commit()


def confirm_entry(conn: sqlite3.Connection, signal_id: str,
                  actual_entry: float, actual_sl: float, actual_tp: float,
                  touch_ts: str = None, sweep_level: float = None,
                  reaction_type: str = None, structure_level: float = None):
    actual_rr = None
    row = conn.execute(
        "SELECT direction FROM tt_signals WHERE signal_id=?", (signal_id,)
    ).fetchone()
    if row:
        direction = row[0]
        risk = abs(actual_entry - actual_sl)
        reward = abs(actual_tp - actual_entry)
        actual_rr = round(reward / risk, 4) if risk > 0 else None

    conn.execute(
        """
        UPDATE tt_signals SET
            status='ENTRY', entry_ts=?,
            actual_entry=?, actual_sl=?, actual_tp=?, actual_rr=?,
            touch_ts=COALESCE(?, touch_ts),
            sweep_5m_confirmed=1, sweep_5m_level=?,
            reaction_5m_type=?, structure_5m_confirmed=1, structure_5m_broken_level=?
        WHERE signal_id=?
        """,
        (datetime.now(timezone.utc).isoformat(), actual_entry, actual_sl, actual_tp,
         actual_rr, touch_ts, sweep_level, reaction_type, structure_level, signal_id),
    )
    conn.commit()


def close_signal(conn: sqlite3.Connection, signal_id: str, result: str,
                 result_r: float, mae: float = None, mfe: float = None,
                 bars_open: int = None):
    conn.execute(
        """
        UPDATE tt_signals SET status=?, result=?, result_r=?,
               closed_at=?, mae=COALESCE(?,mae), mfe=COALESCE(?,mfe),
               bars_open=COALESCE(?,bars_open)
        WHERE signal_id=?
        """,
        (result, "WIN" if result == "TP" else ("LOSS" if result == "SL" else result),
         result_r, datetime.now(timezone.utc).isoformat(), mae, mfe, bars_open, signal_id),
    )
    conn.commit()


def increment_bars_waiting(conn: sqlite3.Connection, signal_id: str) -> int:
    row = conn.execute(
        "SELECT bars_waiting FROM tt_signals WHERE signal_id=?", (signal_id,)
    ).fetchone()
    new_val = (row[0] or 0) + 1 if row else 1
    conn.execute(
        "UPDATE tt_signals SET bars_waiting=? WHERE signal_id=?",
        (new_val, signal_id),
    )
    conn.commit()
    return new_val
