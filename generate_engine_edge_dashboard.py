"""
generate_engine_edge_dashboard.py
Engine Edge Lab — quali engine MIE aiutano davvero ogni strategia

Legge dal Decision Ledger (data/decision_ledger.db — file SEPARATO da
signals.db, vedi core/decision_ledger/ledger_writer.py) e per ogni
strategia presente (V41P1, OTE-SC, LH, TRB, ...) calcola, per ognuno
dei 13 engine MIE:

    Edge % = Win Rate(engine favorevole) − Win Rate(engine neutro/contrario)

Il campo "{engine}_state" e' gia' il voto dell'engine RISPETTO alla
direzione del trade (+1 favorevole, 0 neutro, -1 contrario), calcolato
da decision_collector.py — non serve reinterpretare gli snapshot grezzi.

LIMITE NOTO: le confluenze specifiche di ogni strategia (es. flag_bos_
present per LH, entry_zone_type per TRB) non sono oggi colonne del
Ledger — decision_collector.collect_decision() legge solo i campi
standard dal trade dict (entry/sl/tp/rr/quality/session/trigger_types).
Questa dashboard analizza quindi i 13 engine MIE, non le confluenze SMC.

SOGLIA DI AFFIDABILITA': con un singolo fattore (13 engine, non 18)
la soglia minima per un edge non-rumoroso e' piu' bassa che per l'analisi
combinatoria multi-fattore (~150-180 trade), ma sotto ~20 trade per
gruppo il numero resta un'indicazione, non una conclusione. La dashboard
etichetta esplicitamente ogni edge sotto soglia come "dato provvisorio".

Genera docs/engine_edge_dashboard.html
"""

import sqlite3
import os
from datetime import datetime, timezone

LEDGER_DB_PATH = os.environ.get("LEDGER_DB_PATH", "data/decision_ledger.db")
OUT_PATH       = "docs/engine_edge_dashboard.html"

MIN_SAMPLE = 20  # sotto questa soglia per gruppo, l'edge e' "provvisorio"

CLOSED_OUTCOMES = ("TP", "SL", "BE", "EXPIRED", "VIRTUAL_TP", "VIRTUAL_SL")
WIN_OUTCOMES    = ("TP", "VIRTUAL_TP")

ENGINES = [
    ("structure",     "Structure"),
    ("trend_health",  "Trend Health"),
    ("volatility",    "Volatility"),
    ("displacement",  "Displacement"),
    ("order_block",   "Order Block"),
    ("fvg",           "FVG"),
    ("liquidity",     "Liquidity"),
    ("session_sweep", "Session Sweep"),
    ("reaction_map",  "Reaction Map"),
    ("candlestick",   "Candlestick"),
    ("macro",         "Macro"),
    ("market_state",  "Market State"),
    ("money_flow",    "Money Flow"),
]

STRATEGY_ORDER = ["V41P1", "OTE-SC", "LH", "TRB"]
STRATEGY_COLOR = {
    "V41P1": "#ffd166", "OTE-SC": "#4fffb0",
    "LH": "#38bdf8", "TRB": "#a78bfa",
}


# ============================================================
# Data loading
# ============================================================

def q(conn, sql, params=()):
    return conn.execute(sql, params).fetchall()


def load_strategies(conn):
    try:
        rows = q(conn, """
            SELECT DISTINCT strategy FROM decision_ledger
            WHERE decision_type='EXECUTED' AND outcome != 'PENDING'
        """)
    except sqlite3.OperationalError:
        return []
    found = [r[0] for r in rows if r[0]]
    ordered = [s for s in STRATEGY_ORDER if s in found]
    ordered += sorted(s for s in found if s not in STRATEGY_ORDER)
    return ordered


def load_closed_decisions(conn, strategy):
    cols = ["outcome", "r_realized", "regime"]
    for eng, _ in ENGINES:
        cols.append(f"{eng}_state")
    col_sql = ", ".join(cols)
    placeholders = ",".join("?" for _ in CLOSED_OUTCOMES)
    try:
        rows = q(conn, f"""
            SELECT {col_sql} FROM decision_ledger
            WHERE strategy=? AND decision_type='EXECUTED'
              AND outcome IN ({placeholders})
        """, (strategy, *CLOSED_OUTCOMES))
    except sqlite3.OperationalError:
        return []

    def _to_float(v):
        if v is None:
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    def _to_state(v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return 0

    out = []
    for r in rows:
        d = dict(zip(cols, r))
        d["r_realized"] = _to_float(d.get("r_realized"))
        for eng, _ in ENGINES:
            key = f"{eng}_state"
            d[key] = _to_state(d.get(key))
        out.append(d)
    return out


# ============================================================
# Stats
# ============================================================

def _winrate(group):
    n = len(group)
    if n == 0:
        return None
    wins = sum(1 for r in group if r["outcome"] in WIN_OUTCOMES)
    return round(wins / n * 100, 1)


def _avg_r(group):
    vals = [r["r_realized"] for r in group if r["r_realized"] is not None]
    if not vals:
        return None
    return round(sum(vals) / len(vals), 2)


def engine_edge(rows, eng_key):
    fav = [r for r in rows if r[eng_key] == 1]
    oth = [r for r in rows if r[eng_key] != 1]  # 0 (neutro) o -1 (contrario)

    win_fav, win_oth = _winrate(fav), _winrate(oth)
    r_fav, r_oth     = _avg_r(fav), _avg_r(oth)

    edge   = round(win_fav - win_oth, 1) if (win_fav is not None and win_oth is not None) else None
    r_edge = round(r_fav - r_oth, 2)     if (r_fav is not None and r_oth is not None) else None

    provisional = len(fav) < MIN_SAMPLE or len(oth) < MIN_SAMPLE

    return {
        "n_fav": len(fav), "n_oth": len(oth),
        "win_fav": win_fav, "win_oth": win_oth, "edge": edge,
        "r_fav": r_fav, "r_oth": r_oth, "r_edge": r_edge,
        "provisional": provisional,
    }


def strategy_summary(rows):
    n = len(rows)
    if n == 0:
        return {"n": 0, "win": 0, "exp_r": 0}
    win = _winrate(rows) or 0
    r_vals = [r["r_realized"] for r in rows if r["r_realized"] is not None]
    exp_r = round(sum(r_vals) / len(r_vals), 2) if r_vals else 0
    return {"n": n, "win": win, "exp_r": exp_r}


def regime_breakdown(rows):
    keys = ["TRENDING", "RANGING", "TRANSITIONAL", "UNKNOWN"]
    out = {}
    for k in keys:
        sub = [r for r in rows if (r["regime"] or "UNKNOWN") == k]
        out[k] = strategy_summary(sub)
    return out


# ============================================================
# CSS — stessa identita' visiva delle altre dashboard
# ============================================================

CSS = """
@import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600&family=IBM+Plex+Sans:wght@300;400;600&display=swap');
:root{
  --bg:#0d0f14;--surface:#141720;--border:#1e2330;
  --accent:#4fffb0;--accent2:#ff6b6b;--accent3:#ffd166;--accent4:#a78bfa;--accent5:#38bdf8;
  --text:#e2e8f0;--dim:#5a6478;--buy:#4fffb0;--sell:#ff6b6b;
}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:'IBM Plex Sans',sans-serif;font-size:14px;line-height:1.6}
header{border-bottom:1px solid var(--border);padding:18px 32px;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px}
header h1{font-family:'IBM Plex Mono',monospace;font-size:13px;font-weight:600;letter-spacing:.12em;text-transform:uppercase;color:var(--accent)}
header .meta{font-family:'IBM Plex Mono',monospace;font-size:11px;color:var(--dim)}
header a{color:var(--accent);text-decoration:none;font-family:'IBM Plex Mono',monospace;font-size:11px}
.container{max-width:1320px;margin:0 auto;padding:24px 32px}
.intro{font-size:13px;color:var(--dim);max-width:760px;margin-bottom:24px;line-height:1.7}
.intro strong{color:var(--text)}
.fw-header{padding:14px 20px;font-family:'IBM Plex Mono',monospace;font-size:13px;font-weight:600;letter-spacing:.1em;text-transform:uppercase;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:12px}
.fw-tag{font-size:10px;padding:2px 8px;border-radius:4px;font-weight:600;background:rgba(90,100,120,.2);color:var(--dim)}
.summary-grid{display:grid;gap:1px;background:var(--border)}
.summary-grid.cols3{grid-template-columns:repeat(3,1fr)}
.summary-grid>div{background:var(--surface);padding:14px 8px;text-align:center}
.big{font-family:'IBM Plex Mono',monospace;font-size:18px;font-weight:600}
.big.pos{color:var(--buy)} .big.neg{color:var(--sell)} .big.warn{color:var(--accent3)}
.lbl{font-size:9px;text-transform:uppercase;letter-spacing:.06em;color:var(--dim);display:block;margin-top:3px}
.card{background:var(--surface);border:1px solid var(--border);border-radius:6px;overflow:hidden;margin-bottom:16px}
.ch{padding:10px 16px;border-bottom:1px solid var(--border);font-family:'IBM Plex Mono',monospace;font-size:11px;font-weight:600;letter-spacing:.1em;text-transform:uppercase;color:var(--dim)}
table{width:100%;border-collapse:collapse}
th{font-family:'IBM Plex Mono',monospace;font-size:10px;font-weight:600;text-transform:uppercase;letter-spacing:.08em;color:var(--dim);padding:9px 14px;text-align:left;border-bottom:1px solid var(--border)}
td{padding:8px 14px;border-bottom:1px solid var(--border);font-size:13px;vertical-align:middle}
tr:last-child td{border-bottom:none} tr:hover td{background:rgba(255,255,255,.02)}
.mono{font-family:'IBM Plex Mono',monospace;font-size:12px}
.pos{color:var(--buy);font-weight:600} .neg{color:var(--sell)} .warn{color:var(--accent3)}
.section-divider{margin:36px 0 24px;border-top:2px dashed var(--border);padding-top:8px}
.empty{text-align:center;padding:24px;color:var(--dim);font-size:13px}
.prov{font-size:10px;color:var(--dim);font-style:italic;margin-left:6px}
.edge-row td{padding:10px 14px}
.edge-name{font-weight:600;min-width:120px;display:inline-block}
.edge-bar-wrap{display:flex;align-items:center;gap:10px;min-width:280px}
.edge-bar-track{flex:1;height:16px;background:var(--border);border-radius:3px;position:relative;overflow:hidden}
.edge-bar-mid{position:absolute;left:50%;top:0;bottom:0;width:1px;background:rgba(255,255,255,.15)}
.edge-bar-fill{position:absolute;top:0;bottom:0;border-radius:2px}
.edge-bar-fill.pos-fill{background:var(--buy)}
.edge-bar-fill.neg-fill{background:var(--sell)}
.edge-val{font-family:'IBM Plex Mono',monospace;font-size:13px;font-weight:600;min-width:56px;text-align:right}
@media(max-width:900px){.summary-grid.cols3{grid-template-columns:repeat(3,1fr)}.container{padding:12px}.edge-bar-wrap{min-width:160px}}
"""


# ============================================================
# Rendering
# ============================================================

def _empty_row(cols):
    return f'<tr><td colspan="{cols}" class="empty">Nessun dato</td></tr>'


def edge_bar_row(label, e):
    if e["edge"] is None:
        return f"""<tr class="edge-row">
  <td><span class="edge-name">{label}</span></td>
  <td colspan="2" class="empty" style="text-align:left;padding-left:0">dati insufficienti (fav={e['n_fav']} / altro={e['n_oth']})</td>
</tr>"""

    edge = e["edge"]
    pct  = min(abs(edge), 50) / 50 * 50  # scala barra: ±50pt di edge = barra piena
    side = "left" if edge < 0 else "left:50%"
    fill_cls = "pos-fill" if edge >= 0 else "neg-fill"
    style = f"width:{pct}%;{'left:calc(50% - ' + str(pct) + '%)' if edge < 0 else 'left:50%'}"
    val_cls = "pos" if edge > 0 else ("neg" if edge < 0 else "")
    prov = '<span class="prov">provvisorio</span>' if e["provisional"] else ""

    # I win rate/R medi possono essere None (gruppo senza r_realized registrato):
    # formattazione difensiva per non far crashare la f-string.
    def _pct(v):  return f"{v}%"       if v is not None else "n/d"
    def _r(v):    return f"{v:+.2f}R"  if v is not None else "n/d"

    return f"""<tr class="edge-row">
  <td><span class="edge-name">{label}</span>{prov}</td>
  <td>
    <div class="edge-bar-wrap">
      <div class="edge-bar-track">
        <div class="edge-bar-mid"></div>
        <div class="edge-bar-fill {fill_cls}" style="{style}"></div>
      </div>
      <span class="edge-val {val_cls}">{edge:+.1f}%</span>
    </div>
  </td>
  <td class="mono" style="font-size:11px;color:var(--dim)">
    fav {e['n_fav']} ({_pct(e['win_fav'])} win, {_r(e['r_fav'])} avg) &nbsp;·&nbsp;
    altro {e['n_oth']} ({_pct(e['win_oth'])} win, {_r(e['r_oth'])} avg)
  </td>
</tr>"""


def regime_table(rb):
    body = ""
    for k, v in rb.items():
        if v["n"] == 0:
            continue
        wc = "pos" if v["win"] >= 40 else ("neg" if v["win"] < 25 else "warn")
        ec = "pos" if v["exp_r"] > 0 else "neg"
        body += f"""<tr>
  <td><strong>{k}</strong></td>
  <td class="mono">{v['n']}</td>
  <td class="mono {wc}">{v['win']}%</td>
  <td class="mono {ec}">{v['exp_r']:+.2f}R</td>
</tr>"""
    if not body:
        body = _empty_row(4)
    return f"""<div class="card"><div class="ch">Per Regime di Mercato</div>
  <table><thead><tr><th>Regime</th><th>N</th><th>Win%</th><th>Expectancy</th></tr></thead>
  <tbody>{body}</tbody></table></div>"""


def section_strategy(strategy, rows):
    color = STRATEGY_COLOR.get(strategy, "#e2e8f0")
    s = strategy_summary(rows)
    wc = "pos" if s["win"] >= 40 else ("neg" if s["win"] < 25 else "warn")
    ec = "pos" if s["exp_r"] > 0 else "neg"

    summary = f"""<div class="summary-grid cols3" style="border:1px solid var(--border);border-top:2px solid {color};border-radius:6px;overflow:hidden;margin-bottom:16px">
  <div><span class="big">{s['n']}</span><span class="lbl">Decisioni chiuse</span></div>
  <div><span class="big {wc}">{s['win']}%</span><span class="lbl">Win Rate</span></div>
  <div><span class="big {ec}">{s['exp_r']:+.2f}R</span><span class="lbl">Expectancy</span></div>
</div>"""

    if not rows:
        body = f"""
<div class="card" style="border-top:2px solid {color}">
  <div class="fw-header" style="color:{color}">{strategy}<span class="fw-tag">IN ATTESA DI DATI</span></div>
  <div class="empty">Nessuna decisione chiusa ancora nel Ledger per questa strategia.</div>
</div>"""
        return body

    edges = [(label, engine_edge(rows, f"{key}_state")) for key, label in ENGINES]
    edges.sort(key=lambda x: (x[1]["edge"] is None, -(x[1]["edge"] or 0)))

    edge_rows = "".join(edge_bar_row(label, e) for label, e in edges)

    n_prov = sum(1 for _, e in edges if e.get("provisional") and e["edge"] is not None)

    edge_card = f"""<div class="card">
  <div class="ch">Engine Edge — Win Rate quando favorevole vs. neutro/contrario
    {f'<span class="prov" style="margin-left:8px">{n_prov} engine sotto soglia campione ({MIN_SAMPLE}/gruppo)</span>' if n_prov else ''}
  </div>
  <table><tbody>{edge_rows}</tbody></table>
</div>"""

    return f"""
<div class="card" style="border-top:2px solid {color};background:transparent;border-left:none;border-right:none">
  <div class="fw-header" style="color:{color};border-bottom:none">{strategy}</div>
</div>
{summary}
{edge_card}
{regime_table(regime_breakdown(rows))}
"""


# ============================================================
# Generate
# ============================================================

def generate():
    os.makedirs("docs", exist_ok=True)
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    try:
        conn = sqlite3.connect(LEDGER_DB_PATH)
        conn.row_factory = None
        strategies = load_strategies(conn)
        sections = ""
        counts = []
        for strat in strategies:
            rows = load_closed_decisions(conn, strat)
            counts.append(f"{strat}:{len(rows)}")
            sections += section_strategy(strat, rows)
            sections += '<div class="section-divider"></div>'
        conn.close()
    except Exception as e:
        strategies = []
        sections = f'<div class="card"><div class="empty">Decision Ledger non disponibile: {e}</div></div>'
        counts = []
        print(f"Engine Edge dashboard: errore non gestito — {e}")

    if not strategies:
        sections = '<div class="card"><div class="empty">Nessuna strategia con decisioni chiuse nel Decision Ledger ancora.</div></div>'

    html = f"""<!DOCTYPE html>
<html lang="it">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Engine Edge Lab</title>
<style>{CSS}</style>
</head>
<body>
<header>
  <h1>Engine Edge Lab</h1>
  <div class="meta">{generated} &nbsp;|&nbsp; <a href="analytics_dashboard.html">&larr; Analytics Lab</a></div>
</header>
<div class="container">
  <p class="intro">
    Per ogni strategia, il <strong>voto</strong> di ognuno dei 13 engine MIE rispetto alla direzione
    del trade (favorevole / neutro / contrario) viene confrontato con l'esito reale.
    <strong>Edge % = Win Rate quando l'engine era favorevole − Win Rate quando non lo era.</strong>
    Positivo (verde) = l'engine aiuta; negativo (rosso) = l'engine, quando favorevole, ha coinciso
    con esiti peggiori — vale la pena capire perché, non necessariamente scartarlo.
    Sotto {MIN_SAMPLE} trade per gruppo il numero è etichettato "provvisorio": rumore statistico,
    non un segnale su cui agire.
  </p>
  {sections}
</div>
</body>
</html>"""

    with open(OUT_PATH, "w") as f:
        f.write(html)

    print(f"Engine Edge dashboard generata: {OUT_PATH} ({', '.join(counts) if counts else 'nessuna strategia'})")


if __name__ == "__main__":
    generate()
