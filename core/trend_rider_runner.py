"""
core/trend_rider_runner.py
NMC Trend Rider Balanced — Runner

Sprint 13: MIE Context Enrichment
    - Ogni segnale TRB riceve lo snapshot completo di tutti
      gli engine MIE salvato in market_snapshot JSON
    - MIE context arriva da market_contexts (calcolato in edge_lab_runner)
    - Filtri statistici (OVERLAP, risk floor)

Per ogni asset (BTC_USDT, PAXG_USDT):
    1. Carica candele H4/H1 da candles_cache, M15 da v3_candles_cache
    2. Monitora segnali aperti (TP1/TP2/SL/EXPIRED)
    3. Genera segnale TRB per BUY e SELL
    4. Se segnale valido e nessun duplicato → arricchisce con MIE, inserisce e notifica
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from storage import db as core_db
from core import v3_db
from core import trend_rider_db
from core.decision_ledger import trb_integration as ledger_link
from strategies.edge_lab.trend_rider import generate_trb_signal
from strategies.money_flow_map import build_money_flow_map

logger = logging.getLogger("trend_rider.runner")

TRB_ASSETS     = ["BTC_USDT", "PAXG_USDT"]
TRB_TIMEFRAMES = {"H4": "4h", "H1": "1h", "M15": "15m"}


# ============================================================
# Decision Ledger — snapshot grezzi (Sprint 14)
# ============================================================
# Stessa lista tabelle di lh_runner.py: struttura nidificata originale
# (non appiattita mie_*) richiesta dal Ledger per i voti engine.

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


def _read_raw_snapshots(conn, asset: str) -> dict:
    """
    Legge gli snapshot GREZZI (JSON non appiattito) di ogni engine MIE,
    nel formato che il Decision Ledger si aspetta. Identica a
    lh_runner._read_raw_snapshots — stesse tabelle, stesso formato.
    """
    raw = {}
    for prefix, table in _MIE_SNAPSHOT_TABLES:
        try:
            row = conn.execute(
                f"SELECT snapshot_json FROM {table} "
                f"WHERE asset = ? ORDER BY timestamp_snapshot DESC LIMIT 1",
                (asset,)
            ).fetchone()
            raw[prefix] = json.loads(row[0]) if row and row[0] else None
        except Exception:
            raw[prefix] = None
    return raw


def _get_session(now: datetime) -> str:
    """Sessione di mercato in UTC."""
    t = now.hour * 60 + now.minute
    if 8 * 60 <= t < 13 * 60 + 30:
        return "LONDON"
    if 13 * 60 + 30 <= t <= 16 * 60 + 30:
        return "OVERLAP"
    if 16 * 60 + 31 <= t <= 22 * 60:
        return "NEW_YORK"
    return "ASIA"


def _prepare_dataframes(conn, asset: str, config: dict):
    limit  = config.get("BOOTSTRAP_TARGET_CANDLES", 300)
    df_h4  = core_db.get_candles_df(conn, asset, TRB_TIMEFRAMES["H4"],  limit=limit)
    df_h1  = core_db.get_candles_df(conn, asset, TRB_TIMEFRAMES["H1"],  limit=limit)
    df_m15 = v3_db.get_v3_candles_df(conn, asset, TRB_TIMEFRAMES["M15"], limit=limit)
    return df_h4, df_h1, df_m15


def _run_for_asset(conn, asset: str, config: dict, market_ctx: dict, now: datetime):
    logger.info("TRB Runner: inizio ciclo per %s", asset)

    df_h4, df_h1, df_m15 = _prepare_dataframes(conn, asset, config)

    if len(df_h1) < 60 or len(df_m15) < 25:
        logger.warning(
            "TRB Runner [%s]: dati insufficienti (h1=%d m15=%d), skip.",
            asset, len(df_h1), len(df_m15),
        )
        return

    # ── Monitoraggio segnali aperti ──────────────────────────
    try:
        last_m15 = df_m15.iloc[-1]
        updated  = trend_rider_db.monitor_open_trb_signals(
            conn, asset,
            current_high=float(last_m15["high"]),
            current_low=float(last_m15["low"]),
            now_iso=now.isoformat(),
        )
        for upd in updated:
            logger.info(
                "TRB Monitor [%s]: %s → outcome=%s bars=%d",
                asset, upd["signal_id"][:8], upd["outcome"], upd["bars_open"],
            )
            # ── Collega l'esito al Decision Ledger (passivo) ──
            try:
                row = conn.execute(
                    "SELECT entry, stop_loss, rr2 FROM trb_signals WHERE signal_id=?",
                    (upd["signal_id"],)
                ).fetchone()
                if row:
                    ledger_link.link_outcome(
                        decision_id=upd["signal_id"],
                        outcome=upd["outcome"],
                        entry=row[0], stop_loss=row[1],
                        mae=upd.get("mae"), mfe=upd.get("mfe"),
                        duration_bars=upd.get("bars_open"),
                        rr_planned=row[2],
                    )
            except Exception as e:
                logger.warning("TRB ledger link_outcome fallito (non-blocking): %s", e)
    except Exception as e:
        logger.error("TRB Monitor [%s]: errore: %s", asset, e)

    # ── MIE context (arriva da edge_lab_runner via market_ctx) ─
    mie_context = market_ctx.get("mie_context", {})

    # ── Zone già segnalate e aperte ──────────────────────────
    # Regola "una configurazione = un segnale": la strategia non deve
    # ri-notificare una zona (Order Block / FVG) che ha già un segnale
    # aperto, anche se il prezzo esce e rientra. Letto una volta per scan.
    market_ctx["open_zone_refs"] = trend_rider_db.get_open_zone_refs(conn, asset)

    # ── Genera segnale BUY e SELL ────────────────────────────
    for direction in ("BUY", "SELL"):

        # Check 1: segnale già OPEN sulla stessa direzione
        if trend_rider_db.has_open_trb_signal(conn, asset, direction):
            logger.debug("TRB [%s %s]: segnale OPEN già presente, skip.", asset, direction)
            continue

        # Genera il segnale
        try:
            result = generate_trb_signal(market_ctx, df_h4, df_h1, df_m15, direction)
        except Exception as e:
            logger.error("TRB [%s %s]: errore generazione: %s", asset, direction, e)
            continue

        signal = result["signal"]
        diag   = result["diagnostics"]

        if signal is None:
            logger.info(
                "TRB [%s %s]: no signal — %s",
                asset, direction, diag.get("rejection", "UNKNOWN"),
            )
            continue

        # ══════════════════════════════════════════════════════
        # ── Filtri statistici (Sprint 13) ────────────────────
        # ══════════════════════════════════════════════════════

        current_session = _get_session(now)
        signal["session"] = signal.get("session", current_session)

        # OVERLAP: WR 8.7% cross-strategy
        if signal.get("session") == "OVERLAP":
            logger.info(
                "TRB [%s %s]: REJECT SESSION_OVERLAP",
                asset, direction,
            )
            continue

        # Risk floor: SL troppo stretto
        entry = signal.get("entry", 0)
        sl = signal.get("stop_loss", 0)
        if entry and sl:
            risk_pct = abs(entry - sl) / entry
            if risk_pct < 0.002:
                logger.info(
                    "TRB [%s %s]: REJECT RISK_TOO_TIGHT (%.4f)",
                    asset, direction, risk_pct,
                )
                continue

        # ══════════════════════════════════════════════════════

        # Check 2: duplicato
        if trend_rider_db.has_recent_trb_signal(
            conn, asset, direction, signal["entry"], hours=4
        ):
            logger.info(
                "TRB [%s %s]: duplicato (entry=%.4f già presente nelle ultime 4h), skip.",
                asset, direction, signal["entry"],
            )
            continue

        # ══════════════════════════════════════════════════════
        # ── MIE Context Enrichment (Sprint 13) ───────────────
        # ══════════════════════════════════════════════════════
        signal["market_snapshot"] = json.dumps(mie_context, default=str)

        # Inserisce il segnale
        try:
            signal_id = trend_rider_db.insert_trb_signal(conn, signal)
        except Exception as e:
            logger.error("TRB [%s %s]: errore inserimento: %s", asset, direction, e)
            continue

        # ── Decision Ledger: registra i voti dei 13 engine (passivo) ──
        # Modalita' solo-registrazione, stesso principio di LH. Non-blocking:
        # se fallisce, TRB continua. L'MFM non serve alla generazione del
        # segnale TRB (a differenza di LH), quindi lo calcoliamo qui al volo,
        # solo per il Ledger, senza toccare _prepare_dataframes.
        try:
            raw_snaps = _read_raw_snapshots(conn, asset)
            try:
                df_d1 = v3_db.get_v3_candles_df(conn, asset, "1D", limit=60)
                last_price = float(df_m15.iloc[-1]["close"])
                mfm = build_money_flow_map(df_h4, df_d1, last_price)
            except Exception as e_mfm:
                logger.debug("TRB [%s]: MFM non disponibile per il Ledger: %s", asset, e_mfm)
                mfm = None
            snapshots = ledger_link.build_snapshots_dict(
                raw_snaps.get("structure"), raw_snaps.get("volatility"),
                raw_snaps.get("order_block"), raw_snaps.get("fvg"),
                raw_snaps.get("liquidity"), raw_snaps.get("session_sweep"),
                raw_snaps.get("reaction_map"), raw_snaps.get("candlestick"),
                raw_snaps.get("macro"), raw_snaps.get("market_state"), mfm,
            )
            ledger_link.capture_executed(signal_id, asset, signal, snapshots)
        except Exception as e:
            logger.warning("TRB [%s %s]: ledger capture fallito (non-blocking): %s", asset, direction, e)

        logger.info(
            "TRB [%s %s]: SEGNALE entry=%.4f sl=%.4f tp1=%.4f tp2=%.4f "
            "score=%d (%s) adx=%.1f mie=%d engines (id=%s)",
            asset, direction,
            signal["entry"], signal["stop_loss"], signal["tp1"], signal["tp2"],
            signal["quality_score"], signal["quality_label"],
            signal["adx"],
            sum(1 for k, v in mie_context.items()
                if k.endswith("_available") and v),
            signal_id,
        )

        _notify(signal, config)


def _notify(signal: dict, config: dict):
    """Invia notifiche Telegram e ntfy. Solo MEDIUM, HIGH, PREMIUM."""
    try:
        from notifications import telegram_bot, ntfy_bot

        quality = signal["quality_label"]
        if quality == "LOW":
            return

        direction = signal["direction"]
        asset     = signal["asset"]
        emoji     = "🟢" if direction == "BUY" else "🔴"

        def fp(v):
            if v is None: return "N/A"
            return f"{v:,.2f}" if float(v) > 1000 else f"{v:.4f}"

        text = (
            f"{emoji} *TREND RIDER BALANCED v1.0*\n\n"
            f"*{asset.replace('_',' ')}* — {direction}\n\n"
            f"Score: *{signal['quality_score']}* ({quality})\n"
            f"Trend H1: {signal['trend_h1']} | H4: {signal.get('trend_h4','N/A')}\n"
            f"ADX: {signal['adx']:.1f} | Pullback: ✓\n\n"
            f"Entry:  `{fp(signal['entry'])}`\n"
            f"SL:     `{fp(signal['stop_loss'])}`\n"
            f"TP1:    `{fp(signal['tp1'])}` (1R)\n"
            f"TP2:    `{fp(signal['tp2'])}` ({signal.get('rr2',0):.2f}R)\n\n"
            f"Target: {signal.get('liquidity_target','N/A')}\n"
            f"Session: {signal.get('session','N/A')}"
        )

        if signal.get("new_24h_extreme"):
            text += "\n🚀 Nuovo estremo 24h"

        bot_token  = config.get("TELEGRAM_BOT_TOKEN", "")
        chat_id    = config.get("TELEGRAM_CHAT_ID", "")
        ntfy_topic = config.get("NTFY_TOPIC", "")

        if bot_token and chat_id:
            telegram_bot.send_message(bot_token, chat_id, text)

        if ntfy_topic:
            title = f"TRB {asset.replace('_',' ')} {direction} | {quality} {signal['quality_score']}"
            plain = text.replace("*","").replace("`","")
            ntfy_bot.send_message(ntfy_topic, title, plain)

    except Exception as e:
        logger.warning("TRB _notify: errore: %s", e)


def run_trb_scan(config: dict, market_contexts: dict):
    """
    Entry point principale.

    Args:
        config:          config.yaml
        market_contexts: dict {asset: market_ctx} già calcolati da Edge Lab runner
                         (ora include mie_context per ogni asset)
    """
    conn = core_db.get_connection(config["DB_PATH"])
    trend_rider_db.init_trb_schema(conn)

    now    = datetime.now(timezone.utc)
    assets = config.get("EDGE_LAB", {}).get("assets", TRB_ASSETS)

    logger.info("=== TRB Scanner: inizio ciclo (%s) ===", ", ".join(assets))

    for asset in assets:
        market_ctx = market_contexts.get(asset)
        if market_ctx is None:
            logger.warning("TRB [%s]: market context non disponibile, skip.", asset)
            continue
        try:
            _run_for_asset(conn, asset, config, market_ctx, now)
        except Exception as e:
            logger.error("TRB [%s]: errore non gestito: %s", asset, e)

    conn.close()
    logger.info("=== TRB Scanner: fine ciclo ===")
