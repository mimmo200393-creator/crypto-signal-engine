"""
core/v41_runner.py
Orchestratore di Institutional Scanner Framework V4.1 Intraday Wave Edition.

Notifiche Telegram e ntfy DISABILITATE — V4.1 usato solo come benchmark
storico. I segnali vengono registrati nel DB per le statistiche ma
non vengono notificati.

Sprint 13: Structure Snapshot + Filtri statistici + MIE Context
    - Produce structure_snapshot (richiesto da generate_v41_signal Sprint 2)
    - Filtri OVERLAP, BUY-NEUTRAL, risk floor
    - MIE context salvato in market_snapshot JSON (se la colonna esiste)
"""

import json
import logging
from datetime import datetime, timezone

from storage import db as core_db
from core import indicators, macro
from core import v3_db
from core import v41_db
from core.structure_engine_v2 import produce_structure_snapshot
from core.structure_db import init_structure_schema
from strategies import institutional_scanner_v41 as v41
from strategies.institutional_scanner_v41 import get_session_v41

logger = logging.getLogger("v41_runner")

V41_TIMEFRAMES = {"H4": "4h", "H1": "1h", "M15": "15m"}


# ============================================================
# MIE Context Reader (Sprint 13)
# ============================================================

_MIE_SNAPSHOT_TABLES = [
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


def _read_mie_context(conn, asset: str) -> dict:
    context = {}
    for prefix, table in _MIE_SNAPSHOT_TABLES:
        try:
            row = conn.execute(
                f"SELECT snapshot_json FROM {table} "
                f"WHERE asset = ? ORDER BY timestamp_snapshot DESC LIMIT 1",
                (asset,)
            ).fetchone()
            if row and row[0]:
                snapshot = json.loads(row[0])
                if isinstance(snapshot, dict):
                    for key, value in snapshot.items():
                        context[f"mie_{prefix}_{key}"] = value
                context[f"mie_{prefix}_available"] = True
            else:
                context[f"mie_{prefix}_available"] = False
        except Exception:
            context[f"mie_{prefix}_available"] = False
    return context


def _get_session_range(df_m15) -> tuple[float, float]:
    if len(df_m15) == 0:
        return 0.0, 0.0
    session_candles = df_m15.iloc[-32:]
    return float(session_candles["high"].max()), float(session_candles["low"].min())


def _prepare_dataframes(conn, asset: str, config: dict):
    limit = config.get("BOOTSTRAP_TARGET_CANDLES", 300)

    df_h4  = core_db.get_candles_df(conn, asset, V41_TIMEFRAMES["H4"],  limit=limit)
    df_h1  = core_db.get_candles_df(conn, asset, V41_TIMEFRAMES["H1"],  limit=limit)
    df_m15 = v3_db.get_v3_candles_df(conn, asset, V41_TIMEFRAMES["M15"], limit=limit)
    df_d1  = v3_db.get_v3_candles_df(conn, asset, "1D", limit=30)

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


def _run_for_asset(conn, asset: str, config: dict, macro_provider, now: datetime):
    logger.info("V4.1 Scanner: inizio ciclo per %s", asset)

    df_h4, df_h1, df_m15, df_d1 = _prepare_dataframes(conn, asset, config)

    if len(df_h4) < 15 or len(df_h1) < 20 or len(df_m15) < 25:
        logger.warning(
            "V4.1 Scanner [%s]: dati insufficienti (h4=%d h1=%d m15=%d), skip.",
            asset, len(df_h4), len(df_h1), len(df_m15)
        )
        return

    # Monitoraggio segnali aperti
    try:
        last_m15 = df_m15.iloc[-1]
        updated_signals = v41_db.monitor_open_signals(
            conn, asset,
            current_high=float(last_m15["high"]),
            current_low=float(last_m15["low"]),
            now_iso=now.isoformat(),
            expiry_hours=24,
        )
        for upd in updated_signals:
            logger.info(
                "V4.1 Monitor [%s]: segnale %s -> outcome=%s tp1_hit=%s tp2_hit=%s",
                asset, upd["signal_id"][:8], upd["outcome"],
                upd["tp1_hit"], upd["tp2_hit"]
            )
    except Exception as e:
        logger.error("V4.1 Monitor [%s]: errore monitoraggio: %s", asset, e)

    # ── Structure Snapshot (richiesto da Sprint 2) ────────────
    atr_m15 = float(df_m15.iloc[-1]["atr"]) if "atr" in df_m15.columns else 0.0
    session_high, session_low = _get_session_range(df_m15)

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
    except Exception as e:
        logger.error("V4.1 Structure [%s]: errore: %s", asset, e)

    market_data = {
        "asset": asset,
        "df_h4": df_h4, "df_h1": df_h1, "df_m15": df_m15, "df_d1": df_d1,
        "timestamp": now,
        "macro_provider": macro_provider,
        "structure_snapshot": structure_snapshot,
    }

    result      = v41.generate_v41_signal(market_data)
    signal      = result["signal"]
    diagnostics = result["diagnostics"]

    logger.info(
        "V4.1 Scanner [%s] diagnostics: trigger_found=%s trigger_types=%s rejections=%s",
        asset, diagnostics.get("trigger_found"), diagnostics.get("trigger_types"),
        diagnostics.get("rejections", [])
    )

    if signal is None:
        logger.info("V4.1 Scanner [%s]: nessun alert in questo ciclo.", asset)
        return

    # ── Filtri statistici (Sprint 13) ─────────────────────────
    signal["session"] = get_session_v41(now)

    if signal["session"] == "OVERLAP":
        logger.info("V4.1 Scanner [%s]: REJECT SESSION_OVERLAP", asset)
        return

    if (signal["direction"] == "BUY"
            and signal.get("dow_theory_h4") == "NEUTRAL"):
        logger.info("V4.1 Scanner [%s]: REJECT BUY_DOW_NEUTRAL", asset)
        return

    risk_pct = abs(signal["entry"] - signal["stop_loss"]) / signal["entry"] if signal["entry"] else 0
    if risk_pct < 0.002:
        logger.info("V4.1 Scanner [%s]: REJECT RISK_TOO_TIGHT (%.4f)", asset, risk_pct)
        return

    # ── Duplicate Signal Protection ───────────────────────────
    current_trigger_type     = "BOS" if signal.get("bos_direction") else "CHOCH"
    current_liquidity_source = signal.get("liquidity_source")

    last_state   = v41_db.get_last_alert_state(conn, asset)
    is_duplicate = (
        last_state is not None
        and last_state["direction"]        == signal["direction"]
        and last_state["trigger_type"]     == current_trigger_type
        and last_state["liquidity_source"] == current_liquidity_source
    )

    if is_duplicate:
        logger.info(
            "V4.1 Scanner [%s]: REJECT DUPLICATE_SIGNAL (dir=%s trigger=%s source=%s)",
            asset, signal["direction"], current_trigger_type, current_liquidity_source
        )
        return

    # ── MIE Context (Sprint 13) ───────────────────────────────
    mie_context = _read_mie_context(conn, asset)
    signal["market_snapshot"] = json.dumps(mie_context, default=str)

    signal_id = v41_db.insert_v41_signal(conn, signal)
    logger.info(
        "V4.1 Scanner [%s]: segnale registrato [%s] trigger=%s quality=%d/12 (%s) (id=%s) "
        "[notifiche disabilitate — solo benchmark]",
        asset, signal["direction"], signal["trigger_types"],
        signal["quality_score"], signal["quality_label"], signal_id
    )

    v41_db.set_last_alert_state(
        conn, asset, signal["direction"], current_trigger_type,
        current_liquidity_source, now.isoformat()
    )


def run_v41_scan(config: dict):
    conn = core_db.get_connection(config["DB_PATH"])
    v41_db.init_v41_schema(conn, "storage/v41_schema.sql")
    init_structure_schema(conn)

    macro_provider = macro.get_provider(config)
    now = datetime.now(timezone.utc)

    logger.info("=== V4.1 Scanner Intraday Wave: inizio ciclo (PAXG_USDT + BTC_USDT) ===")

    assets = config.get("V41_SCANNER", {}).get("assets", v41.V41_ASSETS)

    for asset in assets:
        try:
            _run_for_asset(conn, asset, config, macro_provider, now)
        except Exception as e:
            logger.error("V4.1 Scanner [%s]: errore non gestito: %s", asset, e)

    conn.close()
    logger.info("=== V4.1 Scanner Intraday Wave: fine ciclo ===")
