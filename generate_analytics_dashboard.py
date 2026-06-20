"""
generate_analytics_dashboard.py
Dashboard Analitica V4.1 + V4.1 Phase 1 — Crypto Signal Engine.

Laboratorio quantitativo: risponde a domande sui dati gia' raccolti,
senza modificare la logica di trading. Copre solo V4.1 e V4.1 Phase 1,
gli unici due framework con un numero di segnali chiusi sufficiente
per un'analisi statistica (V3.2/V3.2D/V4.0 hanno ~0 segnali chiusi).

Sezioni:
    - Performance per Trigger (BOS / CHOCH / BOS+CHOCH)
    - Performance per Asset (BTC / PAXG)
    - Performance per Sessione (ASIA / LONDON / OVERLAP / NEW_YORK)
    - Expected Move Analysis (bucket 0-100 / 100-200 / 200-300 / 300+)
    - Liquidity Target Analysis (Weekly/Daily/Equal/H4 Swing + le nuove
      Asia/London/NY High-Low quando disponibili nei segnali futuri)

Genera docs/analytics_dashboard.html.
Eseguito automaticamente dal workflow GitHub Actions ad ogni scan,
oppure manualmente con: python3 generate_analytics_dashboard.py
"""

import sqlite3
import json
import os
from datetime import datetime, timezone

DB_PATH = os.environ.get("DB_PATH", "data/signals.db")
OUT_PATH = "docs/analytics_dashboard.html"


def q(conn, sql, params=()):
    return conn.execute(sql, params).fetchall()


def load_signals(conn, table):
    """Carica tutti i segnali chiusi di una tabella (v41_signals o v41p1_signals)
    con i campi necessari per tutte le sezioni analitiche."""
    rows = q(conn, f"""
        SELECT asset, session, final_outcome, mae, mfe, tp1_hit, tp2_hit,
               trigger_types, quality_label, expected_move_points,
               liquidity_target, timestamp_setup
        FROM {table}
        WHERE final_outcome != 'OPEN'
        ORDER BY timestamp_setup DESC
    """)
    result = []
    for r in rows:
        try:
            types = json.loads(r[7]) if r[7] else []
        except Exception:
            types = []
        if "BOS" in types and "CHOCH" in types:
            trigger = "BOS+CHOCH"
        elif "BOS" in types:
            trigger = "BOS"
        elif "CHOCH" in types:
            trigger = "CHOCH"
        else:
            trigger = "OTHER"

        result.append({
            "asset": r[0], "session": r[1], "outcome": r[2],
            "mae": r[3] or 0, "mfe": r[4] or 0,
            "tp1_hit": bool(r[5]), "tp2_hit": bool(r[6]),
            "trigger": trigger, "quality": r[8],
            "em": r[9], "liquidity_target": r[10] or "N/A",
            "ts": r[11],
        })
    return result


def stats(rows):
    """Statistiche aggregate su un sottoinsieme di segnali."""
    n = len(rows)
    if n == 0:
        return {"n": 0, "win": 0, "tp1": 0, "tp2": 0, "sl": 0,
                "exp_r": 0, "avg_mae": 0, "avg_mfe": 0, "avg_em": 0}
    wins = sum(1 for r in rows if r["outcome"] == "TP")
    sls = sum(1 for r in rows if r["outcome"] == "SL")
    tp1 = sum(1 for r in rows if r["tp1_hit"])
    tp2 = sum(1 for r in rows if r["tp2_hit"])
    ems = [r["em"] for r in rows if r["em"] is not None]
    return {
        "n": n,
        "win": round(wins / n * 100, 1),
        "tp1": round(tp1 / n * 100, 1),
        "tp2": round(tp2 / n * 100, 1),
        "sl": round(sls / n * 100, 1),
        "exp_r": round(((wins * 2) - sls) / n, 2),
        "avg_mae": round(sum(r["mae"] for r in rows) / n, 1),
        "avg_mfe": round(sum(r["mfe"] for r in rows) / n, 1),
        "avg_em": round(sum(ems) / len(ems), 1) if ems else 0,
    }


def breakdown(rows, key_fn, keys):
    return {k: stats([r for r in rows if key_fn(r) == k]) for k in keys}


def em_bucket(em):
    if em is None:
        return None
    if em < 100:
        return "0-100"
    if em < 200:
        return "100-200"
    if em < 300:
        return "200-300"
    return "300+"


def perf_table(d, keys, title, key_label="", highlight_threshold=None):
    """Tabella performance generica: N, Win%, TP1%, TP2%, Expectancy."""
    body = ""
    for k in keys:
        v = d.get(k, stats([]))
        if v["n"] == 0:
            continue
        win_cls = "pos" if v["win"] >= 40 else ("neg" if v["win"] < 20 else "")
        exp_cls = "pos" if v["exp_r"] > 0 else "neg"
        hi_cls = "row-highlight" if highlight_threshold and v["win"] >= highlight_threshold else ""
        body += f"""<tr class="{hi_cls}">
  <td><strong>{k}</strong></td>
  <td class="mono">{v["n"]}</td>
  <td class="mono {win_cls}">{v["win"]}%</td>
  <td class="mono">{v["tp1"]}%</td>
  <td class="mono">{v["tp2"]}%</td>
  <td class="mono {exp_cls}">{v["exp_r"]:+.2f}R</td>
</tr>"""
    if not body:
        body = '<tr><td colspan="6" style="text-align:center;color:var(--dim);padding:16px">Nessun dato</td></tr>'
    return f"""<div class="card">
  <div class="ch">{title}</div>
  <table><thead><tr><th>{key_label}</th><th>N</th><th>Win%</th><th>TP1%</th><th>TP2%</th><th>Expectancy</th></tr></thead>
  <tbody>{body}</tbody></table></div>"""


def em_analysis_table(rows, title):
    buckets = ["0-100", "100-200", "200-300", "300+"]
    bucketed = {b: [] for b in buckets}
    for r in rows:
        b = em_bucket(r["em"])
        if b:
            bucketed[b].append(r)

    body = ""
    max_win = 0
    bucket_stats = {}
    for b in buckets:
        s = stats(bucketed[b])
        bucket_stats[b] = s
        if s["n"] > 0:
            max_win = max(max_win, s["win"])

    for b in buckets:
        s = bucket_stats[b]
        if s["n"] == 0:
            continue
        win_cls = "pos" if s["win"] >= 30 else ("neg" if s["win"] < 15 else "")
        exp_cls = "pos" if s["exp_r"] > 0 else "neg"
        is_best = s["win"] == max_win and s["n"] >= 3
        hi_cls = "row-highlight" if is_best else ""
        body += f"""<tr class="{hi_cls}">
  <td><strong>{b} pt</strong></td>
  <td class="mono">{s["n"]}</td>
  <td class="mono {win_cls}">{s["win"]}%</td>
  <td class="mono">{s["tp1"]}%</td>
  <td class="mono">{s["tp2"]}%</td>
  <td class="mono {exp_cls}">{s["exp_r"]:+.2f}R</td>
</tr>"""
    if not body:
        body = '<tr><td colspan="6" style="text-align:center;color:var(--dim);padding:16px">Nessun dato</td></tr>'

    return f"""<div class="card">
  <div class="ch">{title}</div>
  <table><thead><tr><th>Expected Move</th><th>N</th><th>Win%</th><th>TP1%</th><th>TP2%</th><th>Expectancy</th></tr></thead>
  <tbody>{body}</tbody></table>
  <div class="card-note">Bucket con almeno 3 segnali e Win% piu' alto evidenziato in verde.</div>
  </div>"""


def liquidity_target_table(rows, title):
    targets = sorted({r["liquidity_target"] for r in rows if r["liquidity_target"] != "N/A"})
    expected_targets = [
        "Weekly High", "Weekly Low", "Daily High", "Daily Low",
        "Daily High (prev)", "Daily Low (prev)",
        "Equal Highs", "Equal Lows", "H4 Swing High", "H4 Swing Low",
        "Asia High", "Asia Low", "London High", "London Low",
        "NY High", "NY Low",
    ]
    all_targets = [t for t in expected_targets if t in targets] + \
                  [t for t in targets if t not in expected_targets]

    d = breakdown(rows, lambda r: r["liquidity_target"], all_targets)

    body = ""
    not_yet_tracked = []
    for t in expected_targets:
        if t.startswith(("Asia ", "London ", "NY ")) and d.get(t, stats([]))["n"] == 0:
            not_yet_tracked.append(t)

    for t in all_targets:
        v = d.get(t, stats([]))
        if v["n"] == 0:
            continue
        win_cls = "pos" if v["win"] >= 35 else ("neg" if v["win"] < 15 else "")
        exp_cls = "pos" if v["exp_r"] > 0 else "neg"
        body += f"""<tr>
  <td><strong>{t}</strong></td>
  <td class="mono">{v["n"]}</td>
  <td class="mono {win_cls}">{v["win"]}%</td>
  <td class="mono">{v["tp1"]}%</td>
  <td class="mono">{v["tp2"]}%</td>
  <td class="mono {exp_cls}">{v["exp_r"]:+.2f}R</td>
</tr>"""
    if not body:
        body = '<tr><td colspan="6" style="text-align:center;color:var(--dim);padding:16px">Nessun dato</td></tr>'

    note = ""
    if not_yet_tracked:
        note = (f'<div class="card-note">Target sessione ({", ".join(not_yet_tracked)}) '
                f'non ancora presenti nei segnali storici: saranno popolati dai prossimi '
                f'segnali generati dopo l\'estensione della Liquidity Map.</div>')

    return f"""<div class="card">
  <div class="ch">{title}</div>
  <table><thead><tr><th>Target</th><th>N</th><th>Win%</th><th>TP1%</th><th>TP2%</th><th>Expectancy</th></tr></thead>
  <tbody>{body}</tbody></table>
  {note}
  </div>"""


def framework_section(rows, fw_name, fw_color):
    s = stats(rows)
    trigger_table = perf_table(
        breakdown(rows, lambda r: r["trigger"], ["BOS", "CHOCH", "BOS+CHOCH"]),
        ["BOS", "CHOCH", "BOS+CHOCH"], "Performance per Trigger", "Trigger", 40
    )
    asset_table = perf_table(
        breakdown(rows, lambda r: r["asset"].replace("_USDT", ""), ["BTC", "PAXG"]),
        ["BTC", "PAXG"], "Performance per Asset", "Asset", 40
    )
    session_table = perf_table(
        breakdown(rows, lambda r: r["session"] or "N/A", ["ASIA", "LONDON", "OVERLAP", "NEW_YORK"]),
        ["ASIA", "LONDON", "OVERLAP", "NEW_YORK"], "Performance per Sessione", "Sessione", 40
    )
    em_table = em_analysis_table(rows, "Expected Move Analysis")
    liq_table = liquidity_target_table(rows, "Liquidity Target Analysis")

    return f"""
  <div class="fw-summary" style="border-top:2px solid {fw_color}">
    <div class="fw-summary-label" style="color:{fw_color}">{fw_name}</div>
    <div class="fw-summary-grid">
      <div><span class="big">{s["n"]}</span><br><span class="lbl">Segnali chiusi</span></div>
      <div><span class="big {'pos' if s['win']>=30 else 'neg'}">{s["win"]}%</span><br><span class="lbl">Win Rate</span></div>
      <div><span class="big">{s["tp1"]}%</span><br><span class="lbl">TP1 Hit</span></div>
      <div><span class="big">{s["tp2"]}%</span><br><span class="lbl">TP2 Hit</span></div>
      <div><span class="big {'pos' if s['exp_r']>0 else 'neg'}">{s["exp_r"]:+.2f}R</span><br><span class="lbl">Expectancy</span></div>
      <div><span class="big neg">{s["avg_mae"]}</span><br><span class="lbl">MAE medio</span></div>
      <div><span class="big pos">{s["avg_mfe"]}</span><br><span class="lbl">MFE medio</span></div>
      <div><span class="big">{s["avg_em"]}</span><br><span class="lbl">Expected Move medio</span></div>
    </div>
  </div>

  <div class="grid-2">
    {trigger_table}
    {asset_table}
  </div>
  <div class="grid-2">
    {session_table}
    {em_table}
  </div>
  {liq_table}
"""


def generate():
    conn = sqlite3.connect(DB_PATH)

    v41_rows = load_signals(conn, "v41_signals")
    try:
        v41p1_rows = load_signals(conn, "v41p1_signals")
    except sqlite3.OperationalError:
        v41p1_rows = []

    conn.close()

    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    section_v41 = framework_section(v41_rows, "V4.1 — Intraday Wave", "#4fffb0")
    section_v41p1 = framework_section(v41p1_rows, "V4.1 Phase 1 — Money Flow", "#ffd166")

    html = f"""<!DOCTYPE html>
<html lang="it">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Crypto Signal Engine — Analytics Lab</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600&family=IBM+Plex+Sans:wght@300;400;600&display=swap');
:root{{
  --bg:#0d0f14;--surface:#141720;--border:#1e2330;
  --accent:#4fffb0;--accent2:#ff6b6b;--text:#e2e8f0;--dim:#5a6478;
  --buy:#4fffb0;--sell:#ff6b6b;
}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:var(--bg);color:var(--text);font-family:'IBM Plex Sans',sans-serif;font-size:14px;line-height:1.6}}
header{{border-bottom:1px solid var(--border);padding:18px 32px;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px}}
header h1{{font-family:'IBM Plex Mono',monospace;font-size:13px;font-weight:600;letter-spacing:.12em;text-transform:uppercase;color:var(--accent)}}
header .meta{{font-family:'IBM Plex Mono',monospace;font-size:11px;color:var(--dim)}}
header a{{color:var(--accent);text-decoration:none;font-family:'IBM Plex Mono',monospace;font-size:11px}}
.container{{max-width:1320px;margin:0 auto;padding:24px 32px}}
.fw-summary{{background:var(--surface);border:1px solid var(--border);border-radius:6px;overflow:hidden;margin-bottom:16px}}
.fw-summary-label{{padding:12px 16px;font-family:'IBM Plex Mono',monospace;font-size:13px;font-weight:600;letter-spacing:.08em;text-transform:uppercase;border-bottom:1px solid var(--border)}}
.fw-summary-grid{{display:grid;grid-template-columns:repeat(8,1fr);gap:1px;background:var(--border)}}
.fw-summary-grid>div{{background:var(--surface);padding:14px 8px;text-align:center}}
.fw-summary-grid .big{{font-family:'IBM Plex Mono',monospace;font-size:18px;font-weight:600;color:var(--text)}}
.fw-summary-grid .big.pos{{color:var(--buy)}} .fw-summary-grid .big.neg{{color:var(--sell)}}
.fw-summary-grid .lbl{{font-size:9px;text-transform:uppercase;letter-spacing:.06em;color:var(--dim);display:block;margin-top:3px}}
.card{{background:var(--surface);border:1px solid var(--border);border-radius:6px;overflow:hidden;margin-bottom:16px}}
.ch{{padding:10px 16px;border-bottom:1px solid var(--border);font-family:'IBM Plex Mono',monospace;font-size:11px;font-weight:600;letter-spacing:.1em;text-transform:uppercase;color:var(--dim)}}
.card-note{{padding:10px 16px;font-size:11px;color:var(--dim);border-top:1px solid var(--border);font-style:italic}}
table{{width:100%;border-collapse:collapse}}
th{{font-family:'IBM Plex Mono',monospace;font-size:10px;font-weight:600;text-transform:uppercase;letter-spacing:.08em;color:var(--dim);padding:9px 14px;text-align:left;border-bottom:1px solid var(--border)}}
td{{padding:8px 14px;border-bottom:1px solid var(--border);font-size:13px}}
tr:last-child td{{border-bottom:none}} tr:hover td{{background:rgba(255,255,255,.02)}}
tr.row-highlight td{{background:rgba(79,255,176,.06)}}
.mono{{font-family:'IBM Plex Mono',monospace;font-size:12px}}
.pos{{color:var(--buy);font-weight:600}} .neg{{color:var(--sell)}}
.grid-2{{display:grid;grid-template-columns:1fr 1fr;gap:16px}}
.section-divider{{margin:32px 0 20px;padding-top:8px;border-top:2px dashed var(--border)}}
@media(max-width:900px){{.grid-2{{grid-template-columns:1fr}}.fw-summary-grid{{grid-template-columns:repeat(4,1fr)}}.container{{padding:12px}}}}
</style>
</head>
<body>
<header>
  <h1>Crypto Signal Engine — Analytics Lab</h1>
  <div class="meta">
    {generated} &nbsp;|&nbsp;
    <a href="unified_dashboard.html">&larr; Torna alla Dashboard</a>
  </div>
</header>
<div class="container">

  {section_v41}

  <div class="section-divider"></div>

  {section_v41p1}

</div>
</body>
</html>"""

    os.makedirs("docs", exist_ok=True)
    with open(OUT_PATH, "w") as f:
        f.write(html)
    print(f"Analytics dashboard generata: {OUT_PATH} (V4.1: {len(v41_rows)} segnali, V4.1P1: {len(v41p1_rows)} segnali)")


if __name__ == "__main__":
    generate()
