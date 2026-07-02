"""
cleanup_snapshots.py
Pulizia automatica del database — rimuove snapshot time-series
vecchi mantenendo tutti i dati analitici (segnali, order_blocks, esiti).

Chiamato dal workflow GitHub Actions prima del commit.

NON tocca MAI: v41p1_signals, v41_signals, edge_lab_signals,
trb_signals, lh_signals, order_blocks, v3_signals, signals.
"""

import sqlite3
import os

DB_PATH = "data/signals.db"

if not os.path.exists(DB_PATH):
    print(f"DB non trovato: {DB_PATH}")
    exit(0)

conn = sqlite3.connect(DB_PATH)

# Dimensione prima
size_before = os.path.getsize(DB_PATH) / (1024 * 1024)
print(f"DB size prima: {size_before:.1f} MB")

# ── Snapshot time-series: tieni solo ultimi 14 giorni ─────────
snapshot_tables = [
    "structure_snapshots",
    "volatility_snapshots",
    "order_block_snapshots",
    "fvg_snapshots",
    "liquidity_snapshots",
    "session_sweep_snapshots",
    "reaction_map_snapshots",
    "candlestick_snapshots",
    "macro_snapshots",
    "market_state_snapshots",
    "v41p1_mfm_snapshots",
    "market_context_snapshots",
]

total_deleted = 0
for table in snapshot_tables:
    try:
        # Prova timestamp_snapshot (usato dalla maggior parte)
        deleted = conn.execute(
            f"DELETE FROM {table} WHERE timestamp_snapshot < datetime('now', '-14 days')"
        ).rowcount
        total_deleted += deleted
        if deleted > 0:
            print(f"  {table}: {deleted} righe rimosse")
    except Exception:
        try:
            # Alcune tabelle usano nomi diversi
            deleted = conn.execute(
                f"DELETE FROM {table} WHERE rowid NOT IN "
                f"(SELECT rowid FROM {table} ORDER BY rowid DESC LIMIT 500)"
            ).rowcount
            total_deleted += deleted
            if deleted > 0:
                print(f"  {table}: {deleted} righe rimosse (fallback)")
        except Exception as e:
            print(f"  {table}: skip ({e})")

# ── Candles cache: tieni solo ultimi 30 giorni ────────────────
for table in ["candles_cache", "v3_candles_cache"]:
    try:
        before = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        # Tieni le ultime 3000 candele per tabella (circa 30 giorni M15)
        deleted = conn.execute(
            f"DELETE FROM {table} WHERE rowid NOT IN "
            f"(SELECT rowid FROM {table} ORDER BY rowid DESC LIMIT 3000)"
        ).rowcount
        total_deleted += deleted
        if deleted > 0:
            print(f"  {table}: {deleted} righe rimosse (era {before})")
    except Exception as e:
        print(f"  {table}: skip ({e})")

# ── OB invalidati vecchi: tieni solo ultimi 50 ────────────────
try:
    deleted = conn.execute(
        "DELETE FROM order_blocks WHERE status = 'EXPIRED' "
        "OR (status = 'INVALIDATED' AND age_bars > 1000)"
    ).rowcount
    total_deleted += deleted
    if deleted > 0:
        print(f"  order_blocks (expired/old invalidated): {deleted} righe rimosse")
except Exception:
    pass

# ── Watchlist alerts vecchi ───────────────────────────────────
try:
    deleted = conn.execute(
        "DELETE FROM v41p1_watchlist_alerts WHERE timestamp_alert < datetime('now', '-30 days')"
    ).rowcount
    total_deleted += deleted
    if deleted > 0:
        print(f"  v41p1_watchlist_alerts: {deleted} righe rimosse")
except Exception:
    pass

conn.commit()

# ── VACUUM per recuperare spazio su disco ─────────────────────
print(f"\nTotale righe rimosse: {total_deleted}")
print("VACUUM in corso...")
conn.execute("VACUUM")
conn.close()

size_after = os.path.getsize(DB_PATH) / (1024 * 1024)
saved = size_before - size_after
print(f"DB size dopo: {size_after:.1f} MB (risparmiati {saved:.1f} MB)")
