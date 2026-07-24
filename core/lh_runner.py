"""
core/lh_runner.py
Liquidity Hunter v2.0 — Runner

Confluence Sniper: entry su Order Block con bias allineato.
    - M15 per contesto (bias, OB, premium/discount, sessione)
    - M5 per entry precisa (solo XAU — Twelve Data)
    - BTC resta su M15

Per ogni asset (BTC_USDT, XAU_USD):
    1. Carica candele M15 (+ M5 per XAU)
    2. Legge MIE context da snapshot DB
    3. Genera segnale LH v2
    4. Se valido → arricchisce con MIE, inserisce e notifica
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from storage import db as core_db
from core import v3_db
from core import lh_db
from core.decision_ledger import lh_integration as ledger_link
from strategies.liquidity_hunter import generate_lh_signal

logger = logging.getLogger("lh.runner")

LH_ASSETS      = ["BTC_USDT", "XAU_USD"]
LH_TIMEFRAMES  = {"H4": "4h", "M15": "15m", "M5": "5m", "D1": "1D"}


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
    """
    Legge l'ultimo snapshot di ogni engine MIE dal DB.
    Restituisce un dizionario con tutti i campi rilevanti,
    pronto per essere serializzato in market_snapshot JSON.
    """
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

        except Exception as e:
            logger.debug("MIE context [%s/%s]: %s", asset, table, e)
            context[f"mie_{prefix}_available"] = False

    return context


def _read_raw_snapshots(conn, asset: str) -> dict:
    """
    Legge gli snapshot GREZZI (JSON non appiattito) di ogni engine MIE,
    nel formato che il Decision Ledger si aspetta (i reporter leggono la
    struttura nidificata, es. structure_h4.classification).

    Diverso da _read_mie_context che appiattisce con prefisso mie_ per il
    market_snapshot. Qui serve la struttura originale per i voti engine.
    """
    import json as _json
    raw = {}
    for prefix, table in _MIE_SNAPSHOT_TABLES:
        try:
            row = conn.execute(
                f"SELECT snapshot_json FROM {table} "
                f"WHERE asset = ? ORDER BY timestamp_snapshot DESC LIMIT 1",
                (asset,)
            ).fetchone()
            raw[prefix] = _json.loads(row[0]) if row and row[0] else None
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


# ============================================================
# Per-asset runner
# ============================================================

def _run_for_asset(conn, asset: str, config: dict, now: datetime):
    logger.info("LH Runner: inizio ciclo per %s", asset)

    limit  = config.get("BOOTSTRAP_TARGET_CANDLES", 300)
    df_h4  = core_db.get_candles_df(conn, asset, LH_TIMEFRAMES["H4"], limit=limit)
    df_m15 = v3_db.get_v3_candles_df(conn, asset, LH_TIMEFRAMES["M15"], limit=limit)

    if len(df_m15) < 20 or len(df_h4) < 10:
        logger.warning("LH [%s]: dati insufficienti, skip.", asset)
        return

    # ── M5 per XAU (entry precisa, fetchato da v3_runner) ────
    df_m5 = None
    if asset == "XAU_USD":
        df_m5 = v3_db.get_v3_candles_df(conn, asset, LH_TIMEFRAMES["M5"], limit=100)
        if df_m5 is None or len(df_m5) < 5:
            logger.info("LH [%s]: candele M5 insufficienti, uso M15.", asset)
            df_m5 = None

    # ── Leggi MIE context ────────────────────────────────────
    mie_context = _read_mie_context(conn, asset)

    # Monitora segnali aperti
    try:
        last_candle = df_m5.iloc[-1] if df_m5 is not None and len(df_m5) > 0 else df_m15.iloc[-1]
        current_high_m = float(last_candle["high"])
        current_low_m  = float(last_candle["low"])

        # ── Breakeven: se MFE >= 0.3 ATR, sposta SL a entry ──
        atr_m15 = mie_context.get("mie_volatility_atr_m15", 0) or 0
        be_threshold = 0.3 * atr_m15 if atr_m15 > 0 else 0

        if be_threshold > 0:
            open_rows = conn.execute(
                "SELECT signal_id, direction, entry, stop_loss, mfe "
                "FROM lh_signals WHERE final_outcome='OPEN' AND asset=?",
                (asset,)
            ).fetchall()
            for sid, d, entry_p, sl_p, mfe_p in open_rows:
                if entry_p is None or sl_p is None:
                    continue
                fav = max(current_high_m - entry_p, 0) if d == "BUY" else max(entry_p - current_low_m, 0)
                cur_mfe = max(float(mfe_p or 0), fav)
                # SL non ancora a breakeven e MFE supera soglia
                if cur_mfe >= be_threshold:
                    if (d == "BUY" and float(sl_p) < float(entry_p)) or \
                       (d == "SELL" and float(sl_p) > float(entry_p)):
                        conn.execute(
                            "UPDATE lh_signals SET stop_loss=? WHERE signal_id=?",
                            (entry_p, sid)
                        )
                        conn.commit()
                        logger.info(
                            "LH BE [%s]: %s SL spostato a breakeven (entry=%.4f, mfe=%.2f)",
                            asset, sid[:8], entry_p, cur_mfe
                        )

        # ── LH v3.1: ordini PENDENTI ─────────────────────────
        # Un segnale WATCHING e' un ordine in attesa al bordo della zona OB,
        # non un trade. Diventa un trade solo quando il prezzo raggiunge
        # l'entry. Va chiamato PRIMA del monitor: un ordine riempito ORA
        # deve essere gia' monitorato in questo stesso ciclo, altrimenti si
        # perdono 5 minuti di movimento.
        try:
            filled = lh_db.monitor_pending_lh_signals(
                conn, asset,
                current_high=current_high_m,
                current_low=current_low_m,
                now_iso=now.isoformat(),
            )
            for ev in filled:
                logger.info(
                    "LH Pending [%s]: %s -> %s (dopo %d barre)",
                    asset, ev["signal_id"][:8], ev["event"], ev["pending_bars"],
                )
        except AttributeError:
            pass          # lh_db non aggiornato: nessun pendente da gestire

        updated  = lh_db.monitor_open_lh_signals(
            conn, asset,
            current_high=current_high_m,
            current_low=current_low_m,
            now_iso=now.isoformat(),
        )
        for upd in updated:
            logger.info(
                "LH Monitor [%s]: %s → outcome=%s bars=%d",
                asset, upd["signal_id"][:8], upd["outcome"], upd["bars_open"],
            )
            try:
                row = conn.execute(
                    "SELECT entry, stop_loss, rr FROM lh_signals WHERE signal_id=?",
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
                logger.warning("LH ledger link_outcome fallito (non-blocking): %s", e)
    except Exception as e:
        logger.error("LH Monitor [%s]: errore: %s", asset, e)

    # Genera segnale
    try:
        result = generate_lh_signal(asset, df_m15, now,
                                    mie_context=mie_context, df_m5=df_m5)
    except Exception as e:
        logger.error("LH [%s]: errore generazione: %s", asset, e)
        return

    signal = result["signal"]
    diag   = result["diagnostics"]

    if signal is None:
        logger.info("LH [%s]: no signal — %s", asset, diag.get("rejection", "UNKNOWN"))
        return

    direction = signal["direction"]

    # ── Check posizione già aperta ───────────────────────────
    if lh_db.has_open_lh_signal(conn, asset, direction):
        logger.info(
            "LH [%s %s]: segnale OPEN già presente, skip.",
            asset, direction,
        )
        return

    # ── Risk floor: SL troppo stretto ────────────────────────
    entry = signal.get("entry", 0)
    sl = signal.get("stop_loss", 0)
    if entry and sl:
        risk_pct = abs(entry - sl) / entry
        if risk_pct < 0.001:
            logger.info(
                "LH [%s %s]: REJECT RISK_TOO_TIGHT (%.4f)",
                asset, direction, risk_pct,
            )
            return

    # ── Check duplicati: stesso OB nelle ultime 4h ───────────
    ob_ref = signal.get("swept_level_label", "")
    if ob_ref and lh_db.has_recent_lh_signal(
        conn, asset, direction, ob_ref, hours=0.5
    ):
        logger.info(
            "LH [%s %s]: duplicato OB=%s, skip.",
            asset, direction, ob_ref,
        )
        return

    # ── MIE Context Enrichment ───────────────────────────────
    signal["market_snapshot"] = json.dumps(mie_context, default=str)

    try:
        signal_id = lh_db.insert_lh_signal(conn, signal)
    except Exception as e:
        logger.error("LH [%s]: errore inserimento: %s", asset, e)
        return

    # ── Decision Ledger ──────────────────────────────────────
    # SOLO i segnali realmente ENTRATI a mercato. Un ordine PENDENTE
    # (setup_state=WATCHING) non e' una decisione eseguita: registrarlo qui
    # inquinerebbe l'analisi di expectancy con trade mai aperti — e quella
    # analisi e' la base di ogni calibrazione.
    # I pendenti restano tracciati in lh_signals; quando verranno riempiti
    # potranno essere catturati al momento del fill (miglioramento futuro).
    if signal.get("setup_state") == "WATCHING":
        logger.info(
            "LH [%s %s]: ordine PENDENTE — non inviato al Decision Ledger "
            "(sara' catturato al riempimento)", asset, direction,
        )
    else:
      try:
        raw_snaps = _read_raw_snapshots(conn, asset)
        snapshots = ledger_link.build_snapshots_dict(
            raw_snaps.get("structure"), raw_snaps.get("volatility"),
            raw_snaps.get("order_block"), raw_snaps.get("fvg"),
            raw_snaps.get("liquidity"), raw_snaps.get("session_sweep"),
            raw_snaps.get("reaction_map"), raw_snaps.get("candlestick"),
            raw_snaps.get("macro"), raw_snaps.get("market_state"), None,
        )
        ledger_link.capture_executed(signal_id, asset, signal, snapshots)
      except Exception as e:
        logger.warning("LH [%s]: ledger capture fallito (non-blocking): %s", asset, e)

    logger.info(
        "LH [%s %s]: SEGNALE %s (%s) entry=%.4f sl=%.4f tp1=%.4f rr=%.2f "
        "ob=%s score=%.2f (%s) (id=%s)",
        asset, direction,
        signal.get("setup_state", "TRIGGERED"),
        signal.get("order_type", "MARKET"),
        signal["entry"], signal["stop_loss"], signal["tp"], signal["rr"],
        signal.get("swept_level_label", "?"),
        float(signal["quality_score"]), signal["quality_label"],
        signal_id,
    )

    _notify(signal, config)


def _notify(signal: dict, config: dict):
    try:
        from notifications import telegram_bot, ntfy_bot

        if signal["quality_label"] == "LOW":
            return

        direction = signal["direction"]
        asset     = signal["asset"]
        emoji     = "🟢" if direction == "BUY" else "🔴"

        def fp(v):
            if v is None: return "N/A"
            return f"{v:,.2f}" if float(v) > 1000 else f"{v:.4f}"

        state = signal.get("setup_state", "TRIGGERED")
        if state == "WATCHING":
            head = f"👁 *IN ATTESA* — ordine pendente ({signal.get('distance_atr', 0)} ATR)"
        else:
            head = "⚡ *ENTRY ORA* — a mercato"

        tp_lines = f"TP1:    `{fp(signal.get('tp1') or signal['tp'])}`  [{signal.get('tp1_label','?')}]  ({signal['rr']:.2f}R)\n"
        if signal.get("tp2"):
            tp_lines += f"TP2:    `{fp(signal['tp2'])}`  [{signal.get('tp2_label','?')}]\n"
        if signal.get("tp3"):
            tp_lines += f"TP3:    `{fp(signal['tp3'])}`  [{signal.get('tp3_label','?')}]\n"

        # fattori di confluenza che hanno davvero contribuito
        att = ""
        try:
            import json as _j
            facs = signal.get("confluence_factors")
            if isinstance(facs, str):
                facs = _j.loads(facs)
            if isinstance(facs, dict):
                strong = [k for k, v in facs.items() if v >= 0.5]
                att = ", ".join(strong) if strong else "nessun fattore forte"
        except Exception:
            att = ""

        text = (
            f"{emoji} *LIQUIDITY HUNTER v3.1*\n"
            f"*{asset.replace('_',' ')}* — {direction}\n"
            f"{head}\n\n"
            f"Qualita: *{signal['quality_score']}*/9 ({signal['quality_label']})\n\n"
            f"Entry:  `{fp(signal['entry'])}`\n"
            f"SL:     `{fp(signal['stop_loss'])}`\n\n"
            f"{tp_lines}\n"
            f"OB: {signal.get('swept_level_label', '?')} ({signal.get('ob_match_type','?')})\n"
            + (f"Confluenza: {att}\n" if att else "")
            + f"Sessione: {signal.get('session','?')}"
        )

        bot_token  = config.get("TELEGRAM_BOT_TOKEN", "")
        chat_id    = config.get("TELEGRAM_CHAT_ID", "")
        ntfy_topic = config.get("NTFY_TOPIC", "")

        if bot_token and chat_id:
            telegram_bot.send_message(bot_token, chat_id, text)
        if ntfy_topic:
            tag = "ATTESA" if signal.get("setup_state") == "WATCHING" else "ENTRY"
            title = (f"LH {tag} {asset.replace('_',' ')} {direction} | "
                     f"{signal['quality_score']}/9 {signal['quality_label']}")
            ntfy_bot.send_message(ntfy_topic, title, text.replace("*","").replace("`",""))

    except Exception as e:
        logger.warning("LH _notify: %s", e)


def run_lh_scan(config: dict):
    conn = core_db.get_connection(config["DB_PATH"])
    lh_db.init_lh_schema(conn)

    now    = datetime.now(timezone.utc)
    assets = config.get("LH_SCANNER", {}).get("assets", LH_ASSETS)

    logger.info("=== LH Scanner: inizio ciclo (%s) ===", ", ".join(assets))

    for asset in assets:
        try:
            _run_for_asset(conn, asset, config, now)
        except Exception as e:
            logger.error("LH [%s]: errore non gestito: %s", asset, e)

    conn.close()
    logger.info("=== LH Scanner: fine ciclo ===")
    
