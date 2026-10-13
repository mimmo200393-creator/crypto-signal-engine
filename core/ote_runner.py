"""
core/ote_runner.py
OTE-LAB — Clone ESATTO di TRB con 3 modifiche sperimentali.

Questo file e' una copia quasi identica di trend_rider_runner.py.
Le UNICHE differenze sono marcate con "# ── MOD LAB ──":
  1. Target: quando liquidity_priority e' None, cerca reaction zone
  2. Trailing: TRAIL_R=0.5 invece di 0.7 (in trend_rider_db)
  3. Trend H4: scarta segnali contro H4

Salva in ote_lab_signals (schema identico a trb_signals).
NON tocca TRB, TT, LH, V41P1 — zero impatto.

Changelog 29/09/2026: riscritto da zero come clone fedele di TRB.
Changelog 11/10/2026: fix expiry — da contatore cicli (32 cicli = 2h40 con cron 5m)
                       a 6 ore reali basate su timestamp_setup.
Changelog 12/10/2026: v1.2 — TRAIL_R 0.7→0.5 (RECURRING totalR +14→+35),
                       blocco RECURRING con accumulazione 12-24h (WR 18.8%).
Changelog 12/10/2026: v1.3 — Bounce Point filter per RECURRING:
                       usa reaction_map expected_reaction (BOUNCE_UP/DOWN)
                       per filtrare segnali contro la zona di confluenza.
                       Dati: ALIGNED 75% WR vs AGAINST 37.5% (n=16).
                       Rimosso blocco sessione NY (sessioni non vanno bloccate).
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone, timedelta

from storage import db as core_db
from core import v3_db
from strategies.edge_lab.trend_rider import generate_trb_signal

logger = logging.getLogger("ote.lab")

# XAU_USD disabilitato: expectancy negativa (-0.576R/trade, WR 26.3%)
# Analisi diagnostica Oct 2026 — da riattivare solo dopo ottimizzazione
OTE_LAB_ASSETS = ["BTC_USDT"]

# ── Trailing: 0.5R (v1.2 — validato su 150 RECURRING + 28 STANDARD) ──
# Simulazione: TRAIL_R 0.7→0.5 su RECURRING: totalR +14.1→+35.3 (+150%)
# MFE media ~0.85R → con 0.7R il trail cattura solo 0.15R, con 0.5R cattura 0.35R
# 62 TRAIL_HIT su 150 trades: il trailing e' il principale meccanismo di uscita
LAB_TRAIL_R = 0.5
LAB_TRAIL_MIN_LOCK = {"BTC_USDT": 60.0, "XAU_USD": 2.5}

# ── Cap RR2 (verificato 01/10 su dati reali) ──
# Target oltre 5R non si raggiungono e il trade muore: RR2>=5 → sumR -1.8
# (WR 43%), RR2<5 → sumR +2.3 (WR 45%). Limito il target a 4R max: se la
# reaction zone e' piu' lontana, taglio il TP a 4R (piu' raggiungibile).
# v1.1: ridotto da 4.0 a 2.5 — dati mostrano che RR2 > 2.5 ha TP2 hit rate < 5%
# OTE-LAB >= 4.0R: solo 1.1% TP2 hit rate (1/91 trade)
# OTE-LAB 3.0-4.0R: solo 4.0% TP2 hit rate (1/25 trade)
MAX_RR2 = 2.5

# ── Break-even + offset (verificato 01/10) ──
# Chiudere a esattamente l'entry (0R) in realta' perde lo spread.
# Il BE viene spostato un filo SOPRA l'entry per coprire spread+commissioni.
# Valore in frazione di R: 0.3R copre lo spread con margine.
# (XAU SL 10pt → BE+ a +3pt = copre spread 2-4pt + piccolo profitto;
#  BTC SL 300pt → BE+ a +90pt = copre spread 20-50pt con margine)
BE_PLUS_R = 0.3

# ── Gap minimo TP1→TP2 ──
# Se TP2 e' troppo vicino a TP1 (es. 1.0R e 1.3R), avere due target
# separati e' inutile. Richiediamo almeno 1R di gap: se il target RZ
# e' piu' vicino, lo portiamo ad almeno TP1+1R.
MIN_TP_GAP_R = 1.0

# ── Classificazione tier (tracking puro, verificato 01/10) ──
# Audit statistico su 467 trade TRB: i 3 fattori con piu' potere
# predittivo sono zona (FVG/OB vs EMA, spread 25pt), liquidita'
# (CRITICAL 70% vs HIGH 55%, spread 15pt), ADX (alto 63% vs basso
# 48%, spread 14pt). Quando coincidono → WR verso 70%+.
# Questo campo e' SOLO un'etichetta: non cambia entry/SL/TP/size.
# Serve a verificare FUORI CAMPIONE se la classificazione regge,
# prima di applicare una size dinamica (fase 2, solo se confermato).
def _classify_tier(signal):
    zone = signal.get("entry_zone_type")
    liq = signal.get("liquidity_priority")
    adx = signal.get("adx") or 0
    good_zone = zone in ("fvg", "order_block")   # fattore #1 (spread 25pt)
    strong_liq = liq in ("CRITICAL", "HIGH")      # fattore #2 (spread 15pt)
    critical = liq == "CRITICAL"
    strong_adx = adx >= 35                         # fattore #3 (spread 14pt)
    # PREMIUM: tutti e tre forti al massimo (zona buona + CRITICAL + ADX alto)
    if good_zone and critical and strong_adx:
        return "PREMIUM"
    # FORTE: zona buona + almeno un altro fattore forte
    if good_zone and (strong_liq or strong_adx):
        return "FORTE"
    # NORMALE: tutto il resto (incluso zona buona da sola)
    return "NORMALE"

# ── Strict trend H4: DISATTIVATO (verificato 30/09) ──
# I dati mostrano che scarta il 37% dei segnali (130/356) che sono
# buoni quanto gli altri: WR 59.2% identico, avgR +0.295, sumR +38.4
# buttati via senza beneficio. TRB usa solo H1, teniamo uguale.
LAB_STRICT_H4 = False

# Stesse costanti di TRB
MAX_RISK_XAU = 25.0
SIGNAL_EXPIRY_HOURS = 6


# ================================================================
# DB — schema identico a trb_signals + campi LAB extra
# ================================================================

_CREATE = """
CREATE TABLE IF NOT EXISTS ote_lab_signals (
    signal_id           TEXT PRIMARY KEY,
    strategy_name       TEXT DEFAULT 'OTE_LAB',
    strategy_version    TEXT DEFAULT '1.0.0',
    asset               TEXT NOT NULL,
    direction           TEXT NOT NULL,
    timestamp_setup     TEXT NOT NULL,
    timestamp_closed    TEXT,
    entry               REAL,
    stop_loss           REAL,
    tp1                 REAL,
    tp2                 REAL,
    risk                REAL,
    rr1                 REAL,
    rr2                 REAL,
    trend_h1            TEXT,
    trend_h4            TEXT,
    adx                 REAL,
    atr_m15             REAL,
    atr_h1              REAL,
    pullback_valid      BOOLEAN DEFAULT 0,
    new_24h_extreme     BOOLEAN DEFAULT 0,
    session             TEXT,
    entry_zone_type     TEXT,
    zone_ref            TEXT,
    flag_adx_ok         BOOLEAN DEFAULT 1,
    flag_trigger_present BOOLEAN DEFAULT 1,
    flag_volatility_ok  BOOLEAN DEFAULT 1,
    flag_sl_widened     BOOLEAN DEFAULT 0,
    liquidity_target    TEXT,
    liquidity_target_price REAL,
    liquidity_priority  TEXT,
    quality_score       INTEGER,
    quality_label       TEXT,
    final_outcome       TEXT DEFAULT 'OPEN',
    result_r            REAL,
    tp1_hit             BOOLEAN DEFAULT 0,
    tp2_hit             BOOLEAN DEFAULT 0,
    mae                 REAL DEFAULT 0,
    mfe                 REAL DEFAULT 0,
    bars_open           INTEGER DEFAULT 0,
    expiry_bars         INTEGER DEFAULT 32,
    timestamp_tp1       TEXT,
    timestamp_tp2       TEXT,
    timestamp_sl        TEXT,
    rz_target_used      BOOLEAN DEFAULT 0,
    rz_target_label     TEXT,
    tp2_original        REAL,
    tier                TEXT,
    accumulation_hours  INTEGER
);
"""

def _init_schema(conn):
    # Fix 30/09: la tabella creata da versioni precedenti manca di
    # colonne (tp1_hit, rr1, zone_ref, ...). CREATE IF NOT EXISTS non
    # aggiorna una tabella gia' esistente. La ricreo una volta con lo
    # schema corretto. I dati vecchi erano comunque contaminati/vuoti.
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(ote_lab_signals)").fetchall()]
        if cols and "tp1_hit" not in cols:
            conn.execute("DROP TABLE ote_lab_signals")
            conn.commit()
            logger.info("OTE-LAB: tabella ricreata con schema aggiornato")
        elif cols and "tier" not in cols:
            conn.execute("ALTER TABLE ote_lab_signals ADD COLUMN tier TEXT")
            conn.commit()
            logger.info("OTE-LAB: colonna tier aggiunta")
        if cols and "accumulation_hours" not in cols:
            try:
                conn.execute("ALTER TABLE ote_lab_signals ADD COLUMN accumulation_hours INTEGER")
                conn.commit()
                logger.info("OTE-LAB: colonna accumulation_hours aggiunta")
            except Exception:
                pass
    except Exception:
        pass
    conn.execute(_CREATE)
    conn.commit()


def _insert_signal(conn, sig: dict) -> str:
    sid = str(uuid.uuid4())
    sig["signal_id"] = sid
    conn.execute("""
        INSERT INTO ote_lab_signals (
            signal_id, asset, direction, timestamp_setup,
            entry, stop_loss, tp1, tp2, risk, rr1, rr2,
            trend_h1, trend_h4, adx, atr_m15, atr_h1,
            pullback_valid, new_24h_extreme, session,
            entry_zone_type, zone_ref,
            flag_adx_ok, flag_trigger_present, flag_volatility_ok, flag_sl_widened,
            liquidity_target, liquidity_target_price, liquidity_priority,
            quality_score, quality_label, expiry_bars,
            rz_target_used, rz_target_label, tp2_original, tier, accumulation_hours
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        sid, sig["asset"], sig["direction"], sig["timestamp_setup"],
        sig.get("entry"), sig.get("stop_loss"), sig.get("tp1"), sig.get("tp2"),
        sig.get("risk"), sig.get("rr1"), sig.get("rr2"),
        sig.get("trend_h1"), sig.get("trend_h4"), sig.get("adx"),
        sig.get("atr_m15"), sig.get("atr_h1"),
        sig.get("pullback_valid", False), sig.get("new_24h_extreme", False),
        sig.get("session"), sig.get("entry_zone_type"), sig.get("zone_ref"),
        sig.get("flag_adx_ok", True), sig.get("flag_trigger_present", True),
        sig.get("flag_volatility_ok", True), sig.get("flag_sl_widened", False),
        sig.get("liquidity_target"), sig.get("liquidity_target_price"),
        sig.get("liquidity_priority"),
        sig.get("quality_score"), sig.get("quality_label"),
        sig.get("expiry_bars", SIGNAL_EXPIRY_HOURS),  # legacy field, now hours
        sig.get("rz_target_used", False), sig.get("rz_target_label"),
        sig.get("tp2_original"), sig.get("tier"), sig.get("accumulation_hours"),
    ))
    conn.commit()
    return sid


# ================================================================
# Monitoraggio — clone di trend_rider_db.monitor_open_trb_signals
# con TRAIL_R=0.5
# ================================================================

def _monitor_open_signals(conn, asset, current_high, current_low, now_iso):
    """
    Clone esatto della logica di monitoraggio TRB (trend_rider_db.py)
    con l'unica differenza: TRAIL_R=0.5 invece di 0.7.
    """
    cur = conn.execute("""
        SELECT signal_id, direction, entry, stop_loss, tp1, tp2,
               tp1_hit, mae, mfe, bars_open, timestamp_setup, risk
        FROM ote_lab_signals
        WHERE asset=? AND final_outcome='OPEN'
    """, (asset,))
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]

    updated = []
    stop_moves = []

    for sig in rows:
        sid = sig["signal_id"]
        d = sig["direction"]
        entry = sig["entry"]
        sl = sig["stop_loss"]  # SL ORIGINALE (non viene mai modificato nel DB)
        tp1 = sig["tp1"]
        tp2 = sig["tp2"]
        old_mae = float(sig["mae"] or 0)
        old_mfe = float(sig["mfe"] or 0)
        tp1_already = bool(sig["tp1_hit"])
        bars = (sig["bars_open"] or 0) + 1
        # Risk ORIGINALE dal DB (immutabile, calcolato all'insert)
        risk = float(sig["risk"]) if sig.get("risk") else abs(entry - sl)
        if risk <= 0:
            continue

        # MFE/MAE
        if d == "BUY":
            adverse = max(entry - current_low, 0)
            favorable = max(current_high - entry, 0)
        else:
            adverse = max(current_high - entry, 0)
            favorable = max(entry - current_low, 0)
        new_mae = max(old_mae, adverse)
        new_mfe = max(old_mfe, favorable)

        # ── TP1 raggiunto? ──
        tp1_hit_now = False
        if not tp1_already and tp1:
            if (d == "BUY" and current_high >= tp1) or (d == "SELL" and current_low <= tp1):
                tp1_hit_now = True
                tp1_already = True
                conn.execute("UPDATE ote_lab_signals SET tp1_hit=1, timestamp_tp1=? WHERE signal_id=?",
                            (now_iso, sid))

        # ── Calcolo stop protettivo ──
        # REGOLA: lo stop puo' SOLO migliorare. Prendiamo il MAX di tutti
        # i livelli di protezione. NON aggiorniamo stop_loss nel DB
        # (serve per il calcolo risk che deve restare immutabile).
        trail_dist = LAB_TRAIL_R * risk
        min_lock = LAB_TRAIL_MIN_LOCK.get(asset, 0)
        trail_active = False
        be_active = False

        # Livello 1: SL originale
        effective_sl = sl

        # Livello 2: Break-even+ dopo TP1
        if tp1_already:
            be_active = True
            be_level = entry + BE_PLUS_R * risk if d == "BUY" else entry - BE_PLUS_R * risk
            effective_sl = max(effective_sl, be_level) if d == "BUY" else min(effective_sl, be_level)

        # Livello 3: Trailing (il piu' alto)
        if new_mfe >= trail_dist:
            lock = min(max(new_mfe - trail_dist, min_lock), new_mfe)
            trail_level = entry + lock if d == "BUY" else entry - lock
            effective_sl = max(effective_sl, trail_level) if d == "BUY" else min(effective_sl, trail_level)
            trail_active = True

        # ── Notifica SOLO quando lo stop MIGLIORA rispetto al ciclo prima ──
        # Ricalcolo lo stop del CICLO PRECEDENTE con old_mfe
        prev_eff = sl
        if tp1_already and not tp1_hit_now:  # BE era gia' attivo al ciclo prima
            prev_be = entry + BE_PLUS_R * risk if d == "BUY" else entry - BE_PLUS_R * risk
            prev_eff = max(prev_eff, prev_be) if d == "BUY" else min(prev_eff, prev_be)
        if old_mfe >= trail_dist:
            prev_lock = min(max(old_mfe - trail_dist, min_lock), old_mfe)
            prev_trail = entry + prev_lock if d == "BUY" else entry - prev_lock
            prev_eff = max(prev_eff, prev_trail) if d == "BUY" else min(prev_eff, prev_trail)

        sl_improved = (d == "BUY" and effective_sl > prev_eff + 0.01) or \
                      (d == "SELL" and effective_sl < prev_eff - 0.01)

        if sl_improved or tp1_hit_now:
            if tp1_hit_now:
                event = "TP1_HIT_BE"
            elif trail_active:
                event = "TRAIL_ACTIVATED"
            else:
                event = "STOP_IMPROVED"
            stop_moves.append({
                "signal_id": sid, "asset": asset, "direction": d,
                "event": event, "new_stop": round(effective_sl, 5),
            })

        # Check esiti
        if d == "BUY":
            sl_hit = current_low <= effective_sl
            tp2_hit = tp2 is not None and current_high >= tp2
        else:
            sl_hit = current_high >= effective_sl
            tp2_hit = tp2 is not None and current_low <= tp2

        outcome = None
        result_r = None

        # PRIORITA' DEGLI ESITI (importante per non tagliare i win grandi):
        # 1. TP2 pieno — sempre la priorita' massima. Se il prezzo arriva
        #    al target, prendiamo il movimento pieno anche se nella stessa
        #    candela ha toccato TP1. (Bug trovato nel double-check: prima
        #    un trade che andava dritto a TP2 veniva chiuso a +1R.)
        # 2. Trailing — se attivo, cattura il profitto raggiunto (puo'
        #    essere molto > 1R se il prezzo e' salito parecchio).
        # 3. Break-even dopo TP1 — solo se ne' TP2 ne' trailing: chiude a
        #    +1R garantito (il minimo dopo aver toccato TP1).
        # 4. SL pieno — solo se nulla di sopra e il prezzo va contro.
        if tp2_hit:
            result_r = sig.get("rr2") or round(abs(tp2 - entry) / risk, 3)
            outcome = "TP2_HIT"
        elif sl_hit:
            if trail_active:
                lock_val = max(new_mfe - trail_dist, min_lock)
                lock_val = min(lock_val, new_mfe)
                trail_r = round(lock_val / risk, 3)
                if be_active and trail_r < BE_PLUS_R:
                    result_r = BE_PLUS_R
                    outcome = "TP1_HIT"
                else:
                    result_r = trail_r
                    outcome = "TRAIL_HIT"
            elif be_active:
                result_r = BE_PLUS_R
                outcome = "TP1_HIT"
            else:
                result_r = -1.0
                outcome = "SL_HIT"
        # ── Expiry basato su ore reali (fix 11/10/2026) ──
        else:
            try:
                ts_setup = sig.get("timestamp_setup", "")
                if ts_setup:
                    t_open = datetime.fromisoformat(ts_setup.replace("Z", "+00:00"))
                    t_now = datetime.fromisoformat(now_iso.replace("Z", "+00:00"))
                    hours_open = (t_now - t_open).total_seconds() / 3600
                else:
                    hours_open = 0
            except Exception:
                hours_open = 0

            if hours_open >= SIGNAL_EXPIRY_HOURS:
                if trail_active:
                    lock_val = max(new_mfe - trail_dist, min_lock)
                    lock_val = min(lock_val, new_mfe)
                    trail_r = round(lock_val / risk, 3)
                    if be_active and trail_r < BE_PLUS_R:
                        result_r = BE_PLUS_R
                        outcome = "TP1_HIT"
                    else:
                        result_r = trail_r
                        outcome = "TRAIL_HIT"
                elif be_active:
                    result_r = BE_PLUS_R
                    outcome = "TP1_HIT"
                else:
                    result_r = 0
                    outcome = "EXPIRED"

        if outcome:
            conn.execute("""
                UPDATE ote_lab_signals
                SET final_outcome=?, result_r=?, mae=?, mfe=?, bars_open=?,
                    timestamp_closed=?
                WHERE signal_id=?
            """, (outcome, result_r, new_mae, new_mfe, bars, now_iso, sid))
            if outcome == "SL_HIT":
                conn.execute("UPDATE ote_lab_signals SET timestamp_sl=? WHERE signal_id=?", (now_iso, sid))
            elif outcome == "TP2_HIT":
                conn.execute("UPDATE ote_lab_signals SET tp2_hit=1, timestamp_tp2=? WHERE signal_id=?", (now_iso, sid))
            conn.commit()
            updated.append({"signal_id": sid, "outcome": outcome, "result_r": result_r,
                           "mae": new_mae, "mfe": new_mfe, "bars_open": bars,
                           "asset": asset, "direction": d})
        else:
            conn.execute("UPDATE ote_lab_signals SET mae=?, mfe=?, bars_open=? WHERE signal_id=?",
                        (new_mae, new_mfe, bars, sid))
            conn.commit()

    return updated, stop_moves


# ================================================================
# MOD LAB 3: Reaction zone come target fallback
# ================================================================

def _find_rz_target(conn, asset, direction, entry, risk):
    """Fallback TP: prima zona operativa nella direzione del trade."""
    try:
        row = conn.execute(
            "SELECT snapshot_json FROM reaction_map_snapshots "
            "WHERE asset=? ORDER BY timestamp_snapshot DESC LIMIT 1",
            (asset,)).fetchone()
        if not row: return None
        zones = json.loads(row[0]).get("zones", [])
    except Exception:
        return None
    MIN_RR = 1.2
    max_dist_pct = 0.02 if asset == "XAU_USD" else 0.03
    cands = []
    for z in zones:
        mid = z.get("zone_midpoint", 0)
        if mid <= 0: continue
        if direction == "BUY" and mid <= entry: continue
        if direction == "SELL" and mid >= entry: continue
        dist_pct = abs(mid - entry) / entry if entry > 0 else 0
        if dist_pct > max_dist_pct: continue
        rr = abs(mid - entry) / risk if risk > 0 else 0
        if rr < MIN_RR: continue
        cands.append({"price": mid,
                       "label": f"RZ_{z.get('reaction_strength','?')}",
                       "rr": round(rr, 3),
                       "dist": abs(mid - entry)})
    return min(cands, key=lambda c: c["dist"]) if cands else None


# ================================================================
# Notifiche — identiche a TRB
# ================================================================

def _notify_signal(signal, config):
    try:
        from notifications import telegram_bot, ntfy_bot

        quality = signal.get("quality_label", "")
        if quality == "LOW":
            return

        direction = signal["direction"]
        asset = signal["asset"]
        emoji = "🟢" if direction == "BUY" else "🔴"

        def fp(v):
            if v is None: return "N/A"
            return f"{v:,.2f}" if float(v) > 1000 else f"{v:.4f}"

        rz_tag = " [RZ]" if signal.get("rz_target_used") else ""

        text = (
            f"{emoji} *OTE-LAB v1.0*\n\n"
            f"*{asset.replace('_',' ')}* — {direction}\n\n"
            f"Score: *{signal.get('quality_score',0)}* ({quality})\n"
            f"Trend H1: {signal.get('trend_h1','N/A')} | H4: {signal.get('trend_h4','N/A')}\n"
            f"ADX: {signal.get('adx',0):.1f}\n\n"
            f"Entry:  `{fp(signal.get('entry'))}`\n"
            f"SL:     `{fp(signal.get('stop_loss'))}`\n"
            f"TP1:    `{fp(signal.get('tp1'))}` (1R)\n"
            f"TP2:    `{fp(signal.get('tp2'))}` ({signal.get('rr2',0):.2f}R){rz_tag}\n\n"
            f"Target: {signal.get('liquidity_target','N/A')}\n"
            f"Livello: {signal.get('tier','NORMALE')}\n"
            f"Trail: {LAB_TRAIL_R}R | Session: {signal.get('session','N/A')}\n"
            f"⚠️ LAB — solo raccolta dati"
        )

        bot_token = config.get("TELEGRAM_BOT_TOKEN", "")
        chat_id = config.get("TELEGRAM_CHAT_ID", "")
        ntfy_topic = config.get("NTFY_TOPIC", "")

        if bot_token and chat_id:
            telegram_bot.send_message(bot_token, chat_id, text)

        # Invio JSON strutturato su ntfy per EA MT5
        if ntfy_topic:
            import json as _json
            expiry_h = 24 if signal.get("tier") in ("FORTE", "NORMALE") else 8
            payload = _json.dumps({
                "signal_id": signal.get("signal_id", str(uuid.uuid4())),
                "strategy_name": "OTE-LAB",
                "asset": signal["asset"],
                "direction": signal["direction"],
                "entry": signal["entry"],
                "stop_loss": signal["stop_loss"],
                "tp1": signal.get("tp1"),
                "tp2": signal.get("tp2"),
                "tier": signal.get("tier", "NORMALE"),
                "expiry_hours": expiry_h,
            })
            ntfy_bot.send_message(ntfy_topic, f"OTE-LAB {signal['asset']} {signal['direction']}", payload)
    except Exception as e:
        logger.warning("OTE-LAB _notify: %s", e)


def _notify_outcome(sig_info, config):
    try:
        from notifications import telegram_bot
        bot_token = config.get("TELEGRAM_BOT_TOKEN", "")
        chat_id = config.get("TELEGRAM_CHAT_ID", "")
        if not bot_token or not chat_id: return
        r = sig_info.get("result_r", 0)
        emoji = "✅" if r and r > 0 else "❌" if r and r < 0 else "⏱"
        text = (f"{emoji} OTE-LAB | {sig_info['asset']} {sig_info['direction']}\n"
                f"Esito: {sig_info['outcome']} ({r:+.2f}R)\n"
                f"⚠️ LAB — solo raccolta dati")
        telegram_bot.send_message(bot_token, chat_id, text)
    except Exception as e:
        logger.warning("OTE-LAB outcome notify: %s", e)


def _notify_stop_move(sp, config):
    try:
        from notifications import telegram_bot, ntfy_bot
        asset = sp["asset"]
        direction = sp["direction"]
        event = sp.get("event", "TRAIL_ACTIVATED")
        emoji = "🟢" if direction == "BUY" else "🔴"
        def fp(v):
            if v is None: return "N/A"
            return f"{v:,.2f}" if float(v) > 1000 else f"{v:.4f}"
        if event == "TP1_HIT_BE":
            titolo = "OTE-LAB — TP1 RAGGIUNTO ✅ (SL a Break-Even)"
            tag = "TP1/BE"
        else:
            titolo = "OTE-LAB — TRAILING STOP"
            tag = "TRAIL"
        text = (f"{emoji} *{titolo}*\n\n"
                f"*{asset.replace('_',' ')}* — {direction}\n"
                f"Nuovo stop: `{fp(sp['new_stop'])}`\n"
                f"⚠️ LAB — solo raccolta dati")
        bot_token = config.get("TELEGRAM_BOT_TOKEN", "")
        chat_id = config.get("TELEGRAM_CHAT_ID", "")
        if bot_token and chat_id:
            telegram_bot.send_message(bot_token, chat_id, text)

        # Invio JSON strutturato per EA MT5 — modifica SL
        ntfy_topic = config.get("NTFY_TOPIC", "")
        if ntfy_topic:
            import json as _json
            payload = _json.dumps({
                "action": "MODIFY_SL",
                "signal_id": sp.get("signal_id", ""),
                "strategy_name": "OTE-LAB",
                "asset": asset,
                "direction": direction,
                "new_stop": sp["new_stop"],
                "event": event,
            })
            ntfy_bot.send_message(ntfy_topic, f"OTE-LAB {asset} {direction} | {titolo}", payload)
    except Exception as e:
        logger.warning("OTE-LAB stop_move notify: %s", e)


# ================================================================
# Session helper (identico a TRB)
# ================================================================

def _get_session(now):
    h = now.hour
    if 0 <= h < 8:   return "ASIA"
    if 8 <= h < 13:  return "LONDON"
    if 13 <= h < 17: return "OVERLAP"
    if 17 <= h < 22: return "NEW_YORK"
    return "ASIA"


# ================================================================
# Per-asset scan — clone di TRB _run_for_asset con le 3 mod
# ================================================================

def _run_for_asset(conn, asset, config, market_ctx, now):
    logger.info("OTE-LAB: inizio ciclo per %s", asset)

    df_h4 = core_db.get_candles_df(conn, asset, "4h", limit=200)
    df_h1 = core_db.get_candles_df(conn, asset, "1h", limit=300)
    df_m15 = v3_db.get_v3_candles_df(conn, asset, "15m", limit=100)

    if df_h1 is None or len(df_h1) < 60:
        return
    if df_m15 is None or len(df_m15) < 25:
        return

    # ── Monitoraggio segnali aperti ──
    try:
        last_m15 = df_m15.iloc[-1]
        updated, stop_moves = _monitor_open_signals(
            conn, asset,
            current_high=float(last_m15["high"]),
            current_low=float(last_m15["low"]),
            now_iso=now.isoformat(),
        )
        for upd in updated:
            logger.info("OTE-LAB Monitor [%s]: %s → %s (%.2fR)",
                        asset, upd["signal_id"][:8], upd["outcome"],
                        upd.get("result_r", 0))
        for sp in stop_moves:
            logger.info("OTE-LAB Stop [%s]: %s → %s stop=%.4f",
                        asset, sp["signal_id"][:8], sp["event"], sp["new_stop"])
            _notify_stop_move(sp, config)
    except Exception as e:
        logger.error("OTE-LAB Monitor [%s]: errore: %s", asset, e)

    # ── MIE context (da edge_lab_runner) ──
    mie_context = market_ctx.get("mie_context", {})

    # ── Reaction Map per TP barrier check (v1.1) ──
    if "reaction_map" not in market_ctx:
        try:
            row = conn.execute(
                "SELECT snapshot_json FROM reaction_map_snapshots "
                "WHERE asset = ? ORDER BY timestamp_snapshot DESC LIMIT 1",
                (asset,)
            ).fetchone()
            if row and row[0]:
                market_ctx["reaction_map"] = json.loads(row[0])
        except Exception as e:
            logger.debug("OTE-LAB [%s]: reaction_map non disponibile: %s", asset, e)

    # ── Zone gia' aperte ──
    open_refs = set()
    try:
        cur = conn.execute(
            "SELECT zone_ref FROM ote_lab_signals WHERE asset=? AND final_outcome='OPEN'",
            (asset,))
        open_refs = {r[0] for r in cur.fetchall() if r[0]}
    except Exception:
        pass
    market_ctx["open_zone_refs"] = open_refs

    # ── Genera segnali BUY e SELL ──
    for direction in ("BUY", "SELL"):

        # Dedup: gia' aperto
        existing = conn.execute(
            "SELECT 1 FROM ote_lab_signals WHERE asset=? AND direction=? AND final_outcome='OPEN'",
            (asset, direction)).fetchone()
        if existing:
            continue

        # Cooldown 4h
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=4)).isoformat()
        recent = conn.execute(
            "SELECT 1 FROM ote_lab_signals WHERE asset=? AND direction=? "
            "AND timestamp_setup > ?", (asset, direction, cutoff)).fetchone()
        if recent:
            continue

        # Genera segnale (identico a TRB)
        try:
            result = generate_trb_signal(market_ctx, df_h4, df_h1, df_m15, direction)
        except Exception as e:
            logger.error("OTE-LAB [%s %s]: errore generazione: %s", asset, direction, e)
            continue

        signal = result["signal"]
        diag = result["diagnostics"]

        if signal is None:
            reason = diag.get("rejection", "UNKNOWN")
            logger.info("OTE-LAB [%s %s]: no signal — %s", asset, direction, reason)
            continue

        # ── Filtri statistici (identici a TRB) ──
        current_session = _get_session(now)
        signal["session"] = signal.get("session", current_session)

        if signal.get("session") == "OVERLAP":
            logger.info("OTE-LAB [%s %s]: REJECT OVERLAP", asset, direction)
            continue

        entry = signal.get("entry", 0)
        sl = signal.get("stop_loss", 0)
        if entry and sl:
            risk_pct = abs(entry - sl) / entry
            if risk_pct < 0.002:
                logger.info("OTE-LAB [%s %s]: REJECT RISK_TOO_TIGHT", asset, direction)
                continue

        # Risk cap XAU (identico a TRB)
        if asset in ("XAU_USD", "PAXG_USDT") and entry and sl:
            orig_risk = abs(entry - sl)
            if orig_risk > MAX_RISK_XAU:
                rr1 = signal.get("rr1", 1.0)
                rr2 = signal.get("rr2", 2.0)
                if direction == "BUY":
                    signal["stop_loss"] = entry - MAX_RISK_XAU
                    signal["tp1"] = entry + MAX_RISK_XAU * rr1
                    signal["tp2"] = entry + MAX_RISK_XAU * rr2
                else:
                    signal["stop_loss"] = entry + MAX_RISK_XAU
                    signal["tp1"] = entry - MAX_RISK_XAU * rr1
                    signal["tp2"] = entry - MAX_RISK_XAU * rr2
                signal["risk"] = MAX_RISK_XAU

        # Filtro ema (identico a TRB)
        if signal.get("entry_zone_type") == "ema":
            logger.info("OTE-LAB [%s %s]: REJECT EMA", asset, direction)
            continue

        # ══════════════════════════════════════════════════════
        # ── MOD LAB 2: Strict trend H4 ───────────────────────
        # ══════════════════════════════════════════════════════
        if LAB_STRICT_H4:
            h4 = signal.get("trend_h4")
            if h4:
                if direction == "BUY" and h4 == "BEARISH":
                    logger.info("OTE-LAB [%s %s]: REJECT STRICT_H4 (%s)", asset, direction, h4)
                    continue
                if direction == "SELL" and h4 == "BULLISH":
                    logger.info("OTE-LAB [%s %s]: REJECT STRICT_H4 (%s)", asset, direction, h4)
                    continue

        # ══════════════════════════════════════════════════════
        # ── MOD LAB 4: REACTION ENTRY ────────────────────────
        # Simulazione su 350 trade: WR 56.9%→68.9%, sumR +309→+376
        # XAU: WR 59.6%→82.0%, risk medio -70%
        #
        # Logica: TREND → ZONA → REAZIONE → ENTRY
        # TRB ha trovato la zona e la direzione. Invece di entrare
        # subito, cerchiamo una candela M5 di reazione nella zona
        # (body >30% del range, nella direzione giusta).
        # Se trovata: entriamo al close, SL sotto il minimo della
        # candela di reazione → SL molto piu' stretto.
        # Se non trovata: NON entriamo (aspettiamo il prossimo ciclo).
        # ══════════════════════════════════════════════════════
        df_m5 = v3_db.get_v3_candles_df(conn, asset, "5m", limit=20)
        if df_m5 is not None and len(df_m5) >= 3:
            orig_entry = signal["entry"]
            orig_sl = signal["stop_loss"]
            orig_risk = abs(orig_entry - orig_sl)
            
            # Zona approssimata attorno all'entry TRB
            if direction == "BUY":
                zone_high = orig_entry + orig_risk * 0.3
                zone_low = orig_entry - orig_risk * 0.5
            else:
                zone_low = orig_entry - orig_risk * 0.3
                zone_high = orig_entry + orig_risk * 0.5
            
            # Cerca reazione nelle ultime 5 candele M5
            reaction_found = False
            for _, c in df_m5.iloc[-5:].iterrows():
                body = float(c["close"]) - float(c["open"])
                rng = float(c["high"]) - float(c["low"])
                if rng <= 0:
                    continue
                
                if direction == "BUY":
                    in_zone = float(c["low"]) <= zone_high
                    is_reaction = body > 0 and body / rng > 0.3
                    if in_zone and is_reaction:
                        new_entry = float(c["close"])
                        new_sl = min(float(c["low"]), zone_low) - rng * 0.2
                        # Floor/cap XAU
                        new_risk = abs(new_entry - new_sl)
                        if asset == "XAU_USD":
                            if new_risk < 8.0:
                                new_sl = new_entry - 8.0
                                new_risk = 8.0
                            elif new_risk > MAX_RISK_XAU:
                                new_sl = new_entry - MAX_RISK_XAU
                                new_risk = MAX_RISK_XAU
                        if new_risk > 0:
                            rr2_val = signal.get("rr2") or 2.0
                            signal["entry"] = round(new_entry, 5)
                            signal["stop_loss"] = round(new_sl, 5)
                            signal["risk"] = round(new_risk, 5)
                            signal["tp1"] = round(new_entry + new_risk, 5)
                            signal["tp2"] = round(new_entry + new_risk * rr2_val, 5)
                            signal["rr1"] = 1.0
                            signal["rr2"] = rr2_val
                            reaction_found = True
                            logger.info("OTE-LAB [%s %s]: REACTION ENTRY %.2f (orig %.2f) risk %.1f (orig %.1f)",
                                       asset, direction, new_entry, orig_entry, new_risk, orig_risk)
                        break
                else:  # SELL
                    in_zone = float(c["high"]) >= zone_low
                    is_reaction = body < 0 and abs(body) / rng > 0.3
                    if in_zone and is_reaction:
                        new_entry = float(c["close"])
                        new_sl = max(float(c["high"]), zone_high) + rng * 0.2
                        new_risk = abs(new_entry - new_sl)
                        if asset == "XAU_USD":
                            if new_risk < 8.0:
                                new_sl = new_entry + 8.0
                                new_risk = 8.0
                            elif new_risk > MAX_RISK_XAU:
                                new_sl = new_entry + MAX_RISK_XAU
                                new_risk = MAX_RISK_XAU
                        if new_risk > 0:
                            rr2_val = signal.get("rr2") or 2.0
                            signal["entry"] = round(new_entry, 5)
                            signal["stop_loss"] = round(new_sl, 5)
                            signal["risk"] = round(new_risk, 5)
                            signal["tp1"] = round(new_entry - new_risk, 5)
                            signal["tp2"] = round(new_entry - new_risk * rr2_val, 5)
                            signal["rr1"] = 1.0
                            signal["rr2"] = rr2_val
                            reaction_found = True
                            logger.info("OTE-LAB [%s %s]: REACTION ENTRY %.2f (orig %.2f) risk %.1f (orig %.1f)",
                                       asset, direction, new_entry, orig_entry, new_risk, orig_risk)
                        break
            
            if not reaction_found:
                logger.info("OTE-LAB [%s %s]: SKIP no reaction in zone (waiting)", asset, direction)
                continue

        # ══════════════════════════════════════════════════════
        # ── MOD LAB 3: Reaction zone come target fallback ────
        # ══════════════════════════════════════════════════════
        if signal.get("liquidity_priority") is None:
            risk_val = signal.get("risk", 0)
            if risk_val > 0:
                rz = _find_rz_target(conn, asset, direction, entry, risk_val)
                if rz:
                    signal["tp2_original"] = signal.get("tp2")
                    # Cap RR2 a 4R: se la RZ e' piu' lontana, taglio il TP
                    rz_rr = rz["rr"]
                    if rz_rr > MAX_RR2:
                        rz_rr = MAX_RR2
                        if direction == "BUY":
                            signal["tp2"] = entry + MAX_RR2 * risk_val
                        else:
                            signal["tp2"] = entry - MAX_RR2 * risk_val
                    else:
                        signal["tp2"] = rz["price"]
                    signal["rr2"] = round(rz_rr, 3)
                    signal["liquidity_target"] = rz["label"]
                    signal["rz_target_used"] = True
                    signal["rz_target_label"] = rz["label"]
                    logger.info("OTE-LAB [%s %s]: RZ target %s RR=%.2f",
                               asset, direction, rz["label"], rz_rr)

        # ── Classifica tier (tracking puro) ──
        signal["tier"] = _classify_tier(signal)

        # ── Inserisci segnale ──
        signal["timestamp_setup"] = now.isoformat()
        try:
            signal_id = _insert_signal(conn, signal)
        except Exception as e:
            logger.error("OTE-LAB [%s %s]: errore insert: %s", asset, direction, e)
            continue

        logger.info(
            "OTE-LAB [%s %s]: SEGNALE entry=%.4f sl=%.4f tp2=%.4f "
            "rr=%.2f zone=%s target=%s rz=%s (id=%s)",
            asset, direction,
            signal.get("entry",0), signal.get("stop_loss",0),
            signal.get("tp2",0), signal.get("rr2",0),
            signal.get("entry_zone_type","?"),
            signal.get("liquidity_target","N/A"),
            "YES" if signal.get("rz_target_used") else "no",
            signal_id,
        )

        _notify_signal(signal, config)

    # ── RECURRING_ZONE signals ──
    try:
        _generate_recurring_signals(conn, asset, df_h4, df_h1, df_m15, config)
    except Exception as e:
        logger.error("OTE-LAB [%s]: recurring zone error: %s", asset, e)


# ================================================================
# BOUNCE POINT — Reaction Map alignment per RECURRING
# Dati: ALIGNED 75% WR vs AGAINST 37.5% (n=16, Oct 10-12)
# La reaction_map produce zone con expected_reaction (BOUNCE_UP/DOWN).
# Se la zona piu' vicina al prezzo prevede un rimbalzo CONTRO la
# direzione del trade, il trade ha probabilita' molto piu' basse.
# ================================================================

def _check_bounce_point(conn, asset, direction, current_price):
    """
    Legge la reaction_map piu' recente e controlla se una zona vicina
    al prezzo ha expected_reaction coerente con la direzione del trade.

    Ritorna:
      "ALIGNED"  — zona vicina conferma la direzione (BOUNCE_UP + BUY, BOUNCE_DOWN + SELL)
      "AGAINST"  — zona vicina contraddice la direzione → SKIP
      "NEUTRAL"  — nessuna zona vicina o reaction UNKNOWN → procedi
    """
    try:
        row = conn.execute(
            "SELECT snapshot_json FROM reaction_map_snapshots "
            "WHERE asset=? ORDER BY timestamp_snapshot DESC LIMIT 1",
            (asset,)).fetchone()
    except Exception:
        return "NEUTRAL"

    if not row:
        return "NEUTRAL"

    try:
        rm = __import__("json").loads(row[0])
    except Exception:
        return "NEUTRAL"

    zones = rm.get("zones", [])
    if not zones:
        return "NEUTRAL"

    # Trova la zona piu' vicina al prezzo corrente
    best_zone = None
    best_dist = float("inf")
    for z in zones:
        mid = z.get("zone_midpoint", 0)
        if mid <= 0:
            continue
        dist = abs(mid - current_price)
        if dist < best_dist:
            best_dist = dist
            best_zone = z

    if not best_zone:
        return "NEUTRAL"

    # Proximity check: la zona deve essere entro il 2% del prezzo
    if current_price > 0 and (best_dist / current_price) > 0.02:
        return "NEUTRAL"

    exp = best_zone.get("expected_reaction", "UNKNOWN")
    score = best_zone.get("confluence_score", 0)

    # Soglia minima: solo zone con score sufficiente contano
    if score < 30:
        return "NEUTRAL"

    if exp == "UNKNOWN":
        return "NEUTRAL"

    if (direction == "BUY" and exp == "BOUNCE_UP") or \
       (direction == "SELL" and exp == "BOUNCE_DOWN"):
        return "ALIGNED"
    else:
        return "AGAINST"


def _bounce_point_sl_tp(conn, asset, direction, entry, sl_current, risk_current):
    """
    v2.0 — Zone strutturali: il trade context determina l'interpretazione.

    Le zone della Reaction Map sono livelli strutturali dove il prezzo
    ha alta probabilita' di reagire (validato: 77-97% raggiungimento
    per zone entro 0.5%). Non ci affidiamo alla predicted reaction
    (BOUNCE_UP/DOWN) perche' la sua accuracy e' insufficiente (40-64%).

    Logica:
      - TP → prima zona significativa nella direzione del trade.
             BUY: prima zona sopra entry (resistenza strutturale).
             SELL: prima zona sotto entry (supporto strutturale).
             Qualsiasi zona con score sufficiente e' un potenziale ostacolo.
             TP al bordo vicino della zona (dove il prezzo incontra resistenza).
      - SL → oltre la prima zona significativa dietro l'entry.
             BUY: oltre la zona piu' vicina sotto entry (se rotta = invalidazione).
             SELL: oltre la zona piu' vicina sopra entry (se rotta = invalidazione).
             Se lo SL strutturale supera il risk massimo → trade scartato (None).

    Filtro distanza: zone oltre MAX_DIST_PCT vengono ignorate (irraggiungibili).

    Returns: dict {"tp": float|None, "sl": float|None, "tp_zone": dict|None,
                   "sl_zone": dict|None, "tp_label": str, "sl_label": str}
    """
    result = {"tp": None, "sl": None, "tp_zone": None, "sl_zone": None,
              "tp_label": "", "sl_label": ""}

    try:
        row = conn.execute(
            "SELECT snapshot_json FROM reaction_map_snapshots "
            "WHERE asset=? ORDER BY timestamp_snapshot DESC LIMIT 1",
            (asset,)).fetchone()
    except Exception:
        return result

    if not row:
        return result

    try:
        rm = __import__("json").loads(row[0])
    except Exception:
        return result

    zones = rm.get("zones", [])
    if not zones:
        return result

    # ── Parametri ──
    MIN_TP_SCORE = 15     # zona deve avere un minimo di confluenza
    MIN_SL_SCORE = 25     # per SL un po' piu' alta (deve reggere)
    MAX_TP_RR = 5.0       # cap TP: non oltre 5R
    MAX_SL_MULT = 1.5     # SL non oltre 1.5x il risk corrente
    MAX_DIST_PCT = 0.03   # ignora zone oltre 3% di distanza (BTC)
    if asset == "XAU_USD":
        MAX_DIST_PCT = 0.02  # XAU: zone oltre 2% irraggiungibili

    # ── Filtra zone per distanza operativa ──
    operative_zones = []
    for z in zones:
        mid = z.get("zone_midpoint", 0)
        score = z.get("confluence_score", 0)
        if mid <= 0 or score < min(MIN_TP_SCORE, MIN_SL_SCORE):
            continue
        dist_pct = abs(mid - entry) / entry if entry > 0 else 0
        if dist_pct > MAX_DIST_PCT:
            continue
        operative_zones.append(z)

    if not operative_zones:
        return result

    # ── TP: prima zona nella direzione del trade ──
    # BUY → prima zona sopra entry = resistenza/ostacolo
    # SELL → prima zona sotto entry = supporto/ostacolo
    tp_zone = None
    tp_dist = float("inf")
    for z in operative_zones:
        score = z.get("confluence_score", 0)
        if score < MIN_TP_SCORE:
            continue
        mid = z.get("zone_midpoint", 0)

        if direction == "BUY" and mid > entry:
            # TP al bordo vicino della zona (zone_low = dove il prezzo incontra prima resistenza)
            target = z.get("zone_low", mid)
            d = target - entry
            if d >= risk_current and d / risk_current <= MAX_TP_RR and d < tp_dist:
                tp_dist = d
                tp_zone = z
        elif direction == "SELL" and mid < entry:
            # TP al bordo vicino della zona (zone_high = dove il prezzo incontra prima supporto)
            target = z.get("zone_high", mid)
            d = entry - target
            if d >= risk_current and d / risk_current <= MAX_TP_RR and d < tp_dist:
                tp_dist = d
                tp_zone = z

    if tp_zone:
        if direction == "BUY":
            result["tp"] = tp_zone["zone_low"]   # bordo inferiore della zona sopra
        else:
            result["tp"] = tp_zone["zone_high"]   # bordo superiore della zona sotto
        result["tp_zone"] = tp_zone
        score = tp_zone["confluence_score"]
        strength = tp_zone.get("reaction_strength", "?")
        sources = tp_zone.get("sources", [])
        n_sources = len(sources)
        result["tp_label"] = f"RZ_{strength}_{score:.0f}_x{n_sources}"
        logger.info("OTE-LAB [%s %s]: TP structural zone → %.2f "
                    "(score=%.0f, %s, %d sources, dist=%.2f)",
                    asset, direction, result["tp"], score, strength,
                    n_sources, tp_dist)

    # ── SL: oltre la prima zona dietro l'entry (invalidazione strutturale) ──
    # BUY → zona piu' vicina sotto entry. Se rotta → struttura invalida
    # SELL → zona piu' vicina sopra entry. Se rotta → struttura invalida
    sl_zone = None
    sl_dist = float("inf")
    max_sl_reach = risk_current * MAX_SL_MULT

    for z in operative_zones:
        score = z.get("confluence_score", 0)
        if score < MIN_SL_SCORE:
            continue

        if direction == "BUY":
            edge = z.get("zone_low", 0)    # bordo esterno: se rotto, game over
            if 0 < edge < entry:
                d = entry - edge
                if d <= max_sl_reach and d < sl_dist:
                    sl_dist = d
                    sl_zone = z
        else:
            edge = z.get("zone_high", 0)   # bordo esterno: se rotto, game over
            if edge > entry:
                d = edge - entry
                if d <= max_sl_reach and d < sl_dist:
                    sl_dist = d
                    sl_zone = z

    if sl_zone:
        buffer = 2.0 if asset == "XAU_USD" else 30.0
        if direction == "BUY":
            result["sl"] = sl_zone["zone_low"] - buffer
        else:
            result["sl"] = sl_zone["zone_high"] + buffer
        result["sl_zone"] = sl_zone
        score = sl_zone["confluence_score"]
        strength = sl_zone.get("reaction_strength", "?")
        result["sl_label"] = f"RZ_{strength}_{score:.0f}"
        logger.info("OTE-LAB [%s %s]: SL structural zone → %.2f "
                    "(score=%.0f, %s, beyond zone edge)",
                    asset, direction, result["sl"], score, strength)

    return result


# ================================================================
# RECURRING_ZONE — zone con 5+ visite storiche H4+H1
# Simulazione su 753 trade: WR 56.4%, avgR +0.270, sumR +194.1
# ================================================================

def _calc_accumulation_hours(conn, asset, zone_price, bucket_size):
    """
    Wyckoff Causa-Effetto: quante candele H1 nelle ultime 48h hanno
    chiuso nella fascia della zona? Piu' tempo = accumulazione vera.
    
    Verificato 05/10 su 61 RECURRING:
      >9h: WR 62%, sumR +8.8
      <=9h: WR 47%, sumR +0.1
    """
    half = bucket_size / 2
    try:
        cur = conn.execute(
            "SELECT close FROM candles_cache WHERE asset=? AND timeframe='1h' "
            "ORDER BY timestamp DESC LIMIT 48", (asset,))
        closes = [r[0] for r in cur.fetchall()]
        return sum(1 for c in closes if zone_price - half <= c <= zone_price + half)
    except Exception:
        return 0

RECURRING_PROXIMITY = {"XAU_USD": 15.0, "BTC_USDT": 200.0}
MIN_ZONE_VISITS = 5

def _find_recurring_zones(conn, asset, current_price, proximity_filter=True):
    """Trova zone ricorrenti dove il prezzo ha reagito piu' volte.

    Scansiona swing points su H4 (da lh_swing_zones), H1 (da candles_cache)
    e M15 (da v3_candles_cache). I pesi sono: H4×3, H1×1, M15×0.5.

    Args:
        proximity_filter: Se True applica il filtro di prossimita' (per
            segnali RECURRING). Se False restituisce tutte le zone qualificate
            (per integrazione nella Reaction Map).
    """
    from collections import Counter
    bucket_size = 10.0 if asset == "XAU_USD" else 500.0

    # ── H4 swings (da lh_swing_zones) ───────────────────────
    h4_buckets = Counter()
    h4_types = {}
    try:
        cur = conn.execute("SELECT swing_type, price FROM lh_swing_zones WHERE asset=? AND timeframe='H4'", (asset,))
        for st, p in cur.fetchall():
            b = round(p / bucket_size) * bucket_size
            h4_buckets[b] += 1
            h4_types[b] = st
    except Exception:
        pass

    # ── H1 swings (calcolati on-the-fly) ────────────────────
    h1_buckets = Counter()
    h1_types = {}
    try:
        cur = conn.execute("SELECT high, low FROM candles_cache WHERE asset=? AND timeframe='1h' ORDER BY timestamp", (asset,))
        candles = cur.fetchall()
        lb = 3
        for i in range(lb, len(candles)-lb):
            h, l = candles[i]
            if all(h >= candles[j][0] for j in range(i-lb,i)) and all(h >= candles[j][0] for j in range(i+1,i+lb+1)):
                b = round(h / bucket_size) * bucket_size
                h1_buckets[b] += 1
                h1_types[b] = "HIGH"
            if all(l <= candles[j][1] for j in range(i-lb,i)) and all(l <= candles[j][1] for j in range(i+1,i+lb+1)):
                b = round(l / bucket_size) * bucket_size
                h1_buckets[b] += 1
                h1_types[b] = "LOW"
    except Exception:
        pass

    # ── M15 swings (da v3_candles_cache) ────────────────────
    m15_buckets = Counter()
    m15_types = {}
    try:
        cur = conn.execute("SELECT high, low FROM v3_candles_cache WHERE asset=? AND timeframe='15m' ORDER BY timestamp", (asset,))
        candles_m15 = cur.fetchall()
        lb = 3
        for i in range(lb, len(candles_m15)-lb):
            h, l = candles_m15[i]
            if all(h >= candles_m15[j][0] for j in range(i-lb,i)) and all(h >= candles_m15[j][0] for j in range(i+1,i+lb+1)):
                b = round(h / bucket_size) * bucket_size
                m15_buckets[b] += 1
                m15_types[b] = "HIGH"
            if all(l <= candles_m15[j][1] for j in range(i-lb,i)) and all(l <= candles_m15[j][1] for j in range(i+1,i+lb+1)):
                b = round(l / bucket_size) * bucket_size
                m15_buckets[b] += 1
                m15_types[b] = "LOW"
    except Exception:
        pass

    # ── Combina e filtra ────────────────────────────────────
    all_buckets = set(h4_buckets.keys()) | set(h1_buckets.keys()) | set(m15_buckets.keys())
    proximity = RECURRING_PROXIMITY.get(asset, 15.0)
    zones = []
    for b in all_buckets:
        v4 = h4_buckets.get(b, 0)
        v1 = h1_buckets.get(b, 0)
        vm = m15_buckets.get(b, 0)
        total_visits = v4 + v1 + vm
        if total_visits < MIN_ZONE_VISITS:
            continue
        if proximity_filter and abs(current_price - b) > proximity:
            continue
        lt = h4_types.get(b) or h1_types.get(b) or m15_types.get(b)
        zones.append({
            "bucket_price": b, "visits": total_visits,
            "visits_h4": v4, "visits_h1": v1, "visits_m15": vm,
            "score": v4*3 + v1 + vm*0.5,
            "expected_direction": "SELL" if lt == "HIGH" else "BUY",
            "last_swing_type": lt,
        })
    zones.sort(key=lambda z: -z["score"])
    return zones


def _check_m5_reaction(df_m5, direction):
    """
    Conferma M5: candela con body >50% nella direzione nelle ultime 3.

    La conferma Wyckoff (sweep+reclaim+test) e' stata testata il 05/10
    sui 51 RECURRING con dati M5: passava solo il 10% dei segnali e
    quei 5 perdevano (-2.5R), mentre i 46 esclusi guadagnavano (+8.1R).
    Troppo restrittiva. Torniamo alla conferma semplice che, nonostante
    un falso positivo alto (68% su BTC), produce un risultato netto
    positivo (+9R su 61 RECURRING).
    """
    if df_m5 is None or len(df_m5) < 3:
        return False
    for _, c in df_m5.iloc[-3:].iterrows():
        body = float(c["close"]) - float(c["open"])
        rng = float(c["high"]) - float(c["low"])
        if rng <= 0: continue
        if direction == "BUY" and body > 0 and body/rng > 0.5:
            return True
        if direction == "SELL" and body < 0 and abs(body)/rng > 0.5:
            return True
    return False


def _generate_recurring_signals(conn, asset, df_h4, df_h1, df_m15, config):
    if df_m15 is None or len(df_m15) < 5:
        return

    current_price = float(df_m15.iloc[-1]["close"])
    zones = _find_recurring_zones(conn, asset, current_price)

    for zone in zones:
        direction = zone["expected_direction"]

        # Dedup: gia' aperto nella stessa direzione
        if conn.execute(
            "SELECT 1 FROM ote_lab_signals WHERE asset=? AND direction=? AND final_outcome='OPEN'",
            (asset, direction)).fetchone():
            continue

        # ── Cooldown 2h cross-direction (validato 09/10) ──────────────
        # Backtest su 82 RECURRING: 82→57 segnali, Exp +0.107→+0.296R
        # Bootstrap CI>0 (99.5%), permutation p=0.001, walk-forward stabile.
        # Logica: se QUALSIASI RECURRING sullo stesso asset e' stato
        # generato nelle ultime 2h (qualsiasi direzione), skip.
        # Elimina il ping-pong counter-trend (BUY→SELL→BUY in poche ore).
        cutoff_2h = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        if conn.execute(
            "SELECT 1 FROM ote_lab_signals WHERE asset=? "
            "AND zone_ref LIKE 'RECURRING_%' AND timestamp_setup > ?",
            (asset, cutoff_2h)).fetchone():
            logger.info("OTE-LAB [%s %s]: SKIP cooldown 2h (RECURRING recente)",
                        asset, direction)
            continue

        # ── Anti-whipsaw (verificato 01/10) ──
        # I 3 SL XAU di fila dell'1/10 erano SELL→BUY→SELL nella stessa
        # fascia 4170-4180 in 3 ore: mercato laterale che stoppa in
        # entrambe le direzioni. Se ho preso un SL in QUALSIASI direzione
        # in questa fascia nelle ultime 4h, non rientro affatto.
        cutoff_4h = (datetime.now(timezone.utc) - timedelta(hours=4)).isoformat()
        zone_bucket = zone["bucket_price"]
        whipsaw = conn.execute(
            "SELECT 1 FROM ote_lab_signals WHERE asset=? "
            "AND zone_ref LIKE 'RECURRING_%' AND final_outcome='SL_HIT' "
            "AND zone_price_bucket=? AND timestamp_closed > ?",
            (asset, zone_bucket, cutoff_4h)).fetchone() if False else None
        # Nota: zone_price_bucket non e' in questo schema, uso zone_ref
        bucket_str = f"_{zone_bucket:.0f}"
        whipsaw = conn.execute(
            "SELECT 1 FROM ote_lab_signals WHERE asset=? "
            "AND zone_ref LIKE ? AND final_outcome='SL_HIT' "
            "AND timestamp_closed > ?",
            (asset, f"RECURRING_%{bucket_str}", cutoff_4h)).fetchone()
        if whipsaw:
            logger.info("OTE-LAB [%s %s]: SKIP anti-whipsaw zona %.0f (SL recente)",
                        asset, direction, zone_bucket)
            continue

        # ── v1.3: Bounce Point filter (reaction map) ─────────────────
        # Dati: ALIGNED (trade direction = bounce direction) → 75% WR
        #        AGAINST (trade vs bounce opposti) → 37.5% WR
        # Se la zona di confluenza piu' vicina prevede un rimbalzo
        # CONTRO la direzione del trade, il trade ha probabilita'
        # molto piu' basse di successo → SKIP.
        # Se non ci sono zone vicine o reaction=UNKNOWN → procedi.
        bounce_ok = _check_bounce_point(conn, asset, direction, current_price)
        if bounce_ok == "AGAINST":
            logger.info("OTE-LAB [%s %s]: SKIP bounce point AGAINST (RM zona opposta)",
                        asset, direction)
            continue

        # ── v1.2: Filtro accumulation 12-24h ─────────────────────────
        # Dati: accum 12-24h → WR 25% (HIGH), 12.5% (LOW), -1.4R totale
        # Probabilmente range esaurito dopo lunga accumulazione
        bucket_size_acc = 10.0 if asset == "XAU_USD" else 500.0
        accum_h = _calc_accumulation_hours(conn, asset, zone["bucket_price"], bucket_size_acc)
        if 12 <= accum_h < 24:
            logger.info("OTE-LAB [%s %s]: SKIP RECURRING accum=%dh (12-24h dead zone)",
                        asset, direction, accum_h)
            continue

        # Conferma M5
        df_m5 = v3_db.get_v3_candles_df(conn, asset, "5m", limit=20)
        if not _check_m5_reaction(df_m5, direction):
            continue

        # H4 trend (disattivato, verificato 30/09)
        if LAB_STRICT_H4 and df_h4 is not None and len(df_h4) >= 21:
            closes = df_h4["close"].astype(float).values
            ema = closes.copy()
            k = 2.0/21
            for i in range(1, len(ema)):
                ema[i] = closes[i]*k + ema[i-1]*(1-k)
            if len(ema) >= 7:
                move = (ema[-1] - ema[-7]) / ema[-7] * 100
                h4 = "BULLISH" if move > 0.5 else ("BEARISH" if move < -0.5 else "NEUTRAL")
                if direction == "BUY" and h4 == "BEARISH": continue
                if direction == "SELL" and h4 == "BULLISH": continue

        # Entry/SL/TP
        entry = current_price
        bucket_size = 10.0 if asset == "XAU_USD" else 500.0
        half = bucket_size / 2

        # ══════════════════════════════════════════════════════════
        # v1.4 — Bounce Point SL/TP: il mercato racconta una storia
        # Il prezzo si muove da una zona di reazione alla prossima.
        # SL e TP devono essere posizionati dove il mercato reagisce.
        # ══════════════════════════════════════════════════════════

        # --- SL: prima calcolo il base (Wyckoff o fisso), poi bounce point ---
        sweep_sl = None
        if df_m5 is not None and len(df_m5) >= 6:
            zone_low = zone["bucket_price"] - half
            zone_high = zone["bucket_price"] + half
            buffer = 2.0 if asset == "XAU_USD" else 30.0
            for _, c in df_m5.iloc[-12:].iterrows():
                cl, ch, clo = float(c["close"]), float(c["high"]), float(c["low"])
                if direction == "BUY" and clo < zone_low and cl > zone_low:
                    sweep_sl = clo - buffer
                elif direction == "SELL" and ch > zone_high and cl < zone_high:
                    sweep_sl = ch + buffer

        if sweep_sl is not None:
            sl = sweep_sl
            risk = abs(entry - sl)
            if asset == "XAU_USD":
                if risk < 5.0:
                    sl = entry - 5.0 if direction == "BUY" else entry + 5.0
                    risk = 5.0
                elif risk > 25.0:
                    sl = entry - 25.0 if direction == "BUY" else entry + 25.0
                    risk = 25.0
            sl_method = "WYCKOFF"
        else:
            if asset == "XAU_USD":
                sl_dist = min(max(half + 5, 8.0), 25.0)
            else:
                sl_dist = half + 50
            sl = entry - sl_dist if direction == "BUY" else entry + sl_dist
            risk = abs(entry - sl)
            sl_method = "FIXED"

        if risk <= 0: continue

        # --- Bounce Point SL/TP: posiziona dove il mercato reagisce ---
        bp = _bounce_point_sl_tp(conn, asset, direction, entry, sl, risk)

        # SL bounce point: se c'e' una zona protettiva vicina, usala
        if bp["sl"] is not None:
            new_risk = abs(entry - bp["sl"])
            if new_risk > 0:
                sl = bp["sl"]
                risk = new_risk
                sl_method = f"BP_{bp['sl_label']}"
                logger.info("OTE-LAB [%s %s]: SL → bounce point %.2f (%s)",
                            asset, direction, sl, sl_method)

        # TP bounce point: il prossimo target dove il mercato reagira'
        rz_used = False
        if bp["tp"] is not None:
            tp = bp["tp"]
            rr2 = round(abs(tp - entry) / risk, 3)
            if rr2 > MAX_RR2:
                rr2 = MAX_RR2
                tp = entry + MAX_RR2*risk if direction == "BUY" else entry - MAX_RR2*risk
            target_label = bp["tp_label"]
            rz_used = True
            logger.info("OTE-LAB [%s %s]: TP → bounce point %.2f (RR=%.2f, %s)",
                        asset, direction, tp, rr2, target_label)
        else:
            # Fallback: vecchia logica reaction zone generica
            rz = _find_rz_target(conn, asset, direction, entry, risk)
            if rz:
                rr2 = rz["rr"]
                if rr2 > MAX_RR2:
                    rr2 = MAX_RR2
                    tp = entry + MAX_RR2*risk if direction == "BUY" else entry - MAX_RR2*risk
                else:
                    tp = rz["price"]
                target_label = rz["label"]
                rz_used = True
            else:
                tp = entry + risk*2 if direction == "BUY" else entry - risk*2
                rr2 = 2.0
                target_label = "FALLBACK_2R"

        # Gap minimo TP1→TP2: se troppo vicini, porta TP2 ad almeno TP1+1R
        if rr2 < 1.0 + MIN_TP_GAP_R:
            rr2 = 1.0 + MIN_TP_GAP_R
            tp = entry + rr2*risk if direction == "BUY" else entry - rr2*risk
            target_label = "TP1+gap"

        if rr2 < 1.2: continue
        tp1 = entry + risk if direction == "BUY" else entry - risk

        now = datetime.now(timezone.utc)
        sig = {
            "asset": asset, "direction": direction,
            "timestamp_setup": now.isoformat(),
            "entry": round(entry, 5), "stop_loss": round(sl, 5),
            "tp1": round(tp1, 5), "tp2": round(tp, 5),
            "risk": round(risk, 5), "rr1": 1.0, "rr2": round(rr2, 3),
            "entry_zone_type": f"RECURRING_{zone['last_swing_type']}",
            "zone_ref": f"RECURRING_{zone['last_swing_type']}_{zone['bucket_price']:.0f}",
            "liquidity_target": target_label,
            "rz_target_used": rz_used, "rz_target_label": target_label if rz_used else None,
            "quality_score": zone["score"], "quality_label": "LAB",
            "session": _get_session(now),
            "tier": "RECURRING",
            "accumulation_hours": _calc_accumulation_hours(
                conn, asset, zone["bucket_price"], bucket_size),
        }

        try:
            sid = _insert_signal(conn, sig)
        except Exception as e:
            logger.error("OTE-LAB [%s %s]: recurring insert error: %s", asset, direction, e)
            continue

        logger.info("OTE-LAB [%s %s]: 🏦 RECURRING_ZONE %.0f (H4=%dx H1=%dx score=%d) entry=%.2f tp=%.2f rr=%.2f",
                    asset, direction, zone["bucket_price"],
                    zone["visits_h4"], zone["visits_h1"], zone["score"],
                    entry, tp, rr2)

        # Notifica
        try:
            from notifications import telegram_bot, ntfy_bot
            tk = config.get("TELEGRAM_BOT_TOKEN", "")
            ch = config.get("TELEGRAM_CHAT_ID", "")
            if tk and ch:
                emoji = "🟢" if direction == "BUY" else "🔴"
                msg = (f"{emoji} *OTE-LAB 🏦 RECURRING ZONE*\n\n"
                       f"*{asset.replace('_',' ')}* — {direction}\n"
                       f"Zona: {zone['bucket_price']:.0f} "
                       f"(H4: {zone['visits_h4']}x | H1: {zone['visits_h1']}x)\n\n"
                       f"Entry:  `{entry:.2f}`\n"
                       f"SL:     `{sl:.2f}`\n"
                       f"TP1:    `{tp1:.2f}` (1R)\n"
                       f"TP2:    `{tp:.2f}` ({rr2:.2f}R)\n\n"
                       f"Target: {target_label}\n"
                       f"Score: {zone['score']} | Conferma M5: ✅\n"
                       f"Accumulazione: {sig.get('accumulation_hours',0)}h "
                       f"{'⭐' if sig.get('accumulation_hours',0) > 9 else ''}\n"
                       f"⚠️ LAB — solo raccolta dati")
                telegram_bot.send_message(tk, ch, msg)

            # Invio JSON strutturato su ntfy per EA MT5
            ntfy_topic = config.get("NTFY_TOPIC", "")
            if ntfy_topic:
                import json as _json
                payload = _json.dumps({
                    "signal_id": str(sid),
                    "strategy_name": "OTE-LAB",
                    "asset": asset,
                    "direction": direction,
                    "entry": round(entry, 5),
                    "stop_loss": round(sl, 5),
                    "tp1": round(tp1, 5),
                    "tp2": round(tp, 5),
                    "tier": "RECURRING",
                    "expiry_hours": 8,
                })
                ntfy_bot.send_message(ntfy_topic, f"OTE-LAB RECURRING {asset} {direction}", payload)
        except Exception as e:
            logger.warning("OTE-LAB recurring notify: %s", e)


# ================================================================
# Entry point
# ================================================================

def run_ote_scan(config: dict, market_contexts: dict = None):
    """
    Entry point. Chiamato da edge_lab_runner (con market_contexts)
    o da ote_scanner_runner (senza, standalone).
    """
    conn = core_db.get_connection(config["DB_PATH"])
    _init_schema(conn)

    now = datetime.now(timezone.utc)

    if market_contexts is None:
        logger.warning("OTE-LAB: nessun market_contexts ricevuto — skip. "
                       "OTE-LAB deve essere chiamato da edge_lab_runner.")
        conn.close()
        return

    logger.info("=== OTE-LAB Scanner: inizio ciclo (%s) ===", ", ".join(OTE_LAB_ASSETS))

    for asset in OTE_LAB_ASSETS:
        market_ctx = market_contexts.get(asset)
        if market_ctx is None:
            logger.warning("OTE-LAB [%s]: market context non disponibile, skip.", asset)
            continue
        try:
            _run_for_asset(conn, asset, config, market_ctx, now)
        except Exception as e:
            logger.error("OTE-LAB [%s]: errore: %s", asset, e, exc_info=True)

    conn.close()
    logger.info("=== OTE-LAB Scanner: fine ciclo ===")
