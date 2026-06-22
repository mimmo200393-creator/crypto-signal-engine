"""
generate_unified_dashboard.py
Crypto Signal Engine — Homepage Operativa

Mostra lo stato in tempo reale di:
    - Edge Lab OTE-SC (segnali aperti + ultimi chiusi)
    - V4.1 Intraday Wave (benchmark attivo, segnali aperti)

Genera docs/unified_dashboard.html
"""

import sqlite3
import json
import os
from datetime import datetime, timezone, timedelta

DB_PATH  = os.environ.get("DB_PATH", "data/signals.db")
OUT_PATH = "docs/unified_dashboard.html"


def q(conn, sql, params=()):
    return conn.execute(sql, params).fetchall()


# ============================================================
# Data loading
# ============================================================

def load_el_open(conn):
    try:
        rows = q(conn, """
            SELECT signal_id, asset, direction, entry, stop_loss, tp, rr,
                   quality_score, quality_label, session, ref_session,
                   liquidity_target, trend_combined, mae, mfe, bars_open,
                   tradeability_flags, timestamp_setup
            FROM edge_lab_signals
            WHERE final_outcome = 'OPEN'
            ORDER BY timestamp_setup DESC
        """)
    except sqlite3.OperationalError:
        return []
    now = datetime.now(timezone.utc)
    result = []
    for r in rows:
        try:
            setup_dt = datetime.fromisoformat(r[17])
            if setup_dt.tzinfo is None:
                setup_dt = setup_dt.replace(tzinfo=timezone.utc)
            elapsed_h = round((now - setup_dt).total_seconds() / 3600, 1)
            bars_pct  = round((r[15] or 0) / 96 * 100)
        except Exception:
            elapsed_h = 0
            bars_pct  = 0
        try:
            flags = json.loads(r[16]) if r[16] else []
        except Exception:
            flags = []
        result.append({
            "signal_id":   r[0],
            "asset":       r[1],
            "direction":   r[2],
            "entry":       r[3],
            "sl":          r[4],
            "tp":          r[5],
            "rr":          r[6],
            "qs":          r[7],
            "ql":          r[8],
            "session":     r[9],
            "ref_session": r[10],
            "target":      r[11],
            "trend":       r[12],
            "mae":         r[13],
            "mfe":         r[14],
            "bars_open":   r[15],
            "bars_pct":    bars_pct,
            "flags":       flags,
            "ts":          r[17],
            "elapsed_h":   elapsed_h,
        })
    return result


def load_el_recent_closed(conn, limit=10):
    try:
        return q(conn, f"""
            SELECT asset, direction, entry, stop_loss, tp, rr,
                   quality_label, session, ref_session, liquidity_target,
                   final_outcome, mae, mfe, bars_open, timestamp_setup
            FROM edge_lab_signals
            WHERE final_outcome != 'OPEN'
            ORDER BY timestamp_setup DESC LIMIT {limit}
        """)
    except sqlite3.OperationalError:
        return []


def load_el_stats(conn):
    try:
        rows = q(conn, """
            SELECT final_outcome, COUNT(*) as n
            FROM edge_lab_signals
            WHERE final_outcome != 'OPEN'
            GROUP BY final_outcome
        """)
        d = {r[0]: r[1] for r in rows}
        n    = sum(d.values())
        wins = d.get("TP", 0)
        sls  = d.get("SL", 0)
        return {
            "n":     n,
            "open":  q(conn, "SELECT COUNT(*) FROM edge_lab_signals WHERE final_outcome='OPEN'")[0][0],
            "win":   round(wins/n*100, 1) if n > 0 else 0,
            "exp_r": round((wins*2-sls)/n, 2) if n > 0 else 0,
        }
    except sqlite3.OperationalError:
        return {"n": 0, "open": 0, "win": 0, "exp_r": 0}


def load_v41_open(conn):
    try:
        rows = q(conn, """
            SELECT asset, direction, entry, stop_loss, tp1, tp2,
                   quality_label, quality_score, trigger_types,
                   mae, mfe, tp1_hit, liquidity_source, liquidity_target,
                   expected_move_points, timestamp_setup
            FROM v41_signals WHERE final_outcome = 'OPEN'
            ORDER BY timestamp_setup DESC
        """)
    except sqlite3.OperationalError:
        return []
    now = datetime.now(timezone.utc)
    result = []
    for r in rows:
        try:
            types = json.loads(r[8]) if r[8] else []
            trigger = "+".join(types) if types else "—"
        except Exception:
            trigger = "—"
        try:
            setup_dt = datetime.fromisoformat(r[15])
            if setup_dt.tzinfo is None:
                setup_dt = setup_dt.replace(tzinfo=timezone.utc)
            elapsed_h = round((now - setup_dt).total_seconds() / 3600, 1)
        except Exception:
            elapsed_h = 0
        result.append({
            "asset": r[0], "direction": r[1], "entry": r[2],
            "sl": r[3], "tp1": r[4], "tp2": r[5],
            "ql": r[6], "qs": r[7], "trigger": trigger,
            "mae": r[9], "mfe": r[10], "tp1_hit": bool(r[11]),
            "source": r[12] or "N/A", "target": r[13] or "N/A",
            "em": r[14], "elapsed_h": elapsed_h,
        })
    return result


def load_v41_stats(conn):
    try:
        n    = q(conn, "SELECT COUNT(*) FROM v41_signals WHERE final_outcome!='OPEN'")[0][0]
        wins = q(conn, "SELECT COUNT(*) FROM v41_signals WHERE final_outcome='TP'")[0][0]
        sls  = q(conn, "SELECT COUNT(*) FROM v41_signals WHERE final_outcome='SL'")[0][0]
        opn  = q(conn, "SELECT COUNT(*) FROM v41_signals WHERE final_outcome='OPEN'")[0][0]
        return {
            "n":     n,
            "open":  opn,
            "win":   round(wins/n*100, 1) if n > 0 else 0,
            "exp_r": round((wins*2-sls)/n, 2) if n > 0 else 0,
        }
    except sqlite3.OperationalError:
        return {"n": 0, "open": 0, "win": 0, "exp_r": 0}


# ============================================================
# Helpers HTML
# ============================================================

def fp(v):
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


def outcome_badge(o):
    cls = {"TP": "b-tp", "SL": "b-sl", "EXPIRED": "b-exp"}.get(o, "b-exp")
    return f'<span class="badge {cls}">{o}</span>'


def direction_badge(d):
    return f'<span class="badge {"b-buy" if d=="BUY" else "b-sell"}">{d}</span>'


def ql_badge(ql):
    cls = {"HIGH": "b-high", "MEDIUM": "b-med", "LOW": "b-low"}.get(ql, "b-low")
    return f'<span class="badge {cls}">{ql or "—"}</span>'


CSS = """
@import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600&family=IBM+Plex+Sans:wght@300;400;600&display=swap');
:root{
  --bg:#0d0f14;--surface:#141720;--border:#1e2330;
  --accent:#4fffb0;--accent2:#ff6b6b;--accent3:#ffd166;
  --text:#e2e8f0;--dim:#5a6478;--buy:#4fffb0;--sell:#ff6b6b;
}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:'IBM Plex Sans',sans-serif;font-size:14px;line-height:1.6}
header{border-bottom:1px solid var(--border);padding:18px 32px;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px}
header h1{font-family:'IBM Plex Mono',monospace;font-size:13px;font-weight:600;letter-spacing:.12em;text-transform:uppercase;color:var(--accent)}
.meta{font-family:'IBM Plex Mono',monospace;font-size:11px;color:var(--dim)}
.meta a{color:var(--accent);text-decoration:none}
.container{max-width:1320px;margin:0 auto;padding:24px 32px}
.section-title{font-family:'IBM Plex Mono',monospace;font-size:11px;font-weight:600;letter-spacing:.12em;text-transform:uppercase;margin:28px 0 12px;padding-bottom:6px;border-bottom:1px solid var(--border)}
.section-title.el{color:var(--accent)}
.section-title.v41{color:#4fffb0;opacity:.6}
.kpi-row{display:grid;grid-template-columns:repeat(4,1fr);gap:1px;background:var(--border);border:1px solid var(--border);border-radius:6px;overflow:hidden;margin-bottom:20px}
.kpi-row>div{background:var(--surface);padding:16px 12px;text-align:center}
.big{font-family:'IBM Plex Mono',monospace;font-size:22px;font-weight:600}
.big.pos{color:var(--buy)} .big.neg{color:var(--sell)} .big.warn{color:var(--accent3)}
.lbl{font-size:9px;text-transform:uppercase;letter-spacing:.06em;color:var(--dim);display:block;margin-top:3px}
.card{background:var(--surface);border:1px solid var(--border);border-radius:6px;overflow:hidden;margin-bottom:16px}
.ch{padding:10px 16px;border-bottom:1px solid var(--border);font-family:'IBM Plex Mono',monospace;font-size:11px;font-weight:600;letter-spacing:.1em;text-transform:uppercase;color:var(--dim);display:flex;align-items:center;gap:8px}
.pulse{display:inline-block;width:7px;height:7px;border-radius:50%;background:var(--accent);animation:pulse 2s infinite;flex-shrink:0}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}
table{width:100%;border-collapse:collapse}
th{font-family:'IBM Plex Mono',monospace;font-size:10px;font-weight:600;text-transform:uppercase;letter-spacing:.08em;color:var(--dim);padding:9px 14px;text-align:left;border-bottom:1px solid var(--border)}
td{padding:9px 14px;border-bottom:1px solid var(--border);font-size:13px}
tr:last-child td{border-bottom:none} tr:hover td{background:rgba(255,255,255,.02)}
.mono{font-family:'IBM Plex Mono',monospace;font-size:12px}
.pos{color:var(--buy);font-weight:600} .neg{color:var(--sell)}
.badge{display:inline-block;padding:2px 8px;border-radius:4px;font-size:11px;font-family:'IBM Plex Mono',monospace;font-weight:600}
.b-buy{background:rgba(79,255,176,.15);color:var(--buy)}
.b-sell{background:rgba(255,107,107,.15);color:var(--sell)}
.b-tp{background:rgba(79,255,176,.15);color:var(--buy)}
.b-sl{background:rgba(255,107,107,.15);color:var(--sell)}
.b-exp{background:rgba(90,100,120,.2);color:var(--dim)}
.b-high{background:rgba(79,255,176,.15);color:var(--buy)}
.b-med{background:rgba(255,209,102,.15);color:var(--accent3)}
.b-low{background:rgba(90,100,120,.2);color:var(--dim)}
.b-open{background:rgba(255,209,102,.12);color:var(--accent3)}
.progress-bar{height:3px;background:var(--border);border-radius:2px;margin-top:4px;width:80px}
.progress-fill{height:3px;background:var(--accent3);border-radius:2px}
.empty-row td{text-align:center;color:var(--dim);padding:32px;font-size:13px}
.divider{margin:32px 0 24px;border-top:1px dashed var(--border)}
@media(max-width:900px){.kpi-row{grid-template-columns:repeat(2,1fr)}.container{padding:12px}}
"""


# ============================================================
# Componenti HTML
# ============================================================

def el_open_table(rows):
    if not rows:
        return """<div class="card">
  <div class="ch"><span class="pulse"></span>Segnali Aperti — OTE-SC</div>
  <table><tbody><tr class="empty-row"><td colspan="12">
    Nessun segnale aperto. Il sistema sta scansionando ogni 5 minuti.
  </td></tr></tbody></table>
</div>"""

    body = ""
    for r in rows:
        asset = r["asset"].replace("_USDT", "")
        mae_c = "neg" if r["mae"] and r["mae"] > 0 else ""
        mfe_c = "pos" if r["mfe"] and r["mfe"] > 0 else ""
        flags_str = " ".join(f'<span style="color:var(--accent3);font-size:11px">⚠ {f}</span>' for f in r["flags"]) if r["flags"] else ""
        body += f"""<tr>
  <td class="mono" style="color:var(--dim);font-size:11px">{fmt_ts(r['ts'])}</td>
  <td><strong>{asset}</strong></td>
  <td>{direction_badge(r['direction'])}</td>
  <td class="mono">{fp(r['entry'])}</td>
  <td class="mono neg">{fp(r['sl'])}</td>
  <td class="mono">{fp(r['tp'])}</td>
  <td class="mono">{float(r['rr'] or 0):.2f}</td>
  <td>{ql_badge(r['ql'])}</td>
  <td style="font-size:12px;color:var(--dim)">{r['session']} → {r['ref_session']}</td>
  <td class="mono {mae_c}">{fp(r['mae'])}</td>
  <td class="mono {mfe_c}">{fp(r['mfe'])}</td>
  <td>
    <div style="font-family:'IBM Plex Mono',monospace;font-size:11px;color:var(--dim)">{r['bars_open']}/96</div>
    <div class="progress-bar"><div class="progress-fill" style="width:{r['bars_pct']}%"></div></div>
    {flags_str}
  </td>
</tr>"""

    return f"""<div class="card">
  <div class="ch"><span class="pulse"></span>Segnali Aperti — OTE-SC ({len(rows)})</div>
  <div style="overflow-x:auto"><table><thead><tr>
    <th>Data</th><th>Asset</th><th>Dir</th><th>Entry</th><th>SL</th><th>TP</th>
    <th>R/R</th><th>Quality</th><th>Sessione → Ref</th><th>MAE</th><th>MFE</th><th>Bars / Flag</th>
  </tr></thead><tbody>{body}</tbody></table></div>
</div>"""


def el_closed_table(rows):
    if not rows:
        return """<div class="card">
  <div class="ch">Ultimi Segnali Chiusi — OTE-SC</div>
  <table><tbody><tr class="empty-row"><td colspan="10">
    Nessun segnale chiuso ancora. Il primo arriverà quando H4 e H1 si allineano.
  </td></tr></tbody></table>
</div>"""

    body = ""
    for r in rows:
        asset,direction,entry,sl,tp,rr,ql,sess,ref,target,outcome,mae,mfe,bars,ts = r
        body += f"""<tr>
  <td class="mono" style="color:var(--dim);font-size:11px">{fmt_ts(ts)}</td>
  <td><strong>{(asset or '').replace('_USDT','')}</strong></td>
  <td>{direction_badge(direction)}</td>
  <td class="mono">{fp(entry)}</td>
  <td class="mono neg">{fp(sl)}</td>
  <td class="mono">{fp(tp)}</td>
  <td class="mono">{float(rr or 0):.2f}</td>
  <td>{ql_badge(ql)}</td>
  <td style="font-size:12px;color:var(--dim)">{sess or '—'} → {ref or '—'}</td>
  <td style="font-size:12px;color:var(--dim)">{target or '—'}</td>
  <td>{outcome_badge(outcome)}</td>
  <td class="mono" style="color:var(--dim)">{int(bars or 0)}</td>
</tr>"""

    return f"""<div class="card">
  <div class="ch">Ultimi Segnali Chiusi — OTE-SC</div>
  <div style="overflow-x:auto"><table><thead><tr>
    <th>Data</th><th>Asset</th><th>Dir</th><th>Entry</th><th>SL</th><th>TP</th>
    <th>R/R</th><th>Quality</th><th>Sessione → Ref</th><th>Target</th><th>Esito</th><th>Bars</th>
  </tr></thead><tbody>{body}</tbody></table></div>
</div>"""


def v41_open_table(rows):
    if not rows:
        return """<div class="card">
  <div class="ch">Segnali Aperti — V4.1 Intraday Wave</div>
  <table><tbody><tr class="empty-row"><td colspan="10">Nessun segnale aperto.</td></tr></tbody></table>
</div>"""

    body = ""
    for r in rows:
        asset = r["asset"].replace("_USDT", "")
        tp1_badge = '<span class="badge b-tp" style="font-size:10px">TP1✓</span>' if r["tp1_hit"] else ""
        body += f"""<tr>
  <td><strong>{asset}</strong></td>
  <td>{direction_badge(r['direction'])}</td>
  <td class="mono">{fp(r['entry'])}</td>
  <td class="mono neg">{fp(r['sl'])}</td>
  <td class="mono">{fp(r['tp1'])} {tp1_badge}</td>
  <td class="mono">{fp(r['tp2'])}</td>
  <td>{ql_badge(r['ql'])} <span style="color:var(--dim);font-size:11px">({r['qs']}/12)</span></td>
  <td style="font-size:12px;color:var(--dim)">{r['trigger']}</td>
  <td class="mono neg">{fp(r['mae'])}</td>
  <td class="mono pos">{fp(r['mfe'])}</td>
  <td class="mono" style="color:var(--dim)">{r['elapsed_h']}h</td>
</tr>"""

    return f"""<div class="card">
  <div class="ch">Segnali Aperti — V4.1 Intraday Wave ({len(rows)})</div>
  <div style="overflow-x:auto"><table><thead><tr>
    <th>Asset</th><th>Dir</th><th>Entry</th><th>SL</th><th>TP1</th><th>TP2</th>
    <th>Quality</th><th>Trigger</th><th>MAE</th><th>MFE</th><th>Aperto</th>
  </tr></thead><tbody>{body}</tbody></table></div>
</div>"""


def kpi_row(s, label, color):
    wc = "pos" if s["win"] >= 40 else ("neg" if s["win"] < 25 else "warn")
    ec = "pos" if s["exp_r"] > 0 else "neg"
    return f"""<div class="kpi-row" style="border-top:2px solid {color};margin-bottom:16px">
  <div><span class="big">{s['open']}</span><span class="lbl">Aperti ora</span></div>
  <div><span class="big">{s['n']}</span><span class="lbl">Chiusi totale</span></div>
  <div><span class="big {wc}">{s['win']}%</span><span class="lbl">Win Rate</span></div>
  <div><span class="big {ec}">{s['exp_r']:+.2f}R</span><span class="lbl">Expectancy</span></div>
</div>"""


# ============================================================
# Generate
# ============================================================

def generate():
    conn = sqlite3.connect(DB_PATH)

    el_open   = load_el_open(conn)
    el_closed = load_el_recent_closed(conn, 10)
    el_stats  = load_el_stats(conn)
    v41_open  = load_v41_open(conn)
    v41_stats = load_v41_stats(conn)

    conn.close()

    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    html = f"""<!DOCTYPE html>
<html lang="it">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Crypto Signal Engine — Dashboard</title>
<style>{CSS}</style>
</head>
<body>
<header>
  <h1>Crypto Signal Engine — Dashboard</h1>
  <div class="meta">
    {generated} &nbsp;|&nbsp;
    <a href="analytics_dashboard.html">Analytics Lab →</a>
  </div>
</header>
<div class="container">

  <div class="section-title el">⚡ Institutional Edge Lab — OTE-SC</div>
  {kpi_row(el_stats, "OTE-SC", "var(--accent)")}
  {el_open_table(el_open)}
  {el_closed_table(el_closed)}

  <div class="divider"></div>

  <div class="section-title v41">V4.1 Intraday Wave — Benchmark</div>
  {kpi_row(v41_stats, "V4.1", "#4fffb0")}
  {v41_open_table(v41_open)}

</div>
</body>
</html>"""

    os.makedirs("docs", exist_ok=True)
    with open(OUT_PATH, "w") as f:
        f.write(html)
    print(
        f"Dashboard unificata generata: {OUT_PATH} "
        f"(EL aperti={el_stats['open']} chiusi={el_stats['n']} | "
        f"V4.1 aperti={v41_stats['open']} chiusi={v41_stats['n']})"
    )


if __name__ == "__main__":
    generate()
