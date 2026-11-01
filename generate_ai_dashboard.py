#!/usr/bin/env python3
"""
AI Trading Journal dashboard generator.

Reads the repository's SQLite snapshots in read-only mode and refreshes the
AI Trading Journal HTML from its template. It does not modify scanner logic,
signals.db, decision_ledger.db, MT5, or the EA.

Typical GitHub Actions use:
  python generate_ai_dashboard.py
Optional environment variables:
  DB_PATH=data/signals.db
  LEDGER_DB_PATH=data/decision_ledger.db
  AI_JOURNAL_TEMPLATE=docs/ai_trading_journal_template.html
  AI_JOURNAL_OUT=docs/ai_trading_journal_dashboard.html
"""
from __future__ import annotations

import html
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DB_PATH = Path(os.environ.get("DB_PATH", "data/signals.db"))
LEDGER_PATH = Path(os.environ.get("LEDGER_DB_PATH", "data/decision_ledger.db"))
TEMPLATE_PATH = Path(os.environ.get("AI_JOURNAL_TEMPLATE", "docs/ai_trading_journal_template.html"))
OUT_PATH = Path(os.environ.get("AI_JOURNAL_OUT", "docs/ai_trading_journal_dashboard.html"))

# Strategy inventory used by this journal. OTE consists of two separate source tables.
STRATEGY_TABLES = {
    "TT": ("tt_signals",),
    "OTE": ("ote_signals", "ote_lab_signals"),
    "TRB": ("trb_signals",),
    "LH": ("lh_signals",),
    "Edge Lab": ("edge_lab_signals",),
    "V41P1": ("v41p1_signals",),
}

def connect_readonly(path: Path) -> sqlite3.Connection | None:
    if not path.exists():
        return None
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn

def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None

def count_table(conn: sqlite3.Connection, table: str) -> int | None:
    if not table_exists(conn, table):
        return None
    # Table names come only from the fixed allowlist above.
    return int(conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])

def latest_table_timestamp(conn: sqlite3.Connection, table: str) -> str | None:
    if not table_exists(conn, table):
        return None
    columns = {r["name"] for r in conn.execute(f'PRAGMA table_info("{table}")')}
    candidates = (
        "timestamp_setup", "signal_created_at", "timestamp_alert",
        "timestamp_snapshot", "emit_ts", "created_at", "last_updated"
    )
    col = next((c for c in candidates if c in columns), None)
    if not col:
        return None
    row = conn.execute(f'SELECT MAX("{col}") AS latest FROM "{table}"').fetchone()
    return row["latest"] if row and row["latest"] else None

def collect() -> dict[str, Any]:
    result: dict[str, Any] = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "signals_db": str(DB_PATH),
        "ledger_db": str(LEDGER_PATH),
        "strategies": {},
        "signals_total": 0,
        "ledger_rows": None,
        "latest_signal_at": None,
        "source_status": {},
    }
    db = connect_readonly(DB_PATH)
    if db is not None:
        all_latest = []
        for label, tables in STRATEGY_TABLES.items():
            detail = {}
            total = 0
            for table in tables:
                n = count_table(db, table)
                detail[table] = n
                if n is not None:
                    total += n
                latest = latest_table_timestamp(db, table)
                if latest:
                    all_latest.append(str(latest))
            result["strategies"][label] = {"count": total, "tables": detail}
            result["signals_total"] += total
        result["source_status"]["signals_db"] = "available"
        result["latest_signal_at"] = max(all_latest) if all_latest else None
        db.close()
    else:
        result["source_status"]["signals_db"] = "missing"

    ledger = connect_readonly(LEDGER_PATH)
    if ledger is not None:
        result["ledger_rows"] = count_table(ledger, "decision_ledger")
        result["source_status"]["decision_ledger"] = "available"
        ledger.close()
    else:
        result["source_status"]["decision_ledger"] = "missing"
    return result

def safe_json(data: Any) -> str:
    # Prevent a JSON string from terminating the embedding script element.
    return json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")

def update_template(template: str, data: dict[str, Any]) -> str:
    # Existing draft values are replaced by exact count-based patterns. The
    # template remains the source of layout, styling and browser-local journal.
    total = data["signals_total"]
    ledger = data["ledger_rows"]
    strat = data["strategies"]

    def val(name: str) -> int:
        return int(strat.get(name, {}).get("count", 0))

    # Header date chip.
    generated = datetime.fromisoformat(data["generated_at_utc"].replace("Z", "+00:00"))
    date_label = generated.strftime("%d %b %Y")
    template = re.sub(
        r'<span class="chip">Baseline · [^<]*</span>',
        f'<span class="chip">Snapshot · {html.escape(date_label)}</span>', template, count=1
    )

    # Overview metrics.
    template = re.sub(r'(<div class="cap">Signal records <span>⌁</span></div><strong>)[^<]*(</strong><small>)[^<]*(</small>)',
        rf'\g<1>{total:,}\g<2>6 strategy sources · {total:,} rows\g<3>', template, count=1)
    if ledger is not None:
        template = re.sub(r'(<div class="cap">Decision ledger <span>▤</span></div><strong>)[^<]*(</strong><small>)[^<]*(</small>)',
            rf'\g<1>{ledger:,}\g<2>Recorded decisions · read-only\g<3>', template, count=1)

    # Strategy rows: preserve original row markup and replace labels/count/bar width.
    max_count = max((val(k) for k in strat), default=1) or 1
    for label in ("TT", "OTE", "TRB", "LH", "Edge Lab", "V41P1"):
        count = val(label)
        # Locate row by the visible strategy title, then replace its count and bar width.
        pattern = re.compile(
            rf'(<div class="row"><b>{re.escape(label)}<small>.*?</b><div><div class="bar"><i style="--w:)[^%]*%(</i></div></div><div class="num">)[^<]*(</div></div>)',
            re.S
        )
        detail = ""
        if label == "OTE":
            tabs = strat.get("OTE", {}).get("tables", {})
            detail = f'<small>{tabs.get("ote_signals",0) or 0} + OTE-LAB {tabs.get("ote_lab_signals",0) or 0}</small>'
        replacement = rf'\g<1>{count/max_count*100:.1f}%\g<2>{count:,}\g<3>'
        template, n = pattern.subn(replacement, template, count=1)
        if n and label == "OTE" and detail:
            # Use the existing OTE sublabel; the generator keeps it truthful.
            template = re.sub(r'OTE<small>[^<]*</small>', "OTE" + detail, template, count=1)

    # Snapshot status and audit text are generated from actual data inventory only.
    source_status = "Available" if data["source_status"]["signals_db"] == "available" else "Missing"
    template = re.sub(r'(Signals database</span><span class="status">)[^<]*(</span>)',
                      rf'\g<1>{source_status} · {total:,} rows\g<2>', template, count=1)
    if ledger is not None:
        template = re.sub(r'(Decision ledger</span><span class="status">)[^<]*(</span>)',
                          rf'\g<1>{ledger:,} rows\g<2>', template, count=1)

    # Replace the audit paragraph with the current source inventory. This paragraph
    # must never retain stale counts from the template.
    ote_tables = strat.get("OTE", {}).get("tables", {})
    audit_text = (
        f"Audit snapshot: {total:,} signal rows across six strategy sources "
        f"(TT {val('TT')}, OTE {ote_tables.get('ote_signals', 0) or 0} + "
        f"OTE-LAB {ote_tables.get('ote_lab_signals', 0) or 0} = {val('OTE')}, "
        f"TRB {val('TRB')}, LH {val('LH')}, Edge Lab {val('Edge Lab')}, "
        f"V41P1 {val('V41P1')}); "
        f"{ledger:,} decision-ledger rows" if ledger is not None else
        f"Audit snapshot: {total:,} signal rows across six strategy sources; "
        "decision ledger missing"
    )
    audit_text += (
        ". Counts are read directly from SQLite. These are inventory counts, not "
        "executed-trade or P&L metrics. Profit, win rate, expectancy, drawdown, "
        "commissions and realized performance remain unverified until EA calculation "
        "rules are supplied. This output is a static snapshot; regenerate after source "
        "DB changes. MT5 execution history and Shadow A/B/C/D are not connected."
    )
    template = re.sub(
        r"Audit snapshot:.*?(?=</p>)",
        html.escape(audit_text),
        template,
        count=1,
        flags=re.S,
    )

    # Attach machine-readable snapshot for future AI/data views, without executing it.
    payload = '<script id="ai-journal-snapshot" type="application/json">' + safe_json(data) + '</script>'
    if 'id="ai-journal-snapshot"' in template:
        template = re.sub(r'<script id="ai-journal-snapshot" type="application/json">.*?</script>',
                         payload, template, flags=re.S)
    else:
        template = template.replace("</body>", payload + "\n</body>", 1)
    return template

def main() -> int:
    if not TEMPLATE_PATH.exists():
        print(f"ERROR: template not found: {TEMPLATE_PATH}", file=sys.stderr)
        print("Set AI_JOURNAL_TEMPLATE or add docs/ai_trading_journal_template.html.", file=sys.stderr)
        return 2
    data = collect()
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    output = update_template(template, data)
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(output, encoding="utf-8")
    print(f"AI Trading Journal generated: {OUT_PATH}")
    print(f"Signals: {data['signals_total']} | Ledger: {data['ledger_rows']} | Snapshot UTC: {data['generated_at_utc']}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
