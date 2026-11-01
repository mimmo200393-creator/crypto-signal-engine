#!/usr/bin/env python3
"""Read-only AI Trading Journal source collector.

Collects all rows from the five supported signal tables (not just OPEN signals),
keeps source fields intact, and injects a JSON payload into the HTML template.
It does not calculate trading P&L or alter either SQLite database.
"""
import os, sqlite3, json, re
from pathlib import Path
from datetime import datetime, timezone, timedelta

DB = Path(os.getenv("DB_PATH", "data/signals.db"))
LEDGER = Path(os.getenv("LEDGER_DB_PATH", "data/decision_ledger.db"))
TEMPLATE = Path(os.getenv("AI_JOURNAL_TEMPLATE", "docs/ai_trading_journal_template.html"))
OUT = Path(os.getenv("AI_JOURNAL_OUT", "docs/ai_trading_journal_dashboard_generated.html"))

SOURCES = {
    "OTE-LAB": ("ote_lab_signals", "timestamp_setup"),
    "TT": ("tt_signals", "signal_created_at"),
    "LH": ("lh_signals", "timestamp_setup"),
    "TRB": ("trb_signals", "timestamp_setup"),
    "V41P1": ("v41p1_signals", "timestamp_setup"),
}

def connect(path):
    if not path.exists():
        return None
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    con.row_factory = sqlite3.Row
    return con

def table_exists(con, table):
    return bool(con and con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone())

def first(row, *keys):
    return next((row.get(k) for k in keys if row.get(k) is not None), None)

def parse_datetime(value):
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None

def collect_table(con, table, date_col, cutoff):
    if not table_exists(con, table):
        return {"available": False, "count": 0, "columns": [], "rows": [], "error": "table_missing"}
    columns = [r["name"] for r in con.execute(f'PRAGMA table_info("{table}")')]
    try:
        rows = [dict(r) for r in con.execute(f'SELECT * FROM "{table}"')]
    except sqlite3.Error as exc:
        return {"available": True, "count": 0, "columns": columns, "rows": [], "error": str(exc)}
    normalized = []
    undated_retained = 0
    for row in rows:
        stamp = first(row, date_col, "created_at", "timestamp", "signal_created_at", "timestamp_setup")
        parsed = parse_datetime(stamp)
        if parsed is not None and parsed < cutoff:
            continue
        if parsed is None:
            undated_retained += 1
        normalized.append({
            "source": table,
            "id": first(row, "signal_id", "id", "candidate_id"),
            "asset": first(row, "asset", "symbol", "instrument"),
            "direction": first(row, "direction", "side"),
            "status": first(row, "status", "final_outcome", "outcome"),
            "raw_outcome": first(row, "final_outcome", "status", "outcome"),
            "setup_at": first(row, date_col, "created_at", "timestamp", "signal_created_at", "timestamp_setup"),
            "closed_at": first(row, "timestamp_closed", "closed_at", "updated_at"),
            "entry": first(row, "actual_entry", "entry", "planned_entry"),
            "sl": first(row, "actual_sl", "stop_loss", "planned_sl"),
            "tp": first(row, "actual_tp", "take_profit", "planned_tp", "tp"),
            "tp1": first(row, "tp1"), "tp2": first(row, "tp2"),
            "tp1_hit": first(row, "tp1_hit"), "tp2_hit": first(row, "tp2_hit"),
            "mae": first(row, "mae"), "mfe": first(row, "mfe"),
            "rr": first(row, "actual_rr", "planned_rr", "rr", "rr2"),
            "result_r": first(row, "result_r"),
            "quality": first(row, "quality_label", "quality_score"),
            "raw": row,
        })
    return {"available": True, "count": len(normalized), "source_count": len(rows),
            "undated_retained": undated_retained, "columns": columns, "rows": normalized}

def main():
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=30)
    data = {
        "generated_utc": now.isoformat(),
        "source_db": str(DB), "ledger_db": str(LEDGER),
        "strategies": {}, "total": 0, "ledger_rows": 0,
        "included_strategies": list(SOURCES),
        "excluded_strategies": ["OTE", "Edge Lab", "v3_signals", "v4_signals", "v41_signals"],
        "refresh_mode": "Read-only SQLite; rolling 30-day signal window on each run (undated/unparseable rows retained for audit).",
        "period_days": 30,
        "performance_policy": "Signal inventory only. No P&L, win-rate, expectancy or drawdown inferred.",
    }
    con = connect(DB)
    if con:
        try:
            for name, (table, date_col) in SOURCES.items():
                item = collect_table(con, table, date_col, cutoff)
                data["strategies"][name] = item
                data["total"] += item["count"]
        finally:
            con.close()
    else:
        data["source_db_status"] = "missing"
        for name in SOURCES:
            data["strategies"][name] = {"available": False, "count": 0, "columns": [], "rows": [], "error": "database_missing"}
    ledger = connect(LEDGER)
    if ledger:
        try:
            if table_exists(ledger, "decision_ledger"):
                data["ledger_rows"] = ledger.execute("SELECT COUNT(*) FROM decision_ledger").fetchone()[0]
                data["ledger_columns"] = [r["name"] for r in ledger.execute("PRAGMA table_info(decision_ledger)")]
            else:
                data["ledger_status"] = "table_missing"
        finally:
            ledger.close()
    else:
        data["ledger_status"] = "database_missing"
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    template = TEMPLATE.read_text(encoding="utf-8")
    block = f'<script id="ai-journal-payload" type="application/json">{payload}</script>'
    marker = "<!-- AI_JOURNAL_PAYLOAD -->"
    if marker in template:
        template = template.replace(marker, block)
    elif 'id="ai-journal-payload"' in template:
        template = re.sub(r'<script id="ai-journal-payload" type="application/json">.*?</script>', block, template, flags=re.S)
    else:
        template = template.replace("</body>", block + "</body>")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(template, encoding="utf-8")
    print(f"generated {OUT} · signals={data['total']} · ledger={data['ledger_rows']} · read-only")

if __name__ == "__main__":
    main()
