"""
core/ote_runner.py
OTE-LAB — Clone di TRB con modifiche sperimentali.

NON e' l'OTE originale (zone Fibonacci + conferma M5). E' un CLONE
di TRB che gira nello slot OTE del workflow per testare modifiche
che non vogliamo rischiare sul TRB reale (che sta guadagnando).

Modifiche sperimentali rispetto a TRB:
  1. REACTION_ZONE_TARGETS: quando il liquidity engine non trova un
     target reale (61% dei trade TRB), usa la reaction zone piu' vicina
     nella direzione del trade come fallback.
  2. TRAIL_R = 0.5 invece di 0.7: trailing piu' stretto, salva trade
     che muovono 0.5R a favore poi invertono.
  3. STRICT_TREND: richiede H4 allineato alla direzione (non solo H1).

Come funziona:
  1. Chiama generate_trb_signal (identico a TRB)
  2. Post-processa il segnale con le modifiche sopra
  3. Salva in ote_lab_signals (tabella dedicata)
  4. Monitora con trailing TRAIL_R=0.5

Confronto: dopo 2-3 settimane si confrontano OTE-LAB vs TRB sullo
stesso periodo. Se OTE-LAB batte TRB, le modifiche vengono portate
in TRB con fiducia.

Changelog 28/09/2026: creato come clone TRB con mod sperimentali.
"""

from __future__ import annotations
import json, logging, sqlite3, uuid
from datetime import datetime, timezone, timedelta

from storage import db as core_db
from core import v3_db
from strategies.edge_lab.trend_rider import generate_trb_signal

try:
    from core.decision_ledger import ote_integration as ledger_link
except Exception:
    ledger_link = None

logger = logging.getLogger("ote.lab")

OTE_ASSETS = ["BTC_USDT", "XAU_USD"]
STRATEGY_NAME = "OTE_LAB"

# ── Mod 1: reaction zone targets ──
REACTION_ZONE_TARGETS = True

# ── Mod 2: trailing piu' stretto (TRB usa 0.7) ──
TRAIL_R = 0.5
TRAIL_MIN_LOCK = {"BTC_USDT": 60.0, "XAU_USD": 2.5}

# ── Mod 3: trend H4 stretto ──
STRICT_TREND_H4 = True

SIGNAL_EXPIRY_BARS = 32  # 32 barre M15 = 8h


# ================================================================
# DB
# ================================================================

_CREATE = """
CREATE TABLE IF NOT EXISTS ote_lab_signals (
    signal_id TEXT PRIMARY KEY, asset TEXT, direction TEXT,
    signal_type TEXT DEFAULT 'TRB_CLONE',
    timestamp_setup TEXT, entry REAL, stop_loss REAL,
    tp1 REAL, tp2 REAL, tp2_original REAL, risk REAL, rr2 REAL,
    trend_h1 TEXT, trend_h4 TEXT, adx REAL,
    entry_zone_type TEXT, liquidity_target TEXT, liquidity_priority TEXT,
    rz_target_used BOOLEAN DEFAULT 0, rz_target_label TEXT,
    zone_visits INTEGER,
    zone_price_bucket REAL,
    quality_score INTEGER, quality_label TEXT, session TEXT,
    final_outcome TEXT DEFAULT 'OPEN', result_r REAL,
    mae REAL DEFAULT 0, mfe REAL DEFAULT 0, bars_open INTEGER DEFAULT 0,
    expiry_bars INTEGER DEFAULT 32, timestamp_closed TEXT
);"""

def _ensure(conn):
    conn.execute(_CREATE); conn.commit()

def _insert(conn, s):
    conn.execute("""INSERT INTO ote_lab_signals (
        signal_id, asset, direction, signal_type, timestamp_setup,
        entry, stop_loss, tp1, tp2, tp2_original, risk, rr2,
        trend_h1, trend_h4, adx, entry_zone_type,
        liquidity_target, liquidity_priority,
        rz_target_used, rz_target_label, zone_visits, zone_price_bucket,
        quality_score, quality_label, session, expiry_bars
    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
        s["signal_id"], s["asset"], s["direction"],
        s.get("signal_type", "TRB_CLONE"), s["timestamp_setup"],
        s["entry"], s["stop_loss"], s.get("tp1"), s["tp2"],
        s.get("tp2_original"), s["risk"], s["rr2"],
        s.get("trend_h1"), s.get("trend_h4"), s.get("adx"),
        s.get("entry_zone_type"), s.get("liquidity_target"),
        s.get("liquidity_priority"), s.get("rz_target_used", 0),
        s.get("rz_target_label"), s.get("zone_visits"),
        s.get("zone_price_bucket"), s.get("quality_score"),
        s.get("quality_label"), s.get("session"), SIGNAL_EXPIRY_BARS,
    ))
    conn.commit()

def _close(conn, sid, outcome, result_r=None, mae=None, mfe=None, bars=None):
    conn.execute("UPDATE ote_lab_signals SET final_outcome=?, result_r=?, mae=?, mfe=?, bars_open=?, timestamp_closed=? WHERE signal_id=?",
        (outcome, result_r, mae, mfe, bars, datetime.now(timezone.utc).isoformat(), sid))
    conn.commit()

def _get_open(conn, asset):
    cur = conn.execute("SELECT signal_id, direction, entry, stop_loss, tp2, mae, mfe, bars_open, expiry_bars FROM ote_lab_signals WHERE asset=? AND final_outcome='OPEN'", (asset,))
    return [dict(zip([d[0] for d in cur.description], r)) for r in cur.fetchall()]


# ================================================================
# Mod 1: reaction zone target
# ================================================================

def _find_rz_target(conn, asset, direction, entry, risk):
    try:
        row = conn.execute("SELECT snapshot_json FROM reaction_map_snapshots WHERE asset=? ORDER BY timestamp_snapshot DESC LIMIT 1", (asset,)).fetchone()
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
        cands.append({"price": mid, "label": f"RZ_{z.get('reaction_strength','?')}", "rr": round(rr, 3), "dist": abs(mid - entry)})
    return min(cands, key=lambda c: c["dist"]) if cands else None


# ================================================================
# Monitoraggio (trailing 0.5R)
# ================================================================

def _monitor(conn, asset, df_m5):
    if df_m5 is None or len(df_m5) == 0: return
    hi = float(df_m5.iloc[-1]["high"])
    lo = float(df_m5.iloc[-1]["low"])

    for sig in _get_open(conn, asset):
        sid, d = sig["signal_id"], sig["direction"]
        entry, sl, tp = sig["entry"], sig["stop_loss"], sig["tp2"]
        old_mae, old_mfe = float(sig.get("mae") or 0), float(sig.get("mfe") or 0)
        bars = (sig.get("bars_open") or 0) + 1
        risk = abs(entry - sl)
        if risk <= 0: continue

        adv = max(entry - lo, 0) if d == "BUY" else max(hi - entry, 0)
        fav = max(hi - entry, 0) if d == "BUY" else max(entry - lo, 0)
        new_mae, new_mfe = max(old_mae, adv), max(old_mfe, fav)

        # Trailing
        eff_sl = sl
        trail_on = False
        td = TRAIL_R * risk
        ml = TRAIL_MIN_LOCK.get(asset, 0)
        if old_mfe >= td:
            lk = min(max(old_mfe - td, ml), old_mfe)
            eff_sl = max(eff_sl, entry + lk) if d == "BUY" else min(eff_sl, entry - lk)
            trail_on = True

        sl_hit = (lo <= eff_sl) if d == "BUY" else (hi >= eff_sl)
        tp_hit = (tp and hi >= tp) if d == "BUY" else (tp and lo <= tp)

        if sl_hit:
            if trail_on:
                lk = min(max(old_mfe - td, ml), old_mfe)
                rr = round(lk / risk, 3)
                _close(conn, sid, "TRAIL_HIT", rr, new_mae, new_mfe, bars)
                logger.info("OTE-LAB [%s]: %s TRAIL_HIT +%.2fR", asset, sid[:8], rr)
            else:
                _close(conn, sid, "SL_HIT", -1.0, new_mae, new_mfe, bars)
                logger.info("OTE-LAB [%s]: %s SL_HIT", asset, sid[:8])
        elif tp_hit:
            rr = round(abs(tp - entry) / risk, 3)
            _close(conn, sid, "TP2_HIT", rr, new_mae, new_mfe, bars)
            logger.info("OTE-LAB [%s]: %s TP2_HIT +%.2fR", asset, sid[:8], rr)
        elif bars >= (sig.get("expiry_bars") or SIGNAL_EXPIRY_BARS):
            if trail_on:
                lk = min(max(old_mfe - td, ml), old_mfe)
                rr = round(lk / risk, 3)
                _close(conn, sid, "TRAIL_HIT", rr, new_mae, new_mfe, bars)
                logger.info("OTE-LAB [%s]: %s TRAIL_HIT (exp) +%.2fR", asset, sid[:8], rr)
            else:
                _close(conn, sid, "EXPIRED", 0, new_mae, new_mfe, bars)
                logger.info("OTE-LAB [%s]: %s EXPIRED", asset, sid[:8])
        else:
            conn.execute("UPDATE ote_lab_signals SET mae=?, mfe=?, bars_open=? WHERE signal_id=?", (new_mae, new_mfe, bars, sid))
            conn.commit()


# ================================================================
# Notifica
# ================================================================

def _notify(asset, direction, sig, config, rz_used=False):
    try:
        from notifications import telegram_bot
        tk = config.get("TELEGRAM_BOT_TOKEN", "")
        ch = config.get("TELEGRAM_CHAT_ID", "")
        if not tk or not ch: return
        tag = "RZ" if rz_used else "LIQ"
        msg = (f"🧪 OTE-LAB | {asset} {direction}\n"
               f"Entry: {sig['entry']:.2f} | SL: {sig['stop_loss']:.2f}\n"
               f"TP: {sig['tp2']:.2f} (RR {sig['rr2']:.2f}) [{tag}]\n"
               f"Zone: {sig.get('entry_zone_type','?')} | ADX: {sig.get('adx',0):.1f}\n"
               f"Trail: {TRAIL_R}R | H4: {sig.get('trend_h4','?')}\n"
               f"⚠️ LAB — solo raccolta dati")
        telegram_bot.send_message(tk, ch, msg)
    except Exception as e:
        logger.warning("OTE-LAB notify: %s", e)


# ================================================================
# Mod 4: Segnali da ZONE RICORRENTI (ordini istituzionali)
# ================================================================

MIN_ZONE_VISITS = 5        # minimo visite COMBINATE per considerare la zona
RECURRING_PROXIMITY = {"XAU_USD": 15.0, "BTC_USDT": 200.0}

def _find_recurring_zones(conn, asset, current_price):
    """
    Trova zone di prezzo dove gli swing tornano ripetutamente.
    Scansiona H4 (da lh_swing_zones) e H1 (calcolato dalle candele).
    
    H1 da' piu' risoluzione (3x piu' swing), H4 conferma la forza.
    Una zona con visite su ENTRAMBI i TF e' piu' forte.
    
    Scoring:
      visit_h4 * 3 + visit_h1 * 1 = score
      (H4 pesa 3x perche' serve piu' forza istituzionale per girare un H4)
    """
    from collections import Counter
    bucket_size = 10.0 if asset == "XAU_USD" else 500.0
    
    # ── H4: da lh_swing_zones ──
    h4_buckets = Counter()
    h4_types = {}
    try:
        cur = conn.execute(
            "SELECT swing_type, price FROM lh_swing_zones "
            "WHERE asset=? AND timeframe='H4'", (asset,))
        for stype, price in cur.fetchall():
            bucket = round(price / bucket_size) * bucket_size
            h4_buckets[bucket] += 1
            h4_types[bucket] = stype
    except Exception:
        pass
    
    # ── H1: calcolato dalle candele grezze ──
    h1_buckets = Counter()
    h1_types = {}
    try:
        cur = conn.execute(
            "SELECT high, low FROM candles_cache "
            "WHERE asset=? AND timeframe='1h' ORDER BY timestamp",
            (asset,))
        candles = cur.fetchall()
        lb = 3
        for i in range(lb, len(candles) - lb):
            h = candles[i][0]
            l = candles[i][1]
            is_high = all(h >= candles[j][0] for j in range(i-lb, i)) and \
                      all(h >= candles[j][0] for j in range(i+1, i+lb+1))
            is_low = all(l <= candles[j][1] for j in range(i-lb, i)) and \
                     all(l <= candles[j][1] for j in range(i+1, i+lb+1))
            if is_high:
                bucket = round(h / bucket_size) * bucket_size
                h1_buckets[bucket] += 1
                h1_types[bucket] = "HIGH"
            if is_low:
                bucket = round(l / bucket_size) * bucket_size
                h1_buckets[bucket] += 1
                h1_types[bucket] = "LOW"
    except Exception:
        pass
    
    # ── Combina H4 + H1 con scoring pesato ──
    all_buckets = set(h4_buckets.keys()) | set(h1_buckets.keys())
    proximity = RECURRING_PROXIMITY.get(asset, 15.0)
    
    zones = []
    for bucket_price in all_buckets:
        v_h4 = h4_buckets.get(bucket_price, 0)
        v_h1 = h1_buckets.get(bucket_price, 0)
        score = v_h4 * 3 + v_h1 * 1  # H4 pesa 3x
        total_visits = v_h4 + v_h1
        
        if total_visits < MIN_ZONE_VISITS:
            continue
        
        dist = abs(current_price - bucket_price)
        if dist > proximity:
            continue
        
        # Direzione attesa dall'ultimo swing
        last_type = h4_types.get(bucket_price) or h1_types.get(bucket_price)
        expected_dir = "SELL" if last_type == "HIGH" else "BUY"
        
        zones.append({
            "bucket_price": bucket_price,
            "visits": total_visits,
            "visits_h4": v_h4,
            "visits_h1": v_h1,
            "score": score,
            "distance": dist,
            "expected_direction": expected_dir,
            "last_swing_type": last_type,
        })
    
    # Ordina per score (le zone piu' forti prima)
    zones.sort(key=lambda z: -z["score"])
    return zones


def _check_m5_reaction(df_m5, direction):
    """
    Verifica se nelle ultime candele M5 c'e' una reazione nella
    direzione attesa (displacement / strong candle body).
    Non entra al buio — aspetta conferma.
    """
    if df_m5 is None or len(df_m5) < 3:
        return False
    last3 = df_m5.iloc[-3:]
    if direction == "BUY":
        # Cerco una candela verde forte (body > 60% del range)
        for _, c in last3.iterrows():
            body = float(c["close"]) - float(c["open"])
            rng = float(c["high"]) - float(c["low"])
            if rng > 0 and body > 0 and body / rng > 0.6:
                return True
    else:
        for _, c in last3.iterrows():
            body = float(c["open"]) - float(c["close"])
            rng = float(c["high"]) - float(c["low"])
            if rng > 0 and body > 0 and body / rng > 0.6:
                return True
    return False


def _generate_recurring_zone_signals(conn, asset, df_h1, df_h4, df_m5, config):
    """
    Genera segnali quando il prezzo torna in una zona con 5+ visite
    E c'e' conferma di reazione su M5.
    """
    if df_m5 is None or len(df_m5) < 5:
        return
    if df_h1 is None or len(df_h1) < 30:
        return

    current_price = float(df_m5.iloc[-1]["close"])
    zones = _find_recurring_zones(conn, asset, current_price)

    for zone in zones:
        direction = zone["expected_direction"]

        # Dedup: non duplicare
        existing = conn.execute(
            "SELECT 1 FROM ote_lab_signals WHERE asset=? AND direction=? "
            "AND final_outcome='OPEN' AND signal_type='RECURRING_ZONE'",
            (asset, direction)
        ).fetchone()
        if existing:
            continue

        # Aspetta conferma M5 (non entrare al buio)
        if not _check_m5_reaction(df_m5, direction):
            continue

        # Trend H4 check
        if STRICT_TREND_H4 and df_h4 is not None and len(df_h4) >= 30:
            closes = df_h4["close"].astype(float).values
            ema = closes.copy()
            k = 2.0 / 21
            for i in range(1, len(ema)):
                ema[i] = closes[i] * k + ema[i-1] * (1-k)
            if len(ema) > 6:
                move = (ema[-1] - ema[-7]) / ema[-7] * 100
                h4_trend = "BULLISH" if move > 0.5 else ("BEARISH" if move < -0.5 else "NEUTRAL")
                if direction == "BUY" and h4_trend == "BEARISH":
                    continue
                if direction == "SELL" and h4_trend == "BULLISH":
                    continue

        # Calcolo Entry/SL/TP
        entry = current_price
        bucket_size = 10.0 if asset == "XAU_USD" else 500.0
        half_bucket = bucket_size / 2

        if direction == "BUY":
            sl = zone["bucket_price"] - half_bucket - 5  # sotto la zona
            if asset == "XAU_USD":
                sl = max(sl, entry - 25)  # cap XAU
                sl = min(sl, entry - 8)   # floor XAU
        else:
            sl = zone["bucket_price"] + half_bucket + 5  # sopra la zona
            if asset == "XAU_USD":
                sl = min(sl, entry + 25)
                sl = max(sl, entry + 8)

        risk = abs(entry - sl)
        if risk <= 0:
            continue

        # Target: prossima reaction zone nella direzione
        rz = _find_rz_target(conn, asset, direction, entry, risk)
        if rz:
            tp2 = rz["price"]
            rr2 = rz["rr"]
            target_label = rz["label"]
        else:
            # Fallback: 2R fisso
            tp2 = entry + 2 * risk if direction == "BUY" else entry - 2 * risk
            rr2 = 2.0
            target_label = "FALLBACK_2R"

        if rr2 < 1.2:
            continue

        tp1 = entry + risk if direction == "BUY" else entry - risk

        try:
            last_ts_ms = int(df_m5.iloc[-1]["timestamp"])
            ts = datetime.fromtimestamp(last_ts_ms / 1000, tz=timezone.utc).isoformat()
        except Exception:
            ts = datetime.now(timezone.utc).isoformat()

        sig = {
            "signal_id": str(uuid.uuid4()),
            "signal_type": "RECURRING_ZONE",
            "asset": asset,
            "direction": direction,
            "timestamp_setup": ts,
            "entry": round(entry, 5),
            "stop_loss": round(sl, 5),
            "tp1": round(tp1, 5),
            "tp2": round(tp2, 5),
            "risk": round(risk, 5),
            "rr2": round(rr2, 3),
            "entry_zone_type": f"RECURRING_{zone['last_swing_type']}",
            "liquidity_target": target_label,
            "rz_target_used": 1 if rz else 0,
            "rz_target_label": target_label,
            "zone_visits": zone["visits"],
            "zone_price_bucket": zone["bucket_price"],
            "session": None,
        }

        _insert(conn, sig)
        logger.info(
            "OTE-LAB [%s %s]: 🏦 RECURRING_ZONE entry=%.2f tp=%.2f rr=%.2f "
            "zone=%.0f (H4=%d H1=%d score=%d)",
            asset, direction, entry, tp2, rr2,
            zone["bucket_price"], zone.get("visits_h4",0),
            zone.get("visits_h1",0), zone.get("score",0))

        try:
            from notifications import telegram_bot
            tk = config.get("TELEGRAM_BOT_TOKEN", "")
            ch = config.get("TELEGRAM_CHAT_ID", "")
            if tk and ch:
                msg = (f"🏦 OTE-LAB RECURRING ZONE | {asset} {direction}\n"
                       f"Zona: {zone['bucket_price']:.0f} "
                       f"(H4: {zone.get('visits_h4',0)}x | H1: {zone.get('visits_h1',0)}x)\n"
                       f"Entry: {entry:.2f} | SL: {sl:.2f}\n"
                       f"TP: {tp2:.2f} (RR {rr2:.2f})\n"
                       f"Target: {target_label}\n"
                       f"Score: {zone.get('score',0)} | Conferma M5: ✅\n"
                       f"⚠️ LAB — solo raccolta dati")
                telegram_bot.send_message(tk, ch, msg)
        except Exception as e:
            logger.warning("OTE-LAB recurring notify: %s", e)


# ================================================================
# Per-asset scan
# ================================================================

def _run_for_asset(conn, asset, config):
    now = datetime.now(timezone.utc)
    if asset == "XAU_USD":
        wd = now.weekday()
        if wd == 6 or (wd == 5 and now.hour >= 22) or (wd == 4 and now.hour >= 22):
            return

    logger.info("OTE-LAB: ciclo %s", asset)

    df_h1 = core_db.get_candles_df(conn, asset, "1h", limit=300)
    df_h4 = core_db.get_candles_df(conn, asset, "4h", limit=100)
    df_m15 = v3_db.get_v3_candles_df(conn, asset, "15m", limit=100)
    df_m5 = v3_db.get_v3_candles_df(conn, asset, "5m", limit=100)

    if df_h1 is None or len(df_h1) < 30: return
    if df_m15 is None or len(df_m15) < 10: return

    # 1. Monitor open signals FIRST
    try:
        _monitor(conn, asset, df_m5 if df_m5 is not None else df_m15)
    except Exception as e:
        logger.error("OTE-LAB [%s]: monitor error: %s", asset, e)

    # 2. Build market context — COMPLETO, come edge_lab_runner
    #    Bug fix 28/09: la versione precedente passava solo liquidity
    #    e session, mancando i dati MIE (order_block, fvg, structure).
    #    generate_trb_signal senza questi dati rifiuta sempre con
    #    NO_ENTRY_ZONE perche' non trova OB/FVG.
    ctx = {"asset": asset}

    # MIE context: stessa logica di edge_lab_runner._read_mie_context
    MIE_TABLES = [
        ("structure",    "structure_snapshots"),
        ("volatility",   "volatility_snapshots"),
        ("order_block",  "order_block_snapshots"),
        ("fvg",          "fvg_snapshots"),
        ("liquidity",    "liquidity_snapshots"),
        ("session_sweep","session_sweep_snapshots"),
        ("reaction_map", "reaction_map_snapshots"),
        ("candlestick",  "candlestick_snapshots"),
        ("macro",        "macro_snapshots"),
        ("market_state", "market_state_snapshots"),
    ]
    mie = {}
    for prefix, table in MIE_TABLES:
        try:
            row = conn.execute(
                f"SELECT snapshot_json FROM {table} "
                f"WHERE asset = ? ORDER BY timestamp_snapshot DESC LIMIT 1",
                (asset,)
            ).fetchone()
            if row:
                snapshot = json.loads(row[0])
                if isinstance(snapshot, dict):
                    for key, value in snapshot.items():
                        mie[f"mie_{prefix}_{key}"] = value
                mie[f"mie_{prefix}_available"] = True
            else:
                mie[f"mie_{prefix}_available"] = False
        except Exception:
            mie[f"mie_{prefix}_available"] = False
    ctx["mie"] = mie

    # Liquidity map (separata, generate_trb_signal la legge come market_ctx["liquidity"])
    try:
        row = conn.execute("SELECT snapshot_json FROM liquidity_snapshots WHERE asset=? ORDER BY timestamp_snapshot DESC LIMIT 1", (asset,)).fetchone()
        if row: ctx["liquidity"] = json.loads(row[0])
    except Exception: pass

    # Session
    try:
        row = conn.execute("SELECT current_session FROM market_context_snapshots WHERE asset=? ORDER BY timestamp_snapshot DESC LIMIT 1", (asset,)).fetchone()
        if row: ctx["session"] = {"current_session": row[0]}
    except Exception: pass

    # 3. Generate signals (both directions, same as TRB)
    for direction in ["BUY", "SELL"]:
        try:
            result = generate_trb_signal(
                market_ctx=ctx,
                df_h4=df_h4 if df_h4 is not None else df_h1,
                df_h1=df_h1, df_m15=df_m15,
                direction=direction,
            )
        except Exception as e:
            logger.error("OTE-LAB [%s %s]: generate error: %s", asset, direction, e)
            continue

        sig = result.get("signal")
        if sig is None:
            continue

        # ── Mod 3: strict trend H4 ──
        if STRICT_TREND_H4:
            h4 = sig.get("trend_h4")
            if h4:
                if direction == "BUY" and h4 == "BEARISH":
                    logger.info("OTE-LAB [%s %s]: SKIP trend H4=%s", asset, direction, h4)
                    continue
                if direction == "SELL" and h4 == "BULLISH":
                    logger.info("OTE-LAB [%s %s]: SKIP trend H4=%s", asset, direction, h4)
                    continue

        # ── Mod 1: reaction zone target fallback ──
        rz_used = False
        if REACTION_ZONE_TARGETS and sig.get("liquidity_priority") is None:
            risk = sig.get("risk", 0)
            if risk > 0:
                rz = _find_rz_target(conn, asset, direction, sig["entry"], risk)
                if rz:
                    sig["tp2_original"] = sig["tp2"]
                    sig["tp2"] = rz["price"]
                    sig["rr2"] = rz["rr"]
                    sig["liquidity_target"] = rz["label"]
                    sig["rz_target_used"] = True
                    sig["rz_target_label"] = rz["label"]
                    rz_used = True
                    logger.info("OTE-LAB [%s %s]: RZ target %s RR=%.2f", asset, direction, rz["label"], rz["rr"])

        # Dedup
        if conn.execute("SELECT 1 FROM ote_lab_signals WHERE asset=? AND direction=? AND final_outcome='OPEN'", (asset, direction)).fetchone():
            continue

        sig["signal_id"] = str(uuid.uuid4())
        _insert(conn, sig)
        logger.info("OTE-LAB [%s %s]: SIGNAL entry=%.2f tp=%.2f rr=%.2f zone=%s rz=%s",
                    asset, direction, sig["entry"], sig["tp2"], sig["rr2"],
                    sig.get("entry_zone_type","?"), "YES" if rz_used else "no")
        _notify(asset, direction, sig, config, rz_used)

    # 4. Segnali da ZONE RICORRENTI (ordini istituzionali)
    try:
        _generate_recurring_zone_signals(conn, asset, df_h1, df_h4, df_m5, config)
    except Exception as e:
        logger.error("OTE-LAB [%s]: recurring zone error: %s", asset, e)


# ================================================================
# Entry point
# ================================================================

def run_ote_scan(config: dict):
    db_path = config.get("DB_PATH", "signals.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    _ensure(conn)
    for asset in OTE_ASSETS:
        try:
            _run_for_asset(conn, asset, config)
        except Exception as e:
            logger.error("OTE-LAB [%s]: fatal: %s", asset, e, exc_info=True)
    conn.close()
    logger.info("OTE-LAB: done.")
