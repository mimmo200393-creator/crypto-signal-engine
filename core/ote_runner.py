"""
core/ote_runner.py
OTE Fase A — Runner ("zona prima, direzione dopo")

Principio: NON predice la direzione. La OSSERVA.

Ciclo per asset:
    1. Legge le LH Restart Zone attive (lh_db, riuso diretto)
    2. Calcola Liquidity Map sopra E sotto ogni zona (neutrale)
    3. Zona entro 12.5pt? → CANDIDATE (neutro, nessuna direzione)
    4. Prezzo tocca la zona? → TOUCHED → osserva M5
    5. M5 mostra sweep sotto + rigetto? → direzione BUY
       M5 mostra sweep sopra + rigetto? → direzione SELL
    6. Direzione confermata → calcola Entry/SL/TP → SIGNAL
    7. Monitora i SIGNAL aperti → TP/SL/EXPIRED

Nessun nuovo engine — riusa:
    - lh_db (zone con ricorrenza)
    - strategies/tt/liquidity_engine (swing, equal levels, sweep, reaction, target)

La differenza con TT: la direzione nasce dal MERCATO (sweep+reaction M5),
non da una previsione (4H Direction Engine). Zero previsione direzionale.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone, timedelta

from storage import db as core_db
from core import v3_db
from core import lh_db
from core import ote_db

# Riuso le funzioni GIA' TESTATE di TT per l'esecuzione M5 e la Liquidity Map.
# Dipendenza unidirezionale (OTE dipende da TT, TT non sa che OTE esiste) --
# l'isolamento di TT non e' violato.
from strategies.tt.liquidity_engine import (
    _detect_swings, _detect_equal_levels, _manual_atr,
    check_sweep, check_reaction,
)

from strategies.ote.confluence_engine import find_confluence_zones

try:
    from core.decision_ledger import ote_integration as ledger_link
except Exception:
    ledger_link = None

logger = logging.getLogger("ote.runner")

OTE_ASSETS = ["BTC_USDT", "XAU_USD"]
PROXIMITY_POINTS = {"XAU_USD": 12.5, "BTC_USDT": 150.0}
MIN_ZONE_SCORE = 40          # sotto questo score la zona non diventa candidate
MIN_RR = 1.2                 # RR minimo per emettere un segnale
SL_BUFFER_ATR = 0.5          # buffer ATR sotto/sopra il punto di sweep
                              # (bug trovato il 22/08: 0.2 dava SL piu'
                              # stretti di 1 candela M15, stoppati dal
                              # rumore. 0.5xATR_H1 ~= 1xATR_M15 su
                              # entrambi XAU/BTC, verificato sui dati)
CANDIDATE_EXPIRY_BARS = 48   # 48 cicli da 5min = 4 ore
SIGNAL_EXPIRY_BARS = 96      # 96 cicli da 5min = 8 ore
COOLDOWN_HOURS = 2           # dopo un candidate scaduto, stessa zona non ricreata per 2h

# Significativita' per tipo di livello (stessa scala di TT, usata per
# scegliere il target PIU' significativo, non il piu' vicino)
LEVEL_SIGNIFICANCE = {
    "SWING_HIGH": 2, "SWING_LOW": 2,
    "EQUAL_HIGH": 3, "EQUAL_LOW": 3,
    "IMPULSE_HIGH": 1, "IMPULSE_LOW": 1,
    "PREV_DAY_HIGH": 3, "PREV_DAY_LOW": 3,
    "ASIAN_HIGH": 3, "ASIAN_LOW": 3,
}


def _find_best_target(levels_in_direction: list, entry: float, sl: float,
                      direction: str) -> dict:
    """
    Versione OTE di select_dynamic_target — usa MIN_RR di OTE (1.2),
    non quello hardcoded in TT (1.5). Stessa logica: il livello PIU'
    SIGNIFICATIVO con RR sufficiente, non il piu' vicino.
    """
    risk = abs(entry - sl)
    if risk <= 0:
        return None
    valid = []
    for lv in levels_in_direction:
        reward = abs(lv["price"] - entry)
        rr = round(reward / risk, 3)
        if rr >= MIN_RR:
            sig = LEVEL_SIGNIFICANCE.get(lv["type"], 1)
            valid.append({"price": lv["price"], "type": lv["type"],
                         "rr": rr, "significance": sig})
    if not valid:
        return None
    return max(valid, key=lambda c: (c["significance"], -c["rr"]))


# ============================================================
# Session & Previous Day Liquidity Levels
# ============================================================

def _compute_session_levels(conn, asset: str, now: datetime) -> list:
    """
    4 livelli di liquidity universalmente osservati:
    - Previous Day High/Low (il max/min di ieri — quasi tutti i trader lo vedono)
    - Asian Session High/Low (00:00-08:00 UTC — il range notturno)

    Calcolati da candele gia' nel DB, nessun nuovo engine. Restituisce
    una lista di dict nello stesso formato di swings/equal levels, cosi'
    entrano nella stessa Liquidity Map senza codice speciale.
    """
    levels = []

    # ── Previous Day High/Low ──
    # Calcolato da candele H1 di ieri (piu' robusto di cercare una candela D1
    # specifica — non tutti i data provider usano lo stesso orario di taglio)
    try:
        yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
        today = now.strftime("%Y-%m-%d")

        # H1 candles di ieri
        rows = conn.execute("""
            SELECT MAX(high), MIN(low) FROM candles_cache
            WHERE asset=? AND timeframe='1h'
            AND datetime(timestamp/1000, 'unixepoch') >= ?
            AND datetime(timestamp/1000, 'unixepoch') < ?
        """, (asset, yesterday, today)).fetchone()

        if rows and rows[0] is not None and rows[1] is not None:
            levels.append({"type": "PREV_DAY_HIGH", "price": round(float(rows[0]), 5), "timestamp": 0})
            levels.append({"type": "PREV_DAY_LOW", "price": round(float(rows[1]), 5), "timestamp": 0})
    except Exception as e:
        logger.warning("OTE _compute_session_levels prev_day: %s", e)

    # ── Asian Session High/Low (00:00-08:00 UTC di oggi) ──
    try:
        asian_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        asian_end = now.replace(hour=8, minute=0, second=0, microsecond=0)

        # Solo se siamo dopo le 08:00 (la sessione asiatica e' gia' completa)
        if now >= asian_end:
            ts_start = int(asian_start.timestamp() * 1000)
            ts_end = int(asian_end.timestamp() * 1000)

            rows = conn.execute("""
                SELECT MAX(high), MIN(low) FROM v3_candles_cache
                WHERE asset=? AND timeframe='5m'
                AND timestamp >= ? AND timestamp < ?
            """, (asset, ts_start, ts_end)).fetchone()

            if rows and rows[0] is not None and rows[1] is not None:
                levels.append({"type": "ASIAN_HIGH", "price": round(float(rows[0]), 5), "timestamp": 0})
                levels.append({"type": "ASIAN_LOW", "price": round(float(rows[1]), 5), "timestamp": 0})
    except Exception as e:
        logger.warning("OTE _compute_session_levels asian: %s", e)

    return levels


# ============================================================
# Liquidity Map (neutrale — sopra E sotto, senza scegliere una direzione)
# ============================================================

def _build_neutral_liquidity(df_h1, zone_high: float, zone_low: float,
                             extra_levels: list = None) -> dict:
    """
    Mappa di liquidity SOPRA e SOTTO la zona, senza sapere ancora
    se il trade sara' BUY o SELL. Entrambi i lati servono, perche'
    la direzione la decide il mercato dopo, non noi prima.

    extra_levels: livelli aggiuntivi (Previous Day, Asian Session)
    calcolati da _compute_session_levels — stessa struttura dei swing,
    entrano nella mappa senza codice speciale.
    """
    swings = _detect_swings(df_h1)
    equal_levels = _detect_equal_levels(swings)

    all_levels = []
    for s in swings:
        all_levels.append({
            "type": "SWING_HIGH" if s["type"] == "HIGH" else "SWING_LOW",
            "price": s["price"], "timestamp": s.get("timestamp", 0),
        })
    all_levels.extend(equal_levels)

    if extra_levels:
        all_levels.extend(extra_levels)

    above = sorted(
        [lv for lv in all_levels if lv["price"] > zone_high],
        key=lambda lv: lv["price"]
    )
    below = sorted(
        [lv for lv in all_levels if lv["price"] < zone_low],
        key=lambda lv: -lv["price"]
    )

    nearest_above = above[0] if above else None
    nearest_below = below[0] if below else None

    def _fmt(lv):
        if lv is None:
            return {"type": None, "price": None, "distance": None}
        ref = zone_high if lv["price"] > zone_high else zone_low
        return {"type": lv["type"], "price": lv["price"],
                "distance": round(abs(lv["price"] - ref), 5)}

    return {
        "above": _fmt(nearest_above),
        "below": _fmt(nearest_below),
        "all_above": above,
        "all_below": below,
    }


# ============================================================
# Conferma direzionale (il momento chiave — il mercato MOSTRA)
# ============================================================

def _check_directional_confirmation(df_m5, zone_high: float, zone_low: float) -> dict:
    """
    Guarda le ultime candele M5 per capire se il mercato ha MOSTRATO
    una direzione — sweep di un lato della zona + rigetto.

    NON predice — reagisce a cio' che e' gia' successo.

    BUY: prezzo ha sweepato SOTTO zone_low (preso sell-side liquidity)
         e ha richiuso SOPRA → il mercato ha rifiutato il ribasso
    SELL: prezzo ha sweepato SOPRA zone_high (preso buy-side liquidity)
          e ha richiuso SOTTO → il mercato ha rifiutato il rialzo

    Controlla entrambe le direzioni — la zona e' neutra, il mercato decide.
    """
    if df_m5 is None or len(df_m5) < 3:
        return {"confirmed": False}

    # Check BUY: sweep sotto zone_low + rigetto verso l'alto
    sweep_buy = check_sweep(df_m5, "BUY", zone_low)
    if sweep_buy["confirmed"]:
        reaction_buy = check_reaction(df_m5, "BUY",
                                       from_index=sweep_buy.get("sweep_index"))
        if reaction_buy["confirmed"]:
            return {
                "confirmed": True, "direction": "BUY",
                "sweep": sweep_buy, "reaction": reaction_buy,
                "sweep_level_hit": sweep_buy.get("sweep_level_hit"),
            }

    # Check SELL: sweep sopra zone_high + rigetto verso il basso
    sweep_sell = check_sweep(df_m5, "SELL", zone_high)
    if sweep_sell["confirmed"]:
        reaction_sell = check_reaction(df_m5, "SELL",
                                        from_index=sweep_sell.get("sweep_index"))
        if reaction_sell["confirmed"]:
            return {
                "confirmed": True, "direction": "SELL",
                "sweep": sweep_sell, "reaction": reaction_sell,
                "sweep_level_hit": sweep_sell.get("sweep_level_hit"),
            }

    # Nessuna conferma — zona toccata ma il mercato non ha ancora deciso
    sweep_any = sweep_buy["confirmed"] or sweep_sell["confirmed"]
    return {
        "confirmed": False,
        "sweep_detected": sweep_any,
        "sweep_direction": ("BUY" if sweep_buy["confirmed"] else
                           ("SELL" if sweep_sell["confirmed"] else None)),
    }


# ============================================================
# Calcolo Entry/SL/TP DOPO la conferma direzionale
# ============================================================

def _compute_trade_plan(direction: str, df_h1, zone_high: float,
                        zone_low: float, sweep_level_hit: float,
                        liq_data: dict) -> dict:
    """
    Calcola Entry/SL/TP solo DOPO che il mercato ha mostrato la direzione.

    Entry: close corrente (market entry — la conferma e' gia' avvenuta)
           Nota: in un sistema live sarebbe il prezzo di mercato al momento
           della conferma, qui usiamo il close M5 piu' recente come proxy.

    SL: oltre il punto estremo dello sweep + buffer ATR
        (il punto piu' lontano raggiunto dal mercato prima di rigettare)

    TP: prossimo livello significativo nella direzione del trade
        (calcolato dall'entry, non dal prezzo — stessa correzione di LH)
    """
    atr = _manual_atr(df_h1)
    buffer = SL_BUFFER_ATR * atr if atr > 0 else 0

    if direction == "BUY":
        entry = zone_high  # bordo superiore della zona
        sl = (sweep_level_hit - buffer) if sweep_level_hit else (zone_low - buffer)
    else:
        entry = zone_low  # bordo inferiore della zona
        sl = (sweep_level_hit + buffer) if sweep_level_hit else (zone_high + buffer)

    risk = abs(entry - sl)
    if risk <= 0:
        return None

    # TP: prossimo livello significativo nella direzione del trade
    relevant = liq_data.get("above", []) if direction == "BUY" else liq_data.get("below", [])
    target = _find_best_target(relevant, entry, sl, direction)
    if target is None:
        return None

    rr = target["rr"]

    return {
        "planned_entry": round(entry, 5),
        "planned_sl": round(sl, 5),
        "planned_tp": round(target["price"], 5),
        "planned_rr": rr,
        "tp_type": target["type"],
        "tp_ref": f"{target['type']}@{target['price']}",
    }


# ============================================================
# Cooldown: la stessa zona non ricrea un candidate subito dopo uno scaduto
# ============================================================

def _has_recent_expired(conn, asset: str, zone_ref: str) -> bool:
    """Controlla se un candidate per questa zona e' scaduto di recente."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=COOLDOWN_HOURS)).isoformat()
    row = conn.execute("""
        SELECT 1 FROM ote_candidates
        WHERE asset=? AND zone_ref=? AND status IN ('EXPIRED','INVALIDATED')
        AND expired_at > ?
    """, (asset, zone_ref, cutoff)).fetchone()
    return row is not None


# ============================================================
# Notifiche
# ============================================================

def _notify_candidate(asset: str, zone: dict, liq: dict, trade_plans: dict, config: dict):
    """
    Notifica OPERATIVA — zona calda con ENTRAMBI gli scenari gia'
    calcolati. Frank vede il segnale PRIMA che il prezzo arrivi,
    con i piani pronti per entrambe le direzioni. Il sistema poi
    conferma/invalida in background per la raccolta dati.
    """
    try:
        from notifications import telegram_bot, ntfy_bot

        def fp(v):
            if v is None: return "N/A"
            return f"{v:,.2f}" if abs(float(v)) > 1000 else f"{v:.4f}"

        above = liq.get("above", {})
        below = liq.get("below", {})

        buy_plan = trade_plans.get("BUY")
        sell_plan = trade_plans.get("SELL")

        text = (
            f"\U0001f7e1 *OTE — SEGNALE*\n"
            f"*{asset.replace('_',' ')}*\n\n"
            f"Zona: `{fp(zone.get('zone_low'))}` - `{fp(zone.get('zone_high'))}` "
            f"(score {zone.get('restart_score', 0):.0f}/100, {zone.get('zone_strength', '?')})\n"
            f"Ricorrenza: {zone.get('confirmed_restarts', 0)} restart confermati\n\n"
        )

        if buy_plan:
            text += (
                f"\U0001f7e2 *Se sweep SOTTO + rigetto:*\n"
                f"  BUY entry `{fp(buy_plan['planned_entry'])}` "
                f"SL `{fp(buy_plan['planned_sl'])}` "
                f"TP `{fp(buy_plan['planned_tp'])}` "
                f"RR {buy_plan['planned_rr']:.2f}\n\n"
            )

        if sell_plan:
            text += (
                f"\U0001f534 *Se sweep SOPRA + rigetto:*\n"
                f"  SELL entry `{fp(sell_plan['planned_entry'])}` "
                f"SL `{fp(sell_plan['planned_sl'])}` "
                f"TP `{fp(sell_plan['planned_tp'])}` "
                f"RR {sell_plan['planned_rr']:.2f}\n\n"
            )

        if not buy_plan and not sell_plan:
            text += f"_Nessun scenario con RR sufficiente._\n\n"

        text += f"_Preparati — osserva la reazione alla zona._"

        bot_token = config.get("TELEGRAM_BOT_TOKEN", "")
        chat_id = config.get("TELEGRAM_CHAT_ID", "")
        ntfy_topic = config.get("NTFY_TOPIC", "")
        if bot_token and chat_id:
            telegram_bot.send_message(bot_token, chat_id, text)
        if ntfy_topic:
            title = f"OTE Segnale {asset.replace('_',' ')} (score {zone.get('restart_score', 0):.0f})"
            ntfy_bot.send_message(ntfy_topic, title, text.replace("*", "").replace("`", ""))
    except Exception as e:
        logger.warning("OTE _notify_candidate: %s", e)


def _notify_signal(asset: str, direction: str, trade_plan: dict, zone: dict, config: dict):
    """Notifica DIREZIONALE — il mercato ha mostrato BUY/SELL."""
    try:
        from notifications import telegram_bot, ntfy_bot

        emoji = "\U0001f7e2" if direction == "BUY" else "\U0001f534"

        def fp(v):
            if v is None: return "N/A"
            return f"{v:,.2f}" if abs(float(v)) > 1000 else f"{v:.4f}"

        text = (
            f"{emoji} *OTE — {direction}*\n"
            f"*{asset.replace('_',' ')}*\n\n"
            f"Entry: `{fp(trade_plan['planned_entry'])}`\n"
            f"SL: `{fp(trade_plan['planned_sl'])}`\n"
            f"TP: `{fp(trade_plan['planned_tp'])}` ({trade_plan.get('tp_type', '?')})\n"
            f"RR: {trade_plan['planned_rr']:.2f}\n\n"
            f"Zona: {zone.get('zone_strength', '?')} (score {zone.get('restart_score', 0):.0f})\n"
            f"_Direzione confermata da sweep+reaction M5._"
        )

        bot_token = config.get("TELEGRAM_BOT_TOKEN", "")
        chat_id = config.get("TELEGRAM_CHAT_ID", "")
        ntfy_topic = config.get("NTFY_TOPIC", "")
        if bot_token and chat_id:
            telegram_bot.send_message(bot_token, chat_id, text)
        if ntfy_topic:
            title = f"OTE {direction} {asset.replace('_',' ')} RR {trade_plan['planned_rr']:.1f}"
            ntfy_bot.send_message(ntfy_topic, title, text.replace("*", "").replace("`", ""))
    except Exception as e:
        logger.warning("OTE _notify_signal: %s", e)


# ============================================================
# Monitoraggio segnali ENTRY (TP/SL/EXPIRED)
# ============================================================

def _monitor_open_signals(conn, asset: str, df_m5):
    if df_m5 is None or len(df_m5) == 0:
        return
    current_high = float(df_m5.iloc[-1]["high"])
    current_low = float(df_m5.iloc[-1]["low"])

    for sig in ote_db.get_open_signals(conn, asset):
        sid = sig["signal_id"]
        direction = sig["direction"]
        entry = sig.get("actual_entry") or sig["planned_entry"]
        sl = sig.get("actual_sl") or sig["planned_sl"]
        tp = sig.get("actual_tp") or sig["planned_tp"]
        mae = float(sig.get("mae") or 0)
        mfe = float(sig.get("mfe") or 0)
        bars_open = (sig.get("bars_open") or 0) + 1

        if entry is None or sl is None:
            continue

        if direction == "BUY":
            adverse = max(entry - current_low, 0.0)
            favorable = max(current_high - entry, 0.0)
            sl_hit = current_low <= sl
            tp_hit = tp is not None and current_high >= tp
        else:
            adverse = max(current_high - entry, 0.0)
            favorable = max(entry - current_low, 0.0)
            sl_hit = current_high >= sl
            tp_hit = tp is not None and current_low <= tp

        new_mae = max(mae, adverse)
        new_mfe = max(mfe, favorable)

        if sl_hit:
            risk = abs(entry - sl)
            result_r = round(-risk / risk, 3) if risk > 0 else -1.0
            ote_db.close_signal(conn, sid, "SL", result_r=result_r,
                               mae=new_mae, mfe=new_mfe, bars_open=bars_open)
            logger.info("OTE [%s]: %s -> SL", asset, sid[:8])
            if ledger_link:
                try:
                    ledger_link.link_outcome(sid, "SL", entry, sl, mae=new_mae, mfe=new_mfe, duration_bars=bars_open)
                except Exception as e:
                    logger.warning("OTE ledger link_outcome: %s", e)
        elif tp_hit:
            risk = abs(entry - sl)
            reward = abs(tp - entry)
            rr = round(reward / risk, 3) if risk > 0 else 0
            ote_db.close_signal(conn, sid, "TP", result_r=rr,
                               mae=new_mae, mfe=new_mfe, bars_open=bars_open)
            logger.info("OTE [%s]: %s -> TP (+%.2fR)", asset, sid[:8], rr)
            if ledger_link:
                try:
                    ledger_link.link_outcome(sid, "TP", entry, sl, mae=new_mae, mfe=new_mfe,
                                            duration_bars=bars_open, rr_planned=rr)
                except Exception as e:
                    logger.warning("OTE ledger link_outcome: %s", e)
        elif bars_open >= (sig.get("expiry_bars") or SIGNAL_EXPIRY_BARS):
            ote_db.close_signal(conn, sid, "EXPIRED", result_r=0,
                               mae=new_mae, mfe=new_mfe, bars_open=bars_open)
            logger.info("OTE [%s]: %s -> EXPIRED", asset, sid[:8])
            if ledger_link:
                try:
                    ledger_link.link_outcome(sid, "EXPIRED", entry, sl, mae=new_mae, mfe=new_mfe, duration_bars=bars_open)
                except Exception as e:
                    logger.warning("OTE ledger link_outcome: %s", e)
        else:
            conn.execute(
                "UPDATE ote_signals SET mae=?, mfe=?, bars_open=? WHERE signal_id=?",
                (new_mae, new_mfe, bars_open, sid))
            conn.commit()


# ============================================================
# Per-asset runner
# ============================================================

def _read_mie_snapshots(conn, asset: str, now: datetime) -> dict:
    """
    Legge gli snapshot degli engine MIE piu' recenti per il Decision
    Ledger. Solo Structure e Reaction Map per ora -- i due con edge
    piu' forte e consistente su Engine Edge Lab (Structure invertito
    su 4 strategie, Reaction Map positivo su 3). Le chiavi del dict
    restituito corrispondono a ENGINE_REPORTERS in decision_collector.py.
    Nessun crash se mancano: OTE resta un lettore passivo.
    """
    import json as _json
    result = {}
    ts_iso = now.isoformat()
    try:
        row = conn.execute("""
            SELECT snapshot_json FROM structure_snapshots WHERE asset=?
            ORDER BY ABS(strftime('%s',timestamp_snapshot)-strftime('%s',?)) LIMIT 1
        """, (asset, ts_iso)).fetchone()
        if row:
            result["structure"] = _json.loads(row[0])
    except Exception as e:
        logger.warning("OTE _read_mie_snapshots structure: %s", e)

    try:
        row2 = conn.execute("""
            SELECT snapshot_json FROM reaction_map_snapshots WHERE asset=?
            ORDER BY ABS(strftime('%s',timestamp_snapshot)-strftime('%s',?)) LIMIT 1
        """, (asset, ts_iso)).fetchone()
        if row2:
            result["reaction_map"] = _json.loads(row2[0])
    except Exception as e:
        logger.warning("OTE _read_mie_snapshots reaction_map: %s", e)

    return result


def _run_for_asset(conn, asset: str, config: dict, now: datetime):
    # XAU chiuso nel weekend
    if asset == "XAU_USD":
        wd = now.weekday()
        if wd == 6 or (wd == 5 and now.hour >= 22) or (wd == 4 and now.hour >= 22):
            logger.info("OTE [%s]: mercato chiuso (weekend), skip.", asset)
            return

    logger.info("OTE: inizio ciclo per %s", asset)

    limit = config.get("BOOTSTRAP_TARGET_CANDLES", 300)
    df_h1 = core_db.get_candles_df(conn, asset, "1h", limit=limit)
    df_m5 = v3_db.get_v3_candles_df(conn, asset, "5m", limit=100)

    if df_h1 is None or len(df_h1) < 20:
        logger.warning("OTE [%s]: dati H1 insufficienti, skip.", asset)
        return
    if df_m5 is None or len(df_m5) < 5:
        logger.warning("OTE [%s]: dati M5 insufficienti, skip.", asset)
        return

    current_price = float(df_m5.iloc[-1]["close"])
    proximity_threshold = PROXIMITY_POINTS.get(asset, 15.0)

    # Calcolo una volta i livelli Previous Day + Asian Session
    session_levels = _compute_session_levels(conn, asset, now)

    # Engine MIE per il Decision Ledger (Structure + Reaction Map --
    # i due con edge piu' forte e consistente su Engine Edge Lab).
    # Nomi delle chiavi = esattamente quelli attesi da ENGINE_REPORTERS
    # in decision_collector.py, non toccati -- solo letti.
    mie_snapshots = _read_mie_snapshots(conn, asset, now)

    # ── 1. Monitoraggio segnali ENTRY aperti ─────────────────
    try:
        _monitor_open_signals(conn, asset, df_m5)
    except Exception as e:
        logger.error("OTE [%s]: errore monitoraggio segnali: %s", asset, e)

    # ── 2. Monitoraggio candidate WATCHING/TOUCHED ───────────
    try:
        for cand in ote_db.get_watching_candidates(conn, asset):
            cid = cand["candidate_id"]
            zh, zl = cand["zone_high"], cand["zone_low"]

            # Scadenza
            try:
                created = datetime.fromisoformat(cand["created_at"])
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                elapsed_bars = int((now - created).total_seconds() / 300)  # barre M5
            except Exception:
                elapsed_bars = 0

            if elapsed_bars >= CANDIDATE_EXPIRY_BARS:
                ote_db.expire_candidate(conn, cid)
                logger.info("OTE [%s]: candidate %s scaduto dopo %d barre.", asset, cid[:8], elapsed_bars)
                continue

            # Il prezzo e' dentro la zona?
            price_in_zone = current_price >= zl and current_price <= zh
            if price_in_zone:
                ote_db.update_candidate_touch(conn, cid)

            # Conferma direzionale su M5 — il momento chiave
            confirmation = _check_directional_confirmation(df_m5, zh, zl)

            # Registra sweep anche se non c'e' ancora reaction
            if confirmation.get("sweep_detected") and not confirmation["confirmed"]:
                sweep_dir = confirmation.get("sweep_direction")
                if sweep_dir:
                    ote_db.update_candidate_sweep(conn, cid, sweep_dir)
                continue

            if not confirmation["confirmed"]:
                continue

            # Direzione confermata — il mercato ha mostrato BUY o SELL
            direction = confirmation["direction"]
            sweep = confirmation["sweep"]
            reaction = confirmation["reaction"]

            ote_db.update_candidate_sweep(conn, cid, direction)
            ote_db.update_candidate_reaction(conn, cid, direction)

            logger.info("OTE [%s]: candidate %s — %s confermato (sweep+reaction M5).",
                       asset, cid[:8], direction)

            # Calcola Entry/SL/TP — DOPO aver visto la direzione
            liq_data = _build_neutral_liquidity(df_h1, zh, zl, extra_levels=session_levels)

            # Ricostruisco il formato che _find_best_target si aspetta
            liq_for_target = {
                "above": [{"type": lv["type"], "price": lv["price"],
                           "distance_from_poi": abs(lv["price"] - zh)}
                          for lv in liq_data.get("all_above", [])],
                "below": [{"type": lv["type"], "price": lv["price"],
                           "distance_from_poi": abs(lv["price"] - zl)}
                          for lv in liq_data.get("all_below", [])],
            }

            trade_plan = _compute_trade_plan(
                direction, df_h1, zh, zl,
                sweep_level_hit=confirmation.get("sweep_level_hit"),
                liq_data=liq_for_target,
            )

            if trade_plan is None:
                logger.info("OTE [%s]: direzione confermata ma nessun trade plan valido (RR?).", asset)
                continue

            # Crea il SIGNAL
            signal_data = {
                **trade_plan,
                "trigger_type": "sweep_reaction",
                "sweep_level": confirmation.get("sweep_level_hit"),
                "reaction_type": reaction.get("reaction_type"),
                "zone_ref": cand.get("zone_ref"),
                "zone_score": cand.get("zone_score"),
                "zone_strength": cand.get("zone_strength"),
                "quality_score": _compute_quality(cand, trade_plan),
                "quality_label": None,  # calcolato sotto
            }
            qs = signal_data["quality_score"]
            signal_data["quality_label"] = "HIGH" if qs >= 8 else ("MEDIUM" if qs >= 5 else "LOW")

            sid = ote_db.insert_signal(conn, cid, asset, direction, signal_data)
            ote_db.link_candidate_to_signal(conn, cid, sid)

            logger.info(
                "OTE [%s %s]: SIGNAL CREATO (id=%s) entry=%.2f sl=%.2f tp=%.2f rr=%.2f quality=%d/%s",
                asset, direction, sid[:8], trade_plan["planned_entry"],
                trade_plan["planned_sl"], trade_plan["planned_tp"],
                trade_plan["planned_rr"], qs, signal_data["quality_label"],
            )

            zone_for_notify = {
                "zone_high": zh, "zone_low": zl,
                "restart_score": cand.get("zone_score"),
                "zone_strength": cand.get("zone_strength"),
            }
            _notify_signal(asset, direction, trade_plan, zone_for_notify, config)

            if ledger_link:
                try:
                    ledger_link.capture_executed(sid, asset, direction, signal_data, mie_snapshots=mie_snapshots)
                except Exception as e:
                    logger.warning("OTE ledger capture_executed: %s", e)

    except Exception as e:
        logger.error("OTE [%s]: errore monitoraggio candidate: %s", asset, e, exc_info=True)

    # ── 3. Nuovi candidate dal Cervello di Confluenza ─────────
    # Non piu' solo LH — legge TUTTE le fonti (LH + OB + FVG +
    # Swing + Equal + Session + Reaction Map) e trova dove convergono.
    try:
        confluence_zones = find_confluence_zones(conn, asset, df_h1, current_price, now)

        for zone in confluence_zones:
            zh = zone["zone_high"]
            zl = zone["zone_low"]
            zref = zone["zone_ref"]

            # Proximity
            dist = min(abs(current_price - zh), abs(current_price - zl))
            if dist > proximity_threshold:
                continue

            # Dedup: un solo candidate attivo per zona
            if ote_db.has_active_candidate(conn, asset, zref):
                continue

            # Cooldown: non ricreare subito dopo uno scaduto
            if _has_recent_expired(conn, asset, zref):
                continue

            # Liquidity Map neutra (inclusi Previous Day + Asian Session)
            liq_data = _build_neutral_liquidity(df_h1, zh, zl, extra_levels=session_levels)

            zone_dict = {
                "zone_ref": zref, "zone_high": zh, "zone_low": zl,
                "restart_score": zone.get("restart_score", zone["confluence_score"] * 10),
                "zone_strength": zone["zone_strength"],
                "confirmed_restarts": zone.get("confirmed_restarts", 0),
                "failed_visits": zone.get("failed_visits", 0),
                "confluence_score": zone["confluence_score"],
                "source_count": zone["source_count"],
                "sources": zone["sources"],
            }

            cid = ote_db.insert_candidate(
                conn, asset, zone_dict,
                liq_above=liq_data["above"], liq_below=liq_data["below"],
                proximity_points=round(dist, 2),
                reaction_map_score=zone.get("reaction_map_score"),
            )

            # Pre-calcolo ENTRAMBI gli scenari (BUY e SELL)
            liq_for_target = {
                "above": [{"type": lv["type"], "price": lv["price"],
                           "distance_from_poi": abs(lv["price"] - zh)}
                          for lv in liq_data.get("all_above", [])],
                "below": [{"type": lv["type"], "price": lv["price"],
                           "distance_from_poi": abs(lv["price"] - zl)}
                          for lv in liq_data.get("all_below", [])],
            }
            trade_plans = {}
            buy_plan = _compute_trade_plan("BUY", df_h1, zh, zl, sweep_level_hit=zl, liq_data=liq_for_target)
            if buy_plan:
                trade_plans["BUY"] = buy_plan
            sell_plan = _compute_trade_plan("SELL", df_h1, zh, zl, sweep_level_hit=zh, liq_data=liq_for_target)
            if sell_plan:
                trade_plans["SELL"] = sell_plan

            sources_str = "+".join(zone["sources"])
            logger.info(
                "OTE [%s]: CANDIDATE creato (id=%s) conf=%d sources=%s zona=[%.2f,%.2f] dist=%.1f — BUY:%s SELL:%s",
                asset, cid[:8], zone["confluence_score"], sources_str, zl, zh, dist,
                f"RR={buy_plan['planned_rr']:.2f}" if buy_plan else "no",
                f"RR={sell_plan['planned_rr']:.2f}" if sell_plan else "no",
            )

            _notify_candidate(asset, zone_dict, liq_data, trade_plans, config)

            if ledger_link:
                try:
                    ledger_link.capture_candidate(cid, asset, mie_snapshots=mie_snapshots)
                except Exception as e:
                    logger.warning("OTE ledger capture_candidate: %s", e)

    except Exception as e:
        logger.error("OTE [%s]: errore creazione candidate: %s", asset, e, exc_info=True)


def _compute_quality(cand: dict, trade_plan: dict) -> int:
    """Quality score semplice, Fase A — pochi fattori forti."""
    score = 3  # base: il setup ha superato tutti i gate
    zone_score = cand.get("zone_score") or 0
    if zone_score >= 80:
        score += 3
    elif zone_score >= 60:
        score += 2
    elif zone_score >= 40:
        score += 1

    rr = trade_plan.get("planned_rr", 0)
    if rr >= 2.0:
        score += 2
    elif rr >= 1.5:
        score += 1

    recurrence = cand.get("zone_recurrence") or 0
    if recurrence >= 5:
        score += 2
    elif recurrence >= 3:
        score += 1

    return min(score, 12)


# ============================================================
# Entry point
# ============================================================

def run_ote_scan(config: dict):
    conn = core_db.get_connection(config["DB_PATH"])
    ote_db.init_ote_schema(conn)

    now = datetime.now(timezone.utc)
    assets = config.get("OTE_SCANNER", {}).get("assets", OTE_ASSETS)

    logger.info("=== OTE Scanner: inizio ciclo (%s) ===", ", ".join(assets))

    for asset in assets:
        try:
            _run_for_asset(conn, asset, config, now)
        except Exception as e:
            logger.error("OTE [%s]: errore non gestito: %s", asset, e, exc_info=True)

    conn.close()
    logger.info("=== OTE Scanner: fine ciclo ===")
