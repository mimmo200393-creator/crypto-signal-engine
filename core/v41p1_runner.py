"""
core/v41p1_runner.py
Orchestratore di Institutional Scanner V4.1 Phase 1
Money Flow & Intraday Edge Validation.

Sprint 1: integrazione Structure Engine V2.
    - produce_structure_snapshot() chiamato in _run_for_asset()
    - snapshot passato a market_data["structure_snapshot"]
    - signal arricchito con i campi strutturali rilevanti
    - init_structure_schema() chiamato in run_v41p1_scan()

Sprint 2: Trend Health + Volatility Regime Engine.
    - produce_volatility_snapshot() chiamato in _run_for_asset()
    - signal arricchito con campi vol_* e struct_trend_*
    - init_volatility_schema() chiamato in run_v41p1_scan()
"""

import logging
from datetime import datetime, timezone

from storage import db as core_db
from core import indicators, macro
from core import v3_db
from core import v41p1_db
from core.structure_db import init_structure_schema
from core.structure_engine_v2 import produce_structure_snapshot
from core.volatility_engine import produce_volatility_snapshot, init_volatility_schema
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


# ============================================================
# DataFrame preparation
# ============================================================

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


# ============================================================
# Session High/Low helper
# ============================================================

def _get_session_range(df_m15, now: datetime) -> tuple[float, float]:
    if len(df_m15) == 0:
        return 0.0, 0.0

    session_candles = df_m15.iloc[-32:]
    session_high = float(session_candles["high"].max())
    session_low  = float(session_candles["low"].min())
    return session_high, session_low


# ============================================================
# Watchlist
# ============================================================

def _check_watchlist(conn, asset: str, mfm: dict, now: datetime, config: dict):
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

    all_labels = {lv["label"] for lv in mfm["levels"]}
    for label in all_labels:
        if label not in proximities_by_label:
            if v41p1_db.get_watchlist_state(conn, asset, label):
                v41p1_db.set_watchlist_state(conn, asset, label, False, timestamp_str)


# ============================================================
# MFM enrichment
# ============================================================

def _enrich_signal_with_mfm(signal: dict, mfm: dict) -> dict:
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
        source_candidates = [lv for lv in mfm["levels"] if lv["kind"] == "low" and lv["price"] < entry]
        target_candidates = [lv for lv in mfm["levels"] if lv["kind"] == "high" and lv["price"] > entry]
    else:
        source_candidates = [lv for lv in mfm["levels"] if lv["kind"] == "high" and lv["price"] > entry]
        target_candidates = [lv for lv in mfm["levels"] if lv["kind"] == "low" and lv["price"] < entry]

    liq_source = min(source_candidates, key=lambda lv: abs(lv["price"] - entry)) if source_candidates else None
    liq_target = max(target_candidates, key=lambda lv: lv["priority_score"]) if target_candidates else None

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


# ============================================================
# Structure + Context enrichment (Sprint 1 + Sprint 2)
# ============================================================

def _enrich_signal_with_context(signal: dict, snapshot: dict,
                                 vol_snapshot: dict = None) -> dict:
    """
    Aggiunge i campi strutturali e di volatilita' al segnale.

    Sprint 1: campi struct_* dallo StructureSnapshot
    Sprint 2: campi struct_trend_* dal Trend Health
              campi vol_* dal VolatilitySnapshot
    """
    # ── Structure (Sprint 1) ─────────────────────────────────
    signal["struct_h4"]          = snapshot["structure_h4"]["classification"]
    signal["struct_m15"]         = snapshot["structure_m15"]["classification"]
    signal["struct_confidence"]  = snapshot["structure_confidence"]
    signal["struct_volume_cls"]  = snapshot["volume_classification"]
    signal["struct_pd_zone"]     = snapshot["premium_discount"]["zone"]
    signal["struct_pd_pos"]      = snapshot["premium_discount"]["position"]
    signal["struct_bars_bos"]    = snapshot.get("bars_since_bos")
    signal["struct_bars_choch"]  = snapshot.get("bars_since_choch")

    direction = signal.get("direction")
    h4_cls  = snapshot["structure_h4"]["classification"]
    m15_cls = snapshot["structure_m15"]["classification"]

    aligned_h4  = (direction == "BUY"  and h4_cls  == "BULLISH") or \
                  (direction == "SELL" and h4_cls  == "BEARISH")
    aligned_m15 = (direction == "BUY"  and m15_cls == "BULLISH") or \
                  (direction == "SELL" and m15_cls == "BEARISH")

    signal["struct_aligned_h4"]   = aligned_h4
    signal["struct_aligned_m15"]  = aligned_m15
    signal["struct_aligned_both"] = aligned_h4 and aligned_m15

    events = snapshot.get("events", [])
    if events:
        last_ev = events[-1]
        signal["struct_last_event_type"] = last_ev.get("type")
        signal["struct_last_event_dir"]  = last_ev.get("direction")
    else:
        signal["struct_last_event_type"] = None
        signal["struct_last_event_dir"]  = None

    # ── Trend Health (Sprint 2) ──────────────────────────────
    th = snapshot.get("trend_health", {})
    signal["struct_trend_phase"]          = th.get("phase", "NEUTRAL")
    signal["struct_impulse_count"]        = th.get("impulse_count", 0)
    signal["struct_avg_impulse_atr"]      = th.get("avg_impulse_amplitude", 0)
    signal["struct_last_impulse_atr"]     = th.get("last_impulse_amplitude", 0)
    signal["struct_last_impulse_duration"] = th.get("last_impulse_duration", 0)
    signal["struct_trend_duration_bars"]  = th.get("trend_duration_bars", 0)

    # ── Volatility (Sprint 2) ────────────────────────────────
    if vol_snapshot is not None:
        signal["vol_regime"]           = vol_snapshot.get("regime", "NORMAL")
        signal["vol_atr_m15"]          = vol_snapshot.get("atr_m15", 0)
        signal["vol_atr_ratio_m15"]    = vol_snapshot.get("atr_ratio_m15", 1.0)
        signal["vol_atr_ratio_h1"]     = vol_snapshot.get("atr_ratio_h1", 1.0)
        signal["vol_atr_percentile_h1"] = vol_snapshot.get("atr_percentile_h1", 50)
        signal["vol_expanding"]        = vol_snapshot.get("expanding", False)
        signal["vol_contracting"]      = vol_snapshot.get("contracting", False)
        signal["vol_range_24h_pct"]    = vol_snapshot.get("range_24h_pct", 0)
    else:
        signal["vol_regime"]           = "NORMAL"
        signal["vol_atr_m15"]          = 0
        signal["vol_atr_ratio_m15"]    = 1.0
        signal["vol_atr_ratio_h1"]     = 1.0
        signal["vol_atr_percentile_h1"] = 50
        signal["vol_expanding"]        = False
        signal["vol_contracting"]      = False
        signal["vol_range_24h_pct"]    = 0

    return signal


# ============================================================
# Per-asset runner
# ============================================================

def _run_for_asset(conn, asset: str, config: dict, macro_provider, now: datetime):
    logger.info("V41P1 Scanner: inizio ciclo per %s", asset)

    df_h4, df_h1, df_m15, df_d1 = _prepare_dataframes(conn, asset, config)

    if len(df_h4) < 15 or len(df_h1) < 20 or len(df_m15) < 25:
        logger.warning(
            "V41P1 Scanner [%s]: dati insufficienti (h4=%d h1=%d m15=%d), skip.",
            asset, len(df_h4), len(df_h1), len(df_m15)
        )
        return

    # ── Money Flow Map ────────────────────────────────────────
    current_price = float(df_m15.iloc[-1]["close"])
    mfm = build_money_flow_map(df_h4, df_d1, current_price)

    logger.info(format_money_flow_map_summary(mfm, asset))

    try:
        v41p1_db.insert_mfm_snapshot(conn, asset, mfm, now.isoformat())
    except Exception as e:
        logger.warning("V41P1 [%s]: errore salvataggio MFM snapshot: %s", asset, e)

    # ── Structure Engine V2 (Sprint 1 + Sprint 2 Trend Health) ─
    atr_m15 = float(df_m15.iloc[-1]["atr"]) if "atr" in df_m15.columns else 0.0
    session_high, session_low = _get_session_range(df_m15, now)

    structure_snapshot = None
    try:
        structure_snapshot = produce_structure_snapshot(
            asset=asset,
            df_h4=df_h4,
            df_m15=df_m15,
            conn=conn,
            atr_m15=atr_m15,
            session_high=session_high,
            session_low=session_low,
            now=now,
            config=config.get("STRUCTURE_ENGINE", {}),
        )
        th = structure_snapshot.get("trend_health", {})
        logger.info(
            "V41P1 Structure [%s]: H4=%s M15=%s confidence=%d pd=%s(%.2f) "
            "events=%d bars_bos=%s bars_choch=%s "
            "trend=%s phase=%s impulses=%d",
            asset,
            structure_snapshot["structure_h4"]["classification"],
            structure_snapshot["structure_m15"]["classification"],
            structure_snapshot["structure_confidence"],
            structure_snapshot["premium_discount"]["zone"],
            structure_snapshot["premium_discount"]["position"],
            len(structure_snapshot.get("events", [])),
            structure_snapshot.get("bars_since_bos"),
            structure_snapshot.get("bars_since_choch"),
            th.get("current_trend", "NEUTRAL"),
            th.get("phase", "NEUTRAL"),
            th.get("impulse_count", 0),
        )
    except Exception as e:
        logger.error("V41P1 Structure [%s]: errore produce_structure_snapshot: %s", asset, e)

    # ── Volatility Engine (Sprint 2) ─────────────────────────
    vol_snapshot = None
    try:
        vol_snapshot = produce_volatility_snapshot(
            asset=asset,
            df_m15=df_m15,
            df_h1=df_h1,
            df_h4=df_h4,
            conn=conn,
            now=now,
            config=config.get("VOLATILITY_ENGINE", {}),
        )
    except Exception as e:
        logger.error("V41P1 Volatility [%s]: errore produce_volatility_snapshot: %s", asset, e)

    # ── Monitoraggio segnali aperti ───────────────────────────
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

    # ── Watchlist Alert ───────────────────────────────────────
    try:
        _check_watchlist(conn, asset, mfm, now, config)
    except Exception as e:
        logger.error("V41P1 Watchlist [%s]: errore: %s", asset, e)

    # ── Trigger ───────────────────────────────────────────────
    market_data = {
        "asset":              asset,
        "df_h4":              df_h4,
        "df_h1":              df_h1,
        "df_m15":             df_m15,
        "df_d1":              df_d1,
        "timestamp":          now,
        "macro_provider":     macro_provider,
        "mfm":                mfm,
        "structure_snapshot": structure_snapshot,
    }

    result      = v41.generate_v41_signal(market_data)
    signal      = result["signal"]
    diagnostics = result["diagnostics"]

    logger.info(
        "V41P1 Scanner [%s] diagnostics: trigger_found=%s types=%s rejections=%s",
        asset, diagnostics.get("trigger_found"),
        diagnostics.get("trigger_types"), diagnostics.get("rejections", [])
    )

    if signal is None:
        logger.info("V41P1 Scanner [%s]: nessun alert.", asset)
        return

    # ── Sessione corrente ─────────────────────────────────────
    signal["session"] = get_session_v41(now)

    # ── Arricchisce con MFM ───────────────────────────────────
    signal = _enrich_signal_with_mfm(signal, mfm)

    # ── Arricchisce con Structure + Volatility (Sprint 1+2) ──
    if structure_snapshot is not None:
        signal = _enrich_signal_with_context(signal, structure_snapshot, vol_snapshot)
    else:
        signal.update({
            "struct_h4": "NEUTRAL", "struct_m15": "NEUTRAL",
            "struct_confidence": 0, "struct_volume_cls": "NORMAL",
            "struct_pd_zone": "EQUILIBRIUM", "struct_pd_pos": 0.5,
            "struct_bars_bos": None, "struct_bars_choch": None,
            "struct_aligned_h4": False, "struct_aligned_m15": False,
            "struct_aligned_both": False,
            "struct_last_event_type": None, "struct_last_event_dir": None,
            "struct_trend_phase": "NEUTRAL", "struct_impulse_count": 0,
            "struct_avg_impulse_atr": 0, "struct_last_impulse_atr": 0,
            "struct_last_impulse_duration": 0, "struct_trend_duration_bars": 0,
            "vol_regime": "NORMAL", "vol_atr_m15": 0,
            "vol_atr_ratio_m15": 1.0, "vol_atr_ratio_h1": 1.0,
            "vol_atr_percentile_h1": 50,
            "vol_expanding": False, "vol_contracting": False,
            "vol_range_24h_pct": 0,
        })

    # ── Duplicate Signal Protection ───────────────────────────
    current_trigger_type     = "BOS" if signal.get("bos_direction") else "CHOCH"
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
        "source=%s target=%s em=%s session=%s sweep=%s "
        "struct=H4:%s/M15:%s conf=%d aligned=%s "
        "trend=%s/%s impulses=%d vol=%s(%s) (id=%s)",
        asset,
        signal["direction"],
        signal.get("trigger_types"),
        signal["quality_score"],
        signal["quality_label"],
        signal.get("liquidity_source") or "N/A",
        signal.get("liquidity_target") or "N/A",
        f"{signal.get('expected_move_points', 0):.1f}pt"
            if signal.get("expected_move_points") else "N/A",
        signal["session"],
        signal.get("mfm_sweep_confirmed", False),
        signal.get("struct_h4", "N/A"),
        signal.get("struct_m15", "N/A"),
        signal.get("struct_confidence", 0),
        signal.get("struct_aligned_both", False),
        signal.get("struct_trend_phase", "N/A"),
        signal.get("struct_impulse_count", 0),
        signal.get("struct_impulse_count", 0),
        signal.get("vol_regime", "N/A"),
        f"p{signal.get('vol_atr_percentile_h1', 0):.0f}",
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


# ============================================================
# Entry point
# ============================================================

def run_v41p1_scan(config: dict):
    conn = core_db.get_connection(config["DB_PATH"])
    v41p1_db.init_v41p1_schema(conn, "storage/v41p1_schema.sql")
    init_structure_schema(conn)
    init_volatility_schema(conn)

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
