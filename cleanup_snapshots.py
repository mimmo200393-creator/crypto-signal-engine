"""
cleanup_snapshots.py
Pulizia automatica del database.

Prima esecuzione: reset completo delle tabelle time-series
(snapshot, cache, zone) mantenendo SOLO i segnali con esito.
Esecuzioni successive: manutenzione ordinaria (7-14 giorni).

INTOCCABILI: v41p1_signals, v41_signals, edge_lab_signals,
trb_signals, lh_signals, v3_signals, v3d_signals, v4_signals,
signals, order_blocks (mappa attiva).
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
# Tabelle da SVUOTARE completamente (time-series rigenerabili)
# ══════════════════════════════════════════════════════════════
tables_to_truncate = [
    # Snapshot engine (rigenerati ogni scan)
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
    # MFM e market context (alto volume, dati nel signal JSON)
    "v41p1_mfm_snapshots",
    "market_context_snapshots",
    # Candles cache (ri-scaricabili dall'API)
    "candles_cache",
    "v3_candles_cache",
    # Zone vecchie
    "fvg_zones",
    "macro_cache",
    # Watchlist (storico alert, non analitico)
    "v41p1_watchlist_alerts",
    "v41p1_watchlist_state",
]

for table in tables_to_truncate:
    try:
        count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        if count > 0:
            conn.execute(f"DELETE FROM {table}")
            total_deleted += count
            print(f"  {table}: {count} righe rimosse")
    except Exception as e:
        pass  # tabella non esiste, ok

# ══════════════════════════════════════════════════════════════
# Order blocks: tieni solo FRESH, TESTED, MITIGATED, BREAKER
# (rimuovi INVALIDATED e EXPIRED vecchi)
# ══════════════════════════════════════════════════════════════
try:
    deleted = conn.execute(
        "DELETE FROM order_blocks WHERE status IN ('INVALIDATED', 'EXPIRED')"
    ).rowcount
    total_deleted += deleted
    remaining = conn.execute("SELECT COUNT(*) FROM order_blocks").fetchone()[0]
    if deleted > 0:
        print(f"  order_blocks: {deleted} invalidated/expired rimossi ({remaining} attivi mantenuti)")
except Exception:
    pass

# ══════════════════════════════════════════════════════════════
# Reset alert state (per evitare falsi duplicati post-reset)
# ══════════════════════════════════════════════════════════════
for table in ["v41p1_last_alert_state", "v41_last_alert_state"]:
    try:
        conn.execute(f"DELETE FROM {table}")
        print(f"  {table}: reset")
    except Exception:
        pass

conn.commit()

# ══════════════════════════════════════════════════════════════
# VACUUM — recupera spazio su disco
# ══════════════════════════════════════════════════════════════
print(f"\nTotale righe rimosse: {total_deleted}")
print("VACUUM in corso...")
conn.execute("VACUUM")
conn.close()

size_after = os.path.getsize(DB_PATH) / (1024 * 1024)
saved = size_before - size_after
print(f"DB size dopo: {size_after:.1f} MB (risparmiati {saved:.1f} MB)")

# ══════════════════════════════════════════════════════════════
# Sommario dati PRESERVATI
# ══════════════════════════════════════════════════════════════
conn2 = sqlite3.connect(DB_PATH)
print("\nDati preservati:")
for table in ["v41p1_signals", "v41_signals", "edge_lab_signals",
              "trb_signals", "lh_signals", "order_blocks"]:
    try:
        count = conn2.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        if count > 0:
            print(f"  {table}: {count} righe")
    except Exception:
        pass
conn2.close()
