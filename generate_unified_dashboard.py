"""
generate_unified_dashboard.py
Crypto Signal Engine — Homepage Operativa

Mostra lo stato in tempo reale di:
    - Edge Lab OTE-SC
    - V4.1 Phase 1 Money Flow (benchmark)
    - NMC Trend Rider Balanced v1.0
    - Liquidity Hunter v1.0

Genera docs/unified_dashboard.html
"""

import sqlite3
import json
import os
from datetime import datetime, timezone, timedelta

DB_PATH  = os.environ.get("DB_PATH", "data/signals.db")

# LH: stessa costante di generate_analytics_dashboard.py -- filtra la
# vista, non cancella lo storico. Aggiornare per un nuovo "azzeramento".
LH_EPOCH_DATE = "2026-08-24T18:00:00"
TT_EPOCH_DATE = "2026-08-27T00:00:00"
OUT_PATH = "docs/unified_dashboard.html"


def q(conn, sql, params=()):
    return conn.execute(sql, params).fetchall()


# ============================================================
# Data loading — TT (nuovo, aggiunto in cima -- OTE-SC sotto e'
# rimasto INVARIATO, nessuna riga toccata)
# ============================================================

def load_tt_open(conn):
    """
    'Aperto' per TT include due stati distinti (a differenza delle
    altre strategie che hanno solo OPEN): SETUP (rinominato da
    WAITING_CONFIRMATION il 25/08 -- stesso significato, Early Signal
    non ancora un trade) ed ENTRY (trade reale confermato). Il campo
    'status' distingue i due nella tabella.
    """
    try:
        rows = q(conn, """
            SELECT signal_id, asset, direction, status,
                   planned_entry, planned_sl, planned_tp, planned_rr,
                   actual_entry, actual_sl, actual_tp,
                   poi_type, pd_zone, quality_label, quality_score,
                   bars_waiting, bars_open, signal_created_at
            FROM tt_signals WHERE status IN ('SETUP','ENTRY') AND signal_created_at > ?
            ORDER BY signal_created_at DESC
        """, (TT_EPOCH_DATE,))
    except sqlite3.OperationalError:
        return []
    now = datetime.now(timezone.utc)
    result = []
    for r in rows:
        (sid, asset, direction, status, p_entry, p_sl, p_tp, p_rr,
         a_entry, a_sl, a_tp, poi_type, pd_zone, ql, qs, bars_waiting, bars_open, ts) = r
        try:
            setup_dt = datetime.fromisoformat(ts)
            if setup_dt.tzinfo is None: setup_dt = setup_dt.replace(tzinfo=timezone.utc)
            elapsed_h = round((now - setup_dt).total_seconds() / 3600, 1)
        except: elapsed_h = 0
        # Mostra l'ACTUAL se l'entry e' avvenuta, altrimenti il PLANNED
        entry = a_entry if a_entry is not None else p_entry
        sl = a_sl if a_sl is not None else p_sl
        tp = a_tp if a_tp is not None else p_tp
        result.append({
            "asset": asset, "direction": direction, "status": status,
            "entry": entry, "sl": sl, "tp": tp, "rr": p_rr,
            "poi_type": poi_type or "N/A", "pd_zone": pd_zone or "N/A",
            "ql": ql, "qs": qs, "bars_waiting": bars_waiting or 0,
            "bars_open": bars_open or 0, "elapsed_h": elapsed_h, "ts": ts,
        })
    return result

def load_tt_stats(conn):
    """
    Win rate/expectancy SOLO su esiti decisi (TP/SL/EXPIRED). INVALIDATED
    escluso di proposito (spec TT: non e' una loss, gonfierebbe/
    sgonfierebbe il win rate in modo scorretto se mischiato).
    """
    try:
        rows = q(conn, "SELECT status, COUNT(*) FROM tt_signals WHERE status IN ('TP','SL','EXPIRED') AND signal_created_at > ? GROUP BY status", (TT_EPOCH_DATE,))
        d = {r[0]: r[1] for r in rows}
        n = sum(d.values()); wins = d.get("TP",0); sls = d.get("SL",0)
        waiting = q(conn, "SELECT COUNT(*) FROM tt_signals WHERE status='SETUP' AND signal_created_at > ?", (TT_EPOCH_DATE,))[0][0]
        entry_open = q(conn, "SELECT COUNT(*) FROM tt_signals WHERE status='ENTRY' AND signal_created_at > ?", (TT_EPOCH_DATE,))[0][0]
        invalidated = q(conn, "SELECT COUNT(*) FROM tt_signals WHERE status='INVALIDATED' AND signal_created_at > ?", (TT_EPOCH_DATE,))[0][0]
        return {"n":n, "open": waiting + entry_open,
                "win":round(wins/n*100,1) if n>0 else 0,
                "exp_r":round((wins*2-sls)/n,2) if n>0 else 0,
                "invalidated": invalidated}
    except sqlite3.OperationalError:
        return {"n":0,"open":0,"win":0,"exp_r":0,"invalidated":0}


# ============================================================
# Data loading — OTE (sostituisce Edge Lab OTE-SC)
# ============================================================


def load_ote_open_unified(conn):
    """Candidate WATCHING/TOUCHED + Signal ENTRY — tutti visibili."""
    try:
        # Candidate attivi (neutri)
        cand_rows = q(conn, """
            SELECT 'CAND' as src, candidate_id, asset, 'NEUTRAL' as direction, status,
                   zone_high, zone_low, zone_score, zone_strength,
                   proximity_points, created_at, NULL, NULL, NULL
            FROM ote_candidates WHERE status IN ('WATCHING','TOUCHED')
            ORDER BY created_at DESC
        """)
        # Signal attivi (direzionali)
        sig_rows = q(conn, """
            SELECT 'SIG' as src, signal_id, asset, direction, status,
                   planned_entry, planned_sl, planned_tp, zone_strength,
                   planned_rr, signal_created_at, mae, mfe, bars_open
            FROM ote_signals WHERE status='ENTRY'
            ORDER BY signal_created_at DESC
        """)
    except sqlite3.OperationalError:
        return []
    now = datetime.now(timezone.utc)
    result = []
    for r in list(cand_rows) + list(sig_rows):
        src = r[0]
        ts = r[10]
        try:
            setup_dt = datetime.fromisoformat(ts)
            if setup_dt.tzinfo is None: setup_dt = setup_dt.replace(tzinfo=timezone.utc)
            elapsed_h = round((now - setup_dt).total_seconds() / 3600, 1)
        except: elapsed_h = 0
        if src == 'CAND':
            result.append({
                "asset": r[2], "direction": "—", "status": r[4],
                "entry": f"{r[5]:.2f}-{r[6]:.2f}" if r[5] else "—",
                "sl": "—", "tp": "—", "rr": "—",
                "zone_strength": r[8] or "—", "elapsed_h": elapsed_h, "ts": ts,
            })
        else:
            result.append({
                "asset": r[2], "direction": r[3], "status": r[4],
                "entry": r[5], "sl": r[6], "tp": r[7],
                "rr": r[9], "zone_strength": r[8] or "—",
                "elapsed_h": elapsed_h, "ts": ts,
            })
    return result

def load_ote_stats_unified(conn):
    try:
        sig_rows = q(conn, "SELECT status, COUNT(*) FROM ote_signals WHERE status IN ('TP','SL','EXPIRED') GROUP BY status")
        d = {r[0]: r[1] for r in sig_rows}
        n = sum(d.values()); wins = d.get("TP",0); sls = d.get("SL",0)
        cand_active = q(conn, "SELECT COUNT(*) FROM ote_candidates WHERE status IN ('WATCHING','TOUCHED')")[0][0]
        sig_active = q(conn, "SELECT COUNT(*) FROM ote_signals WHERE status='ENTRY'")[0][0]
        return {"n":n, "open": cand_active + sig_active,
                "win":round(wins/n*100,1) if n>0 else 0,
                "exp_r":round((wins*2-sls)/n,2) if n>0 else 0}
    except sqlite3.OperationalError:
        return {"n":0,"open":0,"win":0,"exp_r":0}


# ============================================================
# Data loading — V4.1 Phase 1
# ============================================================

def load_v41p1_open(conn):
    try:
        rows = q(conn, """
            SELECT asset, direction, entry, stop_loss, tp1, tp2,
                   quality_label, quality_score, trigger_types,
                   mae, mfe, tp1_hit, liquidity_source, liquidity_target,
                   expected_move_points, timestamp_setup
            FROM v41p1_signals WHERE final_outcome = 'OPEN'
            ORDER BY timestamp_setup DESC
        """)
    except sqlite3.OperationalError:
        return []
    now = datetime.now(timezone.utc)
    result = []
    for r in rows:
        try: types = json.loads(r[8]) if r[8] else []; trigger = "+".join(types) if types else "—"
        except: trigger = "—"
        try:
            setup_dt = datetime.fromisoformat(r[15])
            if setup_dt.tzinfo is None: setup_dt = setup_dt.replace(tzinfo=timezone.utc)
            elapsed_h = round((now - setup_dt).total_seconds() / 3600, 1)
        except: elapsed_h = 0
        result.append({
            "asset":r[0],"direction":r[1],"entry":r[2],"sl":r[3],"tp1":r[4],"tp2":r[5],
            "ql":r[6],"qs":r[7],"trigger":trigger,"mae":r[9],"mfe":r[10],"tp1_hit":bool(r[11]),
            "source":r[12] or "N/A","target":r[13] or "N/A","em":r[14],"elapsed_h":elapsed_h,
        })
    return result

def load_v41p1_stats(conn):
    try:
        n    = q(conn,"SELECT COUNT(*) FROM v41p1_signals WHERE final_outcome!='OPEN'")[0][0]
        wins = q(conn,"SELECT COUNT(*) FROM v41p1_signals WHERE final_outcome='TP'")[0][0]
        sls  = q(conn,"SELECT COUNT(*) FROM v41p1_signals WHERE final_outcome='SL'")[0][0]
        opn  = q(conn,"SELECT COUNT(*) FROM v41p1_signals WHERE final_outcome='OPEN'")[0][0]
        return {"n":n,"open":opn,"win":round(wins/n*100,1) if n>0 else 0,"exp_r":round((wins*2-sls)/n,2) if n>0 else 0}
    except sqlite3.OperationalError:
        return {"n":0,"open":0,"win":0,"exp_r":0}


# ============================================================
# Data loading — TRB
# ============================================================

def load_trb_open(conn):
    try:
        rows = q(conn, """
            SELECT asset, direction, entry, stop_loss, tp1, tp2,
                   quality_label, quality_score, adx,
                   mae, mfe, tp1_hit, liquidity_target,
                   trend_h1, trend_h4, session, timestamp_setup
            FROM trb_signals WHERE final_outcome = 'OPEN'
            ORDER BY timestamp_setup DESC
        """)
    except sqlite3.OperationalError:
        return []
    now = datetime.now(timezone.utc)
    result = []
    for r in rows:
        try:
            setup_dt = datetime.fromisoformat(r[16])
            if setup_dt.tzinfo is None: setup_dt = setup_dt.replace(tzinfo=timezone.utc)
            elapsed_h = round((now - setup_dt).total_seconds() / 3600, 1)
        except: elapsed_h = 0
        result.append({
            "asset":r[0],"direction":r[1],"entry":r[2],"sl":r[3],"tp1":r[4],"tp2":r[5],
            "ql":r[6],"qs":r[7],"adx":r[8],"mae":r[9],"mfe":r[10],"tp1_hit":bool(r[11]),
            "target":r[12] or "N/A","trend_h1":r[13],"trend_h4":r[14],
            "session":r[15],"elapsed_h":elapsed_h,"ts":r[16],
        })
    return result

def load_trb_stats(conn):
    try:
        rows = q(conn,"SELECT final_outcome, COUNT(*) FROM trb_signals WHERE final_outcome!='OPEN' GROUP BY final_outcome")
        d = {r[0]:r[1] for r in rows}; n = sum(d.values())
        wins = d.get("TP2_HIT",0)+d.get("TP1_HIT",0); sls = d.get("SL_HIT",0)
        opn = q(conn,"SELECT COUNT(*) FROM trb_signals WHERE final_outcome='OPEN'")[0][0]
        return {"n":n,"open":opn,"win":round(wins/n*100,1) if n>0 else 0,"exp_r":round((wins*2-sls)/n,2) if n>0 else 0}
    except sqlite3.OperationalError:
        return {"n":0,"open":0,"win":0,"exp_r":0}


# ============================================================
# Data loading — Liquidity Hunter
# ============================================================

def load_lh_open(conn):
    try:
        rows = q(conn, """
            SELECT asset, direction, entry, stop_loss, tp, rr,
                   quality_label, quality_score,
                   swept_level_label, swept_level_priority,
                   sweep_direction, trigger_type, tp_label,
                   mae, mfe, bars_open, timestamp_setup
            FROM lh_signals WHERE final_outcome = 'OPEN' AND timestamp_setup > ?
            ORDER BY timestamp_setup DESC
        """, (LH_EPOCH_DATE,))
    except sqlite3.OperationalError:
        return []
    now = datetime.now(timezone.utc)
    result = []
    for r in rows:
        try:
            setup_dt = datetime.fromisoformat(r[16])
            if setup_dt.tzinfo is None: setup_dt = setup_dt.replace(tzinfo=timezone.utc)
            elapsed_h = round((now - setup_dt).total_seconds() / 3600, 1)
            bars_pct  = round((r[15] or 0) / 96 * 100)
        except: elapsed_h = 0; bars_pct = 0
        result.append({
            "asset":r[0],"direction":r[1],"entry":r[2],"sl":r[3],"tp":r[4],"rr":r[5],
            "ql":r[6],"qs":r[7],"level":r[8] or "N/A","level_pri":r[9] or "N/A",
            "sweep":r[10] or "N/A","trigger":r[11] or "N/A","tp_label":r[12] or "N/A",
            "mae":r[13],"mfe":r[14],"bars_open":r[15] or 0,"bars_pct":bars_pct,
            "elapsed_h":elapsed_h,"ts":r[16],
        })
    return result

def load_lh_stats(conn):
    try:
        rows = q(conn,"SELECT final_outcome, COUNT(*) FROM lh_signals WHERE final_outcome!='OPEN' AND timestamp_setup > ? GROUP BY final_outcome", (LH_EPOCH_DATE,))
        d = {r[0]:r[1] for r in rows}; n = sum(d.values())
        wins = d.get("TP",0); sls = d.get("SL",0)
        opn = q(conn,"SELECT COUNT(*) FROM lh_signals WHERE final_outcome='OPEN' AND timestamp_setup > ?", (LH_EPOCH_DATE,))[0][0]
        return {"n":n,"open":opn,"win":round(wins/n*100,1) if n>0 else 0,"exp_r":round((wins*2-sls)/n,2) if n>0 else 0}
    except sqlite3.OperationalError:
        return {"n":0,"open":0,"win":0,"exp_r":0}


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
        dt = datetime.fromisoformat(ts.replace("Z","+00:00"))
        return dt.strftime("%d %b %H:%M")
    except: return ts[:16]

def outcome_badge(o):
    cls = {"TP":"b-tp","SL":"b-sl","EXPIRED":"b-exp",
           "TP1_HIT":"b-tp","TP2_HIT":"b-tp","SL_HIT":"b-sl"}.get(o,"b-exp")
    return f'<span class="badge {cls}">{o}</span>'

def direction_badge(d):
    return f'<span class="badge {"b-buy" if d=="BUY" else "b-sell"}">{d}</span>'

def ql_badge(ql):
    cls = {"HIGH":"b-high","MEDIUM":"b-med","LOW":"b-low","PREMIUM":"b-premium"}.get(ql,"b-low")
    return f'<span class="badge {cls}">{ql or "—"}</span>'

def kpi_row(s, color):
    wc = "pos" if s["win"]>=40 else ("neg" if s["win"]<25 else "warn")
    ec = "pos" if s["exp_r"]>0 else "neg"
    return f"""<div class="kpi-row" style="border-top:2px solid {color};margin-bottom:16px">
  <div><span class="big">{s['open']}</span><span class="lbl">Aperti ora</span></div>
  <div><span class="big">{s['n']}</span><span class="lbl">Chiusi totale</span></div>
  <div><span class="big {wc}">{s['win']}%</span><span class="lbl">Win Rate</span></div>
  <div><span class="big {ec}">{s['exp_r']:+.2f}R</span><span class="lbl">Expectancy</span></div>
</div>"""


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
.meta{font-family:'IBM Plex Mono',monospace;font-size:11px;color:var(--dim)}
.meta a{color:var(--accent);text-decoration:none}
.container{max-width:1320px;margin:0 auto;padding:24px 32px}
.section-title{font-family:'IBM Plex Mono',monospace;font-size:11px;font-weight:600;letter-spacing:.12em;text-transform:uppercase;margin:28px 0 12px;padding-bottom:6px;border-bottom:1px solid var(--border)}
.section-title.el{color:var(--accent)}
.section-title.tt{color:#f472b6}
.pulse-tt{background:#f472b6}
.b-waiting{background:rgba(244,114,182,.15);color:#f472b6}
.section-title.trb{color:var(--accent4)}
.section-title.lh{color:var(--accent5)}
.section-title.v41p1{color:var(--accent3);opacity:.8}
.kpi-row{display:grid;grid-template-columns:repeat(4,1fr);gap:1px;background:var(--border);border:1px solid var(--border);border-radius:6px;overflow:hidden;margin-bottom:20px}
.kpi-row>div{background:var(--surface);padding:16px 12px;text-align:center}
.big{font-family:'IBM Plex Mono',monospace;font-size:22px;font-weight:600}
.big.pos{color:var(--buy)} .big.neg{color:var(--sell)} .big.warn{color:var(--accent3)}
.lbl{font-size:9px;text-transform:uppercase;letter-spacing:.06em;color:var(--dim);display:block;margin-top:3px}
.card{background:var(--surface);border:1px solid var(--border);border-radius:6px;overflow:hidden;margin-bottom:16px}
.ch{padding:10px 16px;border-bottom:1px solid var(--border);font-family:'IBM Plex Mono',monospace;font-size:11px;font-weight:600;letter-spacing:.1em;text-transform:uppercase;color:var(--dim);display:flex;align-items:center;gap:8px}
.pulse{display:inline-block;width:7px;height:7px;border-radius:50%;background:var(--accent);animation:pulse 2s infinite;flex-shrink:0}
.pulse-trb{background:var(--accent4)} .pulse-lh{background:var(--accent5)}
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
.b-premium{background:rgba(167,139,250,.15);color:var(--accent4)}
.b-open{background:rgba(255,209,102,.12);color:var(--accent3)}
.progress-bar{height:3px;background:var(--border);border-radius:2px;margin-top:4px;width:80px}
.progress-fill{height:3px;background:var(--accent3);border-radius:2px}
.empty-row td{text-align:center;color:var(--dim);padding:32px;font-size:13px}
.divider{margin:32px 0 24px;border-top:1px dashed var(--border)}
@media(max-width:900px){.kpi-row{grid-template-columns:repeat(2,1fr)}.container{padding:12px}.card table{min-width:600px}}
"""


# ============================================================
# Tabelle HTML
# ============================================================

def tt_open_table(rows):
    if not rows:
        return """<div class="card"><div class="ch"><span class="pulse pulse-tt"></span>Segnali Attivi — TT</div>
  <table><tbody><tr class="empty-row"><td colspan="10">Nessun segnale attivo. In attesa di un Early Signal.</td></tr></tbody></table></div>"""
    body = ""
    for r in rows:
        asset = r["asset"].replace("_USDT","")
        status_badge = (f'<span class="badge b-waiting">IN ATTESA</span>' if r["status"] == "SETUP"
                        else f'<span class="badge b-buy">ENTRY</span>')
        bars_label = f"{r['bars_waiting']} cicli" if r["status"] == "SETUP" else f"{r['bars_open']} cicli"
        body += f"""<tr>
  <td class="mono" style="color:var(--dim);font-size:11px">{fmt_ts(r['ts'])}</td>
  <td><strong>{asset}</strong></td>
  <td>{direction_badge(r['direction'])}</td>
  <td>{status_badge}</td>
  <td class="mono">{fp(r['entry'])}</td>
  <td class="mono neg">{fp(r['sl'])}</td>
  <td class="mono">{fp(r['tp'])}</td>
  <td class="mono">{float(r['rr'] or 0):.2f}</td>
  <td style="font-size:12px;color:var(--dim)">{r['poi_type']} · {r['pd_zone']}</td>
  <td class="mono" style="color:var(--dim)">{r['elapsed_h']}h ({bars_label})</td>
</tr>"""
    return f"""<div class="card"><div class="ch"><span class="pulse pulse-tt"></span>Segnali Attivi — TT ({len(rows)})</div>
  <div style="overflow-x:auto"><table><thead><tr>
    <th>Data</th><th>Asset</th><th>Dir</th><th>Stato</th><th>Entry</th><th>SL</th><th>TP</th>
    <th>R/R</th><th>POI · PD</th><th>Tempo</th>
  </tr></thead><tbody>{body}</tbody></table></div></div>"""


def ote_open_table(rows):
    if not rows:
        return """<div class="card"><div class="ch"><span class="pulse"></span>Segnali Attivi — OTE</div>
  <table><tbody><tr class="empty-row"><td colspan="9">Nessun segnale attivo. In attesa di zone calde.</td></tr></tbody></table></div>"""
    body = ""
    for r in rows:
        asset = r["asset"].replace("_USDT","")
        status = r["status"]
        if status in ("WATCHING","TOUCHED"):
            status_badge = '<span class="badge b-waiting">WATCHING</span>' if status=="WATCHING" else '<span class="badge b-open">TOUCHED</span>'
            dir_show = "—"
        else:
            status_badge = outcome_badge(status)
            dir_show = direction_badge(r["direction"])
        body += f"""<tr>
  <td class="mono" style="color:var(--dim);font-size:11px">{fmt_ts(r['ts'])}</td>
  <td><strong>{asset}</strong></td>
  <td>{dir_show}</td>
  <td>{status_badge}</td>
  <td class="mono">{fp(r['entry']) if isinstance(r['entry'], (int,float)) else r['entry']}</td>
  <td class="mono neg">{fp(r['sl']) if isinstance(r['sl'], (int,float)) else r['sl']}</td>
  <td class="mono">{fp(r['tp']) if isinstance(r['tp'], (int,float)) else r['tp']}</td>
  <td style="font-size:12px;color:var(--dim)">{r.get('zone_strength','—')}</td>
  <td class="mono" style="color:var(--dim)">{r['elapsed_h']}h</td>
</tr>"""
    return f"""<div class="card"><div class="ch"><span class="pulse"></span>Segnali Attivi — OTE ({len(rows)})</div>
  <div style="overflow-x:auto"><table><thead><tr>
    <th>Data</th><th>Asset</th><th>Dir</th><th>Stato</th><th>Entry</th><th>SL</th><th>TP</th>
    <th>Zona</th><th>Tempo</th>
  </tr></thead><tbody>{body}</tbody></table></div></div>"""


def v41p1_open_table(rows):
    if not rows:
        return """<div class="card"><div class="ch">Segnali Aperti — V4.1 Phase 1</div>
  <table><tbody><tr class="empty-row"><td colspan="10">Nessun segnale aperto.</td></tr></tbody></table></div>"""
    body = ""
    for r in rows:
        asset = r["asset"].replace("_USDT","")
        tp1_badge = '<span class="badge b-tp" style="font-size:10px">TP1✓</span>' if r["tp1_hit"] else ""
        body += f"""<tr>
  <td><strong>{asset}</strong></td>
  <td>{direction_badge(r['direction'])}</td>
  <td class="mono">{fp(r['entry'])}</td>
  <td class="mono neg">{fp(r['sl'])}</td>
  <td class="mono">{fp(r['tp1'])} {tp1_badge}</td>
  <td class="mono">{fp(r['tp2'])}</td>
  <td>{ql_badge(r['ql'])}</td>
  <td style="font-size:12px;color:var(--dim)">{r['trigger']}</td>
  <td class="mono neg">{fp(r['mae'])}</td>
  <td class="mono" style="color:var(--dim)">{r['elapsed_h']}h</td>
</tr>"""
    return f"""<div class="card"><div class="ch">Segnali Aperti — V4.1 Phase 1 ({len(rows)})</div>
  <div style="overflow-x:auto"><table><thead><tr>
    <th>Asset</th><th>Dir</th><th>Entry</th><th>SL</th><th>TP1</th><th>TP2</th>
    <th>Quality</th><th>Trigger</th><th>MAE</th><th>Aperto</th>
  </tr></thead><tbody>{body}</tbody></table></div></div>"""


def trb_open_table(rows):
    if not rows:
        return """<div class="card"><div class="ch"><span class="pulse pulse-trb"></span>Segnali Aperti — Trend Rider Balanced</div>
  <table><tbody><tr class="empty-row"><td colspan="11">Nessun segnale aperto. In attesa di pullback verso EMA20 H1.</td></tr></tbody></table></div>"""
    body = ""
    for r in rows:
        asset = r["asset"].replace("_USDT","")
        tp1_badge = '<span class="badge b-tp" style="font-size:10px">TP1✓</span>' if r["tp1_hit"] else ""
        body += f"""<tr>
  <td class="mono" style="color:var(--dim);font-size:11px">{fmt_ts(r['ts'])}</td>
  <td><strong>{asset}</strong></td>
  <td>{direction_badge(r['direction'])}</td>
  <td class="mono">{fp(r['entry'])}</td>
  <td class="mono neg">{fp(r['sl'])}</td>
  <td class="mono">{fp(r['tp1'])} {tp1_badge}</td>
  <td class="mono">{fp(r['tp2'])}</td>
  <td>{ql_badge(r['ql'])}</td>
  <td class="mono" style="color:var(--dim)">{f"{float(r['adx']):.1f}" if r['adx'] else '—'}</td>
  <td style="font-size:12px;color:var(--dim)">{r['trend_h1'] or '—'}</td>
  <td class="mono" style="color:var(--dim)">{r['elapsed_h']}h</td>
</tr>"""
    return f"""<div class="card"><div class="ch"><span class="pulse pulse-trb"></span>Segnali Aperti — Trend Rider Balanced ({len(rows)})</div>
  <div style="overflow-x:auto"><table><thead><tr>
    <th>Data</th><th>Asset</th><th>Dir</th><th>Entry</th><th>SL</th><th>TP1</th><th>TP2</th>
    <th>Quality</th><th>ADX</th><th>H1</th><th>Aperto</th>
  </tr></thead><tbody>{body}</tbody></table></div></div>"""


def lh_open_table(rows):
    if not rows:
        return """<div class="card"><div class="ch"><span class="pulse pulse-lh"></span>Segnali Aperti — Liquidity Hunter v1.0</div>
  <table><tbody><tr class="empty-row"><td colspan="11">Nessun segnale aperto. In attesa di sweep su Liquidity Pool.</td></tr></tbody></table></div>"""
    body = ""
    for r in rows:
        asset = r["asset"].replace("_USDT","")
        body += f"""<tr>
  <td class="mono" style="color:var(--dim);font-size:11px">{fmt_ts(r['ts'])}</td>
  <td><strong>{asset}</strong></td>
  <td>{direction_badge(r['direction'])}</td>
  <td class="mono">{fp(r['entry'])}</td>
  <td class="mono neg">{fp(r['sl'])}</td>
  <td class="mono">{fp(r['tp'])}</td>
  <td class="mono">{float(r['rr'] or 0):.2f}</td>
  <td>{ql_badge(r['ql'])}</td>
  <td style="font-size:12px;color:var(--dim)">{r['level']} ({r['level_pri']})</td>
  <td style="font-size:12px;color:var(--dim)">{r['sweep']} → {r['trigger']}</td>
  <td class="mono neg">{fp(r['mae'])}</td>
</tr>"""
    return f"""<div class="card"><div class="ch"><span class="pulse pulse-lh"></span>Segnali Aperti — Liquidity Hunter v1.0 ({len(rows)})</div>
  <div style="overflow-x:auto"><table><thead><tr>
    <th>Data</th><th>Asset</th><th>Dir</th><th>Entry</th><th>SL</th><th>TP</th>
    <th>R/R</th><th>Quality</th><th>Livello</th><th>Sweep → Trigger</th><th>MAE</th>
  </tr></thead><tbody>{body}</tbody></table></div></div>"""


# ============================================================
# Generate
# ============================================================

def generate():
    conn = sqlite3.connect(DB_PATH)

    tt_open   = load_tt_open(conn)
    tt_stats  = load_tt_stats(conn)

    ote_open    = load_ote_open_unified(conn)
    ote_stats   = load_ote_stats_unified(conn)
    v41p1_open  = load_v41p1_open(conn)
    v41p1_stats = load_v41p1_stats(conn)
    trb_open    = load_trb_open(conn)
    trb_stats   = load_trb_stats(conn)
    lh_open     = load_lh_open(conn)
    lh_stats    = load_lh_stats(conn)

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
  <div class="meta">{generated} &nbsp;|&nbsp; <a href="analytics_dashboard.html">Analytics Lab →</a></div>
</header>
<div class="container">

  <div class="section-title tt">⚡ TT — Direction · Location · Liquidity</div>
  {kpi_row(tt_stats, "#f472b6")}
  {tt_open_table(tt_open)}

  <div class="divider"></div>

  <div class="section-title el">⚡ OTE — Zona prima, direzione dopo</div>
  {kpi_row(ote_stats, "var(--accent)")}
  {ote_open_table(ote_open)}

  <div class="divider"></div>

  <div class="section-title trb">🎯 NMC Trend Rider Balanced v1.0</div>
  {kpi_row(trb_stats, "var(--accent4)")}
  {trb_open_table(trb_open)}

  <div class="divider"></div>

  <div class="section-title lh">🎯 Liquidity Hunter v1.0</div>
  {kpi_row(lh_stats, "var(--accent5)")}
  {lh_open_table(lh_open)}

  <div class="divider"></div>

  <div class="section-title v41p1">V4.1 Phase 1 — Money Flow Benchmark</div>
  {kpi_row(v41p1_stats, "var(--accent3)")}
  {v41p1_open_table(v41p1_open)}

</div>
</body>
</html>"""

    os.makedirs("docs", exist_ok=True)
    with open(OUT_PATH, "w") as f:
        f.write(html)

    print(
        f"Dashboard unificata generata: {OUT_PATH} "
        f"(TT aperti={tt_stats['open']} chiusi={tt_stats['n']} | "
        f"OTE aperti={ote_stats['open']} chiusi={ote_stats['n']} | "
        f"V4.1P1 aperti={v41p1_stats['open']} chiusi={v41p1_stats['n']} | "
        f"TRB aperti={trb_stats['open']} chiusi={trb_stats['n']} | "
        f"LH aperti={lh_stats['open']} chiusi={lh_stats['n']})"
    )


if __name__ == "__main__":
    generate()
