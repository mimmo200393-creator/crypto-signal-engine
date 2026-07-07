"""
core/decision_ledger/ledger_writer.py
Sprint 0 — Ledger Writer

Unico responsabile della persistenza nel decision_ledger.
Isolato dal resto: se fallisce, NON deve mai bloccare un trade.

Modifiche Design Review integrate:
  M2 — file SQLite separato + WAL + busy_timeout + retry su lock
  M4 — update idempotente + riconciliazione PENDING

Filosofia: raccolta passiva. Questo modulo osserva e registra,
non partecipa mai alla decisione di trading.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("decision_ledger.writer")

# File SEPARATO da signals.db (M2: isolamento concorrenza/corruzione)
DEFAULT_LEDGER_PATH = "data/decision_ledger.db"
SCHEMA_PATH = "storage/decision_ledger_schema.sql"

BUSY_TIMEOUT_MS = 30000       # M2: 30s prima di dare errore lock
MAX_RETRIES = 3               # M2: retry su "database is locked"
RETRY_BACKOFF_S = 0.5

# Stati terminali: una volta raggiunti, il record non viene più aggiornato (M4)
TERMINAL_OUTCOMES = {"TP", "SL", "BE", "EXPIRED", "VIRTUAL_TP", "VIRTUAL_SL"}


def _connect(ledger_path: str) -> sqlite3.Connection:
    """Connessione con WAL e busy_timeout (M2)."""
    conn = sqlite3.connect(ledger_path, timeout=BUSY_TIMEOUT_MS / 1000)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS};")
    conn.execute("PRAGMA synchronous=NORMAL;")  # WAL-safe, più veloce di FULL
    return conn


def init_ledger(ledger_path: str = DEFAULT_LEDGER_PATH,
                schema_path: str = SCHEMA_PATH) -> None:
    """Crea il file e la tabella se non esistono. Idempotente."""
    os.makedirs(os.path.dirname(ledger_path), exist_ok=True)
    conn = _connect(ledger_path)
    try:
        if os.path.exists(schema_path):
            with open(schema_path, "r") as f:
                conn.executescript(f.read())
        else:
            logger.error("Ledger schema non trovato: %s", schema_path)
        conn.commit()
    finally:
        conn.close()


def _execute_with_retry(ledger_path: str, sql: str, params: tuple) -> bool:
    """
    Esegue una scrittura con retry su lock (M2).
    Ritorna True se riuscita, False altrimenti. NON solleva mai:
    il Ledger non deve poter propagare eccezioni al runner.
    """
    for attempt in range(MAX_RETRIES):
        conn = None
        try:
            conn = _connect(ledger_path)
            conn.execute(sql, params)
            conn.commit()
            return True
        except sqlite3.OperationalError as e:
            if "locked" in str(e).lower() and attempt < MAX_RETRIES - 1:
                logger.warning("Ledger lock, retry %d/%d", attempt + 1, MAX_RETRIES)
                time.sleep(RETRY_BACKOFF_S * (attempt + 1))
                continue
            logger.error("Ledger write fallita (operational): %s", e)
            return False
        except Exception as e:
            logger.error("Ledger write fallita: %s", e)
            return False
        finally:
            if conn:
                conn.close()
    return False


def write_decision(record: dict,
                   ledger_path: str = DEFAULT_LEDGER_PATH) -> bool:
    """
    Inserisce un nuovo record di decisione (outcome=PENDING).
    record: dict prodotto dal Decision Collector.
    Ritorna True/False, non solleva mai.
    """
    cols = list(record.keys())
    placeholders = ",".join("?" for _ in cols)
    col_names = ",".join(cols)
    sql = f"INSERT OR IGNORE INTO decision_ledger ({col_names}) VALUES ({placeholders})"
    # INSERT OR IGNORE: se per qualsiasi ragione l'ULID collidesse (non dovrebbe),
    # non sovrascrive — la prima scrittura vince (idempotenza sull'insert).
    ok = _execute_with_retry(ledger_path, sql, tuple(record[c] for c in cols))
    if ok:
        logger.info("Ledger: decision %s registrata (%s %s %s)",
                    record.get("decision_id", "?")[:12],
                    record.get("strategy"), record.get("asset"),
                    record.get("decision_type"))
    return ok


def update_outcome(decision_id: str,
                   outcome: str,
                   r_realized: Optional[float] = None,
                   mfe_r: Optional[float] = None,
                   mae_r: Optional[float] = None,
                   duration_bars: Optional[int] = None,
                   mae_abs: Optional[float] = None,
                   mfe_abs: Optional[float] = None,
                   ledger_path: str = DEFAULT_LEDGER_PATH) -> bool:
    """
    Aggiorna l'esito di un record PENDING (M4).
    IDEMPOTENTE: aggiorna solo se il record è ancora PENDING.

    Sprint 0 fix (trade completo): il risk ORIGINALE (entry, stop_loss,
    rr_planned) è già salvato nel record all'ingresso. Se r_realized/mfe_r/
    mae_r non sono forniti, li ricostruisce dai valori originali del record
    — così il breakeven (che altera lo SL nella tabella segnali) non
    corrompe il calcolo R nel Ledger. Questa è la ragione per cui il Ledger
    salva entry/sl/rr all'ingresso: restano la fonte di verità dell'Outcome.
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    now_micro = int(time.time() * 1_000_000)

    conn = None
    try:
        conn = _connect(ledger_path)
        conn.row_factory = sqlite3.Row
        rec = conn.execute(
            "SELECT entry, stop_loss, take_profit, rr_planned, outcome "
            "FROM decision_ledger WHERE decision_id=?", (decision_id,)
        ).fetchone()

        if rec is None:
            conn.close()
            return False
        if rec["outcome"] != "PENDING":
            conn.close()
            return True  # già terminale: idempotente, non riscrive

        # Risk originale dal record (immune al breakeven)
        entry = rec["entry"]
        sl_orig = rec["stop_loss"]
        rr_planned = rec["rr_planned"]
        risk_orig = abs(entry - sl_orig) if (entry and sl_orig) else None

        norm = {"SL": "SL", "TP": "TP", "TP2": "TP",
                "EXPIRED": "EXPIRED", "BE": "BE",
                "VIRTUAL_TP": "VIRTUAL_TP", "VIRTUAL_SL": "VIRTUAL_SL"}
        ledger_outcome = norm.get(outcome, "EXPIRED")

        # Ricostruzione R dai valori originali se non forniti
        if r_realized is None:
            if ledger_outcome == "TP":
                r_realized = rr_planned  # TP raggiunto = RR pianificato
            elif ledger_outcome == "SL":
                r_realized = -1.0
            elif ledger_outcome == "BE":
                r_realized = 0.0
        if mfe_r is None and mfe_abs is not None and risk_orig and risk_orig > 0:
            mfe_r = round(mfe_abs / risk_orig, 3)
        if mae_r is None and mae_abs is not None and risk_orig and risk_orig > 0:
            mae_r = round(mae_abs / risk_orig, 3)

        conn.close()
    except Exception as e:
        if conn:
            conn.close()
        logger.error("Ledger update_outcome lettura fallita: %s", e)
        # fallback: procede con i valori passati
        norm = {"SL": "SL", "TP": "TP", "TP2": "TP", "EXPIRED": "EXPIRED", "BE": "BE"}
        ledger_outcome = norm.get(outcome, "EXPIRED")

    sql = """
        UPDATE decision_ledger
        SET outcome=?, r_realized=?, mfe_r=?, mae_r=?, duration_bars=?,
            outcome_ts_iso=?, last_checked_ts=?
        WHERE decision_id=? AND outcome='PENDING'
    """
    params = (ledger_outcome, r_realized, mfe_r, mae_r, duration_bars,
              now_iso, now_micro, decision_id)
    ok = _execute_with_retry(ledger_path, sql, params)
    if ok:
        logger.info("Ledger: outcome %s → %s (r=%s)", decision_id[:12], ledger_outcome, r_realized)
    return ok


def touch_pending(decision_id: str,
                  ledger_path: str = DEFAULT_LEDGER_PATH) -> bool:
    """
    Aggiorna last_checked_ts su un PENDING senza chiuderlo (M4).
    Usato dal monitor a ogni scan per sapere che il record è ancora vivo.
    """
    now_micro = int(time.time() * 1_000_000)
    sql = ("UPDATE decision_ledger SET last_checked_ts=? "
           "WHERE decision_id=? AND outcome='PENDING'")
    return _execute_with_retry(ledger_path, sql, (now_micro, decision_id))


def sweep_expired(expiry_hours: int = 24,
                  ledger_path: str = DEFAULT_LEDGER_PATH) -> int:
    """
    Riconciliazione (M4): forza a EXPIRED i PENDING più vecchi di expiry_hours
    che nessuno scan ha chiuso. Evita PENDING orfani che inquinano le analisi.
    Ritorna il numero di record forzati a EXPIRED.
    """
    cutoff_micro = int((time.time() - expiry_hours * 3600) * 1_000_000)
    now_iso = datetime.now(timezone.utc).isoformat()
    sql = """
        UPDATE decision_ledger
        SET outcome='EXPIRED', outcome_ts_iso=?
        WHERE outcome='PENDING' AND ts_micro < ?
    """
    conn = None
    try:
        conn = _connect(ledger_path)
        cur = conn.execute(sql, (now_iso, cutoff_micro))
        n = cur.rowcount
        conn.commit()
        if n > 0:
            logger.info("Ledger sweep: %d PENDING orfani → EXPIRED", n)
        return n
    except Exception as e:
        logger.error("Ledger sweep fallito: %s", e)
        return 0
    finally:
        if conn:
            conn.close()


def get_pending_ids(ledger_path: str = DEFAULT_LEDGER_PATH) -> list[dict]:
    """Ritorna i record PENDING da monitorare (per il collegamento col monitor)."""
    conn = None
    try:
        conn = _connect(ledger_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT decision_id, asset, strategy, direction, entry, stop_loss, "
            "take_profit, ts_micro FROM decision_ledger WHERE outcome='PENDING'"
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.error("Ledger get_pending fallito: %s", e)
        return []
    finally:
        if conn:
            conn.close()
