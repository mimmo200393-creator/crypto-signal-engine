"""
cleanup_snapshots.py
Manutenzione database — rimuove dati VECCHI (>3 giorni per snapshot,
>30 giorni per cache). NON svuota mai le tabelle completamente.

NON tocca MAI: segnali, order_blocks attivi, watchlist_state.
"""

import sqlite3
import os

DB_PATH = "data/signals.db"

if not os.path.exists(DB_PATH):
    print(f"DB non trovato: {DB_PATH}")
    exit(0)

conn = sqlite3.connect(DB_PATH)
size_before = os.path.getsize(DB_PATH) / (1024 * 1024)
print(f"DB size prima: {size_before:.1f} MB")

total_deleted = 0

# ── Snapshot engine: tieni ultimi 3 giorni ────────────────────
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
            f"DELETE FROM {table} WHERE timestamp_snapshot < datetime('now', '-3 days')"
        ).rowcount
        total_deleted += deleted
        if deleted > 0:
            print(f"  {table}: {deleted} righe vecchie rimosse")
    except Exception:
        pass

# ── Candles cache: tieni ultimi 7 giorni ──────────────────────
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

# ── FVG zones riempite ────────────────────────────────────────
try:
    deleted = conn.execute(
        "DELETE FROM fvg_zones WHERE status = 'FILLED'"
    ).rowcount
    total_deleted += deleted
    if deleted > 0:
        print(f"  fvg_zones (FILLED): {deleted} rimosse")
except Exception:
    pass

# ── Order blocks expired ──────────────────────────────────────
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

if total_deleted > 100:
    print("VACUUM in corso...")
    conn.execute("VACUUM")

conn.close()

size_after = os.path.getsize(DB_PATH) / (1024 * 1024)
print(f"DB size: {size_after:.1f} MB (delta {size_after-size_before:+.1f} MB)")
