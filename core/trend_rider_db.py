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
import logging
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
        CHECK(final_outcome IN ('OPEN','TP1_HIT','TP2_HIT','SL_HIT','BE_HIT','EXPIRED')),
    tp1_hit              BOOLEAN DEFAULT 0,
    tp2_hit              BOOLEAN DEFAULT 0,
    mae                  REAL DEFAULT 0,
    mfe                  REAL DEFAULT 0,
    bars_open            INTEGER DEFAULT 0,
    expiry_bars          INTEGER DEFAULT 32,
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


def _migrate_add_be_hit(conn: sqlite3.Connection):
    """
    Aggiunge 'BE_HIT' al vincolo CHECK su final_outcome -- SQLite non
    permette di modificare un CHECK esistente con ALTER TABLE, serve
    ricreare la tabella. Stessa tecnica di sicurezza gia' validata per
    tt_signals: backup prima di ogni modifica, mai perdita dati.
    Idempotente: verifica lo schema reale (non un flag), sicuro
    richiamarlo ad ogni avvio.
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='trb_signals'"
    ).fetchone()
    if row is None or "BE_HIT" in row[0]:
        return  # tabella non esiste ancora (la crea init) o gia' aggiornata

    logger_mig = logging.getLogger("trend_rider_db.migration")
    logger_mig.warning("trb_signals: vincolo CHECK senza BE_HIT -- migrazione in corso.")

    conn.execute("DROP TABLE IF EXISTS trb_signals_pre_migration")
    conn.execute("ALTER TABLE trb_signals RENAME TO trb_signals_pre_migration")
    conn.executescript(SCHEMA_SQL)

    old_cols = {r[1] for r in conn.execute("PRAGMA table_info(trb_signals_pre_migration)")}
    new_cols = {r[1] for r in conn.execute("PRAGMA table_info(trb_signals)")}
    comuni = [c for c in old_cols if c in new_cols]
    col_list = ", ".join(comuni)
    try:
        conn.execute(
            f"INSERT INTO trb_signals ({col_list}) SELECT {col_list} FROM trb_signals_pre_migration"
        )
        n = conn.execute("SELECT COUNT(*) FROM trb_signals").fetchone()[0]
        logger_mig.warning("Migrazione completata: %d righe ripristinate.", n)
    except sqlite3.Error as e:
        logger_mig.error(
            "Ripristino dati fallito (%s) -- tabella nuova vuota ma funzionante, "
            "dati vecchi intatti in trb_signals_pre_migration.", e)
    conn.commit()


def init_trb_schema(conn: sqlite3.Connection):
    conn.executescript(SCHEMA_SQL)
    _migrate_add_entry_zone(conn)
    _migrate_add_be_hit(conn)
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
            signal.get("expiry_bars", 32),
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
    where = "WHERE final_outcome IN ('TP1_HIT','TP2_HIT','SL_HIT','BE_HIT','EXPIRED')"
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


STAGE2_PCT = 40
# Breakeven, poi TP1 come protezione avanzata -- validato il 27/08
# fuori campione (sviluppo su meta' dati, applicato a meta' MAI vista):
# +0.387R (solo breakeven) -> +0.587R (con stadio 2), stessa frequenza
# di segnali, nessuno scartato. Stadio 1: appena TP1 e' confermato,
# stop -> breakeven. Stadio 2: se il prezzo supera STAGE2_PCT% della
# distanza rimanente da TP1 a TP2, stop -> TP1 stesso (non un valore
# intermedio calcolato) -- se poi il prezzo torna indietro fino a
# quel livello, l'esito e' TP1_HIT (guadagno vero di TP1), non un
# semplice pareggio.


def monitor_open_trb_signals(
    conn: sqlite3.Connection,
    asset: str,
    current_high: float,
    current_low: float,
    now_iso: str,
) -> tuple[list[dict], list[dict]]:
    """
    Ritorna (chiusi, spostamenti_stop).

    chiusi: trade che hanno chiuso in questo ciclo (TP2_HIT/SL_HIT/
    BE_HIT/EXPIRED) -- stesso formato di sempre.

    spostamenti_stop: eventi "sposta lo stop ORA sul broker" -- il
    breakeven a due stadi aggiorna solo il database (per le statistiche
    e il Ledger), NON uno stop reale piazzato altrove. Questi eventi
    esistono per poter notificare l'utente nel momento esatto in cui
    deve agire manualmente.
    """
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
    spostamenti_stop = []

    for row in rows:
        sid, direction, entry, sl, tp1, tp2, mae, mfe, bars_open, expiry_bars, tp1_hit, tp2_hit = row

        if entry is None or sl is None:
            continue

        bars_open = (bars_open or 0) + 1
        entry_f, sl_f = float(entry), float(sl)
        tp1_f = float(tp1) if tp1 is not None else None
        tp2_f = float(tp2) if tp2 is not None else None
        old_mfe = float(mfe or 0)

        if direction == "BUY":
            adverse   = max(entry_f - current_low,  0.0)
            favorable = max(current_high - entry_f, 0.0)
        else:
            adverse   = max(current_high - entry_f, 0.0)
            favorable = max(entry_f - current_low,  0.0)

        new_mae = max(float(mae or 0), adverse)
        new_mfe = max(old_mfe, favorable)

        # ── Stop EFFETTIVO: breakeven, poi TP1 come protezione avanzata ──
        # Usa bool(tp1_hit) -- il valore GIA' salvato da un ciclo
        # PRECEDENTE, non calcolato in questo stesso ciclo -- cosi' se
        # una singola candela M15 tocca sia TP1 sia lo SL originale,
        # resta valida la stessa convenzione "SL ha priorita'" gia'
        # usata sotto, senza ambiguita' sull'ordine reale degli eventi
        # dentro la candela.
        effective_sl = sl_f
        stage2_was_active = False
        stage2_active_now = False
        stage2_lock = None
        if bool(tp1_hit) and tp1_f is not None and tp2_f is not None:
            effective_sl = entry_f  # Stadio 1: breakeven
            dist_tp1_tp2 = abs(tp2_f - tp1_f)
            if dist_tp1_tp2 > 0:
                extra = dist_tp1_tp2 * (STAGE2_PCT / 100)
                # Soglia di attivazione: STAGE2_PCT% della distanza da
                # TP1 verso TP2 (stessa logica di sempre). Il livello di
                # blocco pero' ora e' TP1 stesso, non un valore
                # intermedio calcolato -- se il prezzo si avvicina
                # abbastanza a TP2 e poi torna indietro fino a TP1, il
                # risultato riflette il guadagno vero di TP1, non un
                # semplice pareggio.
                stage2_lock = tp1_f
                if direction == "BUY":
                    stage2_trigger_mfe = (tp1_f + extra) - entry_f
                else:
                    stage2_trigger_mfe = entry_f - (tp1_f - extra)
                stage2_was_active = old_mfe >= stage2_trigger_mfe
                stage2_active_now = new_mfe >= stage2_trigger_mfe
                if stage2_active_now:
                    if direction == "BUY":
                        effective_sl = max(effective_sl, stage2_lock)
                    else:
                        effective_sl = min(effective_sl, stage2_lock)

        if direction == "BUY":
            sl_hit      = current_low  <= effective_sl
            tp1_hit_now = tp1_f is not None and current_high >= tp1_f
            tp2_hit_now = tp2_f is not None and current_high >= tp2_f
        else:
            sl_hit      = current_high >= effective_sl
            tp1_hit_now = tp1_f is not None and current_low  <= tp1_f
            tp2_hit_now = tp2_f is not None and current_low  <= tp2_f

        new_tp1_hit = bool(tp1_hit) or tp1_hit_now
        new_tp2_hit = bool(tp2_hit) or tp2_hit_now

        # SL/BE/TP1-avanzato priorità su TP2, poi la scadenza
        # (raggiungibile SEMPRE, anche con TP1 gia' toccato), infine il
        # milestone TP1 (che non chiude mai il trade, solo lo registra
        # la prima volta). Uso un flag esplicito 'chiude' invece di
        # confrontare la stringa "TP1_HIT" -- ora puo' comparire sia
        # come traguardo (non chiude) sia come esito vero quando lo
        # Stadio 2 era attivo e il prezzo torna a TP1 (chiude).
        if sl_hit:
            if bool(tp1_hit) and (stage2_active_now or stage2_was_active):
                outcome = "TP1_HIT"  # protetto oltre breakeven, torna a TP1: guadagno vero
            elif bool(tp1_hit):
                outcome = "BE_HIT"   # solo breakeven
            else:
                outcome = "SL_HIT"
            chiude = True
        elif new_tp2_hit:
            outcome = "TP2_HIT"
            chiude = True
        elif bars_open >= (expiry_bars or 32):
            outcome = "EXPIRED"
            chiude = True
        elif new_tp1_hit:
            outcome = "TP1_HIT"  # solo il traguardo, non chiude
            chiude = False
        else:
            outcome = None
            chiude = False

        updates = ["mae = ?", "mfe = ?", "bars_open = ?", "tp1_hit = ?", "tp2_hit = ?"]
        params  = [new_mae, new_mfe, bars_open, new_tp1_hit, new_tp2_hit]

        if outcome and chiude:
            updates += ["final_outcome = ?", "timestamp_closed = ?"]
            params  += [outcome, now_iso]
        elif outcome == "TP1_HIT" and not chiude and not bool(tp1_hit):
            updates += ["timestamp_tp1 = ?"]
            params  += [now_iso]

        params.append(sid)
        conn.execute(
            f"UPDATE trb_signals SET {', '.join(updates)} WHERE signal_id = ?",
            params,
        )

        # ── Eventi "sposta lo stop ORA" -- solo se il trade non ha gia'
        # chiuso in questo stesso ciclo (un trade appena chiuso non ha
        # piu' bisogno che tu sposti nulla). Uso il flag esplicito
        # 'chiude', non piu' un confronto sulla stringa "TP1_HIT" --
        # quella stringa ora e' ambigua (puo' essere solo il traguardo,
        # oppure una chiusura vera se lo Stadio 2 era attivo).
        if not chiude:
            if tp1_hit_now and not bool(tp1_hit):
                spostamenti_stop.append({
                    "signal_id": sid, "asset": asset, "direction": direction,
                    "event": "TP1_REACHED", "new_stop": entry_f,
                })
            elif stage2_active_now and not stage2_was_active:
                spostamenti_stop.append({
                    "signal_id": sid, "asset": asset, "direction": direction,
                    "event": "STAGE2_REACHED", "new_stop": stage2_lock,
                })

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

    return updated, spostamenti_stop
