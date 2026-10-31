#!/usr/bin/env python3
"""Read-only, lightweight SQLite exporter for trading journal analysis.

Usage:
  python export_trading_data.py --signals-db signals.db --ledger-db decision_ledger.db --out ai_export

The source databases are opened with SQLite URI mode=ro. This script never writes to them.
"""
from __future__ import annotations
import argparse
import json
import gzip
import sqlite3
from pathlib import Path
from datetime import datetime, timezone

def connect_readonly(path: Path) -> sqlite3.Connection:
    # URI quoting handles spaces, parentheses and other filename characters.
    uri = path.resolve().as_uri() + "?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only = ON")
    return con

def json_value(value):
    if isinstance(value, bytes):
        return value.hex()
    return value

def export_database(db_path: Path, output_dir: Path, prefix: str) -> dict:
    con = connect_readonly(db_path)
    try:
        tables = [
            row["name"] for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        manifest = {
            "source": db_path.name,
            "exported_at_utc": datetime.now(timezone.utc).isoformat(),
            "read_only": True,
            "tables": {}
        }
        # Full export: include every user table, including candles and snapshots.
        for table in tables:
            # Table names come from sqlite_master, then are safely quoted.
            quoted = '"' + table.replace('"', '""') + '"'
            cursor = con.execute(f"SELECT * FROM {quoted}")
            rows = cursor.fetchall()
            records = []
            for row in rows:
                records.append({key: json_value(row[key]) for key in row.keys()})
            out_file = output_dir / f"{prefix}_{table}.json.gz"
            with gzip.open(out_file, "wt", encoding="utf-8", compresslevel=6) as f:
                json.dump({
                    "source_database": db_path.name,
                    "table": table,
                    "row_count": len(records),
                    "columns": [d[0] for d in cursor.description or []],
                    "records": records
                }, f, ensure_ascii=False, separators=(",", ":"))
            manifest["tables"][table] = {
                "row_count": len(records),
                "file": out_file.name,
                "bytes": out_file.stat().st_size
            }
        return manifest
    finally:
        con.close()

def main():
    parser = argparse.ArgumentParser(description="Read-only SQLite to JSON exporter")
    parser.add_argument("--signals-db", required=True, type=Path)
    parser.add_argument("--ledger-db", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()

    for path in (args.signals_db, args.ledger_db):
        if not path.is_file():
            parser.error(f"Database not found: {path}")

    args.out.mkdir(parents=True, exist_ok=True)
    manifests = [
        export_database(args.signals_db, args.out, "signals"),
        export_database(args.ledger_db, args.out, "ledger"),
    ]
    manifest = {
        "exported_at_utc": datetime.now(timezone.utc).isoformat(),
        "mode": "read-only",
        "source_databases": manifests,
    }
    with (args.out / "manifest.json").open("w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    print(json.dumps({
        "status": "ok",
        "out": str(args.out.resolve()),
        "table_count": sum(len(m["tables"]) for m in manifests),
        "rows": sum(t["row_count"] for m in manifests for t in m["tables"].values()),
        "manifest": str((args.out / "manifest.json").resolve()),
    }, indent=2))

if __ __name__ == "__main__":
    main()
