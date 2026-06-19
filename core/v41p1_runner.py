"""
core/v41p1_runner.py
Orchestratore di Institutional Scanner V4.1 Phase 1
Money Flow & Intraday Edge Validation.

Differenze rispetto a v41_runner.py:
- Costruisce la Money Flow Map con Priority Score ad ogni ciclo
- Salva snapshot MFM per analisi storica
- Watchlist Alert arricchiti con Priority Score e historical_touches
- Segnali tracciati in v41p1_signals (tabella separata)
- Sessione corretta via get_session_v41 (LONDON/OVERLAP/NEW_YORK/ASIA)
- Formato Telegram dedicato con Money Flow Map visibile nell'alert
- time_to_tp e time_to_sl registrati per statistiche Phase 1
"""

import logging
from datetime import datetime, timezone

from storage import db as core_db
from core import indicators, macro
from core import v3_db
from core import v41p1_db
from strategies import institutional_scanner_v41 as v41
from strategies.institutional_scanner_v41 import get_session_v41
from strategies.money_flow_map import (
    build_money_flow_map,
    format_money_flow_map_summary,
)
from notifications import v41p1_telegram
from notifications import ntfy_bot

logger = logging.getLogger("v41p1_runner")

V41P1_TIMEFRAMES = {"H4": "4h", "H1": "1h", "M15": "15m"}

WATCHLIST_PROXIMITY_PCT = 0.005


def _prepare_dataframes(conn, asset: str, config: dict):
    limit = config.get("BOOTSTRAP_TARGET_CANDLES", 300)

    df_h4  = core_db.get_candles_df(conn, asset, V41P1_TIMEFRAMES["H4"],  limit=limit)
    df_h1  = core_db.get_candles_df(conn, asset, V41P1_TIMEFRAMES["H1"],  limit=limit)
    df_m15 = v3_db.get_v3_candles_df(conn, asset, V41P1_TIMEFRAMES["M15"], limit=limit)
    df_d1  = v3_db.get_v3_candles_df(conn, asset, "1D", limit=60)

    ema_periods = config.get("EMA_PERIODS", [21, 50, 100, 200])
    atr_period  = config.get("ATR_PERIOD", 14)

    for df in (df_h4, df_h1, df_m15):
        if len(df) > atr_period:
            indicators.add_atr(df, atr_period)

    if len(df_h4) > max(ema_periods):
        indicators.add_emas(df_h4, ema_periods)
    if len(df_h1) > max(ema_periods):
        indicators.add_emas(df_h1, ema_periods)

    return df_h4, df_h1, df_m15, df_d1


def _check_watchlist(conn, asset: str, mfm: dict, now: datetime, config: dict):
    """
    Genera Watchlist Alert per i livelli entro WATCHLIST_PROXIMITY_PCT
    dal prezzo corrente, usando la Money Flow Map con Priority Score.
    Solo sulla transizione fuori -> dentro fascia.
    """
    timestamp_str = now.isoformat()

    proximities = [
        lv for lv in mfm["levels"]
        if lv["distance_pct"] <= WATCHLIST_PROXIMITY_PCT
    ]

    proximities_by_label = {p["label"]: p for p in proximities}

    for label, level in proximities_by_label.items():
        was_inside = v41p1_db.get_watchlist_state(conn, asset, label)
        if not was_inside:
            alert_id = v41p1_db.insert_watchlist_alert(conn, asset, level, timestamp_str)
            logger.info(
                "V41P1 Watchlist [%s]: ALERT %s @ %.4f dist=%.3f%% "
                "[%s %.2f] touches=%d (id=%s)",
                asset, label, level["price"],
                level["distance_pct"] * 100,
                level["priority_label"], level["priority_score"],
                level["historical_touches"], alert_id,
            )

            bot_token  = config.get("TELEGRAM_BOT_TOKEN", "")
            chat_id    = config.get("TELEGRAM_CHAT_ID", "")
            ntfy_topic = config.get("NTFY_TOPIC", "")

            if bot_token and chat_id:
                sent = v41p1_telegram.send_v41p1_watchlist_alert(
                    bot_token, chat_id, asset, level
                )
                logger.info("V41P1 Watchlist [%s]: Telegram inviato=%s", asset, sent)
            if ntfy_topic:
                # Usa il formato plain watchlist di V4.1 come fallback
                from notifications import v41_telegram
                proximity_for_ntfy = {
                    "label": label,
                    "price": level["price"],
                    "distance_pct": level["distance_pct"],
                    "potential_direction": "SELL" if level["kind"] == "high" else "BUY",
                }
                title, body = v41_telegram.format_v41_watchlist_alert_plain(
                    asset, proximity_for_ntfy
                )
                ntfy_sent = ntfy_bot.send_message(ntfy_topic, title, body)
                logger.info("V41P1 Watchlist [%s]: ntfy inviato=%s", asset, ntfy_sent)

        v41p1_db.set_watchlist_state(conn, asset, label, True, timestamp_str)

    # Resetta livelli usciti dalla fascia
    all_labels = {lv["label"] for lv in mfm["levels"]}
    for label in all_labels:
        if label not in proximities_by_label:
            if v41p1_db.get_watchlist_state(conn, asset, label):
                v41p1_db.set_watchlist_state(conn, asset, label, False, timestamp_str)


def _enrich_signal_with_mfm(signal: dict, mfm: dict) -> dict:
    """
    Arricchisce il segnale con i dati della Money Flow Map:
    nearest_above, nearest_below, liquidity source/target con Priority Score,
    expected move verso il target piu' vicino nella direzione del trade.
    """
    direction = signal["direction"]
    entry     = signal["entry"]

    above = mfm.get("nearest_above")
    below = mfm.get("nearest_below")

    signal["nearest_above_label"]    = above["label"]          if above else None
    signal["nearest_above_price"]    = above["price"]          if above else None
    signal["nearest_above_priority"] = above["priority_label"] if above else None
    signal["nearest_above_score"]    = above["priority_score"] if above else None
    signal["nearest_below_label"]    = below["label"]          if below else None
    signal["nearest_below_price"]    = below["price"]          if below else None
    signal["nearest_below_priority"] = below["priority_label"] if below else None
    signal["nearest_below_score"]    = below["priority_score"] if below else None

    signal["distance_to_nearest_above_pct"] = above["distance_pct"] if above else None
    signal["distance_to_nearest_below_pct"] = below["distance_pct"] if below else None

    if direction == "BUY":
        source_candidates = [
            lv for lv in mfm["levels"]
            if lv["kind"] == "low" and lv["price"] < entry
        ]
        target_candidates = [
            lv for lv in mfm["levels"]
            if lv["kind"] == "high" and lv["price"] > entry
        ]
    else:
        source_candidates = [
            lv for lv in mfm["levels"]
            if lv["kind"] == "high" and lv["price"] > entry
        ]
        target_candidates = [
            lv for lv in mfm["levels"]
            if lv["kind"] == "low" and lv["price"] < entry
        ]

    liq_source = None
    if source_candidates:
        liq_source = min(source_candidates, key=lambda lv: abs(lv["price"] - entry))

    liq_target = None
    if target_candidates:
        liq_target = max(target_candidates, key=lambda lv: lv["priority_score"])

    signal["liquidity_source"]          = liq_source["label"]          if liq_source else None
    signal["liquidity_source_price"]    = liq_source["price"]          if liq_source else None
    signal["liquidity_source_priority"] = liq_source["priority_label"] if liq_source else None
    signal["liquidity_source_score"]    = liq_source["priority_score"] if liq_source else None
    signal["liquidity_target"]          = liq_target["label"]          if liq_target else None
    signal["liquidity_target_price"]    = liq_target["price"]          if liq_target else None
    signal["liquidity_target_priority"] = liq_target["priority_label"] if liq_target else None
    signal["liquidity_target_score"]    = liq_target["priority_score"] if liq_target else None

    if liq_target and entry:
        em_points = abs(liq_target["price"] - entry)
        em_pct    = em_points / entry if entry else 0
        signal["expected_move_points"]  = round(em_points, 4)
        signal["expected_move_pct"]     = round(em_pct, 6)
        signal["expected_move_barrier"] = liq_target["label"]
    else:
        signal["expected_move_points"]  = None
        signal["expected_move_pct"]     = None
        signal["expected_move_barrier"] = None

    return signal


def _run_for_asset(conn, asset: str, config: dict, macro_provider, now: datetime):
    logger.info("V41P1 Scanner: inizio ciclo per %s", asset)

    df_h4, df_h1, df_m15, df_d1 = _prepare_dataframes(conn, asset, config)

    if len(df_h4) < 15 or len(df_h1) < 20 or len(df_m15) < 25:
        logger.warning(
            "V41P1 Scanner [%s]: dati insufficienti (h4=%d h1=%d m15=%d), skip.",
            asset, len(df_h4), len(df_h1), len(df_m15)
        )
        return

    # --- Money Flow Map ---
    current_price = float(df_m15.iloc[-1]["close"])
    mfm = build_money_flow_map(df_h4, df_d1, current_price)

    logger.info(format_money_flow_map_summary(mfm, asset))

    try:
        v41p1_db.insert_mfm_snapshot(conn, asset, mfm, now.isoformat())
    except Exception as e:
        logger.warning("V41P1 [%s]: errore salvataggio MFM snapshot: %s", asset, e)

    # --- Monitoraggio segnali aperti ---
    try:
        last_m15 = df_m15.iloc[-1]
        updated = v41p1_db.monitor_open_signals(
            conn, asset,
            current_high=float(last_m15["high"]),
            current_low=float(last_m15["low"]),
            now_iso=now.isoformat(),
            expiry_hours=24,
        )
        for upd in updated:
            logger.info(
                "V41P1 Monitor [%s]: %s -> outcome=%s tp1=%s tp2=%s",
                asset, upd["signal_id"][:8], upd["outcome"],
                upd["tp1_hit"], upd["tp2_hit"]
            )
    except Exception as e:
        logger.error("V41P1 Monitor [%s]: errore: %s", asset, e)

    # --- Watchlist Alert ---
    try:
        _check_watchlist(conn, asset, mfm, now, config)
    except Exception as e:
        logger.error("V41P1 Watchlist [%s]: errore: %s", asset, e)

    # --- Trigger (riusa la pipeline V4.1 esistente) ---
    market_data = {
        "asset":          asset,
        "df_h4":          df_h4,
        "df_h1":          df_h1,
        "df_m15":         df_m15,
        "df_d1":          df_d1,
        "timestamp":      now,
        "macro_provider": macro_provider,
    }

    result     = v41.generate_v41_signal(market_data)
    signal     = result["signal"]
    diagnostics = result["diagnostics"]

    logger.info(
        "V41P1 Scanner [%s] diagnostics: trigger_found=%s types=%s rejections=%s",
        asset, diagnostics.get("trigger_found"),
        diagnostics.get("trigger_types"), diagnostics.get("rejections", [])
    )

    if signal is None:
        logger.info("V41P1 Scanner [%s]: nessun alert.", asset)
        return

    # --- Sessione corretta (LONDON/OVERLAP/NEW_YORK/ASIA) ---
    signal["session"] = get_session_v41(now)

    # --- Arricchisce il segnale con la Money Flow Map ---
    signal = _enrich_signal_with_mfm(signal, mfm)

    # --- Duplicate Signal Protection ---
    current_trigger_type    = "BOS" if signal.get("bos_direction") else "CHOCH"
    current_liquidity_source = signal.get("liquidity_source")

    last_state   = v41p1_db.get_last_alert_state(conn, asset)
    is_duplicate = (
        last_state is not None
        and last_state["direction"]        == signal["direction"]
        and last_state["trigger_type"]     == current_trigger_type
        and last_state["liquidity_source"] == current_liquidity_source
    )

    if is_duplicate:
        logger.info(
            "V41P1 Scanner [%s]: REJECT DUPLICATE_SIGNAL (dir=%s trigger=%s source=%s)",
            asset, signal["direction"], current_trigger_type, current_liquidity_source
        )
        return

    signal_id = v41p1_db.insert_v41p1_signal(conn, signal)
    logger.info(
        "V41P1 Scanner [%s]: ALERT [%s] trigger=%s quality=%d/12 (%s) "
        "source=%s target=%s em=%s session=%s (id=%s)",
        asset, signal["direction"], signal.get("trigger_types"),
        signal["quality_score"], signal["quality_label"],
        signal.get("liquidity_source") or "N/A",
        signal.get("liquidity_target") or "N/A",
        f"{signal.get('expected_move_points', 0):.1f}pt"
            if signal.get("expected_move_points") else "N/A",
        signal["session"],
        signal_id,
    )

    v41p1_db.set_last_alert_state(
        conn, asset, signal["direction"],
        current_trigger_type, current_liquidity_source, now.isoformat()
    )

    bot_token  = config.get("TELEGRAM_BOT_TOKEN", "")
    chat_id    = config.get("TELEGRAM_CHAT_ID", "")
    ntfy_topic = config.get("NTFY_TOPIC", "")

    if bot_token and chat_id:
        sent = v41p1_telegram.send_v41p1_signal_alert(bot_token, chat_id, signal)
        logger.info("V41P1 Scanner [%s]: Telegram inviato=%s", asset, sent)
    if ntfy_topic:
        ntfy_sent = v41p1_telegram.send_v41p1_signal_alert_ntfy(ntfy_topic, signal)
        logger.info("V41P1 Scanner [%s]: ntfy inviato=%s", asset, ntfy_sent)


def run_v41p1_scan(config: dict):
    conn = core_db.get_connection(config["DB_PATH"])
    v41p1_db.init_v41p1_schema(conn, "storage/v41p1_schema.sql")

    macro_provider = macro.get_provider(config)
    now = datetime.now(timezone.utc)

    logger.info("=== V41P1 Scanner (Phase 1): inizio ciclo ===")

    assets = config.get("V41P1_SCANNER", {}).get("assets", ["PAXG_USDT", "BTC_USDT"])

    for asset in assets:
        try:
            _run_for_asset(conn, asset, config, macro_provider, now)
        except Exception as e:
            logger.error("V41P1 Scanner [%s]: errore non gestito: %s", asset, e)

    conn.close()
    logger.info("=== V41P1 Scanner (Phase 1): fine ciclo ===")
