"""
core/lh_db.py
Liquidity Hunter — Layer accesso dati

Tabella: lh_signals
"""

from __future__ import annotations

import json
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
    direction                TEXT NOT NULL CHECK(direction IN ('BUY','SELL')),
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
    tp_priority              TEXT,

    quality_score            INTEGER,
    quality_label            TEXT CHECK(quality_label IN ('LOW','MEDIUM','HIGH')),

    final_outcome            TEXT DEFAULT 'OPEN'
        CHECK(final_outcome IN ('OPEN','TP','SL','EXPIRED')),
    mae                      REAL DEFAULT 0,
    mfe                      REAL DEFAULT 0,
    bars_open                INTEGER DEFAULT 0,
    expiry_bars              INTEGER DEFAULT 96
);

CREATE INDEX IF NOT EXISTS idx_lh_asset_outcome
    ON lh_signals(asset, final_outcome);
CREATE INDEX IF NOT EXISTS idx_lh_timestamp
    ON lh_signals(timestamp_setup);
CREATE INDEX IF NOT EXISTS idx_lh_level
    ON lh_signals(swept_level_label, swept_level_priority);

-- Zone Scanner (v3.2): alert INFORMATIVI di prossimita' a una zona
-- interessante, separati dai trade veri e propri. Nessun entry/SL/TP
-- operativo, nessun outcome da monitorare — solo dedup per non ripetere
-- lo stesso alert sulla stessa zona a ogni ciclo di scan.
CREATE TABLE IF NOT EXISTS lh_zone_alerts (
    alert_id        TEXT PRIMARY KEY,
    asset           TEXT NOT NULL,
    direction       TEXT NOT NULL,
    tier            TEXT NOT NULL DEFAULT 'WATCH',
    zone_kind       TEXT,
    zone_ref        TEXT,
    zone_high       REAL,
    zone_low        REAL,
    distance_atr    REAL,
    distance_points REAL,
    zone_width      REAL,
    m5_refined      BOOLEAN DEFAULT 0,
    restart_score   REAL,
    zone_strength   TEXT,
    confirmations   TEXT,
    created_at      DATETIME NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_lh_zone_alerts_lookup
    ON lh_zone_alerts(asset, direction, zone_ref, tier, created_at);

-- Ricorrenza (v3.7): stato persistente per zona -- non "quante volte
-- toccata" ma "quante volte da qui e' REALMENTE ripartito un impulso".
-- Una riga per zone_ref, aggiornata ogni ciclo (macchina a stati pura
-- in liquidity_hunter.py, qui solo la persistenza).
CREATE TABLE IF NOT EXISTS lh_zone_recurrence (
    zone_ref        TEXT PRIMARY KEY,
    asset           TEXT NOT NULL,
    direction       TEXT NOT NULL,
    zone_kind       TEXT NOT NULL,
    zone_high       REAL,
    zone_low        REAL,
    visits          INTEGER DEFAULT 0,
    confirmed_restarts INTEGER DEFAULT 0,
    failed_visits   INTEGER DEFAULT 0,
    price_inside    BOOLEAN DEFAULT 0,
    awaiting_confirmation BOOLEAN DEFAULT 0,
    confirmation_bars_remaining INTEGER DEFAULT 0,
    entry_ts        TEXT,
    status          TEXT DEFAULT 'ACTIVE',
    is_virgin       BOOLEAN DEFAULT 1,
    restart_displacements TEXT DEFAULT '[]',
    first_seen_ts   TEXT NOT NULL,
    last_updated_ts TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_lh_zone_recurrence_asset
    ON lh_zone_recurrence(asset, status);

-- Memoria storica swing H4/D1 (v3.12) -- FASE 1: solo raccolta dati.
-- Persistente per sempre, nessuna scadenza per eta' (a differenza di
-- lh_zone_alerts/lh_zone_recurrence). swing_ref e' stabile (asset +
-- timeframe + tipo + timestamp candela), quindi INSERT OR IGNORE
-- garantisce idempotenza tra backfill iniziale e refresh incrementale.
CREATE TABLE IF NOT EXISTS lh_swing_zones (
    swing_ref     TEXT PRIMARY KEY,
    asset         TEXT NOT NULL,
    timeframe     TEXT NOT NULL,
    swing_type    TEXT NOT NULL,
    price         REAL NOT NULL,
    zone_high     REAL NOT NULL,
    zone_low      REAL NOT NULL,
    formation_ts  INTEGER NOT NULL,
    discovered_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_lh_swing_zones_lookup
    ON lh_swing_zones(asset, timeframe, formation_ts);
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
                     ("session", "TEXT"),
                     # --- LH v3.2 fix ---
                     # sl_original: SL del segnale COSI' COM'E' NATO, scritto
                     # una sola volta all'insert e MAI PIU' aggiornato.
                     # stop_loss invece continua a essere spostato dal
                     # breakeven per la gestione reale del trade (il monitor
                     # lo usa per decidere quando chiudere) — ma questo
                     # significa che dopo un breakeven stop_loss non riflette
                     # piu' il rischio originale del segnale che l'utente ha
                     # ricevuto su Telegram. sl_original resta la fonte di
                     # verita' per dashboard/analisi storica: "qual era lo
                     # stop del segnale quando e' stato emesso".
                     ("sl_original", "REAL")]:
        if col not in cols:
            conn.execute(f"ALTER TABLE lh_signals ADD COLUMN {col} {typ}")
    conn.commit()


def _migrate_zone_alerts(conn: sqlite3.Connection):
    """
    Migrazione per lh_zone_alerts -- MANCAVA, causa dell'errore
    "no column named zone_width" in produzione (07/08). La tabella era
    stata creata da un deploy precedente con lo schema piu' vecchio
    (v3.4: confluence_score/reaction_strength/sources/has_order_block);
    CREATE TABLE IF NOT EXISTS non aggiunge colonne a una tabella gia'
    esistente -- serve un ALTER TABLE esplicito, stesso schema gia'
    usato per lh_signals in _migrate_lh_flags qui sopra.

    Copre TUTTE le colonne introdotte nelle evoluzioni di oggi, cosi'
    funziona indipendentemente da quale versione storica del deploy ha
    creato la tabella per prima.
    """
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(lh_zone_alerts)")]
    except sqlite3.OperationalError:
        return  # tabella non ancora creata, ci pensa CREATE TABLE IF NOT EXISTS
    for col, typ in [
        ("tier", "TEXT NOT NULL DEFAULT 'WATCH'"),
        # v3.4 originale
        ("confluence_score", "REAL"),
        ("reaction_strength", "TEXT"),
        ("sources", "TEXT"),
        ("has_order_block", "BOOLEAN DEFAULT 0"),
        # v3.5/3.6/3.7 -- rinominato/esteso
        ("zone_width", "REAL"),
        ("m5_refined", "BOOLEAN DEFAULT 0"),
        ("restart_score", "REAL"),
        ("zone_strength", "TEXT"),
        ("confirmations", "TEXT"),
    ]:
        if col not in cols:
            try:
                conn.execute(f"ALTER TABLE lh_zone_alerts ADD COLUMN {col} {typ}")
            except sqlite3.OperationalError:
                pass  # gia' presente o altra race, non bloccante
    conn.commit()


def _migrate_zone_recurrence(conn: sqlite3.Connection):
    """
    Migrazione per lh_zone_recurrence (v3.10: is_virgin, restart_displacements).
    Stessa lezione di lh_zone_alerts -- se non migro esplicitamente,
    un deploy su un DB con la tabella gia' esistente lascia le colonne
    nuove mancanti e fallisce al primo insert.
    """
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(lh_zone_recurrence)")]
    except sqlite3.OperationalError:
        return
    for col, typ in [
        ("is_virgin", "BOOLEAN DEFAULT 1"),
        ("restart_displacements", "TEXT DEFAULT '[]'"),
    ]:
        if col not in cols:
            try:
                conn.execute(f"ALTER TABLE lh_zone_recurrence ADD COLUMN {col} {typ}")
            except sqlite3.OperationalError:
                pass
    conn.commit()


def init_lh_schema(conn: sqlite3.Connection):
    conn.executescript(SCHEMA_SQL)
    _migrate_lh_flags(conn)
    _migrate_zone_alerts(conn)
    _migrate_zone_recurrence(conn)
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
            confluence_factors, order_status, ob_match_type, session,
            sl_original
        ) VALUES (
            ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,
            ?,?,?,?,?,?,?,?,?,?,?,?,?,
            ?
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
            # sl_original = stop_loss al momento della creazione, sempre.
            # Da qui in poi nessuna funzione in questo file lo tocca piu'.
            signal["stop_loss"],
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


# ============================================================
# Zone Scanner (v3.2) — alert informativi, separati dai trade
# ============================================================

def has_recent_zone_alert(
    conn: sqlite3.Connection,
    asset: str,
    direction: str,
    zone_ref: str,
    tier: str = "WATCH",
    hours: int = 4,
) -> bool:
    """
    Dedup per gli alert di zona: stessa zona (zone_ref) + direzione +
    asset + LIVELLO (WATCH o NEAR), notificata nelle ultime N ore ->
    non ripetere. Il tier e' incluso nella chiave: una zona puo' generare
    prima un WATCH (lontana) e poi, quando il prezzo si avvicina, anche
    un NEAR (urgente) senza che l'uno blocchi l'altro.

    Stessa finestra di 4h gia' usata per i trade (has_recent_lh_signal):
    coerenza con il resto del sistema, nessuna nuova costante inventata.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    row = conn.execute(
        """
        SELECT 1 FROM lh_zone_alerts
        WHERE asset=? AND direction=? AND zone_ref=? AND tier=?
        AND created_at >= ?
        LIMIT 1
        """,
        (asset, direction, zone_ref, tier, cutoff),
    ).fetchone()
    return row is not None


def insert_zone_alert(conn: sqlite3.Connection, asset: str, zone: dict,
                       tier: str = "WATCH") -> str:
    """
    Salva un alert di zona (solo per dedup/storico — nessun outcome da
    monitorare, non e' un trade).
    """
    alert_id = str(uuid.uuid4())
    now_iso = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT INTO lh_zone_alerts (
            alert_id, asset, direction, tier, zone_kind, zone_ref,
            zone_high, zone_low, distance_atr, distance_points,
            zone_width, m5_refined, restart_score, zone_strength,
            confirmations, created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            alert_id, asset, zone["direction"], tier, zone.get("zone_kind"),
            zone.get("zone_ref"), zone.get("zone_high"), zone.get("zone_low"),
            zone.get("distance_atr"), zone.get("distance_points"),
            zone.get("zone_width"), bool(zone.get("m5_refined", False)),
            zone.get("restart_score"), zone.get("zone_strength"),
            json.dumps(zone.get("confirmations", [])),
            now_iso,
        ),
    )
    conn.commit()
    return alert_id


# ============================================================
# Ricorrenza (v3.7) -- persistenza dello stato per zona
# ============================================================

def get_zone_recurrence(conn: sqlite3.Connection, zone_ref: str):
    """Ritorna lo stato di ricorrenza per una zona, o None se mai vista prima."""
    row = conn.execute(
        "SELECT * FROM lh_zone_recurrence WHERE zone_ref=?", (zone_ref,)
    ).fetchone()
    if row is None:
        return None
    cols = [d[0] for d in conn.execute(
        "SELECT * FROM lh_zone_recurrence WHERE zone_ref=?", (zone_ref,)
    ).description]
    state = dict(zip(cols, row))
    # SQLite salva i booleani come 0/1 -- riconverto per la macchina a stati
    state["price_inside"] = bool(state["price_inside"])
    state["awaiting_confirmation"] = bool(state["awaiting_confirmation"])
    state["is_virgin"] = bool(state.get("is_virgin", True))
    try:
        state["restart_displacements"] = json.loads(state.get("restart_displacements") or "[]")
    except Exception:
        state["restart_displacements"] = []
    return state


def upsert_zone_recurrence(conn: sqlite3.Connection, state: dict):
    """Salva (crea o aggiorna) lo stato di ricorrenza per una zona."""
    conn.execute(
        """
        INSERT INTO lh_zone_recurrence (
            zone_ref, asset, direction, zone_kind, zone_high, zone_low,
            visits, confirmed_restarts, failed_visits, price_inside,
            awaiting_confirmation, confirmation_bars_remaining, entry_ts,
            status, is_virgin, restart_displacements,
            first_seen_ts, last_updated_ts
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(zone_ref) DO UPDATE SET
            zone_high=excluded.zone_high, zone_low=excluded.zone_low,
            visits=excluded.visits, confirmed_restarts=excluded.confirmed_restarts,
            failed_visits=excluded.failed_visits, price_inside=excluded.price_inside,
            awaiting_confirmation=excluded.awaiting_confirmation,
            confirmation_bars_remaining=excluded.confirmation_bars_remaining,
            entry_ts=excluded.entry_ts, status=excluded.status,
            is_virgin=excluded.is_virgin,
            restart_displacements=excluded.restart_displacements,
            last_updated_ts=excluded.last_updated_ts
        """,
        (
            state["zone_ref"], state["asset"], state["direction"], state["zone_kind"],
            state["zone_high"], state["zone_low"], state["visits"],
            state["confirmed_restarts"], state["failed_visits"],
            bool(state["price_inside"]), bool(state["awaiting_confirmation"]),
            state["confirmation_bars_remaining"], state.get("entry_ts"),
            state["status"], bool(state.get("is_virgin", True)),
            json.dumps(state.get("restart_displacements", [])),
            state["first_seen_ts"], state["last_updated_ts"],
        ),
    )
    conn.commit()


# ============================================================
# Riepilogo di fine giornata (v3.8) -- selezione zone valide
# ============================================================

def get_zones_for_digest(conn: sqlite3.Connection, asset: str, hours: int = 24) -> list:
    """
    Zone da mostrare nel riepilogo serale: ACTIVE (non invalidate) in
    lh_zone_recurrence, con l'ultimo alert (score/confirmations) entro
    le ultime `hours` ore -- esclude zone vecchie mai piu' toccate.

    Ritorna lista di dict: {zone_kind, zone_high, zone_low, restart_score,
    confirmations, confirmed_restarts, failed_visits}.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    rows = conn.execute(
        """
        SELECT r.zone_ref, r.zone_kind, r.zone_high, r.zone_low,
               r.confirmed_restarts, r.failed_visits,
               a.restart_score, a.confirmations
        FROM lh_zone_recurrence r
        JOIN lh_zone_alerts a ON a.zone_ref = r.zone_ref
        WHERE r.asset = ? AND r.status = 'ACTIVE'
          AND a.created_at = (
              SELECT MAX(a2.created_at) FROM lh_zone_alerts a2
              WHERE a2.zone_ref = r.zone_ref
          )
          AND a.created_at >= ?
        ORDER BY a.restart_score DESC
        """,
        (asset, cutoff),
    ).fetchall()

    zones = []
    for zone_ref, zone_kind, zh, zl, restarts, failures, score, confs_json in rows:
        try:
            confirmations = json.loads(confs_json) if confs_json else []
        except Exception:
            confirmations = []
        zones.append({
            "zone_ref": zone_ref, "zone_kind": zone_kind,
            "zone_high": zh, "zone_low": zl,
            "restart_score": score or 0, "confirmations": confirmations,
            "confirmed_restarts": restarts, "failed_visits": failures,
        })
    return zones


# ============================================================
# Memoria storica swing H4/D1 (v3.12)
# ============================================================

def insert_swings(conn: sqlite3.Connection, swings: list) -> int:
    """
    Salva una lista di swing rilevati. Idempotente: swing_ref e' stabile
    (asset+timeframe+tipo+timestamp candela), INSERT OR IGNORE scarta
    automaticamente quelli gia' presenti -- sicuro chiamarla sia dal
    backfill iniziale sia dal refresh incrementale di ogni ciclo, senza
    doppioni.

    Ritorna quanti swing erano NUOVI (non gia' presenti).
    """
    if not swings:
        return 0
    now_iso = datetime.now(timezone.utc).isoformat()
    inserted = 0
    for s in swings:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO lh_swing_zones (
                swing_ref, asset, timeframe, swing_type, price,
                zone_high, zone_low, formation_ts, discovered_at
            ) VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (
                s["swing_ref"], s["asset"], s["timeframe"], s["swing_type"],
                s["price"], s["zone_high"], s["zone_low"], s["formation_ts"],
                now_iso,
            ),
        )
        if cur.rowcount > 0:
            inserted += 1
    conn.commit()
    return inserted


def count_swings(conn: sqlite3.Connection, asset: str, timeframe: str = None) -> int:
    """Utilita' diagnostica: quanti swing sono gia' salvati."""
    if timeframe:
        row = conn.execute(
            "SELECT COUNT(*) FROM lh_swing_zones WHERE asset=? AND timeframe=?",
            (asset, timeframe),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT COUNT(*) FROM lh_swing_zones WHERE asset=?", (asset,)
        ).fetchone()
    return row[0] if row else 0


def get_swing_zones(conn: sqlite3.Connection, asset: str) -> list:
    """
    Ritorna TUTTI gli swing storici per un asset (nessuna scadenza,
    persistenti per sempre). Usati dal scoring come confluenza: una
    Restart Zone che cade su un vecchio swing H4/D1 ha piu' probabilita'
    di produrre una reazione.
    """
    rows = conn.execute(
        "SELECT swing_ref, asset, timeframe, swing_type, price, zone_high, zone_low, formation_ts "
        "FROM lh_swing_zones WHERE asset=? ORDER BY formation_ts DESC",
        (asset,),
    ).fetchall()
    return [
        {"swing_ref": r[0], "asset": r[1], "timeframe": r[2], "swing_type": r[3],
         "price": r[4], "zone_high": r[5], "zone_low": r[6], "formation_ts": r[7]}
        for r in rows
    ]


def get_all_active_recurrence(conn: sqlite3.Connection, asset: str) -> list:
    """
    Ritorna TUTTE le zone ACTIVE in lh_zone_recurrence per un asset --
    indipendentemente dal fatto che il ciclo corrente le abbia ritrovate
    nella scansione H1/M30 (che ha una finestra di lookback limitata).
    Serve per il monitoraggio persistente: le zone trovate ieri/la
    settimana scorsa devono continuare a essere monitorate se il prezzo
    ci torna, non essere dimenticate perche' uscite dalla finestra.
    """
    rows = conn.execute(
        "SELECT * FROM lh_zone_recurrence WHERE asset=? AND status='ACTIVE'",
        (asset,),
    ).fetchall()
    if not rows:
        return []
    cols = [d[0] for d in conn.execute(
        "SELECT * FROM lh_zone_recurrence WHERE 1=0").description]
    result = []
    for row in rows:
        state = dict(zip(cols, row))
        state["price_inside"] = bool(state.get("price_inside", False))
        state["awaiting_confirmation"] = bool(state.get("awaiting_confirmation", False))
        state["is_virgin"] = bool(state.get("is_virgin", True))
        import json
        try:
            state["restart_displacements"] = json.loads(state.get("restart_displacements") or "[]")
        except Exception:
            state["restart_displacements"] = []
        result.append(state)
    return result
