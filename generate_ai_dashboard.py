#!/usr/bin/env python3
"""AI Trading Agent — Dashboard Data Collector.

Collects signals, KPIs, breakdowns, market context, and decision ledger
data from the signal engine's SQLite databases. Injects a rich JSON
payload into the HTML template for AI-driven daily analysis.

Read-only: does not send orders or modify any database.
"""
import os, sqlite3, json, re
from pathlib import Path
from datetime import datetime, timezone, timedelta
from collections import defaultdict

DB = Path(os.getenv("DB_PATH", "data/signals.db"))
LEDGER = Path(os.getenv("LEDGER_DB_PATH", "data/decision_ledger.db"))
TEMPLATE = Path(os.getenv("AI_JOURNAL_TEMPLATE", "docs/ai_trading_journal_template.html"))
OUT = Path(os.getenv("AI_JOURNAL_OUT", "docs/ai_trading_journal_dashboard_generated.html"))

# Epoch dates — filter the view, don't delete history.
LH_EPOCH_DATE = "2026-08-24T18:00:00"
TT_EPOCH_DATE = "2026-08-27T00:00:00"

SOURCES = {
    "OTE-LAB": ("ote_lab_signals", "timestamp_setup"),
    "TT":      ("tt_signals",      "signal_created_at"),
    "LH":      ("lh_signals",      "timestamp_setup"),
    "TRB":     ("trb_signals",     "timestamp_setup"),
    "V41P1":   ("v41p1_signals",   "timestamp_setup"),
}

# ── helpers ──────────────────────────────────────────────────

def connect(path):
    if not path.exists():
        return None
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    con.row_factory = sqlite3.Row
    return con

def q(con, sql, params=()):
    return con.execute(sql, params).fetchall()

def table_exists(con, table):
    return bool(con and con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone())

def first(row, *keys):
    for k in keys:
        v = row.get(k) if isinstance(row, dict) else None
        if v is not None:
            return v
    return None

def parse_dt(value):
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

def safe_float(v, default=0.0):
    try:
        return float(v) if v is not None else default
    except (ValueError, TypeError):
        return default

def date_key(ts_str):
    """Extract YYYY-MM-DD from an ISO timestamp string."""
    dt = parse_dt(ts_str)
    return dt.strftime("%Y-%m-%d") if dt else None


# ── KPI calculations per strategy (matches unified dashboard logic) ──

def calc_tt_kpis(con):
    """TT KPIs: TP + TRAIL = win, INVALIDATED excluded, result_r for expectancy."""
    epoch = TT_EPOCH_DATE
    try:
        rows = q(con, "SELECT status, COUNT(*) FROM tt_signals "
                 "WHERE status IN ('TP','SL','TRAIL','EXPIRED') AND signal_created_at > ? "
                 "GROUP BY status", (epoch,))
        d = {r[0]: r[1] for r in rows}
        n = sum(d.values())
        wins = d.get("TP", 0) + d.get("TRAIL", 0)
        sls = d.get("SL", 0)
        trails = d.get("TRAIL", 0)
        # Real result_r for expectancy
        r_rows = q(con, "SELECT result_r FROM tt_signals "
                   "WHERE status IN ('TP','SL','TRAIL') AND result_r IS NOT NULL "
                   "AND signal_created_at > ?", (epoch,))
        total_r = sum(safe_float(r[0]) for r in r_rows)
        opn = q(con, "SELECT COUNT(*) FROM tt_signals WHERE status IN ('SETUP','ENTRY') "
                "AND signal_created_at > ?", (epoch,))[0][0]
        inv = q(con, "SELECT COUNT(*) FROM tt_signals WHERE status='INVALIDATED' "
                "AND signal_created_at > ?", (epoch,))[0][0]
        return {"n": n, "open": opn, "wins": wins, "losses": sls, "trails": trails,
                "invalidated": inv, "total_r": round(total_r, 2),
                "win_rate": round(wins / n * 100, 1) if n > 0 else 0,
                "exp_r": round(total_r / n, 2) if n > 0 else 0}
    except sqlite3.OperationalError:
        return {"n": 0, "open": 0, "wins": 0, "losses": 0, "trails": 0,
                "invalidated": 0, "total_r": 0, "win_rate": 0, "exp_r": 0}


def calc_ote_kpis(con):
    """OTE-LAB KPIs: TP2_HIT + TP1_HIT + TRAIL_HIT = win, uses result_r."""
    try:
        rows = q(con, "SELECT final_outcome, COUNT(*), COALESCE(SUM(result_r),0) "
                 "FROM ote_lab_signals WHERE final_outcome NOT IN ('OPEN','') "
                 "GROUP BY final_outcome")
        d = {r[0]: {"n": r[1], "sum_r": r[2]} for r in rows}
        n = sum(v["n"] for v in d.values())
        wins = sum(v["n"] for k, v in d.items() if k in ("TP2_HIT", "TP1_HIT", "TRAIL_HIT"))
        total_r = sum(v["sum_r"] for v in d.values())
        opn = q(con, "SELECT COUNT(*) FROM ote_lab_signals WHERE final_outcome='OPEN'")[0][0]
        return {"n": n, "open": opn, "wins": wins, "losses": d.get("SL_HIT", {}).get("n", 0),
                "total_r": round(total_r, 2),
                "win_rate": round(wins / n * 100, 1) if n > 0 else 0,
                "exp_r": round(total_r / n, 2) if n > 0 else 0,
                "outcomes": {k: v["n"] for k, v in d.items()}}
    except sqlite3.OperationalError:
        return {"n": 0, "open": 0, "wins": 0, "losses": 0, "total_r": 0,
                "win_rate": 0, "exp_r": 0, "outcomes": {}}


def calc_trb_kpis(con):
    """TRB KPIs: TP2_HIT + TP1_HIT + TRAIL_HIT = win, R from rr1/rr2."""
    try:
        rows = q(con, "SELECT final_outcome, COUNT(*) FROM trb_signals "
                 "WHERE final_outcome!='OPEN' GROUP BY final_outcome")
        d = {r[0]: r[1] for r in rows}
        n = sum(d.values())
        wins = d.get("TP2_HIT", 0) + d.get("TP1_HIT", 0) + d.get("TRAIL_HIT", 0)
        # R calculation matching unified dashboard
        r_rows = q(con, "SELECT final_outcome, rr1, rr2 FROM trb_signals "
                   "WHERE final_outcome IN ('TP1_HIT','TP2_HIT','TRAIL_HIT','SL_HIT')")
        total_r = 0.0
        for o, rr1, rr2 in r_rows:
            if o == "TP2_HIT":     total_r += safe_float(rr2, 2.0)
            elif o == "TP1_HIT":   total_r += safe_float(rr1, 1.0)
            elif o == "TRAIL_HIT": total_r += safe_float(rr1, 0.0)
            elif o == "SL_HIT":    total_r -= 1.0
        opn = q(con, "SELECT COUNT(*) FROM trb_signals WHERE final_outcome='OPEN'")[0][0]
        return {"n": n, "open": opn, "wins": wins,
                "losses": d.get("SL_HIT", 0), "total_r": round(total_r, 2),
                "win_rate": round(wins / n * 100, 1) if n > 0 else 0,
                "exp_r": round(total_r / n, 2) if n > 0 else 0,
                "outcomes": d}
    except sqlite3.OperationalError:
        return {"n": 0, "open": 0, "wins": 0, "losses": 0, "total_r": 0,
                "win_rate": 0, "exp_r": 0, "outcomes": {}}


def calc_lh_kpis(con):
    """LH KPIs: TP + STAGE2_HIT = win, STAGE2_HIT = +0.90R."""
    epoch = LH_EPOCH_DATE
    try:
        rows = q(con, "SELECT final_outcome, rr FROM lh_signals "
                 "WHERE final_outcome!='OPEN' AND timestamp_setup > ?", (epoch,))
        n = len(rows)
        if n == 0:
            opn = q(con, "SELECT COUNT(*) FROM lh_signals WHERE final_outcome='OPEN' "
                    "AND timestamp_setup > ?", (epoch,))[0][0]
            return {"n": 0, "open": opn, "wins": 0, "losses": 0, "total_r": 0,
                    "win_rate": 0, "exp_r": 0, "outcomes": {}}
        wins = sum(1 for o, _ in rows if o in ("TP", "STAGE2_HIT"))
        losses = sum(1 for o, _ in rows if o == "SL")
        vals = []
        outcomes = defaultdict(int)
        for outcome, rr in rows:
            outcomes[outcome] += 1
            if outcome == "TP":          vals.append(safe_float(rr, 2.0))
            elif outcome == "STAGE2_HIT": vals.append(0.90)
            elif outcome == "SL":         vals.append(-1.0)
            else:                         vals.append(0.0)
        total_r = sum(vals)
        opn = q(con, "SELECT COUNT(*) FROM lh_signals WHERE final_outcome='OPEN' "
                "AND timestamp_setup > ?", (epoch,))[0][0]
        return {"n": n, "open": opn, "wins": wins, "losses": losses,
                "total_r": round(total_r, 2),
                "win_rate": round(wins / n * 100, 1),
                "exp_r": round(total_r / n, 2),
                "outcomes": dict(outcomes)}
    except sqlite3.OperationalError:
        return {"n": 0, "open": 0, "wins": 0, "losses": 0, "total_r": 0,
                "win_rate": 0, "exp_r": 0, "outcomes": {}}


def calc_v41p1_kpis(con):
    """V41P1 KPIs: TP = win (+2R), SL = loss (-1R)."""
    try:
        n = q(con, "SELECT COUNT(*) FROM v41p1_signals WHERE final_outcome!='OPEN'")[0][0]
        wins = q(con, "SELECT COUNT(*) FROM v41p1_signals WHERE final_outcome='TP'")[0][0]
        sls = q(con, "SELECT COUNT(*) FROM v41p1_signals WHERE final_outcome='SL'")[0][0]
        opn = q(con, "SELECT COUNT(*) FROM v41p1_signals WHERE final_outcome='OPEN'")[0][0]
        total_r = wins * 2 - sls
        return {"n": n, "open": opn, "wins": wins, "losses": sls,
                "total_r": round(total_r, 2),
                "win_rate": round(wins / n * 100, 1) if n > 0 else 0,
                "exp_r": round(total_r / n, 2) if n > 0 else 0}
    except sqlite3.OperationalError:
        return {"n": 0, "open": 0, "wins": 0, "losses": 0, "total_r": 0,
                "win_rate": 0, "exp_r": 0}


# ── Signal collection with full trade data ──

def get_epoch(strategy):
    if strategy == "TT":  return TT_EPOCH_DATE
    if strategy == "LH":  return LH_EPOCH_DATE
    return None

def outcome_col(table):
    return "status" if table == "tt_signals" else "final_outcome"

def collect_trades(con, strategy, table, date_col):
    """Collect all closed trades with full detail for analysis."""
    if not table_exists(con, table):
        return []
    epoch = get_epoch(strategy)
    oc = outcome_col(table)
    where = f'WHERE "{oc}" NOT IN (\'OPEN\',\'SETUP\',\'ENTRY\',\'\')'
    if epoch:
        where += f' AND "{date_col}" > \'{epoch}\''

    try:
        rows = [dict(r) for r in con.execute(f'SELECT * FROM "{table}" {where} ORDER BY "{date_col}" DESC')]
    except sqlite3.Error:
        return []

    trades = []
    for row in rows:
        outcome = row.get(oc, "")
        ts_setup = row.get(date_col) or row.get("timestamp_setup") or row.get("signal_created_at")
        ts_closed = row.get("timestamp_closed") or row.get("closed_at")

        # Determine R result
        result_r = row.get("result_r")
        if result_r is None:
            if strategy == "TRB":
                if outcome == "TP2_HIT":     result_r = safe_float(row.get("rr2"), 2.0)
                elif outcome == "TP1_HIT":   result_r = safe_float(row.get("rr1"), 1.0)
                elif outcome == "TRAIL_HIT": result_r = safe_float(row.get("rr1"), 0.0)
                elif outcome == "SL_HIT":    result_r = -1.0
                else:                        result_r = 0.0
            elif strategy == "LH":
                if outcome == "TP":           result_r = safe_float(row.get("rr"), 2.0)
                elif outcome == "STAGE2_HIT": result_r = 0.90
                elif outcome == "SL":         result_r = -1.0
                else:                         result_r = 0.0
            elif strategy == "V41P1":
                if outcome == "TP":   result_r = 2.0
                elif outcome == "SL": result_r = -1.0
                else:                 result_r = 0.0
            elif strategy == "TT":
                if outcome == "SL": result_r = -1.0
                elif outcome in ("TP", "TRAIL"):
                    result_r = safe_float(row.get("result_r"), 0.0)
                else: result_r = 0.0

        # Classify win/loss
        is_win = False
        if strategy == "TT":
            is_win = outcome in ("TP", "TRAIL")
        elif strategy in ("TRB", "OTE-LAB"):
            is_win = outcome in ("TP2_HIT", "TP1_HIT", "TRAIL_HIT")
        elif strategy == "LH":
            is_win = outcome in ("TP", "STAGE2_HIT")
        elif strategy == "V41P1":
            is_win = outcome == "TP"

        is_loss = outcome in ("SL", "SL_HIT")

        trade = {
            "id": row.get("signal_id") or row.get("id"),
            "strategy": strategy,
            "asset": row.get("asset"),
            "direction": row.get("direction"),
            "outcome": outcome,
            "is_win": is_win,
            "is_loss": is_loss,
            "result_r": round(safe_float(result_r), 3),
            "setup_at": ts_setup,
            "closed_at": ts_closed,
            "date": date_key(ts_setup),
            "entry": row.get("actual_entry") or row.get("entry") or row.get("planned_entry"),
            "sl": row.get("actual_sl") or row.get("stop_loss") or row.get("planned_sl"),
            "tp": row.get("actual_tp") or row.get("take_profit") or row.get("tp") or row.get("tp2"),
            "tp1": row.get("tp1"),
            "tp2": row.get("tp2"),
            "mae": safe_float(row.get("mae"), None),
            "mfe": safe_float(row.get("mfe"), None),
            "bars_open": row.get("bars_open"),
            "quality_label": row.get("quality_label"),
            "quality_score": row.get("quality_score"),
            "session": row.get("session"),
        }
        # Strategy-specific fields
        if strategy == "TT":
            trade.update({
                "poi_type": row.get("poi_type"),
                "pd_zone": row.get("pd_zone"),
                "setup_type": row.get("setup_type"),
                "planned_rr": row.get("planned_rr"),
            })
        elif strategy in ("TRB", "OTE-LAB"):
            trade.update({
                "entry_zone_type": row.get("entry_zone_type"),
                "trend_h1": row.get("trend_h1"),
                "trend_h4": row.get("trend_h4"),
                "adx": row.get("adx"),
                "liquidity_target": row.get("liquidity_target"),
                "rr1": row.get("rr1"),
                "rr2": row.get("rr2"),
            })
        elif strategy == "LH":
            trade.update({
                "swept_level_label": row.get("swept_level_label"),
                "sweep_direction": row.get("sweep_direction"),
                "trigger_type": row.get("trigger_type"),
                "tp_label": row.get("tp_label"),
                "confluence_count": row.get("confluence_count"),
                "pool_type": row.get("pool_type"),
            })
        elif strategy == "V41P1":
            trade.update({
                "trigger_types": row.get("trigger_types"),
                "liquidity_source": row.get("liquidity_source"),
                "liquidity_target": row.get("liquidity_target"),
                "ema_h4": row.get("ema_h4"),
                "momentum": row.get("momentum"),
            })
        trades.append(trade)
    return trades


# ── Breakdowns ──

def calc_breakdowns(trades):
    """Calculate performance breakdowns by asset, session, quality, direction."""
    breakdowns = {
        "by_asset": defaultdict(lambda: {"n": 0, "wins": 0, "losses": 0, "r": 0.0}),
        "by_session": defaultdict(lambda: {"n": 0, "wins": 0, "losses": 0, "r": 0.0}),
        "by_quality": defaultdict(lambda: {"n": 0, "wins": 0, "losses": 0, "r": 0.0}),
        "by_direction": defaultdict(lambda: {"n": 0, "wins": 0, "losses": 0, "r": 0.0}),
        "by_strategy": defaultdict(lambda: {"n": 0, "wins": 0, "losses": 0, "r": 0.0}),
    }
    for t in trades:
        for key, field in [
            ("by_asset", t.get("asset", "unknown")),
            ("by_session", t.get("session", "unknown")),
            ("by_quality", t.get("quality_label", "unknown")),
            ("by_direction", t.get("direction", "unknown")),
            ("by_strategy", t.get("strategy", "unknown")),
        ]:
            b = breakdowns[key][field or "unknown"]
            b["n"] += 1
            if t["is_win"]: b["wins"] += 1
            if t["is_loss"]: b["losses"] += 1
            b["r"] += t["result_r"]

    # Compute win_rate and exp_r for each bucket
    result = {}
    for key, buckets in breakdowns.items():
        result[key] = {}
        for name, b in buckets.items():
            n = b["n"]
            result[key][name] = {
                "n": n, "wins": b["wins"], "losses": b["losses"],
                "total_r": round(b["r"], 2),
                "win_rate": round(b["wins"] / n * 100, 1) if n > 0 else 0,
                "exp_r": round(b["r"] / n, 2) if n > 0 else 0,
            }
    return result


# ── MAE / MFE analysis ──

def analyze_mae_mfe(trades):
    """Analyze MAE/MFE for winning and losing trades separately."""
    winners = [t for t in trades if t["is_win"] and t["mfe"] is not None]
    losers = [t for t in trades if t["is_loss"] and t["mae"] is not None]

    def stats(values):
        if not values:
            return {"count": 0, "avg": 0, "median": 0, "max": 0, "min": 0}
        values = sorted(values)
        n = len(values)
        return {
            "count": n,
            "avg": round(sum(values) / n, 2),
            "median": round(values[n // 2], 2),
            "max": round(values[-1], 2),
            "min": round(values[0], 2),
        }

    # Winners: how much MFE vs actual R (profit left on table)
    winner_mfe = stats([t["mfe"] for t in winners])
    winner_r = stats([t["result_r"] for t in winners])

    # Losers: how deep MAE went
    loser_mae = stats([t["mae"] for t in losers])

    # Profit left on table: MFE - result_r for winners
    profit_left = []
    for t in winners:
        if t["mfe"] is not None and t["result_r"] is not None:
            entry = safe_float(t["entry"])
            sl = safe_float(t["sl"])
            if entry and sl and abs(entry - sl) > 0:
                risk = abs(entry - sl)
                mfe_r = t["mfe"] / risk if risk > 0 else 0
                left = mfe_r - t["result_r"]
                if left > 0:
                    profit_left.append({"id": t["id"], "strategy": t["strategy"],
                                        "asset": t["asset"], "result_r": t["result_r"],
                                        "mfe_r": round(mfe_r, 2), "left_r": round(left, 2)})

    # Sort by most profit left
    profit_left.sort(key=lambda x: -x["left_r"])

    return {
        "winners": {"mfe": winner_mfe, "result_r": winner_r},
        "losers": {"mae": loser_mae},
        "profit_left_on_table": profit_left[:20],  # top 20
    }


# ── Daily summary ──

def build_daily_summary(trades):
    """Group trades by date and compute daily KPIs."""
    by_date = defaultdict(list)
    for t in trades:
        d = t.get("date")
        if d:
            by_date[d].append(t)

    daily = []
    for date_str in sorted(by_date.keys(), reverse=True):
        day_trades = by_date[date_str]
        n = len(day_trades)
        wins = sum(1 for t in day_trades if t["is_win"])
        losses = sum(1 for t in day_trades if t["is_loss"])
        total_r = sum(t["result_r"] for t in day_trades)

        # Best and worst strategy of the day
        strat_r = defaultdict(float)
        strat_n = defaultdict(int)
        for t in day_trades:
            strat_r[t["strategy"]] += t["result_r"]
            strat_n[t["strategy"]] += 1
        best_strat = max(strat_r, key=strat_r.get) if strat_r else None
        worst_strat = min(strat_r, key=strat_r.get) if strat_r else None

        # Assets traded
        assets = list(set(t["asset"] for t in day_trades if t.get("asset")))

        daily.append({
            "date": date_str,
            "trades": n, "wins": wins, "losses": losses,
            "total_r": round(total_r, 2),
            "win_rate": round(wins / n * 100, 1) if n > 0 else 0,
            "best_strategy": best_strat,
            "best_strategy_r": round(strat_r.get(best_strat, 0), 2) if best_strat else 0,
            "worst_strategy": worst_strat,
            "worst_strategy_r": round(strat_r.get(worst_strat, 0), 2) if worst_strat else 0,
            "assets": assets,
            "strategies_active": list(strat_n.keys()),
        })
    return daily[:90]  # last 90 days


# ── Losing trades analysis ──

def analyze_losing_trades(trades):
    """Deep analysis of losing trades: patterns, avoidability signals."""
    losers = [t for t in trades if t["is_loss"]]
    if not losers:
        return {"count": 0, "patterns": {}}

    # Group losses by various dimensions
    by_session = defaultdict(int)
    by_quality = defaultdict(int)
    by_asset = defaultdict(int)
    by_strategy = defaultdict(int)
    by_direction = defaultdict(int)

    for t in losers:
        by_session[t.get("session", "unknown")] += 1
        by_quality[t.get("quality_label", "unknown")] += 1
        by_asset[t.get("asset", "unknown")] += 1
        by_strategy[t.get("strategy", "unknown")] += 1
        by_direction[t.get("direction", "unknown")] += 1

    # MAE analysis for losers — how quickly did they go against?
    mae_values = [t["mae"] for t in losers if t["mae"] is not None and t["mae"] > 0]
    mfe_values = [t["mfe"] for t in losers if t["mfe"] is not None and t["mfe"] > 0]

    # Trades that went in profit before hitting SL
    went_green = [t for t in losers if t["mfe"] is not None and t["mfe"] > 0]

    return {
        "count": len(losers),
        "by_session": dict(by_session),
        "by_quality": dict(by_quality),
        "by_asset": dict(by_asset),
        "by_strategy": dict(by_strategy),
        "by_direction": dict(by_direction),
        "avg_mae": round(sum(mae_values) / len(mae_values), 2) if mae_values else 0,
        "went_green_before_sl": len(went_green),
        "went_green_pct": round(len(went_green) / len(losers) * 100, 1) if losers else 0,
        "worst_losses": sorted(
            [{"id": t["id"], "strategy": t["strategy"], "asset": t["asset"],
              "result_r": t["result_r"], "mae": t["mae"], "mfe": t["mfe"],
              "session": t.get("session"), "quality": t.get("quality_label"),
              "date": t["date"]}
             for t in losers],
            key=lambda x: x["result_r"]
        )[:20],
    }


# ── Winning trades analysis ──

def analyze_winning_trades(trades):
    """Deep analysis of winning trades: MFE utilization, trailing effectiveness."""
    winners = [t for t in trades if t["is_win"]]
    if not winners:
        return {"count": 0}

    by_session = defaultdict(int)
    by_quality = defaultdict(int)
    by_strategy = defaultdict(int)

    for t in winners:
        by_session[t.get("session", "unknown")] += 1
        by_quality[t.get("quality_label", "unknown")] += 1
        by_strategy[t.get("strategy", "unknown")] += 1

    # MFE analysis — how much of the max favorable excursion was captured?
    mfe_capture = []
    for t in winners:
        if t["mfe"] is not None and t["mfe"] > 0 and t["result_r"] > 0:
            entry = safe_float(t["entry"])
            sl = safe_float(t["sl"])
            if entry and sl and abs(entry - sl) > 0:
                risk = abs(entry - sl)
                mfe_r = t["mfe"] / risk if risk > 0 else 0
                capture_pct = (t["result_r"] / mfe_r * 100) if mfe_r > 0 else 0
                mfe_capture.append(round(capture_pct, 1))

    return {
        "count": len(winners),
        "by_session": dict(by_session),
        "by_quality": dict(by_quality),
        "by_strategy": dict(by_strategy),
        "avg_r": round(sum(t["result_r"] for t in winners) / len(winners), 2),
        "avg_mfe_capture_pct": round(sum(mfe_capture) / len(mfe_capture), 1) if mfe_capture else 0,
        "best_wins": sorted(
            [{"id": t["id"], "strategy": t["strategy"], "asset": t["asset"],
              "result_r": t["result_r"], "mfe": t["mfe"],
              "session": t.get("session"), "quality": t.get("quality_label"),
              "date": t["date"]}
             for t in winners],
            key=lambda x: -x["result_r"]
        )[:20],
    }


# ── Market context snapshots ──

def collect_market_context(con, limit=200):
    """Collect recent market context snapshots for correlation analysis."""
    if not table_exists(con, "market_context_snapshots"):
        return []
    try:
        rows = [dict(r) for r in con.execute(
            "SELECT * FROM market_context_snapshots ORDER BY timestamp_snapshot DESC LIMIT ?",
            (limit,))]
        return [{
            "asset": r.get("asset"),
            "timestamp": r.get("timestamp_snapshot"),
            "price": r.get("current_price"),
            "trend_h4": r.get("trend_h4"),
            "trend_h1": r.get("trend_h1"),
            "trend_combined": r.get("trend_combined"),
            "session": r.get("current_session"),
            "vol_m15": r.get("vol_regime_m15"),
            "vol_h1": r.get("vol_regime_h1"),
            "atr_m15": r.get("atr_m15"),
            "atr_h1": r.get("atr_h1"),
            "macro_risk": r.get("macro_risk"),
            "is_tradeable": bool(r.get("is_tradeable")),
            "sr_buy": r.get("sr_score_buy"),
            "sr_sell": r.get("sr_score_sell"),
        } for r in rows]
    except sqlite3.Error:
        return []


# ── Decision ledger ──

def collect_ledger_summary(con):
    """Summary stats from the decision ledger including rejected analysis."""
    if not con or not table_exists(con, "decision_ledger"):
        return {"available": False}

    try:
        total = con.execute("SELECT COUNT(*) FROM decision_ledger").fetchone()[0]

        # Executed vs Rejected per strategy
        exec_rej = con.execute(
            "SELECT strategy, decision_type, COUNT(*) FROM decision_ledger "
            "GROUP BY strategy, decision_type ORDER BY strategy").fetchall()
        breakdown = {}
        for s, dt, c in exec_rej:
            if s not in breakdown:
                breakdown[s] = {}
            breakdown[s][dt] = c

        # Rejected analysis: what gates block the most?
        reject_gates = con.execute(
            "SELECT reject_gate, COUNT(*) FROM decision_ledger "
            "WHERE decision_type='REJECTED' AND reject_gate IS NOT NULL "
            "GROUP BY reject_gate ORDER BY COUNT(*) DESC LIMIT 20").fetchall()

        # Outcomes of executed trades
        exec_outcomes = con.execute(
            "SELECT strategy, outcome, COUNT(*) FROM decision_ledger "
            "WHERE decision_type='EXECUTED' GROUP BY strategy, outcome").fetchall()
        exec_data = defaultdict(dict)
        for s, o, c in exec_outcomes:
            exec_data[s][o] = c

        # Date range
        dr = con.execute("SELECT MIN(ts_iso), MAX(ts_iso) FROM decision_ledger").fetchone()

        return {
            "available": True,
            "total_rows": total,
            "date_range": {"from": dr[0][:10] if dr[0] else None,
                           "to": dr[1][:10] if dr[1] else None},
            "by_strategy": breakdown,
            "top_reject_gates": dict(reject_gates),
            "executed_outcomes": dict(exec_data),
        }
    except sqlite3.Error as e:
        return {"available": False, "error": str(e)}


# ── Raw signal inventory (kept from original) ──

def collect_raw_signals(con, table, date_col, cutoff):
    """Collect raw signal rows for the inventory view (30-day window)."""
    if not table_exists(con, table):
        return {"available": False, "count": 0, "rows": []}
    try:
        rows = [dict(r) for r in con.execute(f'SELECT * FROM "{table}"')]
    except sqlite3.Error:
        return {"available": True, "count": 0, "rows": []}

    normalized = []
    for row in rows:
        stamp = row.get(date_col) or row.get("created_at") or row.get("timestamp")
        parsed = parse_dt(stamp)
        if parsed is not None and parsed < cutoff:
            continue
        oc = "status" if table == "tt_signals" else "final_outcome"
        normalized.append({
            "id": row.get("signal_id") or row.get("id"),
            "asset": row.get("asset"),
            "direction": row.get("direction"),
            "outcome": row.get(oc),
            "setup_at": stamp,
            "entry": row.get("actual_entry") or row.get("entry"),
            "sl": row.get("actual_sl") or row.get("stop_loss"),
            "tp": row.get("actual_tp") or row.get("take_profit") or row.get("tp") or row.get("tp2"),
            "mae": row.get("mae"),
            "mfe": row.get("mfe"),
            "result_r": row.get("result_r"),
            "quality": row.get("quality_label"),
            "session": row.get("session"),
        })
    return {"available": True, "count": len(normalized), "rows": normalized}


# ── Main ──

def main():
    now = datetime.now(timezone.utc)
    cutoff_30d = now - timedelta(days=30)

    data = {
        "generated_utc": now.isoformat(),
        "source_db": str(DB),
        "ledger_db": str(LEDGER),
        "epoch_dates": {"LH": LH_EPOCH_DATE, "TT": TT_EPOCH_DATE},
        "mode": "AI Trading Agent — Full analysis payload",
        "performance_note": "KPIs calculated from scanner signals using same logic as unified dashboard. "
                            "Not MT5 execution — spread, slippage, commissions not included.",
    }

    con = connect(DB)
    if not con:
        data["error"] = "signals database not found"
        _write_output(data)
        return

    try:
        # 1. KPIs per strategy
        data["kpis"] = {
            "TT":      calc_tt_kpis(con),
            "OTE-LAB": calc_ote_kpis(con),
            "TRB":     calc_trb_kpis(con),
            "LH":      calc_lh_kpis(con),
            "V41P1":   calc_v41p1_kpis(con),
        }

        # 2. Collect all trades
        all_trades = []
        for name, (table, date_col) in SOURCES.items():
            trades = collect_trades(con, name, table, date_col)
            all_trades.extend(trades)
        data["trades_count"] = len(all_trades)

        # 3. Breakdowns
        data["breakdowns"] = calc_breakdowns(all_trades)

        # 4. MAE/MFE analysis
        data["mae_mfe"] = analyze_mae_mfe(all_trades)

        # 5. Daily summary
        data["daily_summary"] = build_daily_summary(all_trades)

        # 6. Losing trades analysis
        data["losing_analysis"] = analyze_losing_trades(all_trades)

        # 7. Winning trades analysis
        data["winning_analysis"] = analyze_winning_trades(all_trades)

        # 8. Market context
        data["market_context"] = collect_market_context(con)

        # 9. Raw signal inventory (30-day, for the table view)
        data["signal_inventory"] = {}
        for name, (table, date_col) in SOURCES.items():
            data["signal_inventory"][name] = collect_raw_signals(con, table, date_col, cutoff_30d)
        data["total_30d"] = sum(s["count"] for s in data["signal_inventory"].values())

        # 10. Trade list (full, for AI agent consumption)
        data["trades"] = all_trades

    finally:
        con.close()

    # Decision ledger
    ledger_con = connect(LEDGER)
    if ledger_con:
        try:
            data["ledger"] = collect_ledger_summary(ledger_con)
        finally:
            ledger_con.close()
    else:
        data["ledger"] = {"available": False}

    _write_output(data)
    print(f"generated {OUT} · trades={data['trades_count']} · "
          f"30d_signals={data['total_30d']} · ledger={'yes' if data['ledger'].get('available') else 'no'} · "
          f"read-only")


def _write_output(data):
    payload = json.dumps(data, ensure_ascii=False, default=str,
                         separators=(",", ":")).replace("</", "<\\/")
    template = TEMPLATE.read_text(encoding="utf-8")
    block = f'<script id="ai-journal-payload" type="application/json">{payload}</script>'
    marker = "<!-- AI_JOURNAL_PAYLOAD -->"
    if marker in template:
        template = template.replace(marker, block)
    elif 'id="ai-journal-payload"' in template:
        template = re.sub(
            r'<script id="ai-journal-payload" type="application/json">.*?</script>',
            block, template, flags=re.S)
    else:
        template = template.replace("</body>", block + "</body>")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(template, encoding="utf-8")


if __name__ == "__main__":
    main()
