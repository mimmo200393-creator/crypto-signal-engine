"""
generate_unified_dashboard.py
Dashboard unificata V3.2 + V4.0 + V4.1 — Crypto Signal Engine.
Legge data/signals.db, genera docs/unified_dashboard.html.
Eseguito automaticamente dal workflow GitHub Actions ad ogni scan.
"""

import sqlite3
import json
import os
from datetime import datetime, timezone, timedelta

DB_PATH = os.environ.get("DB_PATH", "data/signals.db")
OUT_PATH = "docs/unified_dashboard.html"


def q(conn, sql, params=()):
    return conn.execute(sql, params).fetchall()


def stats(rows):
    n = len(rows)
    if n == 0:
        return {"n": 0, "wr": 0, "tp1": 0, "sl": 0, "exp_r": 0,
                "avg_mfe": 0, "avg_mae": 0, "expired": 0}
    wins  = sum(1 for r in rows if r["outcome"] == "TP")
    tp1h  = sum(1 for r in rows if r["tp1_hit"])
    sls   = sum(1 for r in rows if r["outcome"] == "SL")
    exps  = sum(1 for r in rows if r["outcome"] == "EXPIRED")
    return {
        "n":       n,
        "wr":      round(wins / n * 100, 1),
        "tp1":     round(tp1h / n * 100, 1),
        "sl":      round(sls  / n * 100, 1),
        "expired": round(exps / n * 100, 1),
        "exp_r":   round(((wins * 2) - sls) / n, 2),
        "avg_mfe": round(sum(r["mfe"] or 0 for r in rows) / n, 1),
        "avg_mae": round(sum(r["mae"] or 0 for r in rows) / n, 1),
    }


def breakdown(rows, key_fn, keys):
    return {k: stats([r for r in rows if key_fn(r) == k]) for k in keys}


def load_v3(conn):
    rows = q(conn, """SELECT asset, direction, session, final_outcome,
                             mae, mfe, tp1, entry, stop_loss, timestamp_setup
                      FROM v3_signals WHERE final_outcome != 'OPEN'
                      ORDER BY timestamp_setup DESC""")
    result = []
    for r in rows:
        entry, sl = r[7] or 0, r[8] or 0
        risk = abs(entry - sl) if entry and sl else 1
        tp1_hit = False
        if r[6]:
            tp1_hit = (r[6] >= entry + risk) if r[1] == "BUY" else (r[6] <= entry - risk)
        result.append({"asset": r[0], "direction": r[1], "session": r[2],
                        "outcome": r[3], "mae": r[4], "mfe": r[5],
                        "tp1_hit": tp1_hit, "ts": r[9], "quality": None, "trigger": "—"})
    return result


def load_v4(conn):
    rows = q(conn, """SELECT asset, direction, session, final_outcome,
                             mae, mfe, tp1, entry, stop_loss, quality_label, timestamp_setup
                      FROM v4_signals WHERE final_outcome != 'OPEN'
                      ORDER BY timestamp_setup DESC""")
    result = []
    for r in rows:
        entry, sl = r[7] or 0, r[8] or 0
        risk = abs(entry - sl) if entry and sl else 1
        tp1_hit = False
        if r[6]:
            tp1_hit = (r[6] >= entry + risk) if r[1] == "BUY" else (r[6] <= entry - risk)
        result.append({"asset": r[0], "direction": r[1], "session": r[2],
                        "outcome": r[3], "mae": r[4], "mfe": r[5],
                        "tp1_hit": tp1_hit, "ts": r[10], "quality": r[9], "trigger": "—"})
    return result


def load_v41(conn):
    rows = q(conn, """SELECT asset, direction, session, final_outcome,
                             mae, mfe, tp1_hit, trigger_types, quality_label, timestamp_setup
                      FROM v41_signals WHERE final_outcome != 'OPEN'
                      ORDER BY timestamp_setup DESC""")
    result = []
    for r in rows:
        try:
            types = json.loads(r[7]) if r[7] else []
        except Exception:
            types = []
        trigger = "+".join(types) if types else "—"
        result.append({"asset": r[0], "direction": r[1], "session": r[2],
                        "outcome": r[3], "mae": r[4], "mfe": r[5],
                        "tp1_hit": bool(r[6]), "ts": r[9], "quality": r[8], "trigger": trigger})
    return result


def load_v41_open(conn):
    """Carica segnali OPEN di V4.1 con MAE/MFE aggiornati."""
    rows = q(conn, """SELECT asset, direction, session, entry, stop_loss,
                             tp1, tp2, quality_label, quality_score,
                             trigger_types, mae, mfe, tp1_hit, timestamp_setup,
                             liquidity_source, liquidity_target, expected_move_points
                      FROM v41_signals WHERE final_outcome = 'OPEN'
                      ORDER BY timestamp_setup DESC""")
    result = []
    now = datetime.now(timezone.utc)
    for r in rows:
        try:
            types = json.loads(r[9]) if r[9] else []
        except Exception:
            types = []
        trigger = "+".join(types) if types else "—"
        ts = r[13]
        try:
            setup_dt = datetime.fromisoformat(ts)
            if setup_dt.tzinfo is None:
                setup_dt = setup_dt.replace(tzinfo=timezone.utc)
            elapsed_h = round((now - setup_dt).total_seconds() / 3600, 1)
        except Exception:
            elapsed_h = 0
        result.append({
            "asset": r[0], "direction": r[1], "session": r[2],
            "entry": r[3], "sl": r[4], "tp1": r[5], "tp2": r[6],
            "quality": r[7], "quality_score": r[8],
            "trigger": trigger,
            "mae": r[10], "mfe": r[11], "tp1_hit": bool(r[12]),
            "ts": ts, "elapsed_h": elapsed_h,
            "source": r[14] or "N/A", "target": r[15] or "N/A",
            "em": r[16],
        })
    return result


def fmt_price(v):
    if v is None: return "—"
    if v > 1000: return f"{v:,.2f}"
    return f"{v:.4f}"


def open_signal_rows(rows):
    if not rows:
        return '<tr><td colspan="10" style="text-align:center;color:var(--dim);padding:20px">Nessun segnale in monitoraggio</td></tr>'
    h = ""
    for r in rows:
        dc = "buy" if r["direction"] == "BUY" else "sell"
        ql = r["quality"] or "—"
        ql_cls = f"ql-{ql.lower()}" if ql in ("HIGH", "MEDIUM", "LOW") else "dim"
        a = r["asset"].replace("_USDT", "")
        mae_str = f"{r['mae']:.1f}" if r["mae"] is not None else "—"
        mfe_str = f"{r['mfe']:.1f}" if r["mfe"] is not None else "—"
        tp1b = '<span class="badge tp1">TP1</span>' if r["tp1_hit"] else ""
        em_str = f"{r['em']:.0f}pt" if r["em"] is not None else "—"
        elapsed = f"{r['elapsed_h']}h"
        h += f"""<tr>
  <td><strong>{a}</strong></td>
  <td class="{dc}">{r["direction"]}</td>
  <td class="mono">{fmt_price(r["entry"])}</td>
  <td class="mono neg">{fmt_price(r["sl"])}</td>
  <td class="mono">{fmt_price(r["tp1"])} {tp1b}</td>
  <td class="mono">{fmt_price(r["tp2"])}</td>
  <td><span class="{ql_cls}">{ql}</span> <span class="dim">({r["quality_score"]}/12)</span></td>
  <td class="mono neg">{mae_str}</td>
  <td class="mono pos">{mfe_str}</td>
  <td class="mono dim">{elapsed}</td>
</tr>"""
    return h


def fw_kpi(s, label, color_var):
    wr_cls  = "neg" if s["wr"] < 40 else ("pos" if s["wr"] >= 55 else "")
    exp_cls = "neg" if s["exp_r"] < 0 else "pos"
    return f"""
<div class="fw-kpi">
  <div class="fw-label" style="border-top:2px solid var({color_var});color:var({color_var})">{label}</div>
  <div class="fw-grid">
    <div><span class="big {wr_cls}">{s["wr"]}%</span><br><span class="lbl">Win Rate</span></div>
    <div><span class="big">{s["tp1"]}%</span><br><span class="lbl">TP1 Hit</span></div>
    <div><span class="big neg">{s["sl"]}%</span><br><span class="lbl">SL Rate</span></div>
    <div><span class="big neutral">{s["expired"]}%</span><br><span class="lbl">Expired</span></div>
    <div><span class="big {exp_cls}">{s["exp_r"]:+.2f}R</span><br><span class="lbl">Expectancy</span></div>
    <div><span class="big">{s["n"]}</span><br><span class="lbl">Segnali</span></div>
  </div>
</div>"""


def breakdown_table(rows, key_fn, keys, title):
    d = breakdown(rows, key_fn, keys)
    body = ""
    for k in keys:
        v = d[k]
        if v["n"] == 0:
            continue
        bar = int(v["wr"])
        ec = "pos" if v["exp_r"] > 0 else "neg"
        body += f"""<tr>
  <td><strong>{k}</strong></td>
  <td class="mono">{v["n"]}</td>
  <td><div class="bw"><div class="b" style="width:{bar}%"></div></div>
      <span class="mono">{v["wr"]}%</span></td>
  <td class="mono">{v["tp1"]}%</td>
  <td class="mono neg">{v["sl"]}%</td>
  <td class="mono {ec}">{v["exp_r"]:+.2f}R</td>
</tr>"""
    return f"""<div class="card">
  <div class="ch">{title}</div>
  <table><thead><tr><th></th><th>N</th><th>Win%</th><th>TP1%</th><th>SL%</th><th>Exp</th></tr></thead>
  <tbody>{body}</tbody></table></div>"""


def outcome_badge(o):
    if o == "TP":  return '<span class="badge win">TP2</span>'
    if o == "SL":  return '<span class="badge loss">SL</span>'
    return '<span class="badge exp">EXP</span>'


def signal_rows(rows):
    h = ""
    for r in rows[:10]:
        ts = r["ts"][:16].replace("T", " ") if r["ts"] else "—"
        a  = r["asset"].replace("_USDT", "")
        dc = "buy" if r["direction"] == "BUY" else "sell"
        ql = r["quality"] or "—"
        ql_cls = f"ql-{ql.lower()}" if ql in ("HIGH", "MEDIUM", "LOW") else "dim"
        tp1b = '<span class="badge tp1">TP1</span>' if r["tp1_hit"] else ""
        h += f"""<tr>
  <td class="mono dim" style="font-size:11px">{ts}</td>
  <td><strong>{a}</strong></td>
  <td class="{dc}">{r["direction"]}</td>
  <td class="mono dim" style="font-size:11px">{r["trigger"]}</td>
  <td><span class="{ql_cls}">{ql}</span></td>
  <td>{r["session"] or "—"}</td>
  <td>{outcome_badge(r["outcome"])} {tp1b}</td>
</tr>"""
    return h


def generate():
    conn = sqlite3.connect(DB_PATH)

    v3  = load_v3(conn)
    v4  = load_v4(conn)
    v41 = load_v41(conn)
    v41_open = load_v41_open(conn)

    sv3  = stats(v3)
    sv4  = stats(v4)
    sv41 = stats(v41)

    open_v3  = q(conn, "SELECT COUNT(*) FROM v3_signals  WHERE final_outcome='OPEN'")[0][0]
    open_v4  = q(conn, "SELECT COUNT(*) FROM v4_signals  WHERE final_outcome='OPEN'")[0][0]
    open_v41 = q(conn, "SELECT COUNT(*) FROM v41_signals WHERE final_outcome='OPEN'")[0][0]

    conn.close()

    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    def trigger_key(r):
        t = r["trigger"]
        if "BOS" in t and "CHOCH" in t: return "BOS+CHOCH"
        if "BOS"   in t: return "BOS"
        if "CHOCH" in t: return "CHOCH"
        return "OTHER"

    open_rows_html = open_signal_rows(v41_open)

    html = f"""<!DOCTYPE html>
<html lang="it">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Crypto Signal Engine — Dashboard</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600&family=IBM+Plex+Sans:wght@300;400;600&display=swap');
:root{{
  --bg:#0d0f14;--surface:#141720;--border:#1e2330;
  --accent:#4fffb0;--accent2:#ff6b6b;--text:#e2e8f0;--dim:#5a6478;
  --buy:#4fffb0;--sell:#ff6b6b;--high:#ffd166;--medium:#a0b4c8;--low:#5a6478;
  --v3:#7b9cff;--v4:#c084fc;--v41:#4fffb0;
}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:var(--bg);color:var(--text);font-family:'IBM Plex Sans',sans-serif;font-size:14px;line-height:1.6}}
header{{border-bottom:1px solid var(--border);padding:18px 32px;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px}}
header h1{{font-family:'IBM Plex Mono',monospace;font-size:13px;font-weight:600;letter-spacing:.12em;text-transform:uppercase;color:var(--accent)}}
header .meta{{font-family:'IBM Plex Mono',monospace;font-size:11px;color:var(--dim)}}
.container{{max-width:1320px;margin:0 auto;padding:24px 32px}}
.fw-row{{display:grid;grid-template-columns:repeat(3,1fr);gap:16px;margin-bottom:24px}}
.fw-kpi{{background:var(--surface);border:1px solid var(--border);border-radius:6px;overflow:hidden}}
.fw-label{{padding:10px 16px;font-family:'IBM Plex Mono',monospace;font-size:11px;font-weight:600;letter-spacing:.1em;text-transform:uppercase;border-bottom:1px solid var(--border)}}
.fw-grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:1px;background:var(--border)}}
.fw-grid>div{{background:var(--surface);padding:14px 12px;text-align:center}}
.fw-grid .big{{font-family:'IBM Plex Mono',monospace;font-size:22px;font-weight:600;color:var(--text)}}
.fw-grid .big.pos{{color:var(--buy)}} .fw-grid .big.neg{{color:var(--sell)}} .fw-grid .big.neutral{{color:var(--dim)}}
.fw-grid .lbl{{font-size:10px;text-transform:uppercase;letter-spacing:.08em;color:var(--dim);display:block;margin-top:2px}}
.card{{background:var(--surface);border:1px solid var(--border);border-radius:6px;overflow:hidden;margin-bottom:16px}}
.ch{{padding:10px 16px;border-bottom:1px solid var(--border);font-family:'IBM Plex Mono',monospace;font-size:11px;font-weight:600;letter-spacing:.1em;text-transform:uppercase;color:var(--dim)}}
.ch.live{{color:var(--accent)}}
.pulse{{display:inline-block;width:7px;height:7px;border-radius:50%;background:var(--accent);margin-right:6px;animation:pulse 2s infinite}}
@keyframes pulse{{0%,100%{{opacity:1}}50%{{opacity:.3}}}}
.tabs{{display:flex;gap:4px;margin-bottom:16px;border-bottom:1px solid var(--border)}}
.tab{{padding:8px 20px;font-family:'IBM Plex Mono',monospace;font-size:11px;font-weight:600;letter-spacing:.08em;text-transform:uppercase;cursor:pointer;border-bottom:2px solid transparent;color:var(--dim);transition:.15s;margin-bottom:-1px}}
.tab.active.t-v41{{color:var(--v41);border-color:var(--v41)}}
.tab.active.t-v4{{color:var(--v4);border-color:var(--v4)}}
.tab.active.t-v3{{color:var(--v3);border-color:var(--v3)}}
.tab-content{{display:none}} .tab-content.active{{display:block}}
table{{width:100%;border-collapse:collapse}}
th{{font-family:'IBM Plex Mono',monospace;font-size:10px;font-weight:600;text-transform:uppercase;letter-spacing:.08em;color:var(--dim);padding:9px 14px;text-align:left;border-bottom:1px solid var(--border)}}
td{{padding:8px 14px;border-bottom:1px solid var(--border);font-size:13px}}
tr:last-child td{{border-bottom:none}} tr:hover td{{background:rgba(255,255,255,.02)}}
.mono{{font-family:'IBM Plex Mono',monospace;font-size:12px}} .dim{{color:var(--dim)}}
.buy{{color:var(--buy);font-weight:600}} .sell{{color:var(--sell);font-weight:600}}
.pos{{color:var(--buy)}} .neg{{color:var(--sell)}} .neutral{{color:var(--dim)}}
.badge{{display:inline-block;padding:2px 6px;border-radius:3px;font-family:'IBM Plex Mono',monospace;font-size:10px;font-weight:600}}
.badge.win{{background:rgba(79,255,176,.12);color:var(--buy)}}
.badge.loss{{background:rgba(255,107,107,.12);color:var(--sell)}}
.badge.exp{{background:rgba(90,100,120,.2);color:var(--dim)}}
.badge.tp1{{background:rgba(79,255,176,.06);color:rgba(79,255,176,.5);border:1px solid rgba(79,255,176,.2)}}
.ql-high{{color:var(--high);font-weight:600}} .ql-medium{{color:var(--medium)}} .ql-low{{color:var(--low)}}
.bw{{display:inline-block;width:52px;height:4px;background:var(--border);border-radius:2px;vertical-align:middle;margin-right:5px}}
.b{{height:4px;background:var(--accent);border-radius:2px}}
.grid-2{{display:grid;grid-template-columns:1fr 1fr;gap:16px}}
.open-pill{{display:inline-block;padding:2px 8px;background:rgba(79,255,176,.08);border:1px solid rgba(79,255,176,.2);border-radius:12px;font-family:'IBM Plex Mono',monospace;font-size:11px;color:var(--accent);margin-left:4px}}
.lab-link{{color:var(--accent);text-decoration:none;font-family:'IBM Plex Mono',monospace;font-size:11px;font-weight:600;letter-spacing:.05em}}
.lab-link:hover{{text-decoration:underline}}
@media(max-width:900px){{.fw-row,.grid-2{{grid-template-columns:1fr}}.container{{padding:12px}}}}
</style>
</head>
<body>
<header>
  <h1>Crypto Signal Engine — Dashboard</h1>
  <div class="meta">
    {generated} &nbsp;|&nbsp;
    Open: V3<span class="open-pill">{open_v3}</span>
    V4<span class="open-pill">{open_v4}</span>
    V4.1<span class="open-pill">{open_v41}</span>
    &nbsp;|&nbsp;
    <a href="analytics_dashboard.html" class="lab-link">Analytics Lab &rarr;</a>
  </div>
</header>
<div class="container">

  <div class="fw-row">
    {fw_kpi(sv3,  "V3.2 — Institutional Trend Following", "--v3")}
    {fw_kpi(sv4,  "V4.0 — Daily Edition",                "--v4")}
    {fw_kpi(sv41, "V4.1 — Intraday Wave",                "--v41")}
  </div>

  <div class="tabs">
    <div class="tab active t-v41" onclick="showTab('v41',this)">V4.1 Intraday Wave</div>
    <div class="tab t-v4"  onclick="showTab('v4',this)">V4.0 Daily</div>
    <div class="tab t-v3"  onclick="showTab('v3',this)">V3.2 Trend</div>
  </div>

  <div id="tab-v41" class="tab-content active">

    <div class="card" style="margin-bottom:16px">
      <div class="ch live"><span class="pulse"></span>Segnali in monitoraggio — V4.1 ({open_v41} aperti)</div>
      <div style="overflow-x:auto"><table>
        <thead><tr>
          <th>Asset</th><th>Dir</th><th>Entry</th><th>SL</th>
          <th>TP1</th><th>TP2</th><th>Quality</th>
          <th>MAE</th><th>MFE</th><th>Aperto</th>
        </tr></thead>
        <tbody>{open_rows_html}</tbody>
      </table></div>
    </div>

    <div class="grid-2">
      {breakdown_table(v41, lambda r: r["quality"], ["HIGH","MEDIUM","LOW"], "Per Quality Score")}
      {breakdown_table(v41, lambda r: r["session"], ["LONDON","OVERLAP","NEW_YORK","ASIA"], "Per Sessione")}
    </div>
    <div class="grid-2">
      {breakdown_table(v41, lambda r: r["asset"].replace("_USDT",""), ["PAXG","BTC"], "Per Asset")}
      {breakdown_table(v41, trigger_key, ["BOS","CHOCH","BOS+CHOCH"], "Per Trigger")}
    </div>
    <div class="card">
      <div class="ch">Segnali chiusi — V4.1</div>
      <div style="overflow-x:auto"><table>
        <thead><tr><th>Timestamp</th><th>Asset</th><th>Dir</th><th>Trigger</th><th>Quality</th><th>Sessione</th><th>Outcome</th></tr></thead>
        <tbody>{signal_rows(v41)}</tbody>
      </table></div>
    </div>
  </div>

  <div id="tab-v4" class="tab-content">
    <div class="grid-2">
      {breakdown_table(v4, lambda r: r["quality"] or "N/A", ["HIGH","MEDIUM","LOW"], "Per Quality Label")}
      {breakdown_table(v4, lambda r: r["session"], ["LONDON","OVERLAP","NEW_YORK","ASIA"], "Per Sessione")}
    </div>
    <div class="card">
      <div class="ch">Segnali recenti — V4.0</div>
      <div style="overflow-x:auto"><table>
        <thead><tr><th>Timestamp</th><th>Asset</th><th>Dir</th><th>Trigger</th><th>Quality</th><th>Sessione</th><th>Outcome</th></tr></thead>
        <tbody>{signal_rows(v4)}</tbody>
      </table></div>
    </div>
  </div>

  <div id="tab-v3" class="tab-content">
    <div class="grid-2">
      {breakdown_table(v3, lambda r: r["asset"].replace("_USDT",""), ["PAXG","BTC"], "Per Asset")}
      {breakdown_table(v3, lambda r: r["session"], ["LONDON","OVERLAP","NEW_YORK","ASIA"], "Per Sessione")}
    </div>
    <div class="card">
      <div class="ch">Segnali recenti — V3.2</div>
      <div style="overflow-x:auto"><table>
        <thead><tr><th>Timestamp</th><th>Asset</th><th>Dir</th><th>Trigger</th><th>Quality</th><th>Sessione</th><th>Outcome</th></tr></thead>
        <tbody>{signal_rows(v3)}</tbody>
      </table></div>
    </div>
  </div>

</div>
<script>
function showTab(id, el) {{
  document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.getElementById('tab-' + id).classList.add('active');
  el.classList.add('active');
}}
</script>
</body>
</html>"""

    os.makedirs("docs", exist_ok=True)
    with open(OUT_PATH, "w") as f:
        f.write(html)
    print(f"Dashboard unificata generata: {OUT_PATH} ({len(v3)+len(v4)+len(v41)} segnali chiusi, {len(v41_open)} aperti)")


if __name__ == "__main__":
    generate()
