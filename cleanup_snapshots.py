"""
cleanup_snapshots.py
Manutenzione database — rimuove dati VECCHI (snapshot + cache).
NON svuota mai le tabelle completamente.

NON tocca MAI: segnali, order_blocks attivi, watchlist_state.

── RETENTION SNAPSHOT (configurabile) ────────────────────────────
SNAPSHOT_RETENTION_HOURS controlla la finestra di conservazione degli
snapshot. Impostato a 36h come misura TEMPORANEA per tenere il DB
sotto la soglia GitHub (50 MB) finché non sarà attiva la deduplicazione
intelligente (has_meaningful_change), che è il fix strutturale definitivo.
Con retention 72h (3 giorni) il DB plateau ~70 MB → sfora.
Con 36h il DB resta ~33 MB. Nessun engine legge snapshot oltre l'ultimo,
quindi ridurre la finestra non toglie informazione usata dal sistema.
Quando la deduplicazione sarà attiva, questo valore potrà essere rialzato.

── FIX VACUUM ────────────────────────────────────────────────────
Prima il VACUUM partiva solo con >100 cancellazioni: girando ogni 5 min
cancellava pochi record per volta, quindi il VACUUM non partiva quasi
mai e lo spazio non veniva rilasciato (SQLite non restringe il file da
solo). Ora il VACUUM parte sempre che ci sia stata almeno 1 cancellazione.
"""

import sqlite3
import os

DB_PATH = "data/signals.db"

# Retention TEMPORANEA (vedi docstring). Rialzare quando la deduplicazione
# intelligente sarà attiva. Valore in ore.
SNAPSHOT_RETENTION_HOURS = 36

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
            f"DELETE FROM {table} WHERE timestamp_snapshot < "
            f"datetime('now', '-{SNAPSHOT_RETENTION_HOURS} hours')"
        ).rowcount
        total_deleted += deleted
        if deleted > 0:
            print(f"  {table}: {deleted} righe vecchie rimosse")
    except Exception:
        pass

# ── Candles cache: tieni ultimi ~7 giorni (invariato) ─────────
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

# ── Watchlist alerts > 14 giorni (invariato) ──────────────────
try:
    deleted = conn.execute(
        "DELETE FROM v41p1_watchlist_alerts "
        "WHERE timestamp_alert < datetime('now', '-14 days')"
    ).rowcount
    total_deleted += deleted
    if deleted > 0:
        print(f"  watchlist_alerts: {deleted} vecchi rimossi")
except Exception:
    pass

conn.commit()

# ── VACUUM: ora parte SEMPRE se c'è stata almeno 1 cancellazione ──
# (prima era condizionato a >100, quindi non partiva quasi mai)
if total_deleted > 0:
    print(f"VACUUM in corso ({total_deleted} righe rimosse)...")
    conn.execute("VACUUM")

conn.close()

size_after = os.path.getsize(DB_PATH) / (1024 * 1024)
print(f"DB size: {size_after:.1f} MB (delta {size_after-size_before:+.1f} MB)")
