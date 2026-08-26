"""
generate_analytics_dashboard.py
Crypto Signal Engine — Analytics Lab (unificato)

Struttura:
    SEZIONE 0 — TT (nuova strategia: Direction/Location/Liquidity)
    SEZIONE 1 — Institutional Edge Lab / OTE-SC
    SEZIONE 2 — NMC Trend Rider Balanced v1.0
    SEZIONE 3 — Liquidity Hunter v1.0
    SEZIONE 4 — V4.1 Phase 1 Money Flow (benchmark storico)

Genera docs/analytics_dashboard.html
"""

import sqlite3
import json
import os
from datetime import datetime, timezone

DB_PATH  = os.environ.get("DB_PATH", "data/signals.db")

# LH: dashboard mostra solo segnali DA QUESTA DATA in poi -- non cancella
# lo storico (resta nel DB per confronto), filtra solo la vista. I dati
# prima di questa data includono i bug di target/quality_label corretti
# il 24/08 e mischierebbero performance pre/post-fix in modo fuorviante.
# Aggiornare questa data quando si vuole un nuovo "azzeramento" della vista.
LH_EPOCH_DATE = "2026-08-24T18:00:00"
# TT_EPOCH_DATE: azzeramento vista richiesto il 26/08 -- i segnali TT
# precedenti usano ancora POI Demand/Supply (concetto sostituito da
# HL/LH nella riscrittura del 25/08) e mescolano risultati pre/post
# riscrittura in modo fuorviante. Aggiornare quando si vuole un nuovo
# azzeramento.
TT_EPOCH_DATE = "2026-08-27T00:00:00"
OUT_PATH = "docs/analytics_dashboard.html"


def q(conn, sql, params=()):
    return conn.execute(sql, params).fetchall()


# ============================================================
# Data loaders — TT (nuovo, aggiunto in cima -- OTE-SC sotto e'
# rimasto INVARIATO, nessuna riga toccata)
# ============================================================

def load_tt_signals(conn):
    """
    Segnali TT con esito DECISO (TP/SL/EXPIRED) -- per le statistiche
    standard (win rate/expectancy). INVALIDATED e' escluso di proposito:
    la spec di TT lo distingue esplicitamente da una loss (sezione 26,
    "non deve essere considerato automaticamente una LOSS") -- mischiarlo
    qui abbasserebbe il win rate in modo scorretto. Ha il suo box
    separato (vedi load_tt_invalidated_count).
    """
    try:
        rows = q(conn, """
            SELECT asset, direction, poi_type, pd_zone, ctx_15m_structure,
                   quality_label, quality_score, planned_tp_type,
                   status, mae, mfe, planned_rr, actual_rr, bars_open,
                   signal_created_at
            FROM tt_signals WHERE status IN ('TP','SL','EXPIRED') AND signal_created_at > ?
            ORDER BY signal_created_at DESC
        """, (TT_EPOCH_DATE,))
    except sqlite3.OperationalError:
        return []
    result = []
    for r in rows:
        rr = r[12] if r[12] is not None else r[11]  # actual_rr se presente, altrimenti planned_rr
        result.append({
            "asset": r[0], "direction": r[1],
            "poi_type": r[2] or "N/A", "pd_zone": r[3] or "N/A",
            "ctx_15m": r[4] or "N/A",
            "quality_label": r[5] or "N/A", "quality_score": r[6] or 0,
            "tp_type": r[7] or "N/A",
            "outcome": r[8],
            "mae": float(r[9] or 0), "mfe": float(r[10] or 0),
            "rr": float(rr or 0), "bars_open": int(r[13] or 0),
            "ts": r[14] or "",
        })
    return result

def load_tt_invalidated_count(conn):
    try:
        row = q(conn, "SELECT COUNT(*) FROM tt_signals WHERE status='INVALIDATED' AND signal_created_at > ?", (TT_EPOCH_DATE,))
        return row[0][0] if row else 0
    except sqlite3.OperationalError:
        return 0

def load_tt_recent(conn, limit=20):
    """
    Righe recenti -- TUTTI gli stati (anche SETUP/ENTRY),
    non solo quelli chiusi: da' visibilita' sui setup ancora in corso,
    coerente con la filosofia "Early Signal prima del touch" di TT.
    """
    try:
        return q(conn, f"""
            SELECT signal_id, asset, direction, planned_entry, planned_sl, planned_tp,
                   planned_rr, quality_score, quality_label, poi_type, pd_zone,
                   status, actual_entry, actual_sl, actual_tp, signal_created_at
            FROM tt_signals WHERE signal_created_at > ? ORDER BY signal_created_at DESC LIMIT {limit}
        """, (TT_EPOCH_DATE,))
    except sqlite3.OperationalError:
        return []


# ============================================================
# Data loaders — OTE (nuovo, sostituisce Edge Lab OTE-SC)
# ============================================================

def load_ote_signals(conn):
    """Segnali OTE con esito deciso (TP/SL/EXPIRED)."""
    try:
        rows = q(conn, """
            SELECT asset, direction, zone_ref, zone_score, zone_strength,
                   quality_label, quality_score, tp_type,
                   status, mae, mfe, planned_rr, actual_rr, bars_open,
                   signal_created_at, trigger_type
            FROM ote_signals WHERE status IN ('TP','SL','EXPIRED')
            ORDER BY signal_created_at DESC
        """)
    except sqlite3.OperationalError:
        return []
    result = []
    for r in rows:
        rr = r[12] if r[12] is not None else r[11]
        result.append({
            "asset": r[0], "direction": r[1],
            "zone_ref": r[2] or "N/A", "zone_score": r[3] or 0,
            "zone_strength": r[4] or "N/A",
            "quality_label": r[5] or "N/A", "quality_score": r[6] or 0,
            "tp_type": r[7] or "N/A",
            "outcome": r[8],
            "mae": float(r[9] or 0), "mfe": float(r[10] or 0),
            "rr": float(rr or 0), "bars_open": int(r[13] or 0),
            "ts": r[14] or "", "trigger_type": r[15] or "N/A",
        })
    return result

def load_ote_candidates_stats(conn):
    """Statistiche sui candidate (neutri) per visibilita'."""
    try:
        rows = q(conn, """
            SELECT status, COUNT(*) FROM ote_candidates GROUP BY status
        """)
        d = {r[0]: r[1] for r in rows}
        return {
            "watching": d.get("WATCHING", 0) + d.get("TOUCHED", 0),
            "expired": d.get("EXPIRED", 0),
            "signal_created": d.get("SIGNAL_CREATED", 0),
            "total": sum(d.values()),
        }
    except sqlite3.OperationalError:
        return {"watching": 0, "expired": 0, "signal_created": 0, "total": 0}

def load_ote_recent(conn, limit=20):
    """Ultimi segnali OTE (tutti gli stati)."""
    try:
        return q(conn, f"""
            SELECT signal_id, asset, direction, planned_entry, planned_sl, planned_tp,
                   planned_rr, quality_score, quality_label, zone_strength, trigger_type,
                   tp_type, status, signal_created_at
            FROM ote_signals ORDER BY signal_created_at DESC LIMIT {limit}
        """)
    except sqlite3.OperationalError:
        return []

# ============================================================
# Data loaders — TRB, LH, V4.1 (INVARIATI)
# ============================================================

def load_trb_signals(conn):
    try:
        rows = q(conn, """
            SELECT asset, direction, session, trend_h1, trend_h4, adx,
                   quality_label, quality_score, liquidity_target,
                   final_outcome, mae, mfe, rr1, rr2, bars_open,
                   new_24h_extreme, timestamp_setup
            FROM trb_signals WHERE final_outcome NOT IN ('OPEN')
            ORDER BY timestamp_setup DESC
        """)
    except sqlite3.OperationalError:
        return []
    return [{
        "asset": r[0], "direction": r[1], "session": r[2] or "N/A",
        "trend_h1": r[3] or "N/A", "trend_h4": r[4] or "N/A",
        "adx": float(r[5] or 0), "quality_label": r[6] or "N/A",
        "quality_score": r[7] or 0, "liq_target": r[8] or "N/A",
        "outcome": r[9], "mae": float(r[10] or 0), "mfe": float(r[11] or 0),
        "rr1": float(r[12] or 0), "rr2": float(r[13] or 0),
        "bars_open": int(r[14] or 0), "new_extreme": bool(r[15]), "ts": r[16] or "",
    } for r in rows]

def load_trb_recent(conn, limit=20):
    try:
        return q(conn, f"""
            SELECT signal_id, asset, direction, entry, stop_loss, tp1, tp2,
                   quality_score, quality_label, adx, trend_h1, trend_h4,
                   liquidity_target, final_outcome, mae, mfe, bars_open, timestamp_setup
            FROM trb_signals ORDER BY timestamp_setup DESC LIMIT {limit}
        """)
    except sqlite3.OperationalError:
        return []

def load_lh_signals(conn):
    try:
        rows = q(conn, """
            SELECT asset, direction,
                   swept_level_label, swept_level_priority, swept_level_touches,
                   sweep_direction, trigger_type,
                   quality_label, quality_score,
                   tp_label, tp_priority,
                   final_outcome, mae, mfe, rr, bars_open, timestamp_setup
            FROM lh_signals WHERE final_outcome != 'OPEN' AND timestamp_setup > ?
            ORDER BY timestamp_setup DESC
        """, (LH_EPOCH_DATE,))
    except sqlite3.OperationalError:
        return []
    return [{
        "asset": r[0], "direction": r[1],
        "level": r[2] or "N/A", "level_priority": r[3] or "N/A",
        "level_touches": r[4] or 0,
        "sweep": r[5] or "N/A", "trigger": r[6] or "N/A",
        "quality_label": r[7] or "N/A", "quality_score": r[8] or 0,
        "tp_label": r[9] or "N/A", "tp_priority": r[10] or "N/A",
        "outcome": r[11],
        "mae": float(r[12] or 0), "mfe": float(r[13] or 0),
        "rr": float(r[14] or 0), "bars_open": int(r[15] or 0),
        "ts": r[16] or "",
    } for r in rows]

def load_lh_recent(conn, limit=20):
    try:
        return q(conn, f"""
            SELECT signal_id, asset, direction, entry, stop_loss, tp, rr,
                   quality_score, quality_label,
                   swept_level_label, swept_level_priority,
                   sweep_direction, trigger_type, tp_label,
                   final_outcome, mae, mfe, bars_open, timestamp_setup,
                   sl_original
            FROM lh_signals WHERE timestamp_setup > ?
            ORDER BY timestamp_setup DESC LIMIT {limit}
        """, (LH_EPOCH_DATE,))
    except sqlite3.OperationalError:
        return []

def load_v41p1_signals(conn):
    try:
        rows = q(conn, """
            SELECT asset, session, final_outcome, mae, mfe, tp1_hit, tp2_hit,
                   trigger_types, quality_label, expected_move_points,
                   liquidity_target, timestamp_setup
            FROM v41p1_signals WHERE final_outcome != 'OPEN'
            ORDER BY timestamp_setup DESC
        """)
    except sqlite3.OperationalError:
        return []
    result = []
    for r in rows:
        try: types = json.loads(r[7]) if r[7] else []
        except: types = []
        trigger = "BOS+CHOCH" if ("BOS" in types and "CHOCH" in types) \
            else ("BOS" if "BOS" in types else ("CHOCH" if "CHOCH" in types else "OTHER"))
        result.append({
            "asset": r[0], "session": r[1], "outcome": r[2],
            "mae": r[3] or 0, "mfe": r[4] or 0,
            "tp1_hit": bool(r[5]), "tp2_hit": bool(r[6]),
            "trigger": trigger, "quality": r[8],
            "em": r[9], "liquidity_target": r[10] or "N/A", "ts": r[11],
        })
    return result


# ============================================================
# Stats — INVARIATE
# ============================================================

def stats_el(rows):
    n = len(rows)
    if n == 0: return {"n":0,"win":0,"sl":0,"exp_r":0,"avg_mae":0,"avg_mfe":0,"avg_rr":0,"avg_bars":0}
    wins = sum(1 for r in rows if r["outcome"] == "TP")
    sls  = sum(1 for r in rows if r["outcome"] == "SL")
    return {"n":n,"win":round(wins/n*100,1),"sl":round(sls/n*100,1),
            "exp_r":round((wins*2-sls)/n,2),
            "avg_mae":round(sum(r["mae"] for r in rows)/n,1),
            "avg_mfe":round(sum(r["mfe"] for r in rows)/n,1),
            "avg_rr":round(sum(r["rr"] for r in rows)/n,2),
            "avg_bars":round(sum(r["bars_open"] for r in rows)/n,1)}

def stats_trb(rows):
    n = len(rows)
    if n == 0: return {"n":0,"win":0,"tp2":0,"sl":0,"exp_r":0,"avg_mae":0,"avg_mfe":0,"avg_adx":0}
    wins = sum(1 for r in rows if r["outcome"] in ("TP1_HIT","TP2_HIT"))
    tp2  = sum(1 for r in rows if r["outcome"] == "TP2_HIT")
    sls  = sum(1 for r in rows if r["outcome"] == "SL_HIT")
    adxs = [r["adx"] for r in rows if r["adx"] > 0]
    return {"n":n,"win":round(wins/n*100,1),"tp2":round(tp2/n*100,1),
            "sl":round(sls/n*100,1),"exp_r":round((wins*2-sls)/n,2),
            "avg_mae":round(sum(r["mae"] for r in rows)/n,1),
            "avg_mfe":round(sum(r["mfe"] for r in rows)/n,1),
            "avg_adx":round(sum(adxs)/len(adxs),1) if adxs else 0}

def stats_lh(rows):
    n = len(rows)
    if n == 0: return {"n":0,"win":0,"sl":0,"exp_r":0,"avg_mae":0,"avg_mfe":0,"avg_rr":0}
    wins = sum(1 for r in rows if r["outcome"] == "TP")
    sls  = sum(1 for r in rows if r["outcome"] == "SL")
    return {"n":n,"win":round(wins/n*100,1),"sl":round(sls/n*100,1),
            "exp_r":round((wins*2-sls)/n,2),
            "avg_mae":round(sum(r["mae"] for r in rows)/n,1),
            "avg_mfe":round(sum(r["mfe"] for r in rows)/n,1),
            "avg_rr":round(sum(r["rr"] for r in rows)/n,2)}

def stats_v41(rows):
    n = len(rows)
    if n == 0: return {"n":0,"win":0,"tp1":0,"tp2":0,"sl":0,"exp_r":0,"avg_mae":0,"avg_mfe":0,"avg_em":0}
    wins = sum(1 for r in rows if r["outcome"] == "TP")
    sls  = sum(1 for r in rows if r["outcome"] == "SL")
    tp1  = sum(1 for r in rows if r["tp1_hit"])
    tp2  = sum(1 for r in rows if r["tp2_hit"])
    ems  = [r["em"] for r in rows if r["em"] is not None]
    return {"n":n,"win":round(wins/n*100,1),"tp1":round(tp1/n*100,1),
            "tp2":round(tp2/n*100,1),"sl":round(sls/n*100,1),
            "exp_r":round((wins*2-sls)/n,2),
            "avg_mae":round(sum(r["mae"] for r in rows)/n,1),
            "avg_mfe":round(sum(r["mfe"] for r in rows)/n,1),
            "avg_em":round(sum(ems)/len(ems),1) if ems else 0}

def breakdown(rows, key_fn, keys, stat_fn):
    return {k: stat_fn([r for r in rows if key_fn(r) == k]) for k in keys}


def asset_keys_from(rows, preferred=("BTC_USDT", "XAU_USD", "PAXG_USDT")):
    present = {r["asset"] for r in rows if r.get("asset")}
    ordered = [a for a in preferred if a in present]
    ordered += sorted(a for a in present if a not in preferred)
    return ordered


# ============================================================
# CSS — INVARIATO salvo l'aggiunta della classe .b-invalid (nuova,
# additiva, non tocca nessuna regola esistente)
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
.fw-header{padding:14px 20px;font-family:'IBM Plex Mono',monospace;font-size:13px;font-weight:600;letter-spacing:.1em;text-transform:uppercase;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:12px}
.fw-tag{font-size:10px;padding:2px 8px;border-radius:4px;font-weight:600}
.tag-active{background:rgba(79,255,176,.15);color:var(--buy)}
.tag-active-purple{background:rgba(167,139,250,.15);color:var(--accent4)}
.tag-active-blue{background:rgba(56,189,248,.15);color:var(--accent5)}
.tag-benchmark{background:rgba(90,100,120,.2);color:var(--dim)}
.summary-grid{display:grid;gap:1px;background:var(--border)}
.summary-grid.cols8{grid-template-columns:repeat(8,1fr)}
.summary-grid.cols7{grid-template-columns:repeat(7,1fr)}
.summary-grid.cols6{grid-template-columns:repeat(6,1fr)}
.summary-grid.cols5{grid-template-columns:repeat(5,1fr)}
.summary-grid>div{background:var(--surface);padding:14px 8px;text-align:center}
.big{font-family:'IBM Plex Mono',monospace;font-size:18px;font-weight:600}
.big.pos{color:var(--buy)} .big.neg{color:var(--sell)} .big.warn{color:var(--accent3)}
.lbl{font-size:9px;text-transform:uppercase;letter-spacing:.06em;color:var(--dim);display:block;margin-top:3px}
.card{background:var(--surface);border:1px solid var(--border);border-radius:6px;overflow:hidden;margin-bottom:16px}
.ch{padding:10px 16px;border-bottom:1px solid var(--border);font-family:'IBM Plex Mono',monospace;font-size:11px;font-weight:600;letter-spacing:.1em;text-transform:uppercase;color:var(--dim)}
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
.b-premium{background:rgba(167,139,250,.15);color:var(--accent4)}
.b-invalid{background:rgba(90,100,120,.15);color:var(--dim);font-style:italic}
.empty{text-align:center;padding:24px;color:var(--dim);font-size:13px}
@media(max-width:900px){.grid-2{grid-template-columns:1fr}.summary-grid.cols8,.summary-grid.cols7,.summary-grid.cols6{grid-template-columns:repeat(3,1fr)}.container{padding:12px}.card table{min-width:600px}}
"""


# ============================================================
# Helpers — outcome_badge esteso (additivo: solo nuove chiavi nel
# dizionario, quelle vecchie invariate) per gli stati TT
# ============================================================

def _empty_row(cols):
    return f'<tr><td colspan="{cols}" class="empty">Nessun dato</td></tr>'

def outcome_badge(o):
    cls = {"TP":"b-tp","SL":"b-sl","EXPIRED":"b-exp","OPEN":"b-open",
           "TP1_HIT":"b-tp","TP2_HIT":"b-tp","SL_HIT":"b-sl",
           "SETUP":"b-open","ENTRY":"b-open",
           "INVALIDATED":"b-invalid"}.get(o,"b-exp")
    return f'<span class="badge {cls}">{o}</span>'

def direction_badge(d):
    return f'<span class="badge {"b-buy" if d=="BUY" else "b-sell"}">{d}</span>'

def fmt_ts(ts):
    if not ts: return "—"
    try:
        dt = datetime.fromisoformat(ts.replace("Z","+00:00"))
        return dt.strftime("%d %b %H:%M")
    except: return ts[:16]

def fmt_p(v):
    if v is None: return "—"
    v = float(v)
    return f"{v:,.2f}" if v > 1000 else f"{v:.4f}"

def perf_table(title, d, keys, key_label, cols, stat_fn_empty):
    body = ""
    for k in keys:
        v = d.get(k, stat_fn_empty([]))
        if v["n"] == 0: continue
        wc = "pos" if v["win"]>=40 else ("neg" if v["win"]<25 else "warn")
        ec = "pos" if v["exp_r"]>0 else "neg"
        body += f"""<tr>
  <td><strong>{k}</strong></td>
  <td class="mono">{v['n']}</td>
  <td class="mono {wc}">{v['win']}%</td>
  <td class="mono {ec}">{v['exp_r']:+.2f}R</td>
  <td class="mono neg">{v['avg_mae']:.1f}</td>
  <td class="mono pos">{v['avg_mfe']:.1f}</td>
</tr>"""
    if not body: body = _empty_row(6)
    return f"""<div class="card">
  <div class="ch">{title}</div>
  <table><thead><tr>
    <th>{key_label}</th><th>N</th><th>Win%</th><th>Expectancy</th>
    <th>Avg MAE</th><th>Avg MFE</th>
  </tr></thead><tbody>{body}</tbody></table>
</div>"""


# ============================================================
# SEZIONE 0 — TT (NUOVA, aggiunta in cima)
# ============================================================

def section_tt(rows, recent, invalidated_count):
    s = stats_el(rows)
    wc = "pos" if s["win"]>=40 else ("neg" if s["win"]<25 else "warn")
    ec = "pos" if s["exp_r"]>0 else "neg"

    summary = f"""<div class="summary-grid cols8" style="border:1px solid var(--border);border-radius:6px;overflow:hidden;margin-bottom:16px">
  <div><span class="big">{s['n']}</span><span class="lbl">Chiusi (TP/SL/EXP)</span></div>
  <div><span class="big warn">{invalidated_count}</span><span class="lbl">Invalidated</span></div>
  <div><span class="big {wc}">{s['win']}%</span><span class="lbl">Win Rate</span></div>
  <div><span class="big neg">{s['sl']}%</span><span class="lbl">SL Rate</span></div>
  <div><span class="big {ec}">{s['exp_r']:+.2f}R</span><span class="lbl">Expectancy</span></div>
  <div><span class="big">{s['avg_rr']:.2f}</span><span class="lbl">Avg R/R</span></div>
  <div><span class="big neg">{s['avg_mae']:.1f}</span><span class="lbl">Avg MAE</span></div>
  <div><span class="big pos">{s['avg_mfe']:.1f}</span><span class="lbl">Avg MFE</span></div>
</div>"""

    no_data = "" if rows else '<div class="card"><div class="empty">In attesa del primo segnale TT chiuso.</div></div>'

    asset_keys = asset_keys_from(rows)
    dir_keys   = ["BUY","SELL"]
    poi_keys   = ["DEMAND","SUPPLY"]
    pd_keys    = ["DISCOUNT","EQUILIBRIUM","PREMIUM"]
    ql_keys    = ["HIGH","MEDIUM","LOW"]

    bd_asset = breakdown(rows, lambda r: r["asset"],         asset_keys, stats_el)
    bd_dir   = breakdown(rows, lambda r: r["direction"],     dir_keys,   stats_el)
    bd_poi   = breakdown(rows, lambda r: r["poi_type"],      poi_keys,   stats_el)
    bd_pd    = breakdown(rows, lambda r: r["pd_zone"],       pd_keys,    stats_el)
    bd_ql    = breakdown(rows, lambda r: r["quality_label"], ql_keys,    stats_el)

    if not recent:
        rec_html = '<div class="card"><div class="empty">Nessun segnale ancora.</div></div>'
    else:
        body = ""
        for r in recent:
            (sid, asset, direction, planned_entry, planned_sl, planned_tp, planned_rr,
             qs, ql, poi_type, pd_zone, status, actual_entry, actual_sl, actual_tp, ts) = r
            entry_show = actual_entry if actual_entry is not None else planned_entry
            sl_show    = actual_sl if actual_sl is not None else planned_sl
            tp_show    = actual_tp if actual_tp is not None else planned_tp
            body += f"""<tr>
  <td class="mono" style="color:var(--dim);font-size:11px">{fmt_ts(ts)}</td>
  <td><strong>{asset.replace('_USDT','')}</strong></td>
  <td>{direction_badge(direction)}</td>
  <td>{outcome_badge(status)}</td>
  <td class="mono">{fmt_p(entry_show)}</td>
  <td class="mono">{fmt_p(sl_show)}</td>
  <td class="mono">{fmt_p(tp_show)}</td>
  <td class="mono">{float(planned_rr or 0):.2f}</td>
  <td style="font-size:12px;color:var(--dim)">{poi_type or '—'}</td>
  <td style="font-size:12px;color:var(--dim)">{pd_zone or '—'}</td>
  <td style="font-size:12px;color:var(--dim)">{ql or '—'}</td>
</tr>"""
        rec_html = f"""<div class="card"><div class="ch">Segnali Recenti TT</div>
  <div style="overflow-x:auto;-webkit-overflow-scrolling:touch">
  <table><thead><tr>
    <th>Data</th><th>Asset</th><th>Dir</th><th>Stato</th><th>Entry</th><th>SL</th><th>TP</th>
    <th>R/R</th><th>POI</th><th>PD</th><th>Quality</th>
  </tr></thead><tbody>{body}</tbody></table>
  </div></div>"""

    return f"""
<div class="card" style="border-top:2px solid var(--accent)">
  <div class="fw-header" style="color:var(--accent)">
    ⚡ TT — Direction · Location · Liquidity
    <span class="fw-tag tag-active">ATTIVO</span>
    <span style="color:var(--dim);font-size:11px;margin-left:auto">4H→1H→15M→5M · BTC · XAU</span>
  </div>
  {summary}{no_data}
  <div class="grid-2">
    {perf_table("Per Asset", bd_asset, asset_keys, "Asset", 6, stats_el)}
    {perf_table("Per Direzione", bd_dir, dir_keys, "Dir", 6, stats_el)}
  </div>
  <div class="grid-2">
    {perf_table("Per Tipo POI", bd_poi, poi_keys, "POI", 6, stats_el)}
    {perf_table("Per Premium/Discount", bd_pd, pd_keys, "Zona", 6, stats_el)}
  </div>
  {perf_table("Per Quality", bd_ql, ql_keys, "Quality", 6, stats_el)}
  {rec_html}
</div>"""


# ============================================================
# SEZIONE 1 — OTE "Zona prima, direzione dopo" (sostituisce Edge Lab OTE-SC)
# ============================================================

def section_ote(rows, recent, cand_stats):
    s = stats_el(rows)
    wc = "pos" if s["win"]>=40 else ("neg" if s["win"]<25 else "warn")
    ec = "pos" if s["exp_r"]>0 else "neg"

    summary = f"""<div class="summary-grid cols8" style="border:1px solid var(--border);border-radius:6px;overflow:hidden;margin-bottom:16px">
  <div><span class="big">{s['n']}</span><span class="lbl">Chiusi (TP/SL/EXP)</span></div>
  <div><span class="big warn">{cand_stats['total']}</span><span class="lbl">Candidate totali</span></div>
  <div><span class="big {wc}">{s['win']}%</span><span class="lbl">Win Rate</span></div>
  <div><span class="big neg">{s['sl']}%</span><span class="lbl">SL Rate</span></div>
  <div><span class="big {ec}">{s['exp_r']:+.2f}R</span><span class="lbl">Expectancy</span></div>
  <div><span class="big">{s['avg_rr']:.2f}</span><span class="lbl">Avg R/R</span></div>
  <div><span class="big neg">{s['avg_mae']:.1f}</span><span class="lbl">Avg MAE</span></div>
  <div><span class="big pos">{s['avg_mfe']:.1f}</span><span class="lbl">Avg MFE</span></div>
</div>"""

    no_data = "" if rows else '<div class="card"><div class="empty">In attesa del primo segnale OTE chiuso. Candidate attivi: ' + str(cand_stats["watching"]) + '</div></div>'

    asset_keys = asset_keys_from(rows)
    dir_keys   = ["BUY","SELL"]
    str_keys   = ["STRONG","MODERATE","WEAK"]
    ql_keys    = ["HIGH","MEDIUM","LOW"]

    bd_asset = breakdown(rows, lambda r: r["asset"],         asset_keys, stats_el)
    bd_dir   = breakdown(rows, lambda r: r["direction"],     dir_keys,   stats_el)
    bd_str   = breakdown(rows, lambda r: r["zone_strength"], str_keys,   stats_el)
    bd_ql    = breakdown(rows, lambda r: r["quality_label"], ql_keys,    stats_el)

    if not recent:
        rec_html = '<div class="card"><div class="empty">Nessun segnale ancora.</div></div>'
    else:
        body = ""
        for r in recent:
            (sid, asset, direction, entry, sl, tp, rr, qs, ql,
             zone_str, trigger, tp_type, status, ts) = r
            body += f"""<tr>
  <td class="mono" style="color:var(--dim);font-size:11px">{fmt_ts(ts)}</td>
  <td><strong>{asset.replace('_USDT','')}</strong></td>
  <td>{direction_badge(direction)}</td>
  <td>{outcome_badge(status)}</td>
  <td class="mono">{fmt_p(entry)}</td>
  <td class="mono">{fmt_p(sl)}</td>
  <td class="mono">{fmt_p(tp)}</td>
  <td class="mono">{float(rr or 0):.2f}</td>
  <td style="font-size:12px;color:var(--dim)">{zone_str or '—'}</td>
  <td style="font-size:12px;color:var(--dim)">{trigger or '—'}</td>
</tr>"""
        rec_html = f"""<div class="card"><div class="ch">Segnali Recenti OTE</div>
  <div style="overflow-x:auto;-webkit-overflow-scrolling:touch">
  <table><thead><tr>
    <th>Data</th><th>Asset</th><th>Dir</th><th>Stato</th><th>Entry</th><th>SL</th><th>TP</th>
    <th>R/R</th><th>Zona</th><th>Trigger</th>
  </tr></thead><tbody>{body}</tbody></table>
  </div></div>"""

    return f"""
<div class="card" style="border-top:2px solid var(--accent)">
  <div class="fw-header" style="color:var(--accent)">
    ⚡ OTE — Zona prima, direzione dopo
    <span class="fw-tag tag-active">ATTIVO</span>
    <span style="color:var(--dim);font-size:11px;margin-left:auto">Sweep+Reaction · BTC · XAU</span>
  </div>
  {summary}{no_data}
  <div class="grid-2">
    {perf_table("Per Asset", bd_asset, asset_keys, "Asset", 6, stats_el)}
    {perf_table("Per Direzione", bd_dir, dir_keys, "Dir", 6, stats_el)}
  </div>
  <div class="grid-2">
    {perf_table("Per Zona Strength", bd_str, str_keys, "Strength", 6, stats_el)}
    {perf_table("Per Quality", bd_ql, ql_keys, "Quality", 6, stats_el)}
  </div>
  {rec_html}
</div>"""


# ============================================================
# SEZIONE 2 — TRB — INVARIATA
# ============================================================

def section_trb(rows, recent):
    s = stats_trb(rows)
    wc = "pos" if s["win"]>=40 else ("neg" if s["win"]<25 else "warn")
    ec = "pos" if s["exp_r"]>0 else "neg"

    summary = f"""<div class="summary-grid cols6" style="border:1px solid var(--border);border-radius:6px;overflow:hidden;margin-bottom:16px">
  <div><span class="big">{s['n']}</span><span class="lbl">Chiusi</span></div>
  <div><span class="big {wc}">{s['win']}%</span><span class="lbl">Win Rate</span></div>
  <div><span class="big">{s['tp2']}%</span><span class="lbl">TP2 Hit</span></div>
  <div><span class="big neg">{s['sl']}%</span><span class="lbl">SL Rate</span></div>
  <div><span class="big {ec}">{s['exp_r']:+.2f}R</span><span class="lbl">Expectancy</span></div>
  <div><span class="big">{s['avg_adx']:.1f}</span><span class="lbl">Avg ADX</span></div>
</div>"""

    no_data = "" if rows else '<div class="card"><div class="empty">In attesa del primo segnale TRB.</div></div>'

    asset_keys = asset_keys_from(rows)
    dir_keys   = ["BUY","SELL"]
    sess_keys  = ["ASIA","LONDON","NEW_YORK"]
    ql_keys    = ["PREMIUM","HIGH","MEDIUM"]
    h1_keys    = ["BULLISH","BEARISH"]
    def adx_bucket(r):
        a = r["adx"]
        if a >= 30: return "ADX>30"
        if a >= 25: return "ADX 25-30"
        return "ADX 20-25"

    bd_asset = breakdown(rows, lambda r: r["asset"],         asset_keys, stats_trb)
    bd_dir   = breakdown(rows, lambda r: r["direction"],     dir_keys,   stats_trb)
    bd_sess  = breakdown(rows, lambda r: r["session"],       sess_keys,  stats_trb)
    bd_ql    = breakdown(rows, lambda r: r["quality_label"], ql_keys,    stats_trb)
    bd_h1    = breakdown(rows, lambda r: r["trend_h1"],      h1_keys,    stats_trb)
    bd_adx   = breakdown(rows, adx_bucket, ["ADX>30","ADX 25-30","ADX 20-25"], stats_trb)

    if not recent:
        rec_html = '<div class="card"><div class="empty">Nessun segnale TRB ancora.</div></div>'
    else:
        body = ""
        for r in recent:
            sid,asset,direction,entry,sl,tp1,tp2,qs,ql,adx,h1,h4,target,outcome,mae,mfe,bars,ts = r
            oc = {"TP1_HIT":"b-tp","TP2_HIT":"b-tp","SL_HIT":"b-sl","EXPIRED":"b-exp","OPEN":"b-open"}.get(outcome,"b-exp")
            body += f"""<tr>
  <td class="mono" style="color:var(--dim);font-size:11px">{fmt_ts(ts)}</td>
  <td><strong>{asset.replace('_USDT','')}</strong></td>
  <td>{direction_badge(direction)}</td>
  <td class="mono">{fmt_p(entry)}</td>
  <td class="mono">{fmt_p(sl)}</td>
  <td class="mono">{fmt_p(tp1)}</td>
  <td class="mono" style="color:var(--dim)">{float(adx or 0):.1f}</td>
  <td style="font-size:12px;color:var(--dim)">{h1 or '—'}</td>
  <td style="font-size:12px;color:var(--dim)">{target or '—'}</td>
  <td><span class="badge {oc}">{outcome}</span></td>
</tr>"""
        rec_html = f"""<div class="card"><div class="ch">Segnali Recenti TRB</div>
  <div style="overflow-x:auto;-webkit-overflow-scrolling:touch">
  <table><thead><tr>
    <th>Data</th><th>Asset</th><th>Dir</th><th>Entry</th><th>SL</th><th>TP1</th>
    <th>ADX</th><th>H1</th><th>Target</th><th>Esito</th>
  </tr></thead><tbody>{body}</tbody></table>
  </div></div>"""

    return f"""
<div class="card" style="border-top:2px solid var(--accent4)">
  <div class="fw-header" style="color:var(--accent4)">
    🎯 NMC Trend Rider Balanced v1.0
    <span class="fw-tag tag-active-purple">ATTIVO</span>
    <span style="color:var(--dim);font-size:11px;margin-left:auto">BTC · PAXG</span>
  </div>
  {summary}{no_data}
  <div class="grid-2">
    {perf_table("Per Asset", bd_asset, asset_keys, "Asset", 6, stats_trb)}
    {perf_table("Per Direzione", bd_dir, dir_keys, "Dir", 6, stats_trb)}
  </div>
  <div class="grid-2">
    {perf_table("Per Quality", bd_ql, ql_keys, "Quality", 6, stats_trb)}
    {perf_table("Per Sessione", bd_sess, sess_keys, "Sessione", 6, stats_trb)}
  </div>
  <div class="grid-2">
    {perf_table("Per Trend H1", bd_h1, h1_keys, "Trend H1", 6, stats_trb)}
    {perf_table("Per ADX Bucket", bd_adx, ["ADX>30","ADX 25-30","ADX 20-25"], "ADX", 6, stats_trb)}
  </div>
  {rec_html}
</div>"""


# ============================================================
# SEZIONE 3 — Liquidity Hunter — INVARIATA
# ============================================================

def section_lh(rows, recent):
    s = stats_lh(rows)
    wc = "pos" if s["win"]>=40 else ("neg" if s["win"]<25 else "warn")
    ec = "pos" if s["exp_r"]>0 else "neg"

    summary = f"""<div class="summary-grid cols7" style="border:1px solid var(--border);border-radius:6px;overflow:hidden;margin-bottom:16px">
  <div><span class="big">{s['n']}</span><span class="lbl">Chiusi</span></div>
  <div><span class="big {wc}">{s['win']}%</span><span class="lbl">Win Rate</span></div>
  <div><span class="big neg">{s['sl']}%</span><span class="lbl">SL Rate</span></div>
  <div><span class="big {ec}">{s['exp_r']:+.2f}R</span><span class="lbl">Expectancy</span></div>
  <div><span class="big">{s['avg_rr']:.2f}</span><span class="lbl">Avg R/R</span></div>
  <div><span class="big neg">{s['avg_mae']:.1f}</span><span class="lbl">Avg MAE</span></div>
  <div><span class="big pos">{s['avg_mfe']:.1f}</span><span class="lbl">Avg MFE</span></div>
</div>"""

    no_data = "" if rows else '<div class="card"><div class="empty">In attesa del primo segnale Liquidity Hunter.</div></div>'

    asset_keys = asset_keys_from(rows)
    dir_keys     = ["BUY","SELL"]
    trigger_keys = ["OB_TOUCH", "OB_PENDING"]
    priority_keys = ["FRESH", "TESTED", "MITIGATED", "BREAKER"]

    bd_asset    = breakdown(rows, lambda r: r["asset"],          asset_keys,    stats_lh)
    bd_dir      = breakdown(rows, lambda r: r["direction"],      dir_keys,      stats_lh)
    bd_trigger  = breakdown(rows, lambda r: r["trigger"],        trigger_keys,  stats_lh)
    bd_priority = breakdown(rows, lambda r: r["level_priority"], priority_keys, stats_lh)

    if not recent:
        rec_html = '<div class="card"><div class="empty">Nessun segnale Liquidity Hunter ancora.</div></div>'
    else:
        body = ""
        for r in recent:
            sid,asset,direction,entry,sl,tp,rr,qs,ql,level,lvl_pri,sweep,trigger,tp_label,outcome,mae,mfe,bars,ts,sl_orig = r
            sl_display = sl_orig if sl_orig is not None else sl
            oc = {"TP":"b-tp","SL":"b-sl","EXPIRED":"b-exp","OPEN":"b-open"}.get(outcome,"b-exp")
            body += f"""<tr>
  <td class="mono" style="color:var(--dim);font-size:11px">{fmt_ts(ts)}</td>
  <td><strong>{asset.replace('_USDT','')}</strong></td>
  <td>{direction_badge(direction)}</td>
  <td><span class="badge {oc}">{outcome}</span></td>
  <td class="mono">{fmt_p(entry)}</td>
  <td class="mono">{fmt_p(sl_display)}</td>
  <td class="mono">{fmt_p(tp)}</td>
  <td class="mono">{float(rr or 0):.2f}</td>
  <td style="font-size:12px;color:var(--dim)">{level or '—'}</td>
  <td style="font-size:12px;color:var(--dim)">{sweep or '—'}</td>
  <td style="font-size:12px;color:var(--dim)">{trigger or '—'}</td>
</tr>"""
        rec_html = f"""<div class="card"><div class="ch">Segnali Recenti Liquidity Hunter</div>
  <div style="overflow-x:auto;-webkit-overflow-scrolling:touch">
  <table><thead><tr>
    <th>Data</th><th>Asset</th><th>Dir</th><th>Esito</th><th>Entry</th><th>SL</th><th>TP</th>
    <th>R/R</th><th>Livello</th><th>Sweep</th><th>Trigger</th>
  </tr></thead><tbody>{body}</tbody></table>
  </div></div>"""

    return f"""
<div class="card" style="border-top:2px solid var(--accent5)">
  <div class="fw-header" style="color:var(--accent5)">
    🎯 Liquidity Hunter v3.2
    <span class="fw-tag tag-active-blue">ATTIVO</span>
    <span style="color:var(--dim);font-size:11px;margin-left:auto">BTC · PAXG · Proximity 0.30% · Sweep 4 candele</span>
  </div>
  {summary}{no_data}
  <div class="grid-2">
    {perf_table("Per Asset", bd_asset, asset_keys, "Asset", 6, stats_lh)}
    {perf_table("Per Direzione", bd_dir, dir_keys, "Dir", 6, stats_lh)}
  </div>
  <div class="grid-2">
    {perf_table("Per Trigger", bd_trigger, trigger_keys, "Trigger", 6, stats_lh)}
    {perf_table("Per Priorità Livello", bd_priority, priority_keys, "Priorità", 6, stats_lh)}
  </div>
  {rec_html}
</div>"""


# ============================================================
# SEZIONE 4 — V4.1 Phase 1 — INVARIATA
# ============================================================

def section_v41p1(rows):
    s = stats_v41(rows)
    color = "#ffd166"
    wc = "pos" if s["win"]>=30 else "neg"
    ec = "pos" if s["exp_r"]>0 else "neg"

    summary = f"""<div class="summary-grid cols5" style="border:1px solid var(--border);border-top:2px solid {color};border-radius:6px;overflow:hidden;margin-bottom:16px">
  <div><span class="big">{s['n']}</span><span class="lbl">Chiusi</span></div>
  <div><span class="big {wc}">{s['win']}%</span><span class="lbl">Win Rate</span></div>
  <div><span class="big">{s['tp1']}%</span><span class="lbl">TP1 Hit</span></div>
  <div><span class="big {ec}">{s['exp_r']:+.2f}R</span><span class="lbl">Expectancy</span></div>
  <div><span class="big neg">{s['avg_mae']}</span><span class="lbl">MAE medio</span></div>
</div>"""

    bd_trigger = breakdown(rows, lambda r: r["trigger"],                   ["BOS","CHOCH","BOS+CHOCH"], stats_v41)
    v41_asset_keys = asset_keys_from(rows, preferred=("BTC","XAU","PAXG"))
    v41_asset_keys = [a.replace("_USDT","").replace("_USD","") for a in v41_asset_keys]
    _seen = set(); v41_asset_keys = [a for a in v41_asset_keys if not (a in _seen or _seen.add(a))]
    bd_asset   = breakdown(rows, lambda r: r["asset"].replace("_USDT","").replace("_USD",""), v41_asset_keys, stats_v41)
    bd_sess    = breakdown(rows, lambda r: r["session"] or "N/A",          ["ASIA","LONDON","NEW_YORK"], stats_v41)

    def v41_table(title, d, keys, key_label):
        body = ""
        for k in keys:
            v = d.get(k, stats_v41([]))
            if v["n"] == 0: continue
            wc2 = "pos" if v["win"]>=40 else ("neg" if v["win"]<20 else "")
            ec2 = "pos" if v["exp_r"]>0 else "neg"
            hl  = "hl" if v["win"]>=40 and v["n"]>=3 else ""
            body += f"""<tr class="{hl}">
  <td><strong>{k}</strong></td><td class="mono">{v['n']}</td>
  <td class="mono {wc2}">{v['win']}%</td><td class="mono">{v['tp1']}%</td>
  <td class="mono">{v['tp2']}%</td><td class="mono {ec2}">{v['exp_r']:+.2f}R</td>
</tr>"""
        if not body: body = _empty_row(6)
        return f"""<div class="card"><div class="ch">{title}</div>
  <table><thead><tr>
    <th>{key_label}</th><th>N</th><th>Win%</th><th>TP1%</th><th>TP2%</th><th>Expectancy</th>
  </tr></thead><tbody>{body}</tbody></table></div>"""

    return f"""
<div class="card" style="border-top:2px solid {color}">
  <div class="fw-header" style="color:{color}">
    V4.1 Phase 1 — Money Flow
    <span class="fw-tag tag-benchmark">BENCHMARK STORICO</span>
  </div>
  {summary}
  <div class="grid-2">
    {v41_table("Per Trigger", bd_trigger, ["BOS","CHOCH","BOS+CHOCH"], "Trigger")}
    {v41_table("Per Asset",   bd_asset,   v41_asset_keys,              "Asset")}
  </div>
  {v41_table("Per Sessione", bd_sess, ["ASIA","LONDON","NEW_YORK"], "Sessione")}
</div>"""


# ============================================================
# Generate — MODIFICATO SOLO per: caricare i dati TT + inserire
# section_tt(...) IN CIMA, prima di section_edge_lab. Il resto invariato.
# ============================================================

def generate():
    conn = sqlite3.connect(DB_PATH)
    tt_rows        = load_tt_signals(conn)
    tt_recent      = load_tt_recent(conn, 20)
    tt_invalidated = load_tt_invalidated_count(conn)
    ote_rows       = load_ote_signals(conn)
    ote_recent     = load_ote_recent(conn, 20)
    ote_cand_stats = load_ote_candidates_stats(conn)
    trb_rows   = load_trb_signals(conn)
    trb_recent = load_trb_recent(conn, 20)
    lh_rows    = load_lh_signals(conn)
    lh_recent  = load_lh_recent(conn, 20)
    v41p1_rows = load_v41p1_signals(conn)
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
  <div class="meta">{generated} &nbsp;|&nbsp; <a href="unified_dashboard.html">&larr; Dashboard</a> &nbsp;|&nbsp; <a href="engine_edge_dashboard.html">Engine Edge Lab →</a></div>
</header>
<div class="container">

  {section_tt(tt_rows, tt_recent, tt_invalidated)}

  <div class="section-divider"><span style="font-family:'IBM Plex Mono',monospace;font-size:10px;color:var(--dim);letter-spacing:.1em;text-transform:uppercase">Strategie Attive</span></div>

  {section_ote(ote_rows, ote_recent, ote_cand_stats)}

  {section_trb(trb_rows, trb_recent)}

  <div class="section-divider"><span style="font-family:'IBM Plex Mono',monospace;font-size:10px;color:var(--dim);letter-spacing:.1em;text-transform:uppercase">Nuove Strategie</span></div>

  {section_lh(lh_rows, lh_recent)}

  <div class="section-divider"><span style="font-family:'IBM Plex Mono',monospace;font-size:10px;color:var(--dim);letter-spacing:.1em;text-transform:uppercase">Benchmark Storico</span></div>

  {section_v41p1(v41p1_rows)}

</div>
</body>
</html>"""

    os.makedirs("docs", exist_ok=True)
    with open(OUT_PATH, "w") as f:
        f.write(html)

    print(
        f"Analytics dashboard generata: {OUT_PATH} "
        f"(TT:{len(tt_rows)}+{tt_invalidated}inv | OTE:{len(ote_rows)} cand={ote_cand_stats['total']} | TRB:{len(trb_rows)} | LH:{len(lh_rows)} | V41P1:{len(v41p1_rows)})"
    )


if __name__ == "__main__":
    generate()
