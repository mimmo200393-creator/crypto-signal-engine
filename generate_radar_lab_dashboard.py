"""
generate_radar_lab_dashboard.py
Radar Lab (BETA) — validazione del Market Radar

V1.1: aggiunta sezione "WR Reale" basata su first_hit (chi tra TP e SL
viene toccato per primo). TP alzato a 2 ATR (RR 1:2).

Legge da signals.db → radar_zones + radar_transitions.

Genera docs/radar_lab_dashboard.html
"""

import sqlite3
import os
import json
import statistics
from datetime import datetime, timezone

DB_PATH  = os.environ.get("DB_PATH", "data/signals.db")
OUT_PATH = "docs/radar_lab_dashboard.html"

MIN_SAMPLE = 30


def q(conn, sql, params=()):
    try:
        return conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        return []


# ============================================================
# Data loading
# ============================================================

def load_zones(conn):
    rows = q(conn, """
        SELECT zone_id, asset, direction, emit_ts, price, zone_ref,
               features_json, status, mae, mfe, bars_open, time_to_mfe, time_to_mae,
               stop_loss, stop_hit, time_to_stop, mfe_after_stop,
               tp_hit, be_reached, mfe_beyond_tp, first_hit, first_hit_bar
        FROM radar_zones
    """)
    out = []
    for r in rows:
        d = {
            "zone_id": r[0], "asset": r[1], "direction": r[2], "emit_ts": r[3],
            "price": r[4], "zone_ref": r[5], "status": r[7],
            "mae": r[8], "mfe": r[9], "bars_open": r[10],
            "time_to_mfe": r[11], "time_to_mae": r[12],
            "stop_loss": r[13], "stop_hit": r[14], "time_to_stop": r[15],
            "mfe_after_stop": r[16],
            "tp_hit": r[17] if len(r) > 17 else None,
            "be_reached": r[18] if len(r) > 18 else None,
            "mfe_beyond_tp": r[19] if len(r) > 19 else None,
            "first_hit": r[20] if len(r) > 20 else None,
            "first_hit_bar": r[21] if len(r) > 21 else None,
        }
        try:
            d["features"] = json.loads(r[6]) if r[6] else {}
        except Exception:
            d["features"] = {}
        out.append(d)
    return out


def load_transition_funnel(conn):
    rows = q(conn, """
        SELECT from_state, to_state, COUNT(*) FROM radar_transitions
        GROUP BY from_state, to_state
    """)
    return {(r[0], r[1]): r[2] for r in rows}


# ============================================================
# Stats
# ============================================================

def _num(vals):
    return [v for v in vals if isinstance(v, (int, float))]

def _avg(vals):
    v = _num(vals)
    return round(sum(v) / len(v), 3) if v else None

def _median(vals):
    v = _num(vals)
    return round(statistics.median(v), 3) if v else None

def mfe_in_atr(z):
    atr = (z.get("features") or {}).get("atr")
    if atr and atr > 0 and z.get("mfe") is not None:
        return z["mfe"] / atr
    return None

def mae_in_atr(z):
    atr = (z.get("features") or {}).get("atr")
    if atr and atr > 0 and z.get("mae") is not None:
        return z["mae"] / atr
    return None


def summarize(zones):
    closed = [z for z in zones if z["status"] == "CLOSED"]
    stop_hits = [z for z in closed if z.get("stop_hit")]
    return {
        "total":     len(zones),
        "closed":    len(closed),
        "open":      sum(1 for z in zones if z["status"] == "OPEN"),
        "mfe_avg_atr":    _avg([mfe_in_atr(z) for z in closed]),
        "mfe_med_atr":    _median([mfe_in_atr(z) for z in closed]),
        "mae_avg_atr":    _avg([mae_in_atr(z) for z in closed]),
        "bars_to_mfe_avg": _avg([z.get("time_to_mfe") for z in closed]),
        "stop_hit_n":     len(stop_hits),
        "stop_hit_pct":   round(len(stop_hits) / len(closed) * 100, 1) if closed else None,
    }


def by_asset(zones):
    out = {}
    for a in sorted({z["asset"] for z in zones}):
        out[a] = summarize([z for z in zones if z["asset"] == a])
    return out


def velocity_buckets(zones):
    closed = [z for z in zones if z["status"] == "CLOSED"]
    def vel(z):
        f = z.get("features") or {}
        return f.get("impulse_velocity")
    buckets = [
        ("sotto soglia (< 0.6) \u26a0", lambda v: v is not None and v < 0.6),
        ("normale (0.6\u20130.8)",      lambda v: v is not None and 0.6 <= v < 0.8),
        ("veloce (0.8\u20131.0)",       lambda v: v is not None and 0.8 <= v < 1.0),
        ("molto veloce (\u2265 1.0)",   lambda v: v is not None and v >= 1.0),
    ]
    out = []
    for label, cond in buckets:
        sub = [z for z in closed if cond(vel(z))]
        out.append((label, len(sub),
                    _avg([mfe_in_atr(z) for z in sub]),
                    _avg([mae_in_atr(z) for z in sub])))
    return out


# ============================================================
# First Hit stats (V1.1)
# ============================================================

def _first_hit_stats(zones):
    """Calcola WR reale e expectancy basati su first_hit."""
    closed = [z for z in zones if z["status"] == "CLOSED"]
    with_hit = [z for z in closed if z.get("first_hit") in ("TP", "SL")]
    if not with_hit:
        return None
    tp_first = sum(1 for z in with_hit if z["first_hit"] == "TP")
    sl_first = sum(1 for z in with_hit if z["first_hit"] == "SL")
    n = len(with_hit)
    wr = round(tp_first / n * 100, 1)
    # Expectancy a RR 1:2: win = +2R, loss = -1R
    exp = round((tp_first * 2 - sl_first) / n, 3)
    avg_bar = _avg([z.get("first_hit_bar") for z in with_hit])
    return {
        "n": n,
        "tp_first": tp_first,
        "sl_first": sl_first,
        "no_hit": len(closed) - n,
        "wr": wr,
        "exp": exp,
        "avg_bar": avg_bar,
    }


# ============================================================
# CSS
# ============================================================

CSS = """
@import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600&family=IBM+Plex+Sans:wght@300;400;600&display=swap');
:root{--bg:#0d0f14;--surface:#141720;--border:#1e2330;--accent:#4fffb0;--accent2:#ff6b6b;
--accent3:#ffd166;--accent5:#38bdf8;--text:#e2e8f0;--dim:#5a6478;--buy:#4fffb0;--sell:#ff6b6b;}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:'IBM Plex Sans',sans-serif;font-size:14px;line-height:1.6}
header{border-bottom:1px solid var(--border);padding:18px 32px;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px}
header h1{font-family:'IBM Plex Mono',monospace;font-size:13px;font-weight:600;letter-spacing:.12em;text-transform:uppercase;color:var(--accent5)}
.beta{font-size:9px;padding:2px 7px;border-radius:4px;background:rgba(255,209,102,.15);color:var(--accent3);margin-left:8px;letter-spacing:.08em}
header .meta{font-family:'IBM Plex Mono',monospace;font-size:11px;color:var(--dim)}
header a{color:var(--accent5);text-decoration:none;font-family:'IBM Plex Mono',monospace;font-size:11px}
.container{max-width:1100px;margin:0 auto;padding:24px 32px}
.intro{font-size:13px;color:var(--dim);max-width:720px;margin-bottom:24px;line-height:1.7}
.intro strong{color:var(--text)}
.summary-grid{display:grid;gap:1px;background:var(--border);border:1px solid var(--border);border-radius:8px;overflow:hidden;margin-bottom:20px}
.summary-grid.c4{grid-template-columns:repeat(4,1fr)}
.summary-grid.c5{grid-template-columns:repeat(5,1fr)}
.summary-grid>div{background:var(--surface);padding:16px 8px;text-align:center}
.big{font-family:'IBM Plex Mono',monospace;font-size:20px;font-weight:600}
.big.pos{color:var(--buy)}.big.neg{color:var(--sell)}.big.warn{color:var(--accent3)}
.lbl{font-size:9px;text-transform:uppercase;letter-spacing:.06em;color:var(--dim);display:block;margin-top:4px}
.card{background:var(--surface);border:1px solid var(--border);border-radius:8px;overflow:hidden;margin-bottom:16px}
.ch{padding:12px 16px;border-bottom:1px solid var(--border);font-family:'IBM Plex Mono',monospace;font-size:11px;font-weight:600;letter-spacing:.1em;text-transform:uppercase;color:var(--dim)}
.table-scroll{overflow-x:auto;-webkit-overflow-scrolling:touch}
table{width:100%;border-collapse:collapse}
th{font-family:'IBM Plex Mono',monospace;font-size:10px;font-weight:600;text-transform:uppercase;letter-spacing:.08em;color:var(--dim);padding:10px 14px;text-align:left;border-bottom:1px solid var(--border);white-space:nowrap}
td{padding:10px 14px;border-bottom:1px solid var(--border);font-size:13px;white-space:nowrap}
tr:last-child td{border-bottom:none}tr:hover td{background:rgba(255,255,255,.02)}
.mono{font-family:'IBM Plex Mono',monospace;font-size:12px}
.pos{color:var(--buy);font-weight:600}.neg{color:var(--sell)}.warn{color:var(--accent3)}
.prov{font-size:10px;color:var(--dim);font-style:italic}
.empty{text-align:center;padding:32px 16px;color:var(--dim);font-size:13px;line-height:1.7}
.note{font-size:11px;color:var(--dim);padding:10px 16px;border-top:1px solid var(--border);line-height:1.6}
@media(max-width:640px){
  header{padding:14px 16px}.container{padding:12px}
  .intro{font-size:12px}
  .summary-grid.c4{grid-template-columns:repeat(2,1fr)}
  .summary-grid.c5{grid-template-columns:repeat(2,1fr)}
  .big{font-size:18px}
  .ch{font-size:10px;padding:10px 12px}
  th,td{padding:9px 12px;font-size:12px}
}
"""


# ============================================================
# Rendering
# ============================================================

def metric(val, unit="", cls=""):
    if val is None:
        return '<span class="big">\u2014</span>'
    sign = "+" if (isinstance(val, (int, float)) and val > 0 and unit == "R") else ""
    return f'<span class="big {cls}">{sign}{val}{unit}</span>'


def summary_block(s):
    mfe_cls = "pos" if (s["mfe_avg_atr"] or 0) > 0 else "neg"
    return f"""<div class="summary-grid c4">
  <div>{metric(s['total'])}<span class="lbl">Zone emesse</span></div>
  <div>{metric(s['closed'])}<span class="lbl">Chiuse</span></div>
  <div>{metric(s['mfe_avg_atr'],'', mfe_cls)}<span class="lbl">MFE medio (ATR)</span></div>
  <div>{metric(s['mae_avg_atr'],'', 'neg')}<span class="lbl">MAE medio (ATR)</span></div>
</div>"""


def asset_table(rows):
    if not rows:
        return ""
    body = ""
    for a, s in rows.items():
        prov = ' <span class="prov">(provv.)</span>' if s["closed"] < MIN_SAMPLE else ""
        mfe = s["mfe_avg_atr"]; mae = s["mae_avg_atr"]
        mfe_s = f'<span class="pos">+{mfe}</span>' if mfe is not None else "\u2014"
        mae_s = f'<span class="neg">{mae}</span>' if mae is not None else "\u2014"
        body += f"""<tr><td><strong>{a}</strong>{prov}</td>
  <td class="mono">{s['total']}</td><td class="mono">{s['closed']}</td>
  <td class="mono">{mfe_s}</td><td class="mono">{mae_s}</td>
  <td class="mono">{s['bars_to_mfe_avg'] if s['bars_to_mfe_avg'] is not None else '\u2014'}</td></tr>"""
    return f"""<div class="card"><div class="ch">Per Asset</div>
  <div class="table-scroll"><table><thead><tr>
    <th>Asset</th><th>Emesse</th><th>Chiuse</th><th>MFE atr</th><th>MAE atr</th><th>Candele al MFE</th>
  </tr></thead><tbody>{body}</tbody></table></div></div>"""


def velocity_table(buckets):
    body = ""
    for label, n, mfe, mae in buckets:
        if n == 0:
            body += f'<tr><td>{label}</td><td class="mono">0</td><td class="empty" colspan="2" style="text-align:left">nessun dato</td></tr>'
            continue
        prov = ' <span class="prov">(provv.)</span>' if n < MIN_SAMPLE else ""
        mfe_s = f'<span class="pos">+{mfe}</span>' if mfe is not None else "\u2014"
        mae_s = f'<span class="neg">{mae}</span>' if mae is not None else "\u2014"
        body += f'<tr><td>{label}{prov}</td><td class="mono">{n}</td><td class="mono">{mfe_s}</td><td class="mono">{mae_s}</td></tr>'
    return f"""<div class="card"><div class="ch">Ipotesi velocit\u00e0 \u2014 rimbalzo per fascia di impulso</div>
  <div class="table-scroll"><table><thead><tr>
    <th>Velocit\u00e0 impulso</th><th>N</th><th>MFE atr</th><th>MAE atr</th>
  </tr></thead><tbody>{body}</tbody></table></div>
  <div class="note">Se il radar ha edge sulla velocit\u00e0, le fasce pi\u00f9 veloci mostrano un MFE medio pi\u00f9 alto.
  Numeri sotto {MIN_SAMPLE} campioni sono provvisori: rumore, non conclusioni.</div></div>"""


def first_hit_card(zones):
    """V1.1: WR reale — chi tra TP (2 ATR) e SL (1 ATR) viene toccato per primo."""
    closed = [z for z in zones if z["status"] == "CLOSED"]
    if not closed:
        return ""

    # Globale
    g = _first_hit_stats(zones)
    if g is None or g["n"] == 0:
        return f"""<div class="card"><div class="ch">WR Reale \u2014 SL 1 ATR vs TP 2 ATR (chi arriva primo)</div>
  <div class="empty">Dati first_hit non ancora disponibili.<br>
  Le zone esistenti usavano TP 1 ATR. I dati si popoleranno con le nuove zone a TP 2 ATR,
  oppure dopo il backfill delle zone storiche (confronto time_to_tp vs time_to_stop).</div></div>"""

    prov = ' <span class="prov">(provv.)</span>' if g["n"] < MIN_SAMPLE else ""
    wr_cls = "pos" if g["wr"] >= 40 else ("neg" if g["wr"] < 25 else "warn")
    exp_cls = "pos" if g["exp"] > 0 else "neg"

    # Per asset
    asset_body = ""
    for a in sorted({z["asset"] for z in zones}):
        a_stats = _first_hit_stats([z for z in zones if z["asset"] == a])
        if a_stats is None or a_stats["n"] == 0:
            continue
        a_prov = ' <span class="prov">(provv.)</span>' if a_stats["n"] < MIN_SAMPLE else ""
        a_wr_cls = "pos" if a_stats["wr"] >= 40 else ("neg" if a_stats["wr"] < 25 else "warn")
        a_exp_cls = "pos" if a_stats["exp"] > 0 else "neg"
        asset_body += f"""<tr>
  <td><strong>{a}</strong>{a_prov}</td>
  <td class="mono">{a_stats['n']}</td>
  <td class="mono pos">{a_stats['tp_first']}</td>
  <td class="mono neg">{a_stats['sl_first']}</td>
  <td class="mono {a_wr_cls}">{a_stats['wr']}%</td>
  <td class="mono {a_exp_cls}">{a_stats['exp']:+.3f}R</td>
</tr>"""

    return f"""<div class="card"><div class="ch">WR Reale \u2014 SL 1 ATR vs TP 2 ATR (chi arriva primo)</div>
  <div class="summary-grid c5" style="border:none;margin:0">
    <div>{metric(g['n'])}<span class="lbl">Zone con esito{prov}</span></div>
    <div>{metric(g['tp_first'],'','pos')}<span class="lbl">TP primo</span></div>
    <div>{metric(g['sl_first'],'','neg')}<span class="lbl">SL primo</span></div>
    <div>{metric(g['wr'],'%',wr_cls)}<span class="lbl">Win Rate reale</span></div>
    <div>{metric(g['exp'],'R',exp_cls)}<span class="lbl">Expectancy (1:2)</span></div>
  </div>
  {f'<div class="table-scroll"><table><thead><tr><th>Asset</th><th>N</th><th>TP primo</th><th>SL primo</th><th>WR</th><th>Exp (1:2)</th></tr></thead><tbody>{asset_body}</tbody></table></div>' if asset_body else ''}
  <div class="note">Il numero che conta: se il TP (2 ATR) viene toccato prima dello SL (1 ATR)
  il trade sarebbe vincente a +2R; se lo SL arriva prima, perdente a -1R.
  Breakeven a 33.3% WR. Zone senza tocco di nessuno dei due non sono conteggiate.</div></div>"""


def gestione_card(zones):
    closed = [z for z in zones if z["status"] == "CLOSED"]
    if not closed:
        return ""
    tp_hit = [z for z in closed if z.get("tp_hit")]
    be = [z for z in closed if z.get("be_reached")]
    beyond_atr = []
    for z in tp_hit:
        atr = (z.get("features") or {}).get("atr")
        mb = z.get("mfe_beyond_tp")
        if atr and atr > 0 and mb is not None:
            beyond_atr.append(mb / atr)
    tp_pct = round(len(tp_hit) / len(closed) * 100, 1) if closed else 0
    beyond_avg = _avg(beyond_atr)
    return f"""<div class="card"><div class="ch">Gestione \u2014 TP target / BE / lascia-correre</div>
  <div class="summary-grid c4" style="border:none;margin:0">
    <div>{metric(tp_pct,'%','pos')}<span class="lbl">zone che colpiscono il TP (2 ATR)</span></div>
    <div>{metric(len(tp_hit))}<span class="lbl">TP colpiti su {len(closed)}</span></div>
    <div>{metric(round(len(be)/len(closed)*100,1) if closed else 0,'%')}<span class="lbl">che raggiungono il BE</span></div>
    <div>{metric(beyond_avg,'', 'pos' if (beyond_avg or 0)>0 else '')}<span class="lbl">respiro OLTRE il TP (ATR)</span></div>
  </div>
  <div class="note">Il numero chiave e' l'ultimo: quanto il respiro continua <strong>oltre</strong> il TP target (2 ATR).
  Se e' alto, il trailing batteva il target secco. Se e' ~0, chiudere a 2 ATR bastava.
  Tutti i livelli sono registrati: nulla viene chiuso, si misura solo cosa fa il prezzo.</div></div>"""


def stop_card(zones):
    closed = [z for z in zones if z["status"] == "CLOSED"]
    if not closed:
        return ""
    hits = [z for z in closed if z.get("stop_hit")]
    rebounded = 0
    for z in hits:
        atr = (z.get("features") or {}).get("atr")
        mas = z.get("mfe_after_stop")
        if atr and atr > 0 and mas is not None and mas / atr >= 1.0:
            rebounded += 1
    pct = round(len(hits) / len(closed) * 100, 1) if closed else 0
    reb_pct = round(rebounded / len(hits) * 100, 1) if hits else 0
    return f"""<div class="card"><div class="ch">Stop Loss \u2014 equilibrio respiro/stop</div>
  <div class="summary-grid c4" style="border:none;margin:0">
    <div>{metric(pct,'%','warn')}<span class="lbl">zone che toccano lo stop</span></div>
    <div>{metric(len(hits))}<span class="lbl">tocchi su {len(closed)}</span></div>
    <div>{metric(reb_pct,'%','pos')}<span class="lbl">di cui rimbalza dopo (\u22651 ATR)</span></div>
    <div>{metric(rebounded)}<span class="lbl">respiro recuperato</span></div>
  </div>
  <div class="note">Se molte zone toccano lo stop <strong>ma poi rimbalzano</strong>, lo stop e'
  troppo stretto e taglierebbe il guadagno. Se quasi nessuna rimbalza dopo il tocco, lo stop e'
  equilibrato. Lo stop e' solo registrato: non interrompe la misura del respiro.</div></div>"""


def funnel_card(funnel, zones):
    invalidated = sum(v for (fr, to), v in funnel.items() if to == "RIPOSO" and fr == "OSSERVAZIONE")
    to_observe  = sum(v for (fr, to), v in funnel.items() if to == "OSSERVAZIONE")
    emitted     = len(zones)
    rows = [
        ("Ingressi in Osservazione", to_observe),
        ("\u2192 diventate Entry Zone", emitted),
        ("\u2192 invalidate (tornate a Riposo)", invalidated),
    ]
    body = "".join(f'<tr><td>{l}</td><td class="mono">{n}</td></tr>' for l, n in rows)
    return f"""<div class="card"><div class="ch">Funnel della macchina a stati</div>
  <div class="table-scroll"><table><tbody>{body}</tbody></table></div>
  <div class="note">Quante osservazioni si trasformano davvero in Entry Zone, e quante
  vengono invalidate. Un funnel sano non emette su ogni osservazione.</div></div>"""


# ============================================================
# Generate
# ============================================================

def generate():
    os.makedirs("docs", exist_ok=True)
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    try:
        conn = sqlite3.connect(DB_PATH)
        zones = load_zones(conn)
        funnel = load_transition_funnel(conn)
        conn.close()
    except Exception as e:
        zones, funnel = [], {}
        print(f"Radar Lab: errore lettura DB \u2014 {e}")

    if not zones:
        body = """<div class="card"><div class="empty">
        Il Market Radar non ha ancora emesso Entry Zone.<br>
        La pagina si popoleranno quando il radar iniziera' a registrare configurazioni.<br>
        <span class="prov">Modalita' sola-osservazione \u00b7 in attesa dei primi dati</span>
        </div></div>"""
        counts = "0 zone"
    else:
        s = summarize(zones)
        body = (summary_block(s)
                + asset_table(by_asset(zones))
                + velocity_table(velocity_buckets(zones))
                + first_hit_card(zones)
                + gestione_card(zones)
                + stop_card(zones)
                + funnel_card(funnel, zones))
        counts = f"{s['total']} zone ({s['closed']} chiuse)"

    html = f"""<!DOCTYPE html>
<html lang="it"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Radar Lab (beta)</title><style>{CSS}</style></head>
<body>
<header>
  <h1>Radar Lab<span class="beta">BETA</span></h1>
  <div class="meta">{generated} &nbsp;|&nbsp; <a href="engine_edge_dashboard.html">&larr; Engine Edge Lab</a></div>
</header>
<div class="container">
  <p class="intro">
    Validazione del <strong>Market Radar</strong> in sola osservazione. Il radar non compra
    e non vende: segnala \u00abzone da osservare\u00bb dopo un impulso esteso che perde forza.
    Qui misuriamo <strong>cosa fa il prezzo dopo</strong> ogni zona \u2014 quanto rimbalza (MFE) e
    quanto soffre prima (MAE), in unita' di ATR. <strong>Nessuna soglia di successo e' imposta:</strong>
    i dati grezzi mostrano se e quanto esiste un edge. Le conclusioni arrivano dopo 300\u2013500 zone.
  </p>
  {body}
</div>
</body></html>"""

    with open(OUT_PATH, "w") as f:
        f.write(html)
    print(f"Radar Lab dashboard generata: {OUT_PATH} ({counts})")


if __name__ == "__main__":
    generate()
