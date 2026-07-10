"""
cleanup_snapshots.py
Manutenzione database — rimuove dati VECCHI (snapshot + cache).
NON svuota mai le tabelle completamente.

NON tocca MAI: segnali, order_blocks attivi, watchlist_state.

── FIX CRITICO: confronto date ───────────────────────────────────
Gli snapshot salvano timestamp in formato ISO 8601:
    "2026-07-08T00:01:26.775776+00:00"
mentre datetime('now','-36 hours') di SQLite produce:
    "2026-07-08 18:37:10"

Il confronto `timestamp_snapshot < datetime(...)` è LESSICALE fra stringhe.
Il carattere 'T' (ASCII 84) viene DOPO lo spazio (ASCII 32), quindi
"2026-07-08T00:01" risulta MAGGIORE di "2026-07-08 18:37" e la riga
NON viene cancellata anche se è vecchia.

Effetto: il retention tagliava solo i GIORNI interi precedenti, mai le
ore parziali del giorno di cutoff. Il DB accumulava ~2.5 giorni invece
di 1.5 e continuava a crescere (misurato: 0 righe cancellate su 534
realmente scadute).

Fix: normalizzare il formato prima del confronto
    replace(substr(timestamp_snapshot, 1, 19), 'T', ' ')
che trasforma "2026-07-08T00:01:26.775776+00:00" → "2026-07-08 00:01:26"

── RETENTION SNAPSHOT (configurabile) ────────────────────────────
36h è una misura TEMPORANEA per tenere il DB sotto la soglia GitHub
(50 MB) finché non sarà attiva la deduplicazione intelligente
(has_meaningful_change), che è il fix strutturale definitivo.
Nessun engine legge snapshot oltre l'ultimo, quindi ridurre la finestra
non toglie informazione usata dal sistema.

── VACUUM ────────────────────────────────────────────────────────
Parte sempre che ci sia stata almeno 1 cancellazione. SQLite non
restringe il file da solo: senza VACUUM lo spazio resta occupato.
"""

import sqlite3
import os

DB_PATH = "data/signals.db"

# Retention TEMPORANEA (vedi docstring). Rialzare quando la deduplicazione
# intelligente sarà attiva. Valore in ore.
SNAPSHOT_RETENTION_HOURS = 36

# Espressione che normalizza il timestamp ISO al formato di SQLite datetime().
# Applicata alla colonna PRIMA del confronto (vedi FIX CRITICO sopra).
_TS_NORM = "replace(substr(timestamp_snapshot, 1, 19), 'T', ' ')"

if not os.path.exists(DB_PATH):
    print(f"DB non trovato: {DB_PATH}")
    exit(0)

conn = sqlite3.connect(DB_PATH)
size_before = os.path.getsize(DB_PATH) / (1024 * 1024)
print(f"DB size prima: {size_before:.1f} MB")

total_deleted = 0

# ── Snapshot engine: tieni ultime SNAPSHOT_RETENTION_HOURS ────
for table in [
    "structure_snapshots", "volatility_snapshots",
    "order_block_snapshots", "fvg_snapshots",
    "liquidity_snapshots", "session_sweep_snapshots",
    "reaction_map_snapshots", "candlestick_snapshots",
    "macro_snapshots", "market_state_snapshots",
    "v41p1_mfm_snapshots", "market_context_snapshots",
]:
    try:
        deleted = conn.execute(
            f"DELETE FROM {table} WHERE {_TS_NORM} < "
            f"datetime('now', '-{SNAPSHOT_RETENTION_HOURS} hours')"
        ).rowcount
        total_deleted += deleted
        if deleted > 0:
            print(f"  {table}: {deleted} righe vecchie rimosse")
    except Exception:
        pass

# ── Candles cache: tieni ultime ~4000 righe (invariato) ───────
for table in ["candles_cache", "v3_candles_cache"]:
    try:
        deleted = conn.execute(
            f"DELETE FROM {table} WHERE rowid NOT IN "
            f"(SELECT rowid FROM {table} ORDER BY rowid DESC LIMIT 4000)"
        ).rowcount
        total_deleted += deleted
        if deleted > 0:
            print(f"  {table}: {deleted} righe vecchie rimosse")
    except Exception:
        pass

# ── FVG zones riempite (invariato) ────────────────────────────
try:
    deleted = conn.execute(
        "DELETE FROM fvg_zones WHERE status = 'FILLED'"
    ).rowcount
    total_deleted += deleted
    if deleted > 0:
        print(f"  fvg_zones (FILLED): {deleted} rimosse")
except Exception:
    pass

# ── Order blocks expired (invariato) ──────────────────────────
try:
    deleted = conn.execute(
        "DELETE FROM order_blocks WHERE status = 'EXPIRED'"
    ).rowcount
    total_deleted += deleted
    if deleted > 0:
        print(f"  order_blocks (EXPIRED): {deleted} rimosse")
except Exception:
    pass

# ── Watchlist alerts > 14 giorni ──────────────────────────────
# Stesso fix di formato applicato alla colonna timestamp_alert.
try:
    deleted = conn.execute(
        "DELETE FROM v41p1_watchlist_alerts "
        "WHERE replace(substr(timestamp_alert, 1, 19), 'T', ' ') "
        "< datetime('now', '-14 days')"
    ).rowcount
    total_deleted += deleted
    if deleted > 0:
        print(f"  watchlist_alerts: {deleted} vecchi rimossi")
except Exception:
    pass

conn.commit()

# ── VACUUM: parte sempre se c'è stata almeno 1 cancellazione ──
if total_deleted > 0:
    print(f"VACUUM in corso ({total_deleted} righe rimosse)...")
    conn.execute("VACUUM")

conn.close()

size_after = os.path.getsize(DB_PATH) / (1024 * 1024)
print(f"DB size: {size_after:.1f} MB (delta {size_after-size_before:+.1f} MB)")
