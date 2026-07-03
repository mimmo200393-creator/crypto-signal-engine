"""
cleanup_snapshots.py
Pulizia automatica del database — rimuove dati time-series vecchi
mantenendo tutti i dati analitici e lo stato operativo.

NON tocca MAI: segnali, order_blocks, watchlist_state.
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

# ══════════════════════════════════════════════════════════════
# Tabelle time-series da pulire (snapshot rigenerati ogni scan)
# ══════════════════════════════════════════════════════════════
tables_to_truncate = [
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
    "candles_cache",
    "v3_candles_cache",
    "fvg_zones",
    "macro_cache",
    # NON includere: v41p1_watchlist_state (serve per evitare flood notifiche)
    # NON includere: v41p1_watchlist_alerts (storico alert)
]

for table in tables_to_truncate:
    try:
        count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        if count > 0:
            conn.execute(f"DELETE FROM {table}")
            total_deleted += count
            print(f"  {table}: {count} righe rimosse")
    except Exception:
        pass

# ── Order blocks: tieni solo attivi ───────────────────────────
try:
    deleted = conn.execute(
        "DELETE FROM order_blocks WHERE status IN ('INVALIDATED', 'EXPIRED')"
    ).rowcount
    total_deleted += deleted
    remaining = conn.execute("SELECT COUNT(*) FROM order_blocks").fetchone()[0]
    if deleted > 0:
        print(f"  order_blocks: {deleted} invalidated/expired rimossi ({remaining} attivi)")
except Exception:
    pass

# ── Watchlist alerts vecchi (>30 giorni, NON lo state) ────────
try:
    deleted = conn.execute(
        "DELETE FROM v41p1_watchlist_alerts WHERE timestamp_alert < datetime('now', '-30 days')"
    ).rowcount
    total_deleted += deleted
    if deleted > 0:
        print(f"  v41p1_watchlist_alerts: {deleted} alert vecchi rimossi")
except Exception:
    pass

conn.commit()

# ── VACUUM ────────────────────────────────────────────────────
print(f"\nTotale righe rimosse: {total_deleted}")
print("VACUUM in corso...")
conn.execute("VACUUM")
conn.close()

size_after = os.path.getsize(DB_PATH) / (1024 * 1024)
saved = size_before - size_after
print(f"DB size dopo: {size_after:.1f} MB (risparmiati {saved:.1f} MB)")

# ── Sommario dati preservati ──────────────────────────────────
conn2 = sqlite3.connect(DB_PATH)
print("\nDati preservati:")
for table in ["v41p1_signals", "v41_signals", "edge_lab_signals",
              "trb_signals", "lh_signals", "order_blocks",
              "v41p1_watchlist_state"]:
    try:
        count = conn2.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        if count > 0:
            print(f"  {table}: {count} righe")
    except Exception:
        pass
conn2.close()
