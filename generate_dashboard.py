"""
generate_dashboard.py
Legge signals.db e genera docs/index.html — dashboard mobile-first
per Crypto Signal Engine V2.1.
Eseguito da GitHub Actions ad ogni scan.
"""

import sqlite3
import os
from datetime import datetime, timezone

DB_PATH = "data/signals.db"
OUT_PATH = "docs/index.html"


def load_data():
    if not os.path.exists(DB_PATH):
        return [], []

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    signals = conn.execute("""
        SELECT signal_id, strategy_name, strategy_version, asset, direction,
               entry, stop_loss, take_profit, rr, raw_score, final_score,
               market_regime, trade_status, timestamp_setup, timestamp_closed,
               mae, mfe, bars_open
        FROM signals
        ORDER BY timestamp_setup DESC
        LIMIT 30
    """).fetchall()

    stats = conn.execute("""
        SELECT
            strategy_name,
            strategy_version,
            COUNT(*) as total,
            SUM(CASE WHEN trade_status = 'TP' THEN 1 ELSE 0 END) as tp_count,
            SUM(CASE WHEN trade_status = 'SL' THEN 1 ELSE 0 END) as sl_count,
            SUM(CASE WHEN trade_status = 'OPEN' THEN 1 ELSE 0 END) as open_count,
            SUM(CASE WHEN trade_status = 'EXPIRED' THEN 1 ELSE 0 END) as expired_count,
            AVG(CASE WHEN trade_status IN ('TP','SL') THEN rr ELSE NULL END) as avg_rr,
            AVG(raw_score) as avg_raw_score,
            AVG(final_score) as avg_final_score
        FROM signals
        GROUP BY strategy_name, strategy_version
        ORDER BY strategy_name
    """).fetchall()

    conn.close()
    return [dict(s) for s in signals], [dict(s) for s in stats]


def fmt_price(v):
    if v is None:
        return "—"
    v = float(v)
    if v > 1000:
        return f"{v:,.2f}"
    elif v > 1:
        return f"{v:.4f}"
    elif v > 0.001:
        return f"{v:.5f}"
    else:
        return f"{v:.8f}"


def fmt_score(raw, final):
    raw = int(raw) if raw is not None else "—"
    final = int(final) if final is not None else "—"
    if raw == final:
        return f"{raw}/10"
    return f"{raw}→{final}/10"


def win_rate(tp, sl):
    closed = tp + sl
    if closed == 0:
        return "—"
    return f"{tp/closed*100:.0f}%"


def profit_factor(tp, sl, avg_rr):
    if sl == 0:
        return "∞" if tp > 0 else "—"
    if avg_rr is None:
        return "—"
    return f"{(tp * avg_rr) / sl:.2f}"


def status_badge(status):
    colors = {
        "TP":        ("var(--bg-success)", "var(--tx-success)"),
        "SL":        ("var(--bg-danger)",  "var(--tx-danger)"),
        "OPEN":      ("var(--bg-info)",    "var(--tx-info)"),
        "EXPIRED":   ("var(--bg-warning)", "var(--tx-warning)"),
        "GENERATED": ("var(--bg-muted)",   "var(--tx-muted)"),
        "REJECTED":  ("var(--bg-muted)",   "var(--tx-muted)"),
    }
    bg, col = colors.get(status, ("var(--bg-muted)", "var(--tx-muted)"))
    return f'<span class="badge" style="background:{bg};color:{col};">{status}</span>'


def direction_badge(direction):
    if direction == "LONG":
        return '<span class="badge" style="background:var(--bg-info);color:var(--tx-info);">LONG</span>'
    return '<span class="badge" style="background:var(--bg-danger);color:var(--tx-danger);">SHORT</span>'


def fmt_ts(ts_str):
    if not ts_str:
        return "—"
    try:
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        return dt.strftime("%d %b %H:%M")
    except Exception:
        return ts_str[:16]


def generate(signals, stats):
    now = datetime.now(timezone.utc).strftime("%d %b %Y %H:%M UTC")

    total_signals = sum(s["total"] for s in stats)
    total_open    = sum(s["open_count"] for s in stats)
    total_tp      = sum(s["tp_count"] for s in stats)
    total_sl      = sum(s["sl_count"] for s in stats)

    global_wr = win_rate(total_tp, total_sl)
    global_pf = "—"
    if total_sl > 0 and stats:
        avg_rr_all = sum(s["avg_rr"] or 0 for s in stats) / len(stats)
        global_pf = f"{(total_tp * avg_rr_all) / total_sl:.2f}"

    strat_cards = ""
    for s in stats:
        wr         = win_rate(s["tp_count"], s["sl_count"])
        pf         = profit_factor(s["tp_count"], s["sl_count"], s["avg_rr"])
        avg_rr_str = f"{s['avg_rr']:.2f}" if s["avg_rr"] else "—"
        strat_cards += f"""
        <div class="card">
          <div class="card-header">
            <span class="card-title">{s['strategy_name']}</span>
            <span class="muted">{s['strategy_version']}</span>
          </div>
          <div class="grid2">
            <div class="metric-sm">
              <div class="metric-label">Signals</div>
              <div class="metric-value">{s['total']}</div>
              <div class="muted small">TP {s['tp_count']} / SL {s['sl_count']} / Open {s['open_count']}</div>
            </div>
            <div class="metric-sm">
              <div class="metric-label">Win rate</div>
              <div class="metric-value" style="color:var(--tx-success);">{wr}</div>
              <div class="muted small">PF {pf} / R/R {avg_rr_str}</div>
            </div>
          </div>
        </div>"""

    if not strat_cards:
        strat_cards = '<div class="empty">Nessuna strategia con segnali ancora.</div>'

    sig_cards = ""
    for s in signals:
        if s["trade_status"] in ("REJECTED", "GENERATED"):
            continue
        sig_cards += f"""
        <div class="card">
          <div class="card-header">
            <div>
              <div class="card-title">{s['asset']}</div>
              <div class="muted small" style="margin-top:2px;">{s['strategy_name']}</div>
            </div>
            <div style="display:flex;flex-direction:column;align-items:flex-end;gap:4px;">
              {status_badge(s['trade_status'])}
              {direction_badge(s['direction'])}
            </div>
          </div>
          <div class="divider"></div>
          <div class="grid4">
            <div>
              <div class="metric-label">Entry</div>
              <div class="small fw500">{fmt_price(s['entry'])}</div>
            </div>
            <div>
              <div class="metric-label">R/R</div>
              <div class="small fw500">{float(s['rr']):.2f}</div>
            </div>
            <div>
              <div class="metric-label">Score</div>
              <div class="small fw500">{fmt_score(s['raw_score'], s['final_score'])}</div>
            </div>
            <div>
              <div class="metric-label">Regime</div>
              <div class="small fw500">{(s['market_regime'] or '—')[:8]}</div>
            </div>
          </div>
          <div class="muted small" style="margin-top:6px;">{fmt_ts(s['timestamp_setup'])}</div>
        </div>"""

    if not sig_cards:
        sig_cards = '<div class="empty">Nessun segnale ancora. Il sistema scansiona ogni 15 minuti.</div>'

    html = f"""<!DOCTYPE html>
<html lang="it">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Signal Engine V2.1</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@tabler/icons-webfont@3.19.0/dist/tabler-icons.min.css">
<style>
:root{{
  --bg:#eeece6;--surface:#fff;--surface2:#f5f5f4;
  --border:rgba(0,0,0,0.12);--tx:#1a1a18;--tx2:#5f5e5a;
  --bg-success:#eaf3de;--tx-success:#3b6d11;
  --bg-danger:#fcebeb;--tx-danger:#a32d2d;
  --bg-info:#e6f1fb;--tx-info:#185fa5;
  --bg-warning:#faeeda;--tx-warning:#854f0b;
  --bg-muted:#f5f5f4;--tx-muted:#5f5e5a;
}}
@media(prefers-color-scheme:dark){{
  :root{{
    --bg:#111110;--surface:#1e1e1c;--surface2:#282825;
    --border:rgba(255,255,255,0.1);--tx:#f0ede6;--tx2:#b4b2a9;
    --bg-success:#173404;--tx-success:#c0dd97;
    --bg-danger:#501313;--tx-danger:#f7c1c1;
    --bg-info:#042c53;--tx-info:#b5d4f4;
    --bg-warning:#412402;--tx-warning:#fac775;
    --bg-muted:#282825;--tx-muted:#b4b2a9;
  }}
}}
*{{box-sizing:border-box;margin:0;padding:0;}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:var(--bg);color:var(--tx);min-height:100vh;padding-bottom:3rem;}}
.header{{background:var(--surface);border-bottom:0.5px solid var(--border);padding:1rem 1.25rem;position:sticky;top:0;z-index:10;display:flex;align-items:center;justify-content:space-between;}}
.header-title{{font-size:17px;font-weight:500;}}
.header-sub{{font-size:12px;color:var(--tx2);margin-top:2px;}}
.live{{font-size:11px;padding:4px 12px;border-radius:6px;background:var(--bg-success);color:var(--tx-success);font-weight:500;}}
.container{{max-width:600px;margin:0 auto;padding:1rem;}}
.section{{font-size:11px;font-weight:500;letter-spacing:0.06em;color:var(--tx2);text-transform:uppercase;margin:1.5rem 0 0.75rem;}}
.grid2{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;}}
.grid4{{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:6px;}}
.metric{{background:var(--surface2);border-radius:8px;padding:12px 14px;}}
.metric-label{{font-size:12px;color:var(--tx2);margin-bottom:4px;}}
.metric-value{{font-size:24px;font-weight:500;}}
.metric-sm{{background:var(--surface2);border-radius:8px;padding:8px 10px;}}
.card{{background:var(--surface);border:0.5px solid var(--border);border-radius:12px;padding:12px 14px;margin-bottom:8px;}}
.card-header{{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:10px;}}
.card-title{{font-size:14px;font-weight:500;}}
.badge{{font-size:11px;padding:2px 8px;border-radius:6px;font-weight:500;}}
.divider{{border-top:0.5px solid var(--border);margin:8px 0;}}
.muted{{color:var(--tx2);}}
.small{{font-size:12px;}}
.fw500{{font-weight:500;}}
.empty{{text-align:center;padding:2rem;color:var(--tx2);font-size:14px;}}
</style>
</head>
<body>
<div class="header">
  <div>
    <div class="header-title">Signal Engine V2.1</div>
    <div class="header-sub"><i class="ti ti-refresh" aria-hidden="true"></i> {now}</div>
  </div>
  <span class="live">LIVE</span>
</div>
<div class="container">
  <div class="section">Overview</div>
  <div class="grid2">
    <div class="metric"><div class="metric-label">Total signals</div><div class="metric-value">{total_signals}</div></div>
    <div class="metric"><div class="metric-label">Open</div><div class="metric-value" style="color:var(--tx-info);">{total_open}</div></div>
    <div class="metric"><div class="metric-label">Win rate</div><div class="metric-value" style="color:var(--tx-success);">{global_wr}</div></div>
    <div class="metric"><div class="metric-label">Profit factor</div><div class="metric-value">{global_pf}</div></div>
  </div>
  <div class="section">Per strategy</div>
  {strat_cards}
  <div class="section">Recent signals</div>
  {sig_cards}
</div>
</body>
</html>"""

    os.makedirs("docs", exist_ok=True)
    with open(OUT_PATH, "w") as f:
        f.write(html)
    print(f"Dashboard generata: {OUT_PATH} ({len(signals)} segnali, {len(stats)} strategie)")


if __name__ == "__main__":
    signals, stats = load_data()
    generate(signals, stats)
