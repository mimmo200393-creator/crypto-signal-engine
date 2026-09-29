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

# ── MOD LAB 1: Trailing piu' stretto ──
LAB_TRAIL_R = 0.5          # TRB usa 0.7
LAB_TRAIL_MIN_LOCK = {"BTC_USDT": 60.0, "XAU_USD": 2.5}

# ── MOD LAB 2: Strict trend H4 ──
LAB_STRICT_H4 = True

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
    tp2_original        REAL
);
"""

def _init_schema(conn):
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
            rz_target_used, rz_target_label, tp2_original
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
        sig.get("tp2_original"),
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

        # ── MOD LAB: Trailing 0.5R ──
        trail_dist = LAB_TRAIL_R * risk
        min_lock = LAB_TRAIL_MIN_LOCK.get(asset, 0)
        effective_sl = sl
        trail_active = False

        if old_mfe >= trail_dist:
            lock = max(old_mfe - trail_dist, min_lock)
            lock = min(lock, old_mfe)
            if d == "BUY":
                effective_sl = max(sl, entry + lock)
            else:
                effective_sl = min(sl, entry - lock)
            trail_active = True

            # Notifica spostamento stop (come TRB)
            if not tp1_already:  # prima attivazione
                stop_moves.append({
                    "signal_id": sid, "asset": asset, "direction": d,
                    "event": "TRAIL_ACTIVATED", "new_stop": effective_sl,
                })

        # Stage2: TP1 raggiunto
        tp1_hit_now = False
        if not tp1_already and tp1:
            if (d == "BUY" and current_high >= tp1) or (d == "SELL" and current_low <= tp1):
                tp1_hit_now = True
                conn.execute("UPDATE ote_lab_signals SET tp1_hit=1, timestamp_tp1=? WHERE signal_id=?",
                            (now_iso, sid))

        # Check esiti
        if d == "BUY":
            sl_hit = current_low <= effective_sl
            tp2_hit = tp2 is not None and current_high >= tp2
        else:
            sl_hit = current_high >= effective_sl
            tp2_hit = tp2 is not None and current_low <= tp2

        outcome = None
        result_r = None

        if sl_hit:
            if trail_active:
                lock_val = max(old_mfe - trail_dist, min_lock)
                lock_val = min(lock_val, old_mfe)
                result_r = round(lock_val / risk, 3)
                outcome = "TRAIL_HIT"
            else:
                result_r = -1.0
                outcome = "SL_HIT"
        elif tp2_hit:
            result_r = sig.get("rr2") or round(abs(tp2 - entry) / risk, 3)
            outcome = "TP2_HIT"
        elif bars >= (sig.get("expiry_bars") or SIGNAL_EXPIRY_BARS):
            if trail_active:
                lock_val = max(old_mfe - trail_dist, min_lock)
                lock_val = min(lock_val, old_mfe)
                result_r = round(lock_val / risk, 3)
                outcome = "TRAIL_HIT"
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
            f"Trail: {LAB_TRAIL_R}R | Session: {signal.get('session','N/A')}\n"
            f"⚠️ LAB — solo raccolta dati"
        )

        bot_token = config.get("TELEGRAM_BOT_TOKEN", "")
        chat_id = config.get("TELEGRAM_CHAT_ID", "")
        ntfy_topic = config.get("NTFY_TOPIC", "")

        if bot_token and chat_id:
            telegram_bot.send_message(bot_token, chat_id, text)
        if ntfy_topic:
            title = f"OTE-LAB {asset.replace('_',' ')} {direction} | {quality}"
            ntfy_bot.send_message(ntfy_topic, title, text.replace("*","").replace("`",""))
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
        emoji = "🟢" if direction == "BUY" else "🔴"
        def fp(v):
            if v is None: return "N/A"
            return f"{v:,.2f}" if float(v) > 1000 else f"{v:.4f}"
        text = (f"{emoji} *OTE-LAB — TRAILING STOP*\n\n"
                f"*{asset.replace('_',' ')}* — {direction}\n"
                f"Nuovo stop: `{fp(sp['new_stop'])}`\n"
                f"⚠️ LAB — solo raccolta dati")
        bot_token = config.get("TELEGRAM_BOT_TOKEN", "")
        chat_id = config.get("TELEGRAM_CHAT_ID", "")
        if bot_token and chat_id:
            telegram_bot.send_message(bot_token, chat_id, text)
        ntfy_topic = config.get("NTFY_TOPIC", "")
        if ntfy_topic:
            ntfy_bot.send_message(ntfy_topic,
                f"OTE-LAB {asset} {direction} | TRAIL",
                text.replace("*","").replace("`",""))
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
        # ── MOD LAB 3: Reaction zone come target fallback ────
        # ══════════════════════════════════════════════════════
        if signal.get("liquidity_priority") is None:
            risk_val = signal.get("risk", 0)
            if risk_val > 0:
                rz = _find_rz_target(conn, asset, direction, entry, risk_val)
                if rz:
                    signal["tp2_original"] = signal.get("tp2")
                    signal["tp2"] = rz["price"]
                    signal["rr2"] = rz["rr"]
                    signal["liquidity_target"] = rz["label"]
                    signal["rz_target_used"] = True
                    signal["rz_target_label"] = rz["label"]
                    logger.info("OTE-LAB [%s %s]: RZ target %s RR=%.2f",
                               asset, direction, rz["label"], rz["rr"])

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


# ================================================================
# Entry point
# ================================================================

def run_ote_scan(config: dict, market_contexts: dict = None):
    """
    Entry point. Chiamato da ote_scanner_runner.py.

    Se market_contexts e' None (standalone), legge gli snapshot dal DB.
    Se passato da edge_lab_runner, usa il context gia' costruito.
    """
    conn = core_db.get_connection(config["DB_PATH"])
    _init_schema(conn)

    # Reset one-time: dati contaminati pre-cooldown
    try:
        n = conn.execute("SELECT COUNT(*) FROM ote_lab_signals").fetchone()[0]
        if 0 < n <= 20:
            conn.execute("DELETE FROM ote_lab_signals")
            conn.commit()
            logger.info("OTE-LAB: reset %d segnali contaminati", n)
    except Exception:
        pass

    now = datetime.now(timezone.utc)

    # Se non riceve market_contexts, li costruisce dal DB
    if market_contexts is None:
        market_contexts = {}
        for asset in OTE_LAB_ASSETS:
            ctx = {"asset": asset}
            # Leggi MIE snapshot (stessa lista di TRB)
            mie = {}
            for prefix, table in [
                ("structure","structure_snapshots"),("volatility","volatility_snapshots"),
                ("order_block","order_block_snapshots"),("fvg","fvg_snapshots"),
                ("liquidity","liquidity_snapshots"),("session_sweep","session_sweep_snapshots"),
                ("reaction_map","reaction_map_snapshots"),("candlestick","candlestick_snapshots"),
                ("macro","macro_snapshots"),("market_state","market_state_snapshots"),
            ]:
                try:
                    row = conn.execute(
                        f"SELECT snapshot_json FROM {table} WHERE asset=? "
                        f"ORDER BY timestamp_snapshot DESC LIMIT 1", (asset,)).fetchone()
                    if row:
                        snap = json.loads(row[0])
                        if isinstance(snap, dict):
                            for k, v in snap.items():
                                mie[f"mie_{prefix}_{k}"] = v
                except Exception:
                    pass
            ctx["mie_context"] = mie
            # Liquidity map
            try:
                row = conn.execute(
                    "SELECT snapshot_json FROM liquidity_snapshots "
                    "WHERE asset=? ORDER BY timestamp_snapshot DESC LIMIT 1",
                    (asset,)).fetchone()
                if row: ctx["liquidity"] = json.loads(row[0])
            except Exception:
                pass
            market_contexts[asset] = ctx

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
