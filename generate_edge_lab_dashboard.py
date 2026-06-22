"""
generate_edge_lab_dashboard.py
Edge Lab Analytics Dashboard — OTE-SC Phase 1A

Genera docs/edge_lab_dashboard.html

Sezioni:
    - Summary globale (N segnali, win rate, expectancy, MAE/MFE medi)
    - Performance per Asset (BTC / PAXG)
    - Performance per Sessione (ASIA / LONDON / OVERLAP / NEW_YORK)
    - Performance per Sessione di Riferimento
    - Quality Label breakdown (HIGH / MEDIUM / LOW)
    - Liquidity Target Analysis
    - Market Context heatmap (trend + sessione + volatilità)
    - Segnali recenti (ultimi 30)

Eseguito automaticamente dal workflow GitHub Actions (Step 12),
oppure manualmente: python3 generate_edge_lab_dashboard.py
"""

import sqlite3
import json
import os
from datetime import datetime, timezone

DB_PATH  = os.environ.get("DB_PATH", "data/signals.db")
OUT_PATH = "docs/edge_lab_dashboard.html"


# ============================================================
# Data loading
# ============================================================

def q(conn, sql, params=()):
    return conn.execute(sql, params).fetchall()


def load_closed_signals(conn):
    try:
        rows = q(conn, """
            SELECT asset, direction, session, ref_session,
                   trend_combined, vol_regime_m15,
                   quality_label, quality_score,
                   liquidity_target, liquidity_target_priority,
                   final_outcome, mae, mfe, rr, bars_open,
                   tradeability_flags, timestamp_setup
            FROM edge_lab_signals
            WHERE final_outcome != 'OPEN'
            ORDER BY timestamp_setup DESC
        """)
    except sqlite3.OperationalError:
        return []

    result = []
    for r in rows:
        try:
            flags = json.loads(r[15]) if r[15] else []
        except Exception:
            flags = []
        result.append({
            "asset":         r[0],
            "direction":     r[1],
            "session":       r[2] or "N/A",
            "ref_session":   r[3] or "N/A",
            "trend":         r[4] or "N/A",
            "vol":           r[5] or "N/A",
            "quality_label": r[6] or "N/A",
            "quality_score": r[7] or 0,
            "liq_target":    r[8] or "N/A",
            "liq_priority":  r[9] or "N/A",
            "outcome":       r[10],
            "mae":           float(r[11] or 0),
            "mfe":           float(r[12] or 0),
            "rr":            float(r[13] or 0),
            "bars_open":     int(r[14] or 0),
            "flags":         flags,
            "ts":            r[16] or "",
        })
    return result


def load_recent_signals(conn, limit=30):
    try:
        return q(conn, f"""
            SELECT signal_id, asset, direction, entry, stop_loss, tp, rr,
                   quality_score, quality_label, session, ref_session,
                   liquidity_target, trend_combined, final_outcome,
                   mae, mfe, bars_open, timestamp_setup
            FROM edge_lab_signals
            ORDER BY timestamp_setup DESC
            LIMIT {limit}
        """)
    except sqlite3.OperationalError:
        return []


def load_context_stats(conn):
    try:
        return q(conn, """
            SELECT trend_combined, current_session, vol_regime_m15,
                   COUNT(*) as n, AVG(CASE WHEN is_tradeable THEN 1 ELSE 0 END) as tradeable_rate
            FROM market_context_snapshots
            GROUP BY trend_combined, current_session, vol_regime_m15
            ORDER BY n DESC
            LIMIT 50
        """)
    except sqlite3.OperationalError:
        return []


# ============================================================
# Stats helpers
# ============================================================

def stats(rows):
    n = len(rows)
    if n == 0:
        return {"n": 0, "win": 0, "sl": 0, "exp": 0, "exp_r": 0,
                "avg_mae": 0, "avg_mfe": 0, "avg_rr": 0, "avg_bars": 0}
    wins = sum(1 for r in rows if r["outcome"] == "TP")
    sls  = sum(1 for r in rows if r["outcome"] == "SL")
    return {
        "n":        n,
        "win":      round(wins / n * 100, 1),
        "sl":       round(sls  / n * 100, 1),
        "exp":      round(wins / n * 100 - sls / n * 100, 1),
        "exp_r":    round((wins * 2 - sls) / n, 2),
        "avg_mae":  round(sum(r["mae"] for r in rows) / n, 1),
        "avg_mfe":  round(sum(r["mfe"] for r in rows) / n, 1),
        "avg_rr":   round(sum(r["rr"]  for r in rows) / n, 2),
        "avg_bars": round(sum(r["bars_open"] for r in rows) / n, 1),
    }


def breakdown(rows, key_fn, keys):
    return {k: stats([r for r in rows if key_fn(r) == k]) for k in keys}


# ============================================================
# HTML components
# ============================================================

CSS = """
@import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600&family=IBM+Plex+Sans:wght@300;400;600&display=swap');
:root{
  --bg:#0d0f14;--surface:#141720;--border:#1e2330;
  --accent:#4fffb0;--accent2:#ff6b6b;--accent3:#ffd166;
  --text:#e2e8f0;--dim:#5a6478;
  --buy:#4fffb0;--sell:#ff6b6b;
}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:'IBM Plex Sans',sans-serif;font-size:14px;line-height:1.6}
header{border-bottom:1px solid var(--border);padding:18px 32px;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px}
header h1{font-family:'IBM Plex Mono',monospace;font-size:13px;font-weight:600;letter-spacing:.12em;text-transform:uppercase;color:var(--accent)}
header .meta{font-family:'IBM Plex Mono',monospace;font-size:11px;color:var(--dim)}
header a{color:var(--accent);text-decoration:none;font-family:'IBM Plex Mono',monospace;font-size:11px}
.container{max-width:1320px;margin:0 auto;padding:24px 32px}
.summary-grid{display:grid;grid-template-columns:repeat(8,1fr);gap:1px;background:var(--border);border:1px solid var(--border);border-radius:6px;overflow:hidden;margin-bottom:20px}
.summary-grid>div{background:var(--surface);padding:16px 10px;text-align:center}
.big{font-family:'IBM Plex Mono',monospace;font-size:20px;font-weight:600}
.big.pos{color:var(--buy)} .big.neg{color:var(--sell)} .big.warn{color:var(--accent3)}
.lbl{font-size:9px;text-transform:uppercase;letter-spacing:.06em;color:var(--dim);display:block;margin-top:3px}
.grid-2{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:16px}
.grid-3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:16px;margin-bottom:16px}
.card{background:var(--surface);border:1px solid var(--border);border-radius:6px;overflow:hidden;margin-bottom:16px}
.ch{padding:10px 16px;border-bottom:1px solid var(--border);font-family:'IBM Plex Mono',monospace;font-size:11px;font-weight:600;letter-spacing:.1em;text-transform:uppercase;color:var(--dim)}
table{width:100%;border-collapse:collapse}
th{font-family:'IBM Plex Mono',monospace;font-size:10px;font-weight:600;text-transform:uppercase;letter-spacing:.08em;color:var(--dim);padding:9px 14px;text-align:left;border-bottom:1px solid var(--border)}
td{padding:8px 14px;border-bottom:1px solid var(--border);font-size:13px}
tr:last-child td{border-bottom:none} tr:hover td{background:rgba(255,255,255,.02)}
tr.hl td{background:rgba(79,255,176,.06)}
.mono{font-family:'IBM Plex Mono',monospace;font-size:12px}
.pos{color:var(--buy);font-weight:600} .neg{color:var(--sell)} .warn{color:var(--accent3)}
.badge{display:inline-block;padding:2px 8px;border-radius:4px;font-size:11px;font-family:'IBM Plex Mono',monospace;font-weight:600}
.b-buy{background:rgba(79,255,176,.15);color:var(--buy)}
.b-sell{background:rgba(255,107,107,.15);color:var(--sell)}
.b-tp{background:rgba(79,255,176,.15);color:var(--buy)}
.b-sl{background:rgba(255,107,107,.15);color:var(--sell)}
.b-exp{background:rgba(90,100,120,.2);color:var(--dim)}
.b-high{background:rgba(79,255,176,.15);color:var(--buy)}
.b-med{background:rgba(255,209,102,.15);color:var(--accent3)}
.b-low{background:rgba(90,100,120,.2);color:var(--dim)}
.section-title{font-family:'IBM Plex Mono',monospace;font-size:11px;font-weight:600;letter-spacing:.1em;text-transform:uppercase;color:var(--accent);margin:28px 0 12px;padding-bottom:6px;border-bottom:1px solid var(--border)}
.empty{text-align:center;padding:32px;color:var(--dim);font-size:13px}
@media(max-width:900px){.grid-2,.grid-3{grid-template-columns:1fr}.summary-grid{grid-template-columns:repeat(4,1fr)}.container{padding:12px}}
"""


def outcome_badge(outcome):
    cls = {"TP": "b-tp", "SL": "b-sl", "EXPIRED": "b-exp", "OPEN": "b-med"}.get(outcome, "b-exp")
    return f'<span class="badge {cls}">{outcome}</span>'


def direction_badge(d):
    cls = "b-buy" if d == "BUY" else "b-sell"
    return f'<span class="badge {cls}">{d}</span>'


def quality_badge(label):
    cls = {"HIGH": "b-high", "MEDIUM": "b-med", "LOW": "b-low"}.get(label, "b-low")
    return f'<span class="badge {cls}">{label}</span>'


def fmt_price(v):
    if v is None: return "—"
    v = float(v)
    return f"{v:,.2f}" if v > 1000 else f"{v:.4f}"


def fmt_ts(ts):
    if not ts: return "—"
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.strftime("%d %b %H:%M")
    except Exception:
        return ts[:16]


def perf_table(title, d, keys, key_label=""):
    body = ""
    for k in keys:
        v = d.get(k, stats([]))
        if v["n"] == 0:
            continue
        win_cls = "pos" if v["win"] >= 40 else ("neg" if v["win"] < 25 else "warn")
        exp_cls = "pos" if v["exp_r"] > 0 else "neg"
        body += f"""<tr>
  <td><strong>{k}</strong></td>
  <td class="mono">{v['n']}</td>
  <td class="mono {win_cls}">{v['win']}%</td>
  <td class="mono">{v['sl']}%</td>
  <td class="mono {exp_cls}">{v['exp_r']:+.2f}R</td>
  <td class="mono">{v['avg_rr']:.2f}</td>
  <td class="mono neg">{v['avg_mae']:.1f}</td>
  <td class="mono pos">{v['avg_mfe']:.1f}</td>
</tr>"""
    if not body:
        body = '<tr><td colspan="8" class="empty">Nessun dato</td></tr>'
    return f"""<div class="card">
  <div class="ch">{title}</div>
  <table><thead><tr>
    <th>{key_label}</th><th>N</th><th>Win%</th><th>SL%</th>
    <th>Expectancy</th><th>Avg R/R</th><th>Avg MAE</th><th>Avg MFE</th>
  </tr></thead><tbody>{body}</tbody></table>
</div>"""


def summary_boxes(s):
    win_cls  = "pos" if s["win"] >= 40 else ("neg" if s["win"] < 25 else "warn")
    expr_cls = "pos" if s["exp_r"] > 0 else "neg"
    return f"""<div class="summary-grid">
  <div><span class="big">{s['n']}</span><span class="lbl">Segnali chiusi</span></div>
  <div><span class="big {win_cls}">{s['win']}%</span><span class="lbl">Win Rate</span></div>
  <div><span class="big neg">{s['sl']}%</span><span class="lbl">SL Rate</span></div>
  <div><span class="big {expr_cls}">{s['exp_r']:+.2f}R</span><span class="lbl">Expectancy</span></div>
  <div><span class="big">{s['avg_rr']:.2f}</span><span class="lbl">Avg R/R</span></div>
  <div><span class="big neg">{s['avg_mae']:.1f}</span><span class="lbl">Avg MAE</span></div>
  <div><span class="big pos">{s['avg_mfe']:.1f}</span><span class="lbl">Avg MFE</span></div>
  <div><span class="big">{s['avg_bars']:.0f}</span><span class="lbl">Avg Bars</span></div>
</div>"""


def recent_signals_table(rows):
    if not rows:
        return '<div class="card"><div class="empty">Nessun segnale ancora.</div></div>'
    body = ""
    for r in rows:
        sid, asset, direction, entry, sl, tp, rr, qs, ql, sess, ref, liq_tgt, trend, outcome, mae, mfe, bars, ts = r
        body += f"""<tr>
  <td class="mono" style="color:var(--dim);font-size:11px">{fmt_ts(ts)}</td>
  <td><strong>{asset.replace('_USDT','')}</strong></td>
  <td>{direction_badge(direction)}</td>
  <td class="mono">{fmt_price(entry)}</td>
  <td class="mono">{fmt_price(sl)}</td>
  <td class="mono">{fmt_price(tp)}</td>
  <td class="mono">{float(rr or 0):.2f}</td>
  <td>{quality_badge(ql or 'N/A')}</td>
  <td style="color:var(--dim);font-size:12px">{sess or '—'}</td>
  <td style="color:var(--dim);font-size:12px">{liq_tgt or '—'}</td>
  <td>{outcome_badge(outcome)}</td>
  <td class="mono" style="color:var(--dim)">{int(bars or 0)}</td>
</tr>"""
    return f"""<div class="card">
  <div class="ch">Segnali Recenti (ultimi 30)</div>
  <table><thead><tr>
    <th>Data</th><th>Asset</th><th>Dir</th><th>Entry</th><th>SL</th><th>TP</th>
    <th>R/R</th><th>Quality</th><th>Session</th><th>Target</th><th>Outcome</th><th>Bars</th>
  </tr></thead><tbody>{body}</tbody></table>
</div>"""


def context_stats_table(rows):
    if not rows:
        return '<div class="card"><div class="empty">Nessuno snapshot di contesto ancora.</div></div>'
    body = ""
    for trend, sess, vol, n, trate in rows[:20]:
        trate_pct = round((trate or 0) * 100, 1)
        tr_cls = "pos" if trate_pct >= 70 else ("neg" if trate_pct < 40 else "warn")
        body += f"""<tr>
  <td class="mono">{trend or '—'}</td>
  <td class="mono">{sess or '—'}</td>
  <td class="mono">{vol or '—'}</td>
  <td class="mono">{n}</td>
  <td class="mono {tr_cls}">{trate_pct}%</td>
</tr>"""
    return f"""<div class="card">
  <div class="ch">Market Context Heatmap (top 20 combinazioni)</div>
  <table><thead><tr>
    <th>Trend</th><th>Sessione</th><th>Volatilità M15</th><th>N Scan</th><th>Tradeable%</th>
  </tr></thead><tbody>{body}</tbody></table>
</div>"""


# ============================================================
# Generate
# ============================================================

def generate():
    conn = sqlite3.connect(DB_PATH)
    signals  = load_closed_signals(conn)
    recent   = load_recent_signals(conn, 30)
    ctx_rows = load_context_stats(conn)
    conn.close()

    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    s_all     = stats(signals)

    # Breakdown
    asset_keys   = ["BTC_USDT", "PAXG_USDT"]
    sess_keys    = ["ASIA", "LONDON", "OVERLAP", "NEW_YORK"]
    ref_keys     = ["ASIA", "LONDON", "OVERLAP", "NEW_YORK", "EUROPEAN_COMPOSITE"]
    quality_keys = ["HIGH", "MEDIUM", "LOW"]
    trend_keys   = ["BULLISH", "BEARISH", "NEUTRAL", "TRANSITION"]

    bd_asset   = breakdown(signals, lambda r: r["asset"],       asset_keys)
    bd_sess    = breakdown(signals, lambda r: r["session"],     sess_keys)
    bd_ref     = breakdown(signals, lambda r: r["ref_session"], ref_keys)
    bd_quality = breakdown(signals, lambda r: r["quality_label"], quality_keys)
    bd_trend   = breakdown(signals, lambda r: r["trend"],       trend_keys)
    bd_dir     = breakdown(signals, lambda r: r["direction"],   ["BUY", "SELL"])

    # Liquidity targets
    liq_targets = sorted({r["liq_target"] for r in signals if r["liq_target"] != "N/A"})
    bd_liq = breakdown(signals, lambda r: r["liq_target"], liq_targets)

    # Summary boxes per asset
    def asset_summary(asset):
        rows = [r for r in signals if r["asset"] == asset]
        s = stats(rows)
        if s["n"] == 0:
            return f'<div style="color:var(--dim);padding:16px">Nessun segnale chiuso per {asset}</div>'
        win_cls = "pos" if s["win"] >= 40 else ("neg" if s["win"] < 25 else "warn")
        return f"""<div style="display:grid;grid-template-columns:repeat(4,1fr);gap:1px;background:var(--border)">
  <div style="background:var(--surface);padding:12px 8px;text-align:center">
    <div class="big">{s['n']}</div><div class="lbl">Segnali</div></div>
  <div style="background:var(--surface);padding:12px 8px;text-align:center">
    <div class="big {win_cls}">{s['win']}%</div><div class="lbl">Win Rate</div></div>
  <div style="background:var(--surface);padding:12px 8px;text-align:center">
    <div class="big {'pos' if s['exp_r']>0 else 'neg'}">{s['exp_r']:+.2f}R</div><div class="lbl">Expectancy</div></div>
  <div style="background:var(--surface);padding:12px 8px;text-align:center">
    <div class="big">{s['avg_rr']:.2f}</div><div class="lbl">Avg R/R</div></div>
</div>"""

    no_data_msg = "" if signals else """
<div class="card" style="border-color:var(--accent3)">
  <div class="ch" style="color:var(--accent3)">Dati non ancora disponibili</div>
  <div style="padding:20px;color:var(--dim)">
    L'Edge Lab ha appena iniziato. Le sezioni analitiche si popoleranno
    dopo i primi segnali chiusi (TP / SL / EXPIRED).
  </div>
</div>"""

    html = f"""<!DOCTYPE html>
<html lang="it">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Edge Lab — OTE-SC Analytics</title>
<style>{CSS}</style>
</head>
<body>
<header>
  <h1>⚡ Institutional Edge Lab — OTE-SC Analytics</h1>
  <div class="meta">
    {generated} &nbsp;|&nbsp;
    <a href="unified_dashboard.html">&larr; Dashboard</a> &nbsp;|&nbsp;
    <a href="analytics_dashboard.html">V4.1 Analytics</a>
  </div>
</header>
<div class="container">

{no_data_msg}

<div class="section-title">Overview Globale</div>
{summary_boxes(s_all)}

<div class="grid-2">
  <div>
    <div class="section-title">BTC_USDT</div>
    <div class="card">{asset_summary('BTC_USDT')}</div>
  </div>
  <div>
    <div class="section-title">PAXG_USDT</div>
    <div class="card">{asset_summary('PAXG_USDT')}</div>
  </div>
</div>

<div class="section-title">Performance per Dimensione</div>
<div class="grid-2">
  {perf_table("Per Asset", bd_asset, asset_keys, "Asset")}
  {perf_table("Per Direzione", bd_dir, ["BUY","SELL"], "Direzione")}
</div>
<div class="grid-2">
  {perf_table("Per Quality Label", bd_quality, quality_keys, "Quality")}
  {perf_table("Per Trend Combined", bd_trend, trend_keys, "Trend")}
</div>

<div class="section-title">Analisi Sessione</div>
<div class="grid-2">
  {perf_table("Per Sessione Corrente", bd_sess, sess_keys, "Sessione")}
  {perf_table("Per Sessione Riferimento", bd_ref, ref_keys, "Ref. Session")}
</div>

<div class="section-title">Liquidity Target Analysis</div>
{perf_table("Performance per Target", bd_liq, liq_targets, "Target")}

<div class="section-title">Market Context Heatmap</div>
{context_stats_table(ctx_rows)}

<div class="section-title">Segnali Recenti</div>
{recent_signals_table(recent)}

</div>
</body>
</html>"""

    os.makedirs("docs", exist_ok=True)
    with open(OUT_PATH, "w") as f:
        f.write(html)

    print(
        f"Edge Lab dashboard generata: {OUT_PATH} "
        f"({len(signals)} segnali chiusi, {len(recent)} recenti)"
    )


if __name__ == "__main__":
    generate()
