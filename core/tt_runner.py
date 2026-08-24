"""
core/tt_runner.py
TT — Runner (Fase 10)

Collega tutti i moduli isolati (direction_engine, location_engine,
liquidity_engine, tt_db) ai dati reali. Stesso pattern gia' usato da
lh_runner.py/edge_lab_runner.py per candele/notifiche/DB -- quelle sono
infrastruttura generica condivisa (accesso storage), non logica di
un'altra strategia, quindi riusarle non viola l'isolamento di TT.

Ciclo per asset (BTC_USDT, XAU_USD):
    1. Carica H4/H1/M15/M5
    2. Monitora i segnali WAITING_CONFIRMATION esistenti:
       - il prezzo ha toccato la POI? -> valuta l'esecuzione 5M
       - il setup e' ancora valido? (bias, POI, scadenza)
    3. Monitora i segnali ENTRY (aperti): TP/SL/EXPIRED
    4. Cerca un nuovo Early Signal (solo se nessun setup attivo sulla
       stessa POI -- niente duplicati, spec sezione 27)
"""

import json
import logging
from datetime import datetime, timezone, timedelta

from storage import db as core_db
from core import v3_db
from core import tt_db

from strategies.tt.liquidity_engine import evaluate_setup, evaluate_execution
from strategies.tt.direction_engine import compute_direction_4h

try:
    from core.decision_ledger import tt_integration as ledger_link
except Exception:
    ledger_link = None  # Decision Ledger non disponibile -- TT funziona comunque

logger = logging.getLogger("tt.runner")

TT_ASSETS = ["BTC_USDT", "XAU_USD"]
TT_TIMEFRAMES = {"H4": "4h", "H1": "1h", "M15": "15m", "M5": "5m"}

# Quanto lontano dal SL prima di considerare un WAITING_CONFIRMATION
# ormai "andato" senza essere mai entrato (invalidazione, non una loss:
# il trade non e' mai partito). Non calibrato su dati reali.
MAX_ADVERSE_MOVE_ATR = 1.0

# Cooldown dopo un'invalidazione: la stessa POI (o una POI con prezzo
# sovrapposto) non genera un nuovo Early Signal per questo tempo.
# Bug osservato il 24/08: la stessa POI [4373.74-4386.16] ha generato
# 13 Early Signal identici in ~35 ore, ognuno scaduto (EXPIRED_WAITING)
# e immediatamente riproposto senza pausa -- stesso problema gia'
# risolto per LH/OTE.
COOLDOWN_HOURS = 2


def _prepare_dataframes(conn, asset: str, config: dict):
    limit = config.get("BOOTSTRAP_TARGET_CANDLES", 300)
    df_h4 = core_db.get_candles_df(conn, asset, TT_TIMEFRAMES["H4"], limit=limit)
    df_h1 = core_db.get_candles_df(conn, asset, TT_TIMEFRAMES["H1"], limit=limit)
    df_m15 = v3_db.get_v3_candles_df(conn, asset, TT_TIMEFRAMES["M15"], limit=limit)
    df_m5 = v3_db.get_v3_candles_df(conn, asset, TT_TIMEFRAMES["M5"], limit=100)
    return df_h4, df_h1, df_m15, df_m5


def _price_touched_poi(df_m5, poi_low: float, poi_high: float) -> bool:
    """Il prezzo (M5 recente) e' mai entrato nel range della POI?"""
    if df_m5 is None or len(df_m5) == 0:
        return False
    recent = df_m5.iloc[-6:]  # ultimi ~30 minuti
    for _, row in recent.iterrows():
        lo, hi = float(row["low"]), float(row["high"])
        if lo <= poi_high and hi >= poi_low:
            return True
    return False


def _has_recent_invalidated_poi(conn, asset: str, direction: str,
                                poi_low: float, poi_high: float) -> bool:
    """
    Cooldown: una POI invalidata di recente (stesso poi_ref O area di
    prezzo sovrapposta) non genera un nuovo Early Signal per
    COOLDOWN_HOURS. Controlla sia il match esatto (poi_ref uguale) sia
    la sovrapposizione di prezzo, per coprire entrambe le classi di bug
    gia' viste su LH/OTE.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=COOLDOWN_HOURS)).isoformat()
    rows = conn.execute(
        """
        SELECT poi_low, poi_high FROM tt_signals
        WHERE asset=? AND direction=? AND status='INVALIDATED' AND closed_at > ?
        """,
        (asset, direction, cutoff),
    ).fetchall()
    for prev_low, prev_high in rows:
        if prev_low is None or prev_high is None:
            continue
        # Sovrapposizione: le due zone si intersecano?
        if poi_low <= prev_high and prev_low <= poi_high:
            return True
    return False


def _monitor_open_signals(conn, asset: str, df_m5, now):
    """TP/SL/EXPIRED per i segnali gia' in ENTRY -- stesso pattern gia' visto altrove."""
    if df_m5 is None or len(df_m5) == 0:
        return
    current_high = float(df_m5.iloc[-1]["high"])
    current_low = float(df_m5.iloc[-1]["low"])

    import sqlite3
    rows = conn.execute(
        "SELECT signal_id, direction, actual_entry, actual_sl, actual_tp, mae, mfe, bars_open, expiry_bars_open "
        "FROM tt_signals WHERE asset=? AND status='ENTRY'", (asset,)
    ).fetchall()

    for row in rows:
        sid, direction, entry, sl, tp, mae, mfe, bars_open, expiry = row
        if entry is None or sl is None:
            continue
        bars_open = (bars_open or 0) + 1

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

        new_mae = max(float(mae or 0), adverse)
        new_mfe = max(float(mfe or 0), favorable)

        if sl_hit:
            result_r = round(-(abs(entry-sl))/abs(entry-sl), 3) if entry != sl else 0
            tt_db.close_signal(conn, sid, "SL", result_r=result_r,
                              mae=new_mae, mfe=new_mfe, bars_open=bars_open)
            logger.info("TT [%s]: %s -> SL", asset, sid[:8])
            if ledger_link:
                try:
                    ledger_link.link_outcome(sid, "SL", entry, sl, mae=new_mae, mfe=new_mfe, duration_bars=bars_open)
                except Exception as e:
                    logger.warning("TT [%s]: ledger link_outcome fallito (non-blocking): %s", asset, e)
        elif tp_hit:
            risk = abs(entry - sl)
            reward = abs(tp - entry)
            rr = round(reward / risk, 3) if risk > 0 else 0
            tt_db.close_signal(conn, sid, "TP", result_r=rr, mae=new_mae, mfe=new_mfe, bars_open=bars_open)
            logger.info("TT [%s]: %s -> TP (+%.2fR)", asset, sid[:8], rr)
            if ledger_link:
                try:
                    ledger_link.link_outcome(sid, "TP", entry, sl, mae=new_mae, mfe=new_mfe,
                                            duration_bars=bars_open, rr_planned=rr)
                except Exception as e:
                    logger.warning("TT [%s]: ledger link_outcome fallito (non-blocking): %s", asset, e)
        elif bars_open >= (expiry or 96):
            tt_db.close_signal(conn, sid, "EXPIRED", result_r=0, mae=new_mae, mfe=new_mfe, bars_open=bars_open)
            logger.info("TT [%s]: %s -> EXPIRED", asset, sid[:8])
            if ledger_link:
                try:
                    ledger_link.link_outcome(sid, "EXPIRED", entry, sl, mae=new_mae, mfe=new_mfe, duration_bars=bars_open)
                except Exception as e:
                    logger.warning("TT [%s]: ledger link_outcome fallito (non-blocking): %s", asset, e)
        else:
            conn.execute(
                "UPDATE tt_signals SET mae=?, mfe=?, bars_open=? WHERE signal_id=?",
                (new_mae, new_mfe, bars_open, sid),
            )
            conn.commit()


def _notify_early_signal(asset: str, signal: dict, config: dict):
    try:
        from notifications import telegram_bot, ntfy_bot
        direction = signal["direction"]
        emoji = "\U0001f7e2" if direction == "BUY" else "\U0001f534"

        def fp(v):
            return f"{v:,.2f}" if abs(v) > 1000 else f"{v:.4f}"

        text = (
            f"{emoji} *TT — EARLY SIGNAL*\n"
            f"*{asset.replace('_',' ')}* — {direction}\n\n"
            f"POI: {signal['poi_type']} `{fp(signal['poi_low'])}` - `{fp(signal['poi_high'])}` "
            f"(quality {signal['poi_quality']}/10)\n"
            f"PD: {signal['pd_zone']} ({signal['pd_pct']:.0f}%)\n"
            f"15M: {signal['ctx_15m_structure']}, {signal['ctx_15m_momentum']}\n"
            f"Prossimita': {signal['proximity_points']:.1f} punti\n\n"
            f"Planned Entry: `{fp(signal['planned_entry'])}`\n"
            f"Planned SL: `{fp(signal['planned_sl'])}`\n"
            f"Planned TP: `{fp(signal['planned_tp'])}` ({signal['planned_tp_type']})\n"
            f"RR: {signal['planned_rr']:.2f}\n\n"
            f"_In attesa di conferma -- non e' ancora un trade._"
        )
        bot_token = config.get("TELEGRAM_BOT_TOKEN", "")
        chat_id = config.get("TELEGRAM_CHAT_ID", "")
        ntfy_topic = config.get("NTFY_TOPIC", "")
        if bot_token and chat_id:
            telegram_bot.send_message(bot_token, chat_id, text)
        if ntfy_topic:
            title = f"TT Early Signal {asset.replace('_',' ')} {direction}"
            ntfy_bot.send_message(ntfy_topic, title, text.replace("*", "").replace("`", ""))
    except Exception as e:
        logger.warning("TT _notify_early_signal: %s", e)


def _notify_entry(asset, direction, signal, entry, sl, tp, config):
    try:
        from notifications import telegram_bot, ntfy_bot
        emoji = "\U0001f7e2" if direction == "BUY" else "\U0001f534"

        def fp(v):
            return f"{v:,.2f}" if abs(v) > 1000 else f"{v:.4f}"

        text = (
            f"{emoji} *TT — ENTRY CONFERMATA*\n"
            f"*{asset.replace('_',' ')}* — {direction}\n\n"
            f"Entry: `{fp(entry)}`\n"
            f"SL: `{fp(sl)}`\n"
            f"TP: `{fp(tp)}`\n"
        )
        bot_token = config.get("TELEGRAM_BOT_TOKEN", "")
        chat_id = config.get("TELEGRAM_CHAT_ID", "")
        ntfy_topic = config.get("NTFY_TOPIC", "")
        if bot_token and chat_id:
            telegram_bot.send_message(bot_token, chat_id, text)
        if ntfy_topic:
            title = f"TT ENTRY {asset.replace('_',' ')} {direction}"
            ntfy_bot.send_message(ntfy_topic, title, text.replace("*", "").replace("`", ""))
    except Exception as e:
        logger.warning("TT _notify_entry: %s", e)


def _read_reaction_map_score(conn, asset: str) -> float:
    """
    Legge la Reaction Map piu' recente -- l'unico engine con edge
    positivo confermato su piu' strategie (verificato il 24/08: +4.8%
    V41P1, +4.1% TRB). Usa la stessa zona "strongest_below"/"strongest_above"
    gia' letta da OTE, senza assumere una direzione (il chiamante decide
    quale lato guardare, qui restituiamo solo il punteggio della zona
    piu' forte in generale come proxy semplice).
    """
    try:
        row = conn.execute(
            "SELECT snapshot_json FROM reaction_map_snapshots WHERE asset=? "
            "ORDER BY timestamp_snapshot DESC LIMIT 1", (asset,)
        ).fetchone()
        if not row:
            return None
        snap = json.loads(row[0])
        below = snap.get("strongest_below") or {}
        above = snap.get("strongest_above") or {}
        scores = [z.get("confluence_score", 0) for z in (below, above) if z]
        return max(scores) if scores else None
    except Exception as e:
        logger.debug("TT _read_reaction_map_score [%s]: %s", asset, e)
        return None


def _classify_regime(conn, asset: str) -> str:
    """
    Classificatore di regime minimale -- stesso principio di
    decision_collector.classify_regime, riletto qui per non introdurre
    una dipendenza incrociata con l'infrastruttura del Decision Ledger
    (TT resta isolato). Legge structure_snapshots + volatility_snapshots,
    le stesse due tabelle gia' lette da OTE.
    """
    try:
        srow = conn.execute(
            "SELECT snapshot_json FROM structure_snapshots WHERE asset=? "
            "ORDER BY timestamp_snapshot DESC LIMIT 1", (asset,)
        ).fetchone()
        vrow = conn.execute(
            "SELECT snapshot_json FROM volatility_snapshots WHERE asset=? "
            "ORDER BY timestamp_snapshot DESC LIMIT 1", (asset,)
        ).fetchone()
        if not srow:
            return "UNKNOWN"
        s = json.loads(srow[0])
        v = json.loads(vrow[0]) if vrow else {}

        th = s.get("trend_health", {})
        current_trend = th.get("current_trend", "NEUTRAL")
        impulse_count = th.get("impulse_count", 0)
        h4 = s.get("structure_h4", {}).get("classification", "NEUTRAL")
        vol_regime = v.get("regime", "NORMAL")
        contracting = v.get("contracting", False)

        if h4 in ("BULLISH", "BEARISH") and current_trend == h4 and impulse_count >= 1:
            return "TRENDING"
        if h4 == "NEUTRAL" or contracting or vol_regime == "CONTRACTING":
            return "RANGING"
        return "TRANSITIONAL"
    except Exception as e:
        logger.debug("TT _classify_regime [%s]: %s", asset, e)
        return "UNKNOWN"


def _run_for_asset(conn, asset: str, config: dict, now: datetime):
    logger.info("TT: inizio ciclo per %s", asset)

    df_h4, df_h1, df_m15, df_m5 = _prepare_dataframes(conn, asset, config)

    if len(df_h4) < 15 or len(df_h1) < 20 or len(df_m15) < 15:
        logger.warning("TT [%s]: dati insufficienti, skip.", asset)
        return

    # ── Monitoraggio segnali WAITING_CONFIRMATION ────────────
    try:
        waiting = tt_db.get_waiting_signals(conn, asset)
        current_price = float(df_m5.iloc[-1]["close"]) if df_m5 is not None and len(df_m5) > 0 else None

        for sig in waiting:
            sid = sig["signal_id"]
            direction = sig["direction"]

            new_bars = tt_db.increment_bars_waiting(conn, sid)
            if new_bars >= sig.get("expiry_bars_waiting", 24):
                tt_db.invalidate_signal(conn, sid, "EXPIRED_WAITING")
                logger.info("TT [%s %s]: WAITING scaduto, INVALIDATED.", asset, direction)
                continue

            if current_price is not None:
                sl = sig["planned_sl"]
                if (direction == "BUY" and current_price < sl) or \
                   (direction == "SELL" and current_price > sl):
                    tt_db.invalidate_signal(conn, sid, "PRICE_PASSED_SL_BEFORE_ENTRY")
                    logger.info("TT [%s %s]: prezzo oltre SL prima dell'entry, INVALIDATED.", asset, direction)
                    continue

            # 4H bias ancora valido? (spec sezione 26: "4H bias cambia
            # significativamente" e' un motivo esplicito di invalidazione)
            current_dir_ctx = compute_direction_4h(df_h4)
            if current_dir_ctx["direction"] != sig["direction_4h"]:
                tt_db.invalidate_signal(conn, sid, "4H_BIAS_CHANGED")
                logger.info("TT [%s %s]: bias 4H cambiato (%s -> %s), INVALIDATED.",
                           asset, direction, sig["direction_4h"], current_dir_ctx["direction"])
                continue

            poi_low, poi_high = sig["poi_low"], sig["poi_high"]
            if not _price_touched_poi(df_m5, poi_low, poi_high):
                continue

            logger.info("TT [%s %s]: tocco POI, valuto esecuzione 5M...", asset, direction)
            exec_result = evaluate_execution(
                df_m5, direction, sig["sweep_target_level"],
                setup_type=sig.get("setup_type", "CONSERVATIVE"),
            )

            if exec_result["entry_confirmed"]:
                sweep = exec_result.get("sweep") or {}
                reaction = exec_result.get("reaction") or {}
                structure = exec_result.get("structure") or {}
                tt_db.confirm_entry(
                    conn, sid, actual_entry=current_price, actual_sl=sig["planned_sl"],
                    actual_tp=sig["planned_tp"], touch_ts=now.isoformat(),
                    sweep_level=sweep.get("sweep_level_hit"),
                    reaction_type=reaction.get("reaction_type"),
                    structure_level=structure.get("broken_level"),
                )
                logger.info("TT [%s %s]: ENTRY CONFERMATA @ %.4f", asset, direction, current_price)
                _notify_entry(asset, direction, sig, current_price, sig["planned_sl"], sig["planned_tp"], config)

                if ledger_link:
                    try:
                        sig_for_ledger = dict(sig)
                        raw_snap = sig_for_ledger.get("context_snapshot")
                        if isinstance(raw_snap, str):
                            sig_for_ledger["context_snapshot"] = json.loads(raw_snap)
                        ledger_link.capture_executed(sid, asset, sig_for_ledger)
                    except Exception as e:
                        logger.warning("TT [%s]: ledger capture_executed fallito (non-blocking): %s", asset, e)
            else:
                logger.info("TT [%s %s]: esecuzione non confermata (%s).",
                           asset, direction, exec_result.get("rejection"))
    except Exception as e:
        logger.error("TT [%s]: errore monitoraggio WAITING: %s", asset, e)

    # ── Monitoraggio segnali ENTRY (aperti) ──────────────────
    try:
        _monitor_open_signals(conn, asset, df_m5, now)
    except Exception as e:
        logger.error("TT [%s]: errore monitoraggio ENTRY: %s", asset, e)

    # ── Nuovo Early Signal ────────────────────────────────────
    try:
        current_price = float(df_m5.iloc[-1]["close"]) if df_m5 is not None and len(df_m5) > 0 else float(df_m15.iloc[-1]["close"])
        reaction_map_score = _read_reaction_map_score(conn, asset)
        regime = _classify_regime(conn, asset)
        result = evaluate_setup(asset, df_h4, df_h1, df_m15, current_price,
                                reaction_map_score=reaction_map_score, regime=regime)
        signal = result["signal"]

        if signal is None:
            rejection = result["diagnostics"].get("rejection", "?")
            logger.info("TT [%s]: no signal — %s", asset, rejection)
            if ledger_link:
                try:
                    import uuid
                    diag = result["diagnostics"]
                    fake_signal = {
                        "direction": diag.get("direction_4h"),
                        "context_snapshot": {
                            "direction": {"direction": diag.get("direction_4h")},
                            "poi": diag.get("poi"),
                            "liquidity": diag.get("liquidity"),
                            "premium_discount": diag.get("premium_discount"),
                            "context_15m": diag.get("context_15m"),
                        },
                    }
                    ledger_link.capture_rejected(
                        str(uuid.uuid4()), asset, diag.get("direction_4h"), rejection,
                        signal=fake_signal,
                    )
                except Exception as e:
                    logger.warning("TT [%s]: ledger capture_rejected fallito (non-blocking): %s", asset, e)
            return

        if tt_db.has_active_setup_for_poi(conn, asset, signal["direction"], signal["poi_ref"]):
            logger.info("TT [%s %s]: setup gia' attivo su questa POI, skip (no duplicati).",
                       asset, signal["direction"])
            return

        if _has_recent_invalidated_poi(conn, asset, signal["direction"],
                                       signal["poi_low"], signal["poi_high"]):
            logger.info("TT [%s %s]: POI invalidata di recente (cooldown %dh), skip.",
                       asset, signal["direction"], COOLDOWN_HOURS)
            return

        sid = tt_db.insert_tt_signal(conn, signal)
        logger.info("TT [%s %s]: EARLY SIGNAL creato (id=%s) entry=%.4f sl=%.4f tp=%.4f rr=%.2f",
                   asset, signal["direction"], sid[:8], signal["planned_entry"],
                   signal["planned_sl"], signal["planned_tp"], signal["planned_rr"])
        _notify_early_signal(asset, signal, config)
    except Exception as e:
        logger.error("TT [%s]: errore generazione segnale: %s", asset, e)


def run_tt_scan(config: dict):
    """Entry point principale, chiamato dal workflow GitHub Actions."""
    conn = core_db.get_connection(config["DB_PATH"])
    tt_db.init_tt_schema(conn)

    now = datetime.now(timezone.utc)
    assets = config.get("TT_SCANNER", {}).get("assets", TT_ASSETS)

    logger.info("=== TT Scanner: inizio ciclo (%s) ===", ", ".join(assets))

    for asset in assets:
        try:
            _run_for_asset(conn, asset, config, now)
        except Exception as e:
            logger.error("TT [%s]: errore non gestito: %s", asset, e)

    conn.close()
    logger.info("=== TT Scanner: fine ciclo ===")
