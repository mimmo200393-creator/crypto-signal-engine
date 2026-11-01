from pathlib import Path
import os
import sqlite3, json, html, re, zipfile, datetime
BASE=Path('/mnt/data')
DB=Path(os.environ.get('DB_PATH', 'data/signals.db'))
LEDGER=Path(os.environ.get('LEDGER_DB_PATH', 'data/decision_ledger.db'))
OUT=Path(os.environ.get('AI_JOURNAL_OUT', 'docs/ai_trading_journal_dashboard_generated.html'))
JSONOUT=Path(os.environ.get('AI_JOURNAL_JSON_OUT', 'docs/ai_trading_journal_data.json'))
# Unified schema mapping. Read only, same source tables as Unified dashboard.
STRATEGIES = {
    "OTE-LAB": {"table":"ote_lab_signals", "date":"timestamp_setup", "outcome":"final_outcome",
                "epoch":None, "r_mode":"ote"},
    "TT": {"table":"tt_signals", "date":"signal_created_at", "outcome":"status",
           "epoch":"2026-08-27T00:00:00", "r_mode":"tt"},
    "LH": {"table":"lh_signals", "date":"timestamp_setup", "outcome":"final_outcome",
           "epoch":"2026-08-24T18:00:00", "r_mode":"lh"},
    "TRB": {"table":"trb_signals", "date":"timestamp_setup", "outcome":"final_outcome",
            "epoch":None, "r_mode":"trb"},
    "V41P1": {"table":"v41p1_signals", "date":"timestamp_setup", "outcome":"final_outcome",
              "epoch":None, "r_mode":"v41"},
}
DB=Path(os.environ.get("DB_PATH", "data/signals.db"))
LEDGER=Path(os.environ.get("LEDGER_DB_PATH", "data/decision_ledger.db"))
OUT=Path(os.environ.get("AI_JOURNAL_OUT", "docs/ai_trading_journal_dashboard_generated.html"))
JSONOUT=Path(os.environ.get("AI_JOURNAL_JSON_OUT", "docs/ai_trading_journal_data.json"))

def connect_readonly(path):
    if not path.exists(): return None
    conn=sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True, timeout=10)
    conn.row_factory=sqlite3.Row
    return conn

def table_exists(conn, table):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None

def columns(conn, table):
    return {r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')}

def iso_date(v):
    if v is None: return None
    if isinstance(v,(int,float)):
        try: return datetime.datetime.fromtimestamp(v/(1000 if v>1e11 else 1),datetime.timezone.utc).isoformat()
        except (ValueError,OverflowError,OSError): return str(v)
    s=str(v).strip()
    if not s: return None
    try:
        d=datetime.datetime.fromisoformat(s.replace("Z","+00:00"))
        if d.tzinfo is None: d=d.replace(tzinfo=datetime.timezone.utc)
        return d.isoformat()
    except ValueError: return s

def safe_num(v):
    try:
        if v is None or v=="": return None
        return float(v)
    except (ValueError,TypeError): return None

def normalize_outcome(strategy, row):
    # Preserve raw outcome/status; add a canonical category without rewriting source semantics.
    raw=str(row.get("final_outcome") or row.get("result") or row.get("status") or "").strip().upper()
    if strategy=="TT":
        return {"TP":"WIN","TRAIL":"WIN","SL":"LOSS","EXPIRED":"EXPIRED",
                "SETUP":"OPEN_SETUP","ENTRY":"OPEN_TRADE","INVALIDATED":"INVALIDATED"}.get(raw,raw or "UNKNOWN")
    if raw in ("TP","TP1_HIT","TP2_HIT","TRAIL_HIT","STAGE2_HIT","TP_FIXED"): return "WIN"
    if raw in ("SL","SL_HIT"): return "LOSS"
    if raw in ("OPEN","ENTRY"): return "OPEN"
    if raw in ("BE_HIT","BREAKEVEN","BE"): return "BREAKEVEN"
    if raw in ("EXPIRED","EXPIRY","TIMEOUT"): return "EXPIRED"
    return raw or "UNKNOWN"

def realized_r(strategy, row):
    raw=str(row.get("final_outcome") or row.get("result") or row.get("status") or "").strip().upper()
    explicit=safe_num(row.get("result_r"))
    if strategy=="TT":
        if explicit is not None and raw in ("TP","SL","TRAIL"): return explicit
        if raw=="SL": return -1.0
        return 0.0
    if strategy=="OTE-LAB":
        if explicit is not None: return explicit
        if raw=="SL_HIT": return -1.0
        if raw=="TP2_HIT": return safe_num(row.get("rr2")) or 2.0
        if raw=="TP1_HIT": return safe_num(row.get("rr1")) or 1.0
        if raw=="TRAIL_HIT": return safe_num(row.get("rr1")) or 0.0
        return 0.0
    if strategy=="TRB":
        if raw=="TP2_HIT": return safe_num(row.get("rr2")) or 2.0
        if raw in ("TP1_HIT","TRAIL_HIT"): return safe_num(row.get("rr1")) or 0.0
        if raw=="SL_HIT": return -1.0
        return 0.0
    if strategy=="LH":
        if raw=="TP": return safe_num(row.get("rr")) or 2.0
        if raw=="STAGE2_HIT": return 0.90
        if raw=="SL": return -1.0
        return 0.0
    if strategy=="V41P1":
        if raw=="TP": return safe_num(row.get("rr")) or 2.0
        if raw=="SL": return -1.0
        return 0.0
    return explicit or 0.0

def collect():
    now=datetime.datetime.now(datetime.timezone.utc)
    cutoff=now-datetime.timedelta(days=35)
    data={"generated_at_utc":now.isoformat(timespec="seconds"),"period_days":35,
          "signals_db":str(DB),"ledger_db":str(LEDGER),"strategies":{},
          "table_counts":{},"records":[],"ledger":{},"source_status":{}}
    conn=connect_readonly(DB)
    if conn:
        for name,cfg in STRATEGIES.items():
            table=cfg["table"]
            if not table_exists(conn,table):
                data["source_status"][table]="missing";data["table_counts"][table]=None
                data["strategies"][name]={"count":0,"closed":0,"open":0,"wins":0,"losses":0,"win_rate":None,"expectancy_r":None}
                continue
            cols=columns(conn,table)
            datecol=cfg["date"] if cfg["date"] in cols else next((x for x in ("timestamp_setup","signal_created_at","created_at") if x in cols),None)
            outcols=[x for x in ("final_outcome","result","status") if x in cols]
            selected=list(cols)
            rows=[dict(r) for r in conn.execute(f'SELECT * FROM "{table}"')]
            records=[]; outcomes={}
            for rawrow in rows:
                row={k:rawrow.get(k) for k in selected}
                dt=iso_date(row.get(datecol)) if datecol else None
                row["date"]=dt;row["strategy"]=name;row["source_table"]=table
                rawout=next((row.get(x) for x in outcols if row.get(x) not in (None,"")),None)
                row["raw_outcome"]=str(rawout).strip().upper() if rawout is not None else "UNKNOWN"
                row["outcome"]=normalize_outcome(name,row)
                row["result_r_calc"]=realized_r(name,row)
                if cfg["epoch"] and dt and dt<=cfg["epoch"]: continue
                # Apply same 5-week dashboard scope; unparseable dates remain in inventory but not period records.
                if dt:
                    try:
                        d=datetime.datetime.fromisoformat(dt.replace("Z","+00:00"))
                        if d<cutoff: continue
                    except ValueError: continue
                else: continue
                records.append(row); outcomes[row["outcome"]]=outcomes.get(row["outcome"],0)+1
            # Unified KPI semantics: each strategy keeps its original outcome set and denominator.
            if name=="TT":
                closed=[r for r in records if r["raw_outcome"] in ("TP","SL","TRAIL","EXPIRED")]
                decisive=[r for r in closed if r["raw_outcome"] in ("TP","SL","TRAIL")]
                wins=sum(r["raw_outcome"] in ("TP","TRAIL") for r in closed)
                total_r=sum((safe_num(r.get("result_r")) if safe_num(r.get("result_r")) is not None else (-1.0 if r["raw_outcome"]=="SL" else 0.0)) for r in decisive)
                win_rate=round(wins/len(closed)*100,1) if closed else 0
                exp_r=round(total_r/len(closed),2) if closed else 0
                open_n=sum(r["raw_outcome"] in ("SETUP","ENTRY") for r in records)
            elif name=="OTE-LAB":
                closed=[r for r in records if r["raw_outcome"] not in ("OPEN","")]
                wins=sum(r["raw_outcome"] in ("TP2_HIT","TP1_HIT","TRAIL_HIT") for r in closed)
                total_r=sum(safe_num(r.get("result_r")) or 0.0 for r in closed)
                win_rate=round(wins/len(closed)*100,1) if closed else 0
                exp_r=round(total_r/len(closed),2) if closed else 0
                open_n=sum(r["raw_outcome"]=="OPEN" for r in records)
            elif name=="TRB":
                closed=[r for r in records if r["raw_outcome"]!="OPEN"]
                wins=sum(r["raw_outcome"] in ("TP2_HIT","TP1_HIT","TRAIL_HIT") for r in closed)
                total_r=sum(realized_r(name,r) for r in closed if r["raw_outcome"] in ("TP1_HIT","TP2_HIT","TRAIL_HIT","SL_HIT"))
                win_rate=round(wins/len(closed)*100,1) if closed else 0
                exp_r=round(total_r/len(closed),2) if closed else 0
                open_n=sum(r["raw_outcome"]=="OPEN" for r in records)
            elif name=="LH":
                closed=[r for r in records if r["raw_outcome"]!="OPEN"]
                wins=sum(r["raw_outcome"] in ("TP","STAGE2_HIT") for r in closed)
                total_r=sum(realized_r(name,r) for r in closed)
                win_rate=round(wins/len(closed)*100,1) if closed else 0
                exp_r=round(total_r/len(closed),2) if closed else 0
                open_n=sum(r["raw_outcome"]=="OPEN" for r in records)
            else: # V41P1 Unified: TP=+2R, SL=-1R, other closed outcomes=0R
                closed=[r for r in records if r["raw_outcome"]!="OPEN"]
                wins=sum(r["raw_outcome"]=="TP" for r in closed)
                sls=sum(r["raw_outcome"]=="SL" for r in closed)
                total_r=wins*2-sls
                win_rate=round(wins/len(closed)*100,1) if closed else 0
                exp_r=round(total_r/len(closed),2) if closed else 0
                open_n=sum(r["raw_outcome"]=="OPEN" for r in records)
            stats={"count":len(records),"closed":len(closed),"open":open_n,"wins":wins,
                   "losses":sum(r["raw_outcome"] in ("SL","SL_HIT") for r in closed),
                   "win_rate":win_rate,"expectancy_r":exp_r,
                   "outcomes":outcomes,"epoch":cfg["epoch"]}
            data["strategies"][name]=stats;data["table_counts"][table]=len(rows);data["source_status"][table]="available"
            data["records"].extend(records)
        conn.close()
    else:
        data["source_status"]["signals_db"]="missing"
    # Decision ledger schema is retained in detail; recent 5-week rows, no inferred performance.
    ledger=connect_readonly(LEDGER)
    if ledger:
        if table_exists(ledger,"decision_ledger"):
            lc=columns(ledger,"decision_ledger")
            tscol="ts_iso" if "ts_iso" in lc else ("created_ts_iso" if "created_ts_iso" in lc else None)
            allrows=[dict(r) for r in ledger.execute("SELECT * FROM decision_ledger")]
            lr=[]
            for r in allrows:
                dt=iso_date(r.get(tscol)) if tscol else None
                if dt:
                    try:
                        if datetime.datetime.fromisoformat(dt.replace("Z","+00:00"))<cutoff: continue
                    except ValueError: continue
                else: continue
                r["date"]=dt;lr.append(r)
            data["ledger"]={"available":True,"table":"decision_ledger","total_rows":len(allrows),"period_rows":len(lr),"records":lr}
            data["source_status"]["decision_ledger"]="available"
        else: data["source_status"]["decision_ledger"]="table_missing"
        ledger.close()
    else:
        data["ledger"]={"available":False,"total_rows":None,"period_rows":0,"records":[]}
        data["source_status"]["decision_ledger"]="missing"
    return data

payload=collect()
js=json.dumps(payload,ensure_ascii=False,separators=(",",":")).replace("</","<\\/")
JSONOUT.parent.mkdir(parents=True,exist_ok=True)
JSONOUT.write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding="utf-8")
html_doc=r'''<!doctype html><html lang="it"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>AI Trading Journal · Unified Snapshot</title><style>
:root{--bg:#0b1018;--panel:#131c27;--line:#263443;--txt:#e8eef6;--muted:#91a0b3;--mint:#69dfbd;--blue:#86b8ff;--amber:#efc477;--red:#ff9292}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--txt);font:14px/1.5 system-ui}header{padding:25px max(16px,calc((100vw - 1280px)/2));background:#101927;border-bottom:1px solid var(--line)}main{max-width:1280px;margin:auto;padding:20px}h1{margin:0;font-size:27px}h2{font-size:16px;margin:0 0 12px}.muted,small{color:var(--muted)}.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}.panel{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:16px;margin-bottom:14px}.cards{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px;margin:15px 0}.metric{background:#111a25;border:1px solid var(--line);border-radius:10px;padding:13px}.metric strong{font-size:25px;display:block}.btn,select{background:#182636;color:var(--txt);border:1px solid #34445b;border-radius:8px;padding:9px 12px;cursor:pointer}.btn.primary{background:var(--mint);color:#07151a;border-color:var(--mint);font-weight:800}.btn:disabled{opacity:.5;cursor:wait}select{cursor:pointer}.status{padding:8px 11px;border:1px solid var(--line);border-radius:8px;color:var(--amber)}.strats{display:grid;grid-template-columns:repeat(7,minmax(0,1fr));gap:7px}.strat{padding:9px;border:1px solid var(--line);border-radius:8px;background:#101923}.strat b{display:block}.scroll{overflow:auto}table{border-collapse:collapse;width:100%;font-size:12px;white-space:nowrap}th,td{padding:8px;border-bottom:1px solid var(--line);text-align:left}th{color:#9aa9bd;position:sticky;top:0;background:#131c27}.tag{padding:3px 6px;border-radius:5px;background:#203244}.positive{color:var(--mint)}.negative{color:var(--red)}.warn{color:var(--amber)}#report{display:none}.note{font-size:12px;color:var(--muted)}textarea{width:100%;background:#0d141e;color:var(--txt);border:1px solid var(--line);border-radius:8px;padding:10px;min-height:70px}@media(max-width:850px){.cards{grid-template-columns:repeat(2,1fr)}.strats{grid-template-columns:repeat(3,1fr)}}@media(max-width:520px){main{padding:12px}.cards{grid-template-columns:1fr 1fr}.strats{grid-template-columns:repeat(2,1fr)}header{padding:18px 14px}}
</style></head><body><header><div class="row"><div style="flex:1"><small>LOCAL DATA · READ ONLY</small><h1>AI Trading Journal — Unified Snapshot</h1><div class="muted">Snapshot database · analisi locale · ultime 5 settimane</div></div><span class="status" id="status">Ready · scan non ancora eseguito</span></div></header><main>
<section class="panel"><div class="row"><label>Periodo <select id="days"><option value="35" selected>Ultime 5 settimane (35 giorni)</option><option value="14">14 giorni</option><option value="7">7 giorni</option><option value="90">90 giorni</option><option value="0">Tutto lo snapshot</option></select></label><label>Strategia <select id="strategy"><option value="ALL">Tutte le strategie</option><option>OTE-LAB</option><option>TT</option><option>LH</option><option>TRB</option><option>V41P1</option></select></label><button class="btn primary" id="scan">▶ Run Scan</button><button class="btn" id="export">Export report JSON</button><button class="btn" id="saveNote">Save note</button></div><p class="note">Scan filtra e analizza i record incorporati in questo snapshot. Non chiama ChatGPT, MT5 o l'EA; non calcola P&L monetario.</p></section>
<div class="cards"><div class="metric"><small>Records in scope</small><strong id="n">—</strong></div><div class="metric"><small>Closed / outcome available</small><strong id="closed">—</strong></div><div class="metric"><small>Outcome buckets</small><strong id="buckets">—</strong></div><div class="metric"><small>Data coverage</small><strong id="coverage">—</strong></div></div>
<section class="panel"><h2>Strategy inventory</h2><div id="strats" class="strats"></div></section>
<section class="panel" id="report"><h2>Scan report</h2><div id="summary"></div><h2 style="margin-top:15px">Outcome breakdown</h2><div class="scroll"><table><thead><tr><th>Strategy</th><th>Outcome/status</th><th>Count</th></tr></thead><tbody id="outcomes"></tbody></table></div><h2 style="margin-top:15px">Records in selected period</h2><div class="scroll"><table><thead><tr><th>Date</th><th>Strategy</th><th>Asset</th><th>Direction</th><th>Status/outcome</th><th>R</th><th>Quality</th><th>Source</th></tr></thead><tbody id="records"></tbody></table></div></section>
<section class="panel"><h2>Journal notes</h2><textarea id="note" placeholder="Annotazioni analyst: ipotesi, pattern, anomalie, follow-up..."></textarea><div class="row" style="margin-top:8px"><button class="btn" id="exportNotes">Export notes</button><button class="btn" id="importNotes">Import notes</button><input type="file" id="noteFile" accept="application/json" hidden></div><div id="notesList" class="note"></div></section>
<section class="panel"><h2>Audit / limitations</h2><div id="audit" class="note"></div></section></main><script id="snapshot" type="application/json">__PAYLOAD__</script><script>
const DATA=JSON.parse(document.getElementById('snapshot').textContent);const $=id=>document.getElementById(id);let lastReport=null;const all=DATA.records||[];
function esc(s){return String(s??'—').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function dateOf(r){if(!r.date)return null;let d=new Date(r.date);return Number.isNaN(d.getTime())?null:d}
function renderInventory(){let by={};all.forEach(r=>by[r.strategy]=(by[r.strategy]||0)+1);$('strats').innerHTML=['OTE-LAB','TT','LH','TRB','V41P1'].map(s=>`<div class="strat"><b>${s}</b><strong>${by[s]||0}</strong><small>snapshot rows</small></div>`).join('')}
function scan(){let days=Number($('days').value),st=$('strategy').value,cut=days?Date.now()-days*86400000:0;let filtered=all.filter(r=>{let d=dateOf(r);return(st==='ALL'||r.strategy===st)&&(!cut||(d&&d.getTime()>=cut))});let outcome=r=>String(r.outcome??r.status??'UNAVAILABLE').trim().toUpperCase()||'UNAVAILABLE';let buckets={};filtered.forEach(r=>{let k=r.strategy+' · '+outcome(r);buckets[k]=(buckets[k]||0)+1});let known=filtered.filter(r=>{let x=outcome(r);return x!=='UNAVAILABLE'&&x!=='NONE'&&x!=='NULL'&&x!=='—'});let dateable=filtered.filter(r=>dateOf(r));$('n').textContent=filtered.length.toLocaleString();$('closed').textContent=known.length.toLocaleString();$('buckets').textContent=Object.keys(buckets).length.toLocaleString();$('coverage').textContent=filtered.length?Math.round(dateable.length/filtered.length*100)+'%':'—';$('outcomes').innerHTML=Object.entries(buckets).sort((a,b)=>b[1]-a[1]).map(([k,v])=>{let p=k.split(' · ');return `<tr><td>${esc(p[0])}</td><td>${esc(p[1])}</td><td>${v}</td></tr>`}).join('');$('records').innerHTML=filtered.slice().sort((a,b)=>String(b.date||'').localeCompare(String(a.date||''))).map(r=>`<tr><td>${esc(r.date)}</td><td>${esc(r.strategy)}</td><td>${esc(r.asset)}</td><td>${esc(r.direction)}</td><td><span class="tag">${esc(outcome(r))}</span></td><td>${esc(r.result_r??r.rr)}</td><td>${esc(r.quality_label??r.quality_score)}</td><td>${esc(r.source_table)}</td></tr>`).join('');$('summary').innerHTML=`<p>Selected <b>${filtered.length}</b> records; date coverage <b>${dateable.length}/${filtered.length}</b>. Outcomes available: <b>${known.length}</b>.</p><div class=\"scroll\"><table><thead><tr><th>Strategy</th><th>Closed</th><th>Win rate</th><th>Expectancy R</th><th>Open</th></tr></thead><tbody>${Object.entries(DATA.strategies||{}).map(([s,x])=>`<tr><td>${esc(s)}</td><td>${x.closed}</td><td>${x.win_rate==null?'—':x.win_rate+'%'}</td><td>${x.expectancy_r==null?'—':x.expectancy_r+'R'}</td><td>${x.open}</td></tr>`).join('')}</tbody></table></div><p>Stats are from Unified source tables, period/epoch filters applied. R normalization is strategy-specific; inspect JSON fields for audit.</p>`;$('report').style.display='block';$('status').textContent='Scan completed · '+new Date().toLocaleTimeString();lastReport={generated_at:new Date().toISOString(),period_days:days,strategy:st,records_count:filtered.length,known_outcome_count:known.length,date_coverage:dateable.length,by_strategy:filtered.reduce((a,r)=>(a[r.strategy]=(a[r.strategy]||0)+1,a),{}),outcomes:buckets,records:filtered};}
$('scan').addEventListener('click',scan);$('export').addEventListener('click',()=>{if(!lastReport)scan();download('scan_report.json',JSON.stringify(lastReport,null,2))});function download(name,txt){let a=document.createElement('a');a.href=URL.createObjectURL(new Blob([txt],{type:'application/json'}));a.download=name;a.click();URL.revokeObjectURL(a.href)}
let notes=JSON.parse(localStorage.getItem('aiJournalNotes')||'[]');function renderNotes(){$('notesList').innerHTML=notes.map((n,i)=>`<div style="border-top:1px solid var(--line);padding:8px 0"><b>${esc(n.date)} · ${esc(n.strategy)}</b><p>${esc(n.text)}</p><button class="btn" data-del="${i}">Delete</button></div>`).join('');$('notesList').querySelectorAll('[data-del]').forEach(b=>b.onclick=()=>{notes.splice(+b.dataset.del,1);localStorage.setItem('aiJournalNotes',JSON.stringify(notes));renderNotes()})}
$('saveNote').onclick=()=>{let t=$('note').value.trim();if(!t){alert('Inserisci una nota prima di salvarla.');return}notes.unshift({date:new Date().toISOString(),strategy:$('strategy').value,text:t,period_days:+$('days').value});localStorage.setItem('aiJournalNotes',JSON.stringify(notes));$('note').value='';renderNotes()};$('exportNotes').onclick=()=>download('journal_notes.json',JSON.stringify(notes,null,2));$('importNotes').onclick=()=>$('noteFile').click();$('noteFile').onchange=async e=>{try{let x=JSON.parse(await e.target.files[0].text());if(!Array.isArray(x))throw Error('JSON must be array');notes=x.filter(n=>n&&typeof n.text==='string');localStorage.setItem('aiJournalNotes',JSON.stringify(notes));renderNotes()}catch(err){alert('Import non valido: '+err.message)}};
renderInventory();renderNotes();$('audit').textContent=`Snapshot Unified generato ${DATA.generated_at_utc}. DB: ${DATA.signals_db}. Scope 35 giorni. Tabelle e conteggi: ${JSON.stringify(DATA.table_counts)}. Decision ledger: ${DATA.ledger?.period_rows??'non disponibile'} righe nel periodo. Win rate ed expectancy sono normalizzati per strategia usando outcome/risk fields disponibili; le assunzioni R sono documentate nel payload. Non è P&L monetario né execution reconciliation MT5. Note browser localStorage; esportare per conservarle.`;
</script></body></html>'''
html_doc=html_doc.replace('__PAYLOAD__',js)
OUT.parent.mkdir(parents=True,exist_ok=True)
OUT.write_text(html_doc,encoding='utf-8')
print(f"AI Journal generated: {OUT} | records={len(payload['records'])} | ledger={payload.get('ledger',{}).get('period_rows')}")
