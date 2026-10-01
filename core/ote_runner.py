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

OTE_LAB_ASSETS = ["BTC_USDT", "XAU_USD"]

# ── Trailing: IDENTICO a TRB (0.7) per isolare l'effetto reaction entry ──
# Test disciplinato: cambiamo una variabile alla volta. Se OTE-LAB batte
# TRB, sappiamo che e' merito della reaction entry, non di un mix di
# effetti (trail 0.5 che magari peggiora + reaction che migliora).
LAB_TRAIL_R = 0.7          # uguale a TRB
LAB_TRAIL_MIN_LOCK = {"BTC_USDT": 60.0, "XAU_USD": 2.5}

# ── Cap RR2 (verificato 01/10 su dati reali) ──
# Target oltre 5R non si raggiungono e il trade muore: RR2>=5 → sumR -1.8
# (WR 43%), RR2<5 → sumR +2.3 (WR 45%). Limito il target a 4R max: se la
# reaction zone e' piu' lontana, taglio il TP a 4R (piu' raggiungibile).
MAX_RR2 = 4.0

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
SIGNAL_EXPIRY_BARS = 32


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
    tier                TEXT
);
"""

def _init_schema(conn):
    # Fix 30/09: la tabella creata da versioni precedenti manca di
    # colonne (tp1_hit, rr1, zone_ref, ...). CREATE IF NOT EXISTS non
    # aggiorna una tabella gia' esistente. La ricreo una volta con lo
    # schema corretto. I dati vecchi erano comunque contaminati/vuoti.
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(ote_lab_signals)").fetchall()]
        if cols and ("tp1_hit" not in cols or "tier" not in cols):
            # Aggiungo solo la colonna tier se e' l'unica mancante
            # (non cancello i dati se lo schema e' gia' quello nuovo)
            if "tp1_hit" in cols and "tier" not in cols:
                conn.execute("ALTER TABLE ote_lab_signals ADD COLUMN tier TEXT")
                conn.commit()
                logger.info("OTE-LAB: colonna tier aggiunta")
            else:
                conn.execute("DROP TABLE ote_lab_signals")
                conn.commit()
                logger.info("OTE-LAB: tabella ricreata con schema aggiornato")
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
            rz_target_used, rz_target_label, tp2_original, tier
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
        sig.get("expiry_bars", SIGNAL_EXPIRY_BARS),
        sig.get("rz_target_used", False), sig.get("rz_target_label"),
        sig.get("tp2_original"), sig.get("tier"),
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
               tp1_hit, mae, mfe, bars_open, expiry_bars
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
        sl = sig["stop_loss"]
        tp1 = sig["tp1"]
        tp2 = sig["tp2"]
        old_mae = float(sig["mae"] or 0)
        old_mfe = float(sig["mfe"] or 0)
        tp1_already = bool(sig["tp1_hit"])
        bars = (sig["bars_open"] or 0) + 1
        risk = abs(entry - sl) if entry and sl else 0
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

        # ── TP1 raggiunto? (rilevato PRIMA di calcolare lo stop) ──
        tp1_hit_now = False
        if not tp1_already and tp1:
            if (d == "BUY" and current_high >= tp1) or (d == "SELL" and current_low <= tp1):
                tp1_hit_now = True
                tp1_already = True
                conn.execute("UPDATE ote_lab_signals SET tp1_hit=1, timestamp_tp1=? WHERE signal_id=?",
                            (now_iso, sid))
                stop_moves.append({
                    "signal_id": sid, "asset": asset, "direction": d,
                    "event": "TP1_HIT_BE", "new_stop": entry,
                })

        # ── Sistema di sicurezza (come TRB) ──
        # 1. Dopo TP1 → SL a break-even (non perdi piu')
        # 2. Trailing → segue il prezzo con distanza TRAIL_R, floor minimo
        trail_dist = LAB_TRAIL_R * risk
        min_lock = LAB_TRAIL_MIN_LOCK.get(asset, 0)
        effective_sl = sl
        trail_active = False
        be_active = False

        # Break-even+ dopo TP1 (un filo sopra entry per coprire lo spread)
        if tp1_already:
            be_active = True
            be_offset = BE_PLUS_R * risk
            if d == "BUY":
                effective_sl = max(effective_sl, entry + be_offset)
            else:
                effective_sl = min(effective_sl, entry - be_offset)

        # Trailing (sopra il break-even)
        if old_mfe >= trail_dist:
            lock = max(old_mfe - trail_dist, min_lock)
            lock = min(lock, old_mfe)
            if d == "BUY":
                effective_sl = max(effective_sl, entry + lock)
            else:
                effective_sl = min(effective_sl, entry - lock)
            trail_active = True
            if not tp1_already:
                stop_moves.append({
                    "signal_id": sid, "asset": asset, "direction": d,
                    "event": "TRAIL_ACTIVATED", "new_stop": effective_sl,
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
                lock_val = max(old_mfe - trail_dist, min_lock)
                lock_val = min(lock_val, old_mfe)
                trail_r = round(lock_val / risk, 3)
                # Dopo TP1 il minimo garantito e' 1R: prendo il maggiore
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
        elif bars >= (sig.get("expiry_bars") or SIGNAL_EXPIRY_BARS):
            if trail_active:
                lock_val = max(old_mfe - trail_dist, min_lock)
                lock_val = min(lock_val, old_mfe)
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
    cands = []
    for z in zones:
        mid = z.get("zone_midpoint", 0)
        if mid <= 0: continue
        if direction == "BUY" and mid <= entry: continue
        if direction == "SELL" and mid >= entry: continue
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

        # LAB: solo Telegram, niente ntfy (ntfy serve all'esecuzione MT5
        # automatica, ma i segnali LAB non vanno eseguiti — mandare
        # entrambi causava il doppio messaggio).
        if bot_token and chat_id:
            telegram_bot.send_message(bot_token, chat_id, text)
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
        # LAB: solo Telegram, niente ntfy (evita doppio messaggio)
        if bot_token and chat_id:
            telegram_bot.send_message(bot_token, chat_id, text)
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
# RECURRING_ZONE — zone con 5+ visite storiche H4+H1
# Simulazione su 753 trade: WR 56.4%, avgR +0.270, sumR +194.1
# ================================================================

RECURRING_PROXIMITY = {"XAU_USD": 15.0, "BTC_USDT": 200.0}
MIN_ZONE_VISITS = 5

def _find_recurring_zones(conn, asset, current_price):
    from collections import Counter
    bucket_size = 10.0 if asset == "XAU_USD" else 500.0

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

    proximity = RECURRING_PROXIMITY.get(asset, 15.0)
    zones = []
    for b in set(h4_buckets.keys()) | set(h1_buckets.keys()):
        v4 = h4_buckets.get(b, 0)
        v1 = h1_buckets.get(b, 0)
        if v4 + v1 < MIN_ZONE_VISITS:
            continue
        if abs(current_price - b) > proximity:
            continue
        lt = h4_types.get(b) or h1_types.get(b)
        zones.append({
            "bucket_price": b, "visits": v4+v1, "visits_h4": v4, "visits_h1": v1,
            "score": v4*3 + v1,
            "expected_direction": "SELL" if lt == "HIGH" else "BUY",
            "last_swing_type": lt,
        })
    zones.sort(key=lambda z: -z["score"])
    return zones


def _check_m5_reaction(df_m5, direction):
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

        # Cooldown 4h per zona specifica (stessa direzione)
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=4)).isoformat()
        if conn.execute(
            "SELECT 1 FROM ote_lab_signals WHERE asset=? AND direction=? "
            "AND zone_ref LIKE 'RECURRING_%' AND timestamp_setup > ?",
            (asset, direction, cutoff)).fetchone():
            continue

        # ── Anti-whipsaw (verificato 01/10) ──
        # I 3 SL XAU di fila dell'1/10 erano SELL→BUY→SELL nella stessa
        # fascia 4170-4180 in 3 ore: mercato laterale che stoppa in
        # entrambe le direzioni. Se ho preso un SL in QUALSIASI direzione
        # in questa fascia nelle ultime 4h, non rientro affatto.
        zone_bucket = zone["bucket_price"]
        whipsaw = conn.execute(
            "SELECT 1 FROM ote_lab_signals WHERE asset=? "
            "AND zone_ref LIKE 'RECURRING_%' AND final_outcome='SL_HIT' "
            "AND zone_price_bucket=? AND timestamp_closed > ?",
            (asset, zone_bucket, cutoff)).fetchone() if False else None
        # Nota: zone_price_bucket non e' in questo schema, uso zone_ref
        bucket_str = f"_{zone_bucket:.0f}"
        whipsaw = conn.execute(
            "SELECT 1 FROM ote_lab_signals WHERE asset=? "
            "AND zone_ref LIKE ? AND final_outcome='SL_HIT' "
            "AND timestamp_closed > ?",
            (asset, f"RECURRING_%{bucket_str}", cutoff)).fetchone()
        if whipsaw:
            logger.info("OTE-LAB [%s %s]: SKIP anti-whipsaw zona %.0f (SL recente)",
                        asset, direction, zone_bucket)
            continue

        # Conferma M5
        df_m5 = v3_db.get_v3_candles_df(conn, asset, "5m", limit=20)
        if not _check_m5_reaction(df_m5, direction):
            continue

        # H4 trend
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
        if asset == "XAU_USD":
            sl_dist = min(max(half + 5, 8.0), 25.0)
        else:
            sl_dist = half + 50

        sl = entry - sl_dist if direction == "BUY" else entry + sl_dist
        risk = abs(entry - sl)
        if risk <= 0: continue

        # Target: reaction zone o RR 2.0 (con cap a MAX_RR2)
        rz = _find_rz_target(conn, asset, direction, entry, risk)
        rz_used = False
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
            "tier": "RECURRING",  # categoria propria, non classificata coi 3 fattori TRB
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
                       f"TP:     `{tp:.2f}` ({rr2:.2f}R)\n\n"
                       f"Target: {target_label}\n"
                       f"Score: {zone['score']} | Conferma M5: ✅\n"
                       f"⚠️ LAB — solo raccolta dati")
                telegram_bot.send_message(tk, ch, msg)
            # LAB: solo Telegram, niente ntfy (evita doppio messaggio)
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
