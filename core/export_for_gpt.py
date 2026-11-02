#!/usr/bin/env python3
"""
export_for_gpt.py — Esporta dati MIE in JSON statici per ChatGPT Custom GPT Actions.

Legge signals.db e decision_ledger.db, genera file JSON leggeri in docs/api/.
Eseguito dallo scanner GitHub Actions dopo ogni scan (~ogni 5 minuti).

NON modifica nulla nel trading engine, strategie o configurazioni.
"""

import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

# ── Paths ─────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent.parent
SIGNALS_DB = BASE_DIR / "data" / "signals.db"
LEDGER_DB = BASE_DIR / "data" / "decision_ledger.db"
CONFIG_YAML = BASE_DIR / "config.yaml"
API_DIR = BASE_DIR / "docs" / "api"

# ── Limiti per mantenere i file leggeri ───────────────────────────
MAX_CANDLES = 500          # candele per timeframe
MAX_RECENT_TRADES = 100    # ultimi trade chiusi
MAX_LEDGER = 500           # ultime decisioni

# ── Colonne pesanti da escludere (blob JSON ridondanti) ───────────
# Questi dati sono già disponibili in analytics/snapshots_latest.json
EXCLUDE_COLS = {
    "tt_signals": {"context_snapshot"},          # ~18 KB per riga
    "v41p1_signals": {"market_snapshot"},         # ~1.8 KB per riga
    "structure_snapshots": {"snapshot_json", "config_json"},
    "volatility_snapshots": {"snapshot_json", "config_json"},
    "order_block_snapshots": {"snapshot_json", "config_json"},
    "fvg_snapshots": {"snapshot_json", "config_json"},
    "liquidity_snapshots": {"snapshot_json", "config_json"},
    "session_sweep_snapshots": {"snapshot_json", "config_json"},
    "reaction_map_snapshots": {"snapshot_json", "config_json"},
    "candlestick_snapshots": {"snapshot_json", "config_json"},
    "macro_snapshots": {"snapshot_json", "config_json"},
    "market_state_snapshots": {"snapshot_json", "config_json"},
    "market_context_snapshots": {"snapshot_json", "config_json"},
    "v41p1_mfm_snapshots": {"snapshot_json", "config_json"},
}


def _ensure_dirs():
    """Crea le sottocartelle docs/api/ se non esistono."""
    for sub in ["candles", "signals", "trades", "analytics", "settings"]:
        (API_DIR / sub).mkdir(parents=True, exist_ok=True)


def _write_json(path: Path, data):
    """Scrive JSON compatto (separators per risparmiare spazio)."""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, separators=(",", ":"), ensure_ascii=False)


def _get_light_cols(conn, table):
    """Ritorna le colonne della tabella escludendo i blob pesanti."""
    all_cols = [r[1] for r in conn.execute(f"PRAGMA table_info([{table}])").fetchall()]
    exclude = EXCLUDE_COLS.get(table, set())
    return [c for c in all_cols if c not in exclude]


def _query(conn, sql, params=()):
    """Esegue query e ritorna lista di dict."""
    cur = conn.execute(sql, params)
    cols = [d[0] for d in cur.description]
    return [{c: v for c, v in zip(cols, row)} for row in cur.fetchall()]


def _query_light(conn, table, where="", order="", limit=0, params=()):
    """Query su tabella escludendo colonne pesanti."""
    cols = _get_light_cols(conn, table)
    sql = f"SELECT {','.join(cols)} FROM [{table}]"
    if where:
        sql += f" WHERE {where}"
    if order:
        sql += f" ORDER BY {order}"
    if limit:
        sql += f" LIMIT {limit}"
    cur = conn.execute(sql, params)
    return [{c: v for c, v in zip(cols, row)} for row in cur.fetchall()]


# ── Export Functions ──────────────────────────────────────────────

def export_candles(conn):
    """Esporta candele per ogni combinazione asset/timeframe."""
    combos = conn.execute(
        "SELECT DISTINCT asset, timeframe FROM candles_cache"
    ).fetchall()
    combos += conn.execute(
        "SELECT DISTINCT asset, timeframe FROM v3_candles_cache"
    ).fetchall()

    # Deduplica — preferisci v3 (ha più timeframe)
    seen = set()
    for asset, tf in combos:
        key = f"{asset}_{tf}"
        if key in seen:
            continue
        seen.add(key)

        # Prendi da v3 se disponibile, fallback a candles_cache
        rows = _query(conn,
            "SELECT * FROM v3_candles_cache "
            "WHERE asset=? AND timeframe=? "
            "ORDER BY timestamp DESC LIMIT ?",
            (asset, tf, MAX_CANDLES))

        if not rows:
            rows = _query(conn,
                "SELECT * FROM candles_cache "
                "WHERE asset=? AND timeframe=? "
                "ORDER BY timestamp DESC LIMIT ?",
                (asset, tf, MAX_CANDLES))

        if rows:
            rows.reverse()  # Ordine cronologico
            fname = f"{asset.lower()}_{tf.lower()}.json"
            _write_json(API_DIR / "candles" / fname, {
                "asset": asset,
                "timeframe": tf,
                "count": len(rows),
                "candles": rows
            })


def export_signals(conn):
    """Esporta tutti i segnali per ogni strategia (senza blob pesanti)."""
    strategies = {
        "ote_lab": "ote_lab_signals",
        "tt": "tt_signals",
        "trb": "trb_signals",
        "lh": "lh_signals",
        "v41p1": "v41p1_signals",
    }
    for name, table in strategies.items():
        rows = _query_light(conn, table, order="rowid DESC")
        _write_json(API_DIR / "signals" / f"{name}.json", {
            "strategy": name,
            "total": len(rows),
            "signals": rows
        })


def export_trades(conn):
    """Esporta trade aperti e ultimi chiusi (tutte le strategie)."""
    strategies = {
        "ote_lab": ("ote_lab_signals", "final_outcome", "timestamp_setup"),
        "tt": ("tt_signals", "result", "signal_created_at"),
        "trb": ("trb_signals", "final_outcome", "timestamp_setup"),
        "lh": ("lh_signals", "final_outcome", "timestamp_setup"),
        "v41p1": ("v41p1_signals", "final_outcome", "timestamp_setup"),
    }

    open_trades = []
    recent_closed = []

    for strat, (table, outcome_col, ts_col) in strategies.items():
        # Trade aperti
        rows = _query_light(conn, table,
            where=f"{outcome_col} = 'OPEN' OR {outcome_col} IS NULL")
        for r in rows:
            r["strategy_name"] = strat
        open_trades.extend(rows)

        # Trade chiusi recenti
        rows = _query_light(conn, table,
            where=f"{outcome_col} IS NOT NULL AND {outcome_col} != 'OPEN'",
            order=f"{ts_col} DESC",
            limit=MAX_RECENT_TRADES)
        for r in rows:
            r["strategy_name"] = strat
        recent_closed.extend(rows)

    _write_json(API_DIR / "trades" / "open.json", {
        "count": len(open_trades),
        "trades": open_trades
    })
    _write_json(API_DIR / "trades" / "recent.json", {
        "count": len(recent_closed),
        "trades": recent_closed[:MAX_RECENT_TRADES]
    })


def export_snapshots(conn):
    """Esporta l'ultimo snapshot di ogni engine per ogni asset (senza blob)."""
    snapshot_tables = [
        "structure_snapshots", "volatility_snapshots",
        "order_block_snapshots", "fvg_snapshots",
        "liquidity_snapshots", "session_sweep_snapshots",
        "reaction_map_snapshots", "candlestick_snapshots",
        "macro_snapshots", "market_state_snapshots",
        "market_context_snapshots", "v41p1_mfm_snapshots",
    ]

    latest = {}
    for table in snapshot_tables:
        try:
            rows = _query_light(conn, table,
                order="rowid DESC", limit=4)  # 2 asset × 2 (margine)
            latest[table.replace("_snapshots", "")] = rows
        except Exception:
            pass

    _write_json(API_DIR / "analytics" / "snapshots_latest.json", {
        "engines": latest
    })


def export_zones(conn):
    """Esporta zone attive: swing zones, radar, OB, FVG."""
    data = {}

    data["lh_swing_zones"] = _query(conn,
        "SELECT * FROM lh_swing_zones ORDER BY rowid DESC LIMIT 200")

    data["radar_zones"] = _query(conn,
        "SELECT * FROM radar_zones ORDER BY rowid DESC LIMIT 200")

    data["order_blocks"] = _query(conn,
        "SELECT * FROM order_blocks ORDER BY rowid DESC LIMIT 100")

    data["fvg_zones"] = _query(conn,
        "SELECT * FROM fvg_zones ORDER BY rowid DESC LIMIT 200")

    _write_json(API_DIR / "analytics" / "zones.json", data)


def export_ledger(conn_ledger):
    """Esporta ultime N decisioni dal decision ledger."""
    rows = _query(conn_ledger,
        "SELECT * FROM decision_ledger ORDER BY rowid DESC LIMIT ?",
        (MAX_LEDGER,))

    _write_json(API_DIR / "analytics" / "ledger_recent.json", {
        "count": len(rows),
        "decisions": rows
    })


def export_config():
    """Esporta config.yaml come JSON."""
    try:
        import yaml
        with open(CONFIG_YAML, "r") as f:
            cfg = yaml.safe_load(f)
        _write_json(API_DIR / "settings" / "config.json", cfg)
    except ImportError:
        # Fallback: copia raw YAML come stringa JSON
        with open(CONFIG_YAML, "r") as f:
            raw = f.read()
        _write_json(API_DIR / "settings" / "config.json", {"raw_yaml": raw})


def export_meta(conn, conn_ledger):
    """Genera meta.json con timestamp e conteggi."""
    now = datetime.now(timezone.utc).isoformat()

    counts = {}
    for table in ["ote_lab_signals", "tt_signals", "trb_signals",
                   "lh_signals", "v41p1_signals", "candles_cache",
                   "v3_candles_cache"]:
        counts[table] = conn.execute(
            f"SELECT COUNT(*) FROM [{table}]").fetchone()[0]

    counts["decision_ledger"] = conn_ledger.execute(
        "SELECT COUNT(*) FROM decision_ledger").fetchone()[0]

    _write_json(API_DIR / "meta.json", {
        "last_updated": now,
        "update_interval_minutes": 5,
        "counts": counts,
        "endpoints": [
            "candles/{asset}_{timeframe}.json",
            "signals/{strategy}.json",
            "trades/open.json",
            "trades/recent.json",
            "analytics/snapshots_latest.json",
            "analytics/zones.json",
            "analytics/ledger_recent.json",
            "settings/config.json",
        ]
    })


# ── Main ──────────────────────────────────────────────────────────

def main():
    t0 = time.time()
    _ensure_dirs()

    conn = sqlite3.connect(str(SIGNALS_DB))
    conn_ledger = sqlite3.connect(str(LEDGER_DB))

    try:
        export_candles(conn)
        export_signals(conn)
        export_trades(conn)
        export_snapshots(conn)
        export_zones(conn)
        export_ledger(conn_ledger)
        export_config()
        export_meta(conn, conn_ledger)
    finally:
        conn.close()
        conn_ledger.close()

    # Report dimensioni
    total = 0
    for p in sorted(API_DIR.rglob("*.json")):
        size = p.stat().st_size
        total += size
        rel = p.relative_to(API_DIR)
        print(f"  {rel}: {size / 1024:.1f} KB")
    print(f"  ─────────────────────────")
    print(f"  TOTALE: {total / 1024:.1f} KB ({total / 1024 / 1024:.2f} MB)")
    print(f"  Tempo: {time.time() - t0:.2f}s")


if __name__ == "__main__":
    main()
