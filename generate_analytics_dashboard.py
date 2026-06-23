"""
generate_analytics_dashboard.py
Crypto Signal Engine — Analytics Lab (unificato)

Struttura:
    SEZIONE 1 — Institutional Edge Lab / OTE-SC (framework attivo)
    SEZIONE 2 — V4.1 Intraday Wave        (benchmark storico)
    SEZIONE 3 — V4.1 Phase 1 Money Flow   (benchmark storico)

Genera docs/analytics_dashboard.html
Eseguito dal workflow GitHub Actions ad ogni scan oppure
manualmente: python3 generate_analytics_dashboard.py
"""

import sqlite3
import json
import os
from datetime import datetime, timezone

DB_PATH  = os.environ.get("DB_PATH", "data/signals.db")
OUT_PATH = "docs/analytics_dashboard.html"


# ============================================================
# Helpers query
# ============================================================

def q(conn, sql, params=()):
    return conn.execute(sql, params).fetchall()


# ============================================================
# Edge Lab data
# ============================================================

def load_el_signals(conn):
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


def load_el_recent(conn, limit=20):
    try:
        return q(conn, f"""
            SELECT signal_id, asset, direction, entry, stop_loss, tp, rr,
                   quality_score, quality_label, session, ref_session,
                   liquidity_target, trend_combined, final_outcome,
                   mae, mfe, bars_open, timestamp_setup
            FROM edge_lab_signals
            ORDER BY timestamp_setup DESC LIMIT {limit}
        """)
    except sqlite3.OperationalError:
        return []


# ============================================================
# V4.1 data (invariato dal legacy)
# ============================================================

def load_v41_signals(conn, table):
    try:
        rows = q(conn, f"""
            SELECT asset, session, final_outcome, mae, mfe, tp1_hit, tp2_hit,
                   trigger_types, quality_label, expected_move_points,
                   liquidity_target, timestamp_setup
            FROM {table}
            WHERE final_outcome != 'OPEN'
            ORDER BY timestamp_setup DESC
        """)
    except sqlite3.OperationalError:
        return []
    result = []
    for r in rows:
        try:
            types = json.loads(r[7]) if r[7] else []
        except Exception:
            types = []
        trigger = "BOS+CHOCH" if ("BOS" in types and "CHOCH" in types) \
            else ("BOS" if "BOS" in types else ("CHOCH" if "CHOCH" in types else "OTHER"))
        result.append({
            "asset": r[0], "session": r[1], "outcome": r[2],
            "mae": r[3] or 0, "mfe": r[4] or 0,
            "tp1_hit": bool(r[5]), "tp2_hit": bool(r[6]),
            "trigger": trigger, "quality": r[8],
            "em": r[9], "liquidity_target": r[10] or "N/A",
            "ts": r[11],
        })
    return result


# ============================================================
# Stats
# ============================================================

def stats_el(rows):
    n = len(rows)
    if n == 0:
        return {"n":0,"win":0,"sl":0,"exp_r":0,"avg_mae":0,"avg_mfe":0,"avg_rr":0,"avg_bars":0}
    wins = sum(1 for r in rows if r["outcome"] == "TP")
    sls  = sum(1 for r in rows if r["outcome"] == "SL")
    return {
        "n":        n,
        "win":      round(wins/n*100, 1),
        "sl":       round(sls/n*100,  1),
        "exp_r":    round((wins*2-sls)/n, 2),
        "avg_mae":  round(sum(r["mae"]      for r in rows)/n, 1),
        "avg_mfe":  round(sum(r["mfe"]      for r in rows)/n, 1),
        "avg_rr":   round(sum(r["rr"]       for r in rows)/n, 2),
        "avg_bars": round(sum(r["bars_open"] for r in rows)/n, 1),
    }


def stats_v41(rows):
    n = len(rows)
    if n == 0:
        return {"n":0,"win":0,"tp1":0,"tp2":0,"sl":0,"exp_r":0,"avg_mae":0,"avg_mfe":0,"avg_em":0}
    wins = sum(1 for r in rows if r["outcome"] == "TP")
    sls  = sum(1 for r in rows if r["outcome"] == "SL")
    tp1  = sum(1 for r in rows if r["tp1_hit"])
    tp2  = sum(1 for r in rows if r["tp2_hit"])
    ems  = [r["em"] for r in rows if r["em"] is not None]
    return {
        "n":       n,
        "win":     round(wins/n*100, 1),
        "tp1":     round(tp1/n*100,  1),
        "tp2":     round(tp2/n*100,  1),
        "sl":      round(sls/n*100,  1),
        "exp_r":   round((wins*2-sls)/n, 2),
        "avg_mae": round(sum(r["mae"] for r in rows)/n, 1),
        "avg_mfe": round(sum(r["mfe"] for r in rows)/n, 1),
        "avg_em":  round(sum(ems)/len(ems), 1) if ems else 0,
    }


def breakdown(rows, key_fn, keys, stat_fn):
    return {k: stat_fn([r for r in rows if key_fn(r) == k]) for k in keys}


# ============================================================
# CSS condiviso
# ============================================================

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
header .meta{font-family:'IBM Plex Mono',monospace;font-size:11px;color:var(--dim)}
header a{color:var(--accent);text-decoration:none;font-family:'IBM Plex Mono',monospace;font-size:11px}
.container{max-width:1320px;margin:0 auto;padding:24px 32px}
.fw-header{padding:14px 20px;font-family:'IBM Plex Mono',monospace;font-size:13px;font-weight:600;letter-spacing:.1em;text-transform:uppercase;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:12px}
.fw-tag{font-size:10px;padding:2px 8px;border-radius:4px;font-weight:600}
.tag-active{background:rgba(79,255,176,.15);color:var(--buy)}
.tag-benchmark{background:rgba(90,100,120,.2);color:var(--dim)}
.summary-grid{display:grid;gap:1px;background:var(--border)}
.summary-grid.cols8{grid-template-columns:repeat(8,1fr)}
.summary-grid.cols5{grid-template-columns:repeat(5,1fr)}
.summary-grid>div{background:var(--surface);padding:14px 8px;text-align:center}
.big{font-family:'IBM Plex Mono',monospace;font-size:18px;font-weight:600}
.big.pos{color:var(--buy)} .big.neg{color:var(--sell)} .big.warn{color:var(--accent3)}
.lbl{font-size:9px;text-transform:uppercase;letter-spacing:.06em;color:var(--dim);display:block;margin-top:3px}
.card{background:var(--surface);border:1px solid var(--border);border-radius:6px;overflow:hidden;margin-bottom:16px}
.ch{padding:10px 16px;border-bottom:1px solid var(--border);font-family:'IBM Plex Mono',monospace;font-size:11px;font-weight:600;letter-spacing:.1em;text-transform:uppercase;color:var(--dim)}
.card-note{padding:10px 16px;font-size:11px;color:var(--dim);border-top:1px solid var(--border);font-style:italic}
table{width:100%;border-collapse:collapse}
th{font-family:'IBM Plex Mono',monospace;font-size:10px;font-weight:600;text-transform:uppercase;letter-spacing:.08em;color:var(--dim);padding:9px 14px;text-align:left;border-bottom:1px solid var(--border)}
td{padding:8px 14px;border-bottom:1px solid var(--border);font-size:13px}
tr:last-child td{border-bottom:none} tr:hover td{background:rgba(255,255,255,.02)}
tr.hl td{background:rgba(79,255,176,.06)}
.mono{font-family:'IBM Plex Mono',monospace;font-size:12px}
.pos{color:var(--buy);font-weight:600} .neg{color:var(--sell)} .warn{color:var(--accent3)}
.grid-2{display:grid;grid-template-columns:1fr 1fr;gap:16px}
.section-divider{margin:36px 0 24px;border-top:2px dashed var(--border);padding-top:8px}
.badge{display:inline-block;padding:2px 8px;border-radius:4px;font-size:11px;font-family:'IBM Plex Mono',monospace;font-weight:600}
.b-tp{background:rgba(79,255,176,.15);color:var(--buy)}
.b-sl{background:rgba(255,107,107,.15);color:var(--sell)}
.b-exp{background:rgba(90,100,120,.2);color:var(--dim)}
.b-open{background:rgba(255,209,102,.15);color:var(--accent3)}
.b-buy{background:rgba(79,255,176,.15);color:var(--buy)}
.b-sell{background:rgba(255,107,107,.15);color:var(--sell)}
.empty{text-align:center;padding:24px;color:var(--dim);font-size:13px}
@media(max-width:900px){.grid-2{grid-template-columns:1fr}.summary-grid.cols8{grid-template-columns:repeat(4,1fr)}.container{padding:12px}}
"""


# ============================================================
# Componenti HTML
# ============================================================

def _empty_row(cols):
    return f'<tr><td colspan="{cols}" class="empty">Nessun dato</td></tr>'


def outcome_badge(o):
    cls = {"TP":"b-tp","SL":"b-sl","EXPIRED":"b-exp","OPEN":"b-open"}.get(o,"b-exp")
    return f'<span class="badge {cls}">{o}</span>'


def direction_badge(d):
    return f'<span class="badge {"b-buy" if d=="BUY" else "b-sell"}">{d}</span>'


def fmt_ts(ts):
    if not ts: return "—"
    try:
        dt = datetime.fromisoformat(ts.replace("Z","+00:00"))
        return dt.strftime("%d %b %H:%M")
    except Exception:
        return ts[:16]


def fmt_p(v):
    if v is None: return "—"
    v = float(v)
    return f"{v:,.2f}" if v > 1000 else f"{v:.4f}"


# ── Edge Lab tables ──────────────────────────────────────────

def el_summary_boxes(s):
    wc = "pos" if s["win"]>=40 else ("neg" if s["win"]<25 else "warn")
    ec = "pos" if s["exp_r"]>0 else "neg"
    return f"""<div class="summary-grid cols8" style="border:1px solid var(--border);border-radius:6px;overflow:hidden;margin-bottom:16px">
  <div><span class="big">{s['n']}</span><span class="lbl">Segnali chiusi</span></div>
  <div><span class="big {wc}">{s['win']}%</span><span class="lbl">Win Rate</span></div>
  <div><span class="big neg">{s['sl']}%</span><span class="lbl">SL Rate</span></div>
  <div><span class="big {ec}">{s['exp_r']:+.2f}R</span><span class="lbl">Expectancy</span></div>
  <div><span class="big">{s['avg_rr']:.2f}</span><span class="lbl">Avg R/R</span></div>
  <div><span class="big neg">{s['avg_mae']:.1f}</span><span class="lbl">Avg MAE</span></div>
  <div><span class="big pos">{s['avg_mfe']:.1f}</span><span class="lbl">Avg MFE</span></div>
  <div><span class="big">{s['avg_bars']:.0f}</span><span class="lbl">Avg Bars M15</span></div>
</div>"""


def el_perf_table(title, d, keys, key_label):
    body = ""
    for k in keys:
        v = d.get(k, stats_el([]))
        if v["n"] == 0: continue
        wc = "pos" if v["win"]>=40 else ("neg" if v["win"]<25 else "warn")
        ec = "pos" if v["exp_r"]>0 else "neg"
        body += f"""<tr>
  <td><strong>{k}</strong></td>
  <td class="mono">{v['n']}</td>
  <td class="mono {wc}">{v['win']}%</td>
  <td class="mono {ec}">{v['exp_r']:+.2f}R</td>
  <td class="mono">{v['avg_rr']:.2f}</td>
  <td class="mono neg">{v['avg_mae']:.1f}</td>
  <td class="mono pos">{v['avg_mfe']:.1f}</td>
</tr>"""
    if not body: body = _empty_row(7)
    return f"""<div class="card">
  <div class="ch">{title}</div>
  <table><thead><tr>
    <th>{key_label}</th><th>N</th><th>Win%</th><th>Expectancy</th>
    <th>Avg R/R</th><th>Avg MAE</th><th>Avg MFE</th>
  </tr></thead><tbody>{body}</tbody></table>
</div>"""


def el_recent_table(rows):
    if not rows:
        return '<div class="card"><div class="empty">Nessun segnale ancora — il primo arriverà quando H4 e H1 si allineano.</div></div>'
    body = ""
    for r in rows:
        sid,asset,direction,entry,sl,tp,rr,qs,ql,sess,ref,liq,trend,outcome,mae,mfe,bars,ts = r
        oc = {"TP":"b-tp","SL":"b-sl","EXPIRED":"b-exp","OPEN":"b-open"}.get(outcome,"b-exp")
        body += f"""<tr>
  <td class="mono" style="color:var(--dim);font-size:11px">{fmt_ts(ts)}</td>
  <td><strong>{asset.replace('_USDT','')}</strong></td>
  <td>{direction_badge(direction)}</td>
  <td class="mono">{fmt_p(entry)}</td>
  <td class="mono">{fmt_p(sl)}</td>
  <td class="mono">{fmt_p(tp)}</td>
  <td class="mono">{float(rr or 0):.2f}</td>
  <td style="font-size:12px;color:var(--dim)">{ql or '—'}</td>
  <td style="font-size:12px;color:var(--dim)">{sess or '—'} → {ref or '—'}</td>
  <td style="font-size:12px;color:var(--dim)">{liq or '—'}</td>
  <td><span class="badge {oc}">{outcome}</span></td>
</tr>"""
    return f"""<div class="card">
  <div class="ch">Segnali Recenti OTE-SC</div>
  <table><thead><tr>
    <th>Data</th><th>Asset</th><th>Dir</th><th>Entry</th><th>SL</th><th>TP</th>
    <th>R/R</th><th>Quality</th><th>Sessione → Ref</th><th>Target</th><th>Esito</th>
  </tr></thead><tbody>{body}</tbody></table>
</div>"""


# ── V4.1 tables (invariate dal legacy) ──────────────────────

def v41_summary_boxes(s, color):
    wc = "pos" if s["win"]>=30 else "neg"
    ec = "pos" if s["exp_r"]>0 else "neg"
    return f"""<div class="summary-grid cols5" style="border-top:2px solid {color};border:1px solid var(--border);border-top:2px solid {color};border-radius:6px;overflow:hidden;margin-bottom:16px">
  <div><span class="big">{s['n']}</span><span class="lbl">Segnali chiusi</span></div>
  <div><span class="big {wc}">{s['win']}%</span><span class="lbl">Win Rate</span></div>
  <div><span class="big">{s['tp1']}%</span><span class="lbl">TP1 Hit</span></div>
  <div><span class="big {ec}">{s['exp_r']:+.2f}R</span><span class="lbl">Expectancy</span></div>
  <div><span class="big neg">{s['avg_mae']}</span><span class="lbl">MAE medio</span></div>
</div>"""


def v41_perf_table(title, d, keys, key_label, highlight=40):
    body = ""
    for k in keys:
        v = d.get(k, stats_v41([]))
        if v["n"] == 0: continue
        wc = "pos" if v["win"]>=40 else ("neg" if v["win"]<20 else "")
        ec = "pos" if v["exp_r"]>0 else "neg"
        hl = "hl" if v["win"]>=highlight and v["n"]>=3 else ""
        body += f"""<tr class="{hl}">
  <td><strong>{k}</strong></td>
  <td class="mono">{v['n']}</td>
  <td class="mono {wc}">{v['win']}%</td>
  <td class="mono">{v['tp1']}%</td>
  <td class="mono">{v['tp2']}%</td>
  <td class="mono {ec}">{v['exp_r']:+.2f}R</td>
</tr>"""
    if not body: body = _empty_row(6)
    return f"""<div class="card">
  <div class="ch">{title}</div>
  <table><thead><tr>
    <th>{key_label}</th><th>N</th><th>Win%</th><th>TP1%</th><th>TP2%</th><th>Expectancy</th>
  </tr></thead><tbody>{body}</tbody></table>
</div>"""


# ============================================================
# Sezioni complete
# ============================================================

def section_edge_lab(rows, recent):
    s = stats_el(rows)
    asset_keys   = ["BTC_USDT","PAXG_USDT"]
    dir_keys     = ["BUY","SELL"]
    sess_keys    = ["ASIA","LONDON","OVERLAP","NEW_YORK"]
    ref_keys     = ["ASIA","LONDON","OVERLAP","NEW_YORK","EUROPEAN_COMPOSITE"]
    quality_keys = ["HIGH","MEDIUM","LOW"]
    liq_keys     = sorted({r["liq_target"] for r in rows if r["liq_target"]!="N/A"})

    bd_asset   = breakdown(rows, lambda r: r["asset"],       asset_keys,   stats_el)
    bd_dir     = breakdown(rows, lambda r: r["direction"],   dir_keys,     stats_el)
    bd_sess    = breakdown(rows, lambda r: r["session"],     sess_keys,    stats_el)
    bd_ref     = breakdown(rows, lambda r: r["ref_session"], ref_keys,     stats_el)
    bd_quality = breakdown(rows, lambda r: r["quality_label"], quality_keys, stats_el)
    bd_liq     = breakdown(rows, lambda r: r["liq_target"],  liq_keys,     stats_el)

    no_data = "" if rows else """<div class="card" style="border-color:var(--accent3)">
  <div class="ch" style="color:var(--accent3)">In attesa del primo segnale</div>
  <div style="padding:16px;color:var(--dim)">
    OTE-SC entra solo quando H4 e H1 sono allineati (BULLISH o BEARISH).
    Il sistema sta scansionando e raccogliendo contesto ad ogni ciclo.
  </div>
</div>"""

    return f"""
<div class="card" style="border-top:2px solid var(--accent)">
  <div class="fw-header" style="color:var(--accent)">
    ⚡ Institutional Edge Lab — OTE-SC
    <span class="fw-tag tag-active">ATTIVO</span>
    <span style="color:var(--dim);font-size:11px;margin-left:auto">Phase 1A · BTC_USDT · PAXG_USDT</span>
  </div>
  {el_summary_boxes(s)}
  {no_data}
  <div class="grid-2" style="padding:0 0 0 0">
    {el_perf_table("Per Asset", bd_asset, asset_keys, "Asset")}
    {el_perf_table("Per Direzione", bd_dir, dir_keys, "Dir")}
  </div>
  <div class="grid-2">
    {el_perf_table("Per Quality Label", bd_quality, quality_keys, "Quality")}
    {el_perf_table("Per Sessione Corrente", bd_sess, sess_keys, "Sessione")}
  </div>
  <div class="grid-2">
    {el_perf_table("Per Sessione Riferimento", bd_ref, ref_keys, "Ref Session")}
    {el_perf_table("Per Liquidity Target", bd_liq, liq_keys, "Target")}
  </div>
  {el_recent_table(recent)}
</div>"""


def section_v41(rows, fw_name, color):
    s = stats_v41(rows)
    bd_trigger = breakdown(rows, lambda r: r["trigger"],                    ["BOS","CHOCH","BOS+CHOCH"], stats_v41)
    bd_asset   = breakdown(rows, lambda r: r["asset"].replace("_USDT",""),  ["BTC","PAXG"],              stats_v41)
    bd_sess    = breakdown(rows, lambda r: r["session"] or "N/A",           ["ASIA","LONDON","OVERLAP","NEW_YORK"], stats_v41)

    return f"""
<div class="card" style="border-top:2px solid {color}">
  <div class="fw-header" style="color:{color}">
    {fw_name}
    <span class="fw-tag tag-benchmark">BENCHMARK STORICO</span>
  </div>
  {v41_summary_boxes(s, color)}
  <div class="grid-2">
    {v41_perf_table("Per Trigger", bd_trigger, ["BOS","CHOCH","BOS+CHOCH"], "Trigger")}
    {v41_perf_table("Per Asset",   bd_asset,   ["BTC","PAXG"],              "Asset")}
  </div>
  {v41_perf_table("Per Sessione", bd_sess, ["ASIA","LONDON","OVERLAP","NEW_YORK"], "Sessione")}
</div>"""


# ============================================================
# Generate
# ============================================================

def generate():
    conn = sqlite3.connect(DB_PATH)

    el_rows   = load_el_signals(conn)
    el_recent = load_el_recent(conn, 20)
    v41_rows  = load_v41_signals(conn, "v41_signals")
    try:
        v41p1_rows = load_v41_signals(conn, "v41p1_signals")
    except sqlite3.OperationalError:
        v41p1_rows = []

    conn.close()

    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    html = f"""<!DOCTYPE html>
<html lang="it">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Crypto Signal Engine — Analytics Lab</title>
<style>{CSS}</style>
</head>
<body>
<header>
  <h1>Crypto Signal Engine — Analytics Lab</h1>
  <div class="meta">
    {generated} &nbsp;|&nbsp;
    <a href="unified_dashboard.html">&larr; Dashboard</a>
  </div>
</header>
<div class="container">

  {section_edge_lab(el_rows, el_recent)}

  <div class="section-divider">
    <span style="font-family:'IBM Plex Mono',monospace;font-size:10px;color:var(--dim);letter-spacing:.1em;text-transform:uppercase">
      Benchmark Storici
    </span>
  </div>

  {section_v41(v41_rows,  "V4.1 — Intraday Wave",       "#4fffb0")}
  {section_v41(v41p1_rows,"V4.1 Phase 1 — Money Flow",  "#ffd166")}

</div>
</body>
</html>"""

    os.makedirs("docs", exist_ok=True)
    with open(OUT_PATH, "w") as f:
        f.write(html)

    print(
        f"Analytics dashboard generata: {OUT_PATH} "
        f"(Edge Lab: {len(el_rows)} chiusi | "
        f"V4.1: {len(v41_rows)} | V4.1P1: {len(v41p1_rows)})"
    )


if __name__ == "__main__":
    generate()
