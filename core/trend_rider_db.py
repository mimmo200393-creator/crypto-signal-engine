"""
core/trend_rider_db.py
NMC Trend Rider Balanced — Layer accesso dati

Tabella: trb_signals

── NOVITA': statistiche per Entry Zone ───────────────────────────
La strategia calcola gia' quale zona ha generato il segnale
(ema / order_block / fvg / support_resistance) in _in_entry_zone(),
ma finora quel dato veniva scartato: finiva nelle diagnostics e non
veniva mai salvato. Ora la colonna entry_zone_type lo persiste, cosi'
si puo' misurare QUALI zone hanno davvero edge, non solo il WR globale.

ATTENZIONE metodologica: le statistiche per zona sono affidabili solo
sopra un campione minimo (vedi MIN_SAMPLE_PER_ZONE). Sotto quella soglia
i numeri sono rumore e non vanno usati per decidere. La colonna va
popolata da subito comunque: ogni trade non registrato oggi e' un dato
perso per sempre (i 25 trade precedenti hanno entry_zone_type = NULL,
non recuperabile).
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional

import pandas as pd


# Soglia minima di trade CHIUSI per considerare affidabili le statistiche
# di una zona. Sotto questo numero: raccogliere, non concludere.
MIN_SAMPLE_PER_ZONE = 25


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS trb_signals (
    signal_id            TEXT PRIMARY KEY,
    strategy_name        TEXT NOT NULL DEFAULT 'TRB',
    strategy_version     TEXT NOT NULL DEFAULT 'v1.0',
    asset                TEXT NOT NULL,
    direction            TEXT NOT NULL CHECK(direction IN ('BUY','SELL')),
    timestamp_setup      DATETIME NOT NULL,
    timestamp_closed     DATETIME,

    entry                REAL NOT NULL,
    stop_loss            REAL NOT NULL,
    tp1                  REAL,
    tp2                  REAL,
    risk                 REAL,
    rr1                  REAL DEFAULT 1.0,
    rr2                  REAL,

    trend_h1             TEXT,
    trend_h4             TEXT,
    adx                  REAL,
    atr_m15              REAL,
    atr_h1               REAL,
    pullback_valid       BOOLEAN DEFAULT 0,
    new_24h_extreme      BOOLEAN DEFAULT 0,
    session              TEXT,

    -- NUOVO: quale Entry Zone ha generato il segnale.
    -- Valori: 'ema' | 'order_block' | 'fvg' | 'support_resistance'
    entry_zone_type      TEXT,
    zone_ref             TEXT,
    flag_adx_ok          BOOLEAN DEFAULT 0,
    flag_trigger_present BOOLEAN DEFAULT 0,
    flag_volatility_ok   BOOLEAN DEFAULT 1,
    flag_sl_widened      BOOLEAN DEFAULT 0,

    liquidity_target       TEXT,
    liquidity_target_price REAL,
    liquidity_priority     TEXT,

    quality_score        INTEGER,
    quality_label        TEXT CHECK(quality_label IN ('LOW','MEDIUM','HIGH','PREMIUM')),

    final_outcome        TEXT DEFAULT 'OPEN'
        CHECK(final_outcome IN ('OPEN','TP1_HIT','TP2_HIT','SL_HIT','EXPIRED')),
    tp1_hit              BOOLEAN DEFAULT 0,
    tp2_hit              BOOLEAN DEFAULT 0,
    mae                  REAL DEFAULT 0,
    mfe                  REAL DEFAULT 0,
    bars_open            INTEGER DEFAULT 0,
    expiry_bars          INTEGER DEFAULT 96,
    timestamp_tp1        DATETIME,
    timestamp_tp2        DATETIME,
    timestamp_sl         DATETIME
);

CREATE INDEX IF NOT EXISTS idx_trb_asset_outcome
    ON trb_signals(asset, final_outcome);
CREATE INDEX IF NOT EXISTS idx_trb_timestamp
    ON trb_signals(timestamp_setup);
CREATE INDEX IF NOT EXISTS idx_trb_quality
    ON trb_signals(quality_label);
"""


def init_trb_schema(conn: sqlite3.Connection):
    conn.executescript(SCHEMA_SQL)
    _migrate_add_entry_zone(conn)
    conn.commit()


def _migrate_add_entry_zone(conn: sqlite3.Connection):
    """
    Garantisce colonna entry_zone_type + indice, sia sui DB gia' esistenti
    (ALTER) sia sui nuovi (dove la colonna e' gia' nello schema ma l'indice
    no, perche' spostato qui per non fallire sui DB vecchi).
    Idempotente: sicuro chiamarlo ad ogni avvio.
    """
    cols = [r[1] for r in conn.execute("PRAGMA table_info(trb_signals)")]
    if "entry_zone_type" not in cols:
        conn.execute("ALTER TABLE trb_signals ADD COLUMN entry_zone_type TEXT")
    if "zone_ref" not in cols:
        conn.execute("ALTER TABLE trb_signals ADD COLUMN zone_ref TEXT")
    for fcol, fdef in [("flag_adx_ok","0"),("flag_trigger_present","0"),
                       ("flag_volatility_ok","1"),("flag_sl_widened","0")]:
        if fcol not in cols:
            conn.execute(f"ALTER TABLE trb_signals ADD COLUMN {fcol} BOOLEAN DEFAULT {fdef}")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_trb_zone "
        "ON trb_signals(entry_zone_type, final_outcome)"
    )
    conn.commit()


def insert_trb_signal(conn: sqlite3.Connection, signal: dict) -> str:
    signal_id = signal.get("signal_id") or str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO trb_signals (
            signal_id, strategy_name, strategy_version,
            asset, direction, timestamp_setup,
            entry, stop_loss, tp1, tp2, risk, rr1, rr2,
            trend_h1, trend_h4, adx, atr_m15, atr_h1,
            pullback_valid, new_24h_extreme, session,
            entry_zone_type, zone_ref,
            flag_adx_ok, flag_trigger_present, flag_volatility_ok, flag_sl_widened,
            liquidity_target, liquidity_target_price, liquidity_priority,
            quality_score, quality_label,
            final_outcome, expiry_bars
        ) VALUES (
            ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
        )
        """,
        (
            signal_id,
            signal.get("strategy_name", "TRB"),
            signal.get("strategy_version", "v1.0"),
            signal["asset"],
            signal["direction"],
            signal["timestamp_setup"],
            signal["entry"],
            signal["stop_loss"],
            signal.get("tp1"),
            signal.get("tp2"),
            signal.get("risk"),
            signal.get("rr1", 1.0),
            signal.get("rr2"),
            signal.get("trend_h1"),
            signal.get("trend_h4"),
            signal.get("adx"),
            signal.get("atr_m15"),
            signal.get("atr_h1"),
            bool(signal.get("pullback_valid", False)),
            bool(signal.get("new_24h_extreme", False)),
            signal.get("session"),
            # NUOVO: la zona che ha generato il segnale. Il runner deve
            # metterla in signal['entry_zone_type'] (vedi patch trend_rider).
            signal.get("entry_zone_type"),
            signal.get("zone_ref"),
            bool(signal.get("flag_adx_ok", False)),
            bool(signal.get("flag_trigger_present", False)),
            bool(signal.get("flag_volatility_ok", True)),
            bool(signal.get("flag_sl_widened", False)),
            signal.get("liquidity_target"),
            signal.get("liquidity_target_price"),
            signal.get("liquidity_priority"),
            signal.get("quality_score"),
            signal.get("quality_label"),
            "OPEN",
            signal.get("expiry_bars", 96),
        ),
    )
    conn.commit()
    return signal_id


def get_zone_statistics(conn: sqlite3.Connection,
                        asset: Optional[str] = None) -> list[dict]:
    """
    Statistiche aggregate per tipo di Entry Zone, sui trade CHIUSI.

    Ritorna una riga per zona con:
      - trades          : quanti trade chiusi (il campione)
      - reliable        : True se trades >= MIN_SAMPLE_PER_ZONE
      - win_rate        : % di TP (TP1_HIT o TP2_HIT) sui chiusi
      - avg_rr2         : RR2 medio pianificato
      - avg_mfe         : max escursione favorevole media (il prezzo ha
                          "reagito" dalla zona?)
      - avg_mae         : max escursione avversa media
      - avg_bars_to_close: durata media in barre M15

    IMPORTANTE: usare solo le righe con reliable=True per decidere.
    Le altre sono in raccolta: mostrarle serve a vedere l'accumulo, non
    a trarre conclusioni.
    """
    where = "WHERE final_outcome IN ('TP1_HIT','TP2_HIT','SL_HIT','EXPIRED')"
    params: list = []
    if asset:
        where += " AND asset = ?"
        params.append(asset)

    rows = conn.execute(
        f"""
        SELECT
            COALESCE(entry_zone_type, 'UNKNOWN')          AS zone,
            COUNT(*)                                       AS trades,
            SUM(CASE WHEN final_outcome IN ('TP1_HIT','TP2_HIT')
                     THEN 1 ELSE 0 END)                    AS wins,
            AVG(rr2)                                       AS avg_rr2,
            AVG(mfe)                                       AS avg_mfe,
            AVG(mae)                                       AS avg_mae,
            AVG(bars_open)                                 AS avg_bars
        FROM trb_signals
        {where}
        GROUP BY zone
        ORDER BY trades DESC
        """,
        params,
    ).fetchall()

    out = []
    for zone, trades, wins, avg_rr2, avg_mfe, avg_mae, avg_bars in rows:
        out.append({
            "zone": zone,
            "trades": trades,
            "reliable": trades >= MIN_SAMPLE_PER_ZONE,
            "win_rate": round(100.0 * wins / trades, 1) if trades else 0.0,
            "wins": wins,
            "losses": trades - wins,
            "avg_rr2": round(avg_rr2, 2) if avg_rr2 is not None else None,
            "avg_mfe": round(avg_mfe, 4) if avg_mfe is not None else None,
            "avg_mae": round(avg_mae, 4) if avg_mae is not None else None,
            "avg_bars_to_close": round(avg_bars, 1) if avg_bars is not None else None,
        })
    return out


def get_open_zone_refs(conn: sqlite3.Connection, asset: str) -> set:
    """
    Insieme delle configurazioni gia' segnalate e ancora OPEN per un asset,
    nel formato "{asset}|{direction}|{zone_ref}".

    Serve alla regola "una configurazione = un segnale": il runner la legge
    una volta per scan e la passa in market_ctx['open_zone_refs'], cosi' la
    strategia non ri-notifica una zona che ha gia' un segnale aperto.

    Nota: usa la colonna zone_ref. Se non esiste ancora (DB pre-migrazione),
    ritorna set vuoto (nessuna dedup, fail-open).
    """
    cols = [r[1] for r in conn.execute("PRAGMA table_info(trb_signals)")]
    if "zone_ref" not in cols:
        return set()

    rows = conn.execute(
        """
        SELECT asset, direction, zone_ref
        FROM trb_signals
        WHERE asset = ? AND final_outcome = 'OPEN' AND zone_ref IS NOT NULL
        """,
        (asset,),
    ).fetchall()
    return {f"{a}|{d}|{z}" for a, d, z in rows}


def has_recent_trb_signal(
    conn: sqlite3.Connection,
    asset: str,
    direction: str,
    entry_price: float,
    hours: int = 4,
) -> bool:
    """
    Ritorna True se esiste già un segnale con:
    - stesso asset e direzione
    - entry price entro 1.0 punto (BTC) o 0.5 punto (PAXG)
    - generato nelle ultime N ore

    Previene duplicati quando lo stesso setup viene trovato
    in scan consecutivi con la stessa candela trigger.
    """
    cutoff    = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    tolerance = 1.0  # punti di tolleranza sull'entry

    row = conn.execute(
        """
        SELECT 1 FROM trb_signals
        WHERE asset = ?
          AND direction = ?
          AND ABS(entry - ?) <= ?
          AND timestamp_setup >= ?
        LIMIT 1
        """,
        (asset, direction, entry_price, tolerance, cutoff),
    ).fetchone()
    return row is not None


def has_open_trb_signal(
    conn: sqlite3.Connection,
    asset: str,
    direction: str,
) -> bool:
    row = conn.execute(
        """
        SELECT 1 FROM trb_signals
        WHERE asset=? AND direction=? AND final_outcome='OPEN'
        LIMIT 1
        """,
        (asset, direction),
    ).fetchone()
    return row is not None


def monitor_open_trb_signals(
    conn: sqlite3.Connection,
    asset: str,
    current_high: float,
    current_low: float,
    now_iso: str,
) -> list[dict]:
    rows = conn.execute(
        """
        SELECT signal_id, direction, entry, stop_loss, tp1, tp2,
               mae, mfe, bars_open, expiry_bars, tp1_hit, tp2_hit
        FROM trb_signals
        WHERE final_outcome = 'OPEN' AND asset = ?
        """,
        (asset,),
    ).fetchall()

    updated = []

    for row in rows:
        sid, direction, entry, sl, tp1, tp2, mae, mfe, bars_open, expiry_bars, tp1_hit, tp2_hit = row

        if entry is None or sl is None:
            continue

        bars_open = (bars_open or 0) + 1

        if direction == "BUY":
            adverse   = max(float(entry) - current_low,  0.0)
            favorable = max(current_high - float(entry), 0.0)
        else:
            adverse   = max(current_high - float(entry), 0.0)
            favorable = max(float(entry) - current_low,  0.0)

        new_mae = max(float(mae or 0), adverse)
        new_mfe = max(float(mfe or 0), favorable)

        if direction == "BUY":
            sl_hit      = current_low  <= float(sl)
            tp1_hit_now = tp1 is not None and current_high >= float(tp1)
            tp2_hit_now = tp2 is not None and current_high >= float(tp2)
        else:
            sl_hit      = current_high >= float(sl)
            tp1_hit_now = tp1 is not None and current_low  <= float(tp1)
            tp2_hit_now = tp2 is not None and current_low  <= float(tp2)

        new_tp1_hit = bool(tp1_hit) or tp1_hit_now
        new_tp2_hit = bool(tp2_hit) or tp2_hit_now

        # SL priorità su TP
        if sl_hit:
            outcome = "SL_HIT"
        elif new_tp2_hit:
            outcome = "TP2_HIT"
        elif new_tp1_hit:
            outcome = "TP1_HIT"
        elif bars_open >= (expiry_bars or 96):
            outcome = "EXPIRED"
        else:
            outcome = None

        updates = ["mae = ?", "mfe = ?", "bars_open = ?", "tp1_hit = ?", "tp2_hit = ?"]
        params  = [new_mae, new_mfe, bars_open, new_tp1_hit, new_tp2_hit]

        if outcome and outcome != "TP1_HIT":
            updates += ["final_outcome = ?", "timestamp_closed = ?"]
            params  += [outcome, now_iso]
        elif outcome == "TP1_HIT" and not bool(tp1_hit):
            updates += ["timestamp_tp1 = ?"]
            params  += [now_iso]

        params.append(sid)
        conn.execute(
            f"UPDATE trb_signals SET {', '.join(updates)} WHERE signal_id = ?",
            params,
        )

    conn.commit()

    for row in rows:
        sid = row[0]
        updated_row = conn.execute(
            "SELECT signal_id, final_outcome, mae, mfe, bars_open FROM trb_signals WHERE signal_id=?",
            (sid,)
        ).fetchone()
        if updated_row and updated_row[1] != "OPEN":
            updated.append({
                "signal_id": updated_row[0],
                "outcome":   updated_row[1],
                "mae":       updated_row[2],
                "mfe":       updated_row[3],
                "bars_open": updated_row[4],
            })

    return updated
