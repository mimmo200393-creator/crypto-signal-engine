"""
core/lh_runner.py
Liquidity Hunter v3.2 — Runner

Confluence Sniper: entry su Order Block con bias allineato.
    - M15 per contesto (bias, OB, premium/discount, sessione)
    - M5 per entry precisa (solo XAU — Twelve Data)
    - BTC resta su M15

Per ogni asset (BTC_USDT, XAU_USD):
    1. Carica candele M15 (+ M5 per XAU)
    2. Legge MIE context da snapshot DB
    3. Genera segnale LH v3.2
    4. Se valido → arricchisce con MIE, inserisce e notifica
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone, timedelta

from storage import db as core_db
from core import v3_db
from core import lh_db
from core.decision_ledger import lh_integration as ledger_link
from strategies.liquidity_hunter import (
    generate_lh_signal, scan_restart_zones,
    _new_recurrence_state, _update_zone_recurrence, _apply_recurrence_to_score,
    _params as _lh_params, format_zone_digest,
    _detect_swings, SWING_CONFIRM_K,
)

logger = logging.getLogger("lh.runner")

LH_ASSETS      = ["BTC_USDT", "XAU_USD"]
LH_TIMEFRAMES  = {"H4": "4h", "H1": "1h", "M30": "30m", "M15": "15m", "M5": "5m", "D1": "1D"}


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
        except Exception as e:
            logger.debug("MIE context [%s/%s]: %s", asset, table, e)
            context[f"mie_{prefix}_available"] = False
    return context


def _read_raw_snapshots(conn, asset: str) -> dict:
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
    # XAU chiuso nel weekend (venerdi' 22 UTC -> domenica 22 UTC)
    if asset == "XAU_USD":
        wd = now.weekday()  # 0=Mon ... 6=Sun
        if wd == 6 or (wd == 5 and now.hour >= 22) or (wd == 4 and now.hour >= 22):
            logger.info("LH [%s]: mercato chiuso (weekend), skip.", asset)
            return

    logger.info("LH Runner: inizio ciclo per %s", asset)

    limit  = config.get("BOOTSTRAP_TARGET_CANDLES", 300)
    df_h4  = core_db.get_candles_df(conn, asset, LH_TIMEFRAMES["H4"], limit=limit)
    df_m15 = v3_db.get_v3_candles_df(conn, asset, LH_TIMEFRAMES["M15"], limit=limit)

    # M30/H1 -- SOLO per il Restart Zone Engine (detection impulso su
    # timeframe piu' alti). Il segnale di trading non li usa, restano
    # separati dal df_h4/df_m15 sopra per non toccare comportamento gia'
    # validato.
    df_h1_zones = core_db.get_candles_df(conn, asset, LH_TIMEFRAMES["H1"], limit=limit)
    df_m30_zones = v3_db.get_v3_candles_df(conn, asset, LH_TIMEFRAMES["M30"], limit=limit)

    # H4: 3 anni =~ 6570 candele. D1: 3 anni =~ 1095 candele.
    # Spazio totale: < 1MB (irrilevante rispetto ai 65MB di signals.db).
    # Motivazione: BTC ha livelli strutturali di anni fa ancora attivi
    # (ATH 2021, minimo bear 2022, range breakout 2023). Oro uguale.
    # Un anno li perde.
    df_h4_swings = core_db.get_candles_df(conn, asset, LH_TIMEFRAMES["H4"], limit=6600)
    df_d1_swings = v3_db.get_v3_candles_df(conn, asset, LH_TIMEFRAMES["D1"], limit=1100)

    if len(df_m15) < 20 or len(df_h4) < 10:
        logger.warning("LH [%s]: dati insufficienti, skip.", asset)
        return

    df_m5 = None
    if asset == "XAU_USD":
        df_m5 = v3_db.get_v3_candles_df(conn, asset, LH_TIMEFRAMES["M5"], limit=100)
        if df_m5 is None or len(df_m5) < 5:
            logger.info("LH [%s]: candele M5 insufficienti, uso M15.", asset)
            df_m5 = None

    # M5 per il Restart Zone Engine — SEPARATO da df_m5 sopra, che resta
    # XAU-only per non toccare la precisione di entry del segnale di
    # trading (design invariato). Il Restart Zone Engine serve M5 su
    # ENTRAMBI gli asset per raffinare le zone (deploy 19/07: v3_candles_cache
    # ha gia' M5 anche per BTC).
    df_m5_zones = df_m5 if asset == "XAU_USD" else None
    if df_m5_zones is None:
        df_m5_zones = v3_db.get_v3_candles_df(conn, asset, LH_TIMEFRAMES["M5"], limit=100)
        if df_m5_zones is None or len(df_m5_zones) < 5:
            logger.info("LH ZoneScan [%s]: candele M5 insufficienti, zone non raffinate.", asset)
            df_m5_zones = None

    # ── Memoria storica swing H4/D1 (v3.12) ──────────────────────────
    # Gira ogni ciclo, ma e' economico (poche migliaia di candele, pure
    # funzioni Python) e idempotente (insert_swings scarta i doppioni via
    # swing_ref stabile) -- funge sia da backfill iniziale (la prima volta
    # che gira su un DB vuoto trova tutto lo storico disponibile) sia da
    # refresh incrementale (i cicli successivi trovano solo gli swing
    # nuovi, confermati dalle candele piu' recenti). Nessuna scadenza:
    # lo storico resta per sempre, come richiesto.
    try:
        for df_swing, tf_label in ((df_h4_swings, "H4"), (df_d1_swings, "D1")):
            if df_swing is None or len(df_swing) < 2 * SWING_CONFIRM_K + 1:
                continue
            new_swings = _detect_swings(df_swing, asset, tf_label)
            n_new = lh_db.insert_swings(conn, new_swings)
            if n_new > 0:
                total = lh_db.count_swings(conn, asset, tf_label)
                logger.info(
                    "LH Swing [%s %s]: %d nuovi swing (totale storico: %d)",
                    asset, tf_label, n_new, total,
                )
    except Exception as e:
        logger.error("LH Swing [%s]: errore (non-blocking): %s", asset, e)

    mie_context = _read_mie_context(conn, asset)

    # ── Restart Zone Engine (v3.4) — informativo, indipendente dal segnale ──
    # Gira SEMPRE, anche se poi il segnale di trading viene rifiutato o
    # e' un duplicato: e' un canale separato, non deve dipendere dalla
    # logica di trading qui sotto.
    try:
        # Leggo gli swing storici H4/D1 per la confluenza nel punteggio
        swing_zones = lh_db.get_swing_zones(conn, asset)
        zones = scan_restart_zones(asset, df_m15, now, mie_context=mie_context,
                                   df_m5=df_m5_zones, df_h1=df_h1_zones, df_m30=df_m30_zones,
                                   swing_zones=swing_zones)
        # v3.11: log corretto -- prima mostrava "OB attivi in mie_context",
        # fuorviante dal v3.5 (la detection non dipende piu' dagli OB).
        # E soprattutto: NON mostrava se H1/M30 fossero disponibili, che
        # sono le uniche due fonti reali dell'impulso da v3.6. Senza
        # questo, "0 Restart Zone trovate" era ambiguo -- impossibile
        # distinguere "nessun impulso oggi" da "dati H1/M30 mancanti".
        logger.info(
            "LH ZoneScan [%s]: %d Restart Zone trovate "
            "(h1=%s/%d barre, m30=%s/%d barre, m5=%s/%d barre)",
            asset, len(zones),
            "disponibili" if df_h1_zones is not None else "ASSENTI",
            len(df_h1_zones) if df_h1_zones is not None else 0,
            "disponibili" if df_m30_zones is not None else "ASSENTI",
            len(df_m30_zones) if df_m30_zones is not None else 0,
            "disponibili" if df_m5_zones is not None else "ASSENTI",
            len(df_m5_zones) if df_m5_zones is not None else 0,
        )

        # ── Ricorrenza (v3.7): aggiorno lo stato di ogni zona PRIMA di
        # notificare -- "quante volte da qui e' REALMENTE ripartito un
        # impulso", non solo quante volte il prezzo l'ha toccata. Zone
        # INVALIDATED (attraversate senza reazione troppe volte) vengono
        # escluse dalle notifiche da qui in poi.
        rec_params = _lh_params(asset)
        confirmation_bars = rec_params.get("recurrence_confirmation_bars", 6)
        invalidate_after = rec_params.get("recurrence_invalidate_after_failures", 2)
        min_impulse_atr_m15 = rec_params.get("min_impulse_atr", 0.8)
        now_iso = now.isoformat()

        # Prezzo corrente -- serve sia al loop di ricorrenza delle zone
        # del ciclo corrente sia al monitoraggio persistente delle zone
        # storiche. Definito PRIMA del loop, non dentro: con zero zone
        # trovate il loop non gira, e current_price resterebbe indefinita
        # (bug trovato in produzione 15/08).
        if df_m5_zones is not None and len(df_m5_zones) > 0:
            current_price = float(df_m5_zones.iloc[-1]["close"])
        else:
            current_price = float(df_m15.iloc[-1]["close"])

        enriched_zones = []
        for zone in zones:
            zref = zone.get("zone_ref")
            if not zref:
                enriched_zones.append(zone)
                continue

            prev_state = lh_db.get_zone_recurrence(conn, zref)
            if prev_state is None:
                prev_state = _new_recurrence_state(
                    zref, asset, zone["direction"], zone["zone_kind"],
                    zone["zone_high"], zone["zone_low"], now_iso,
                )

            new_state = _update_zone_recurrence(
                prev_state, current_price,
                df_m15, min_impulse_atr_m15, confirmation_bars, invalidate_after, now_iso,
                zone_high=zone["zone_high"], zone_low=zone["zone_low"],
            )
            try:
                lh_db.upsert_zone_recurrence(conn, new_state)
            except Exception as e:
                logger.warning("LH Recurrence [%s]: salvataggio fallito: %s", asset, e)

            new_score, new_confirmations = _apply_recurrence_to_score(
                zone["restart_score"], zone["confirmations"], new_state)

            if new_state["status"] == "INVALIDATED":
                logger.info("LH Recurrence [%s]: zona %s INVALIDATA (attraversata senza reazione x%d), esclusa.",
                           asset, zref, new_state["failed_visits"])
                continue  # esclusa dalle notifiche

            zone = dict(zone)
            zone["restart_score"] = new_score
            zone["confirmations"] = new_confirmations
            zone["recurrence_confirmed_restarts"] = new_state["confirmed_restarts"]
            zone["recurrence_failed_visits"] = new_state["failed_visits"]
            if new_score >= 70:
                zone["zone_strength"] = "STRONG"
            elif new_score >= 40:
                zone["zone_strength"] = "MODERATE"
            else:
                zone["zone_strength"] = "WEAK"
            enriched_zones.append(zone)

        zones = enriched_zones

        # ── Monitoraggio persistente (v3.12): aggiorno la ricorrenza di
        # TUTTE le zone ACTIVE nel DB, non solo quelle ritrovate in questo
        # ciclo. Una zona trovata ieri/la settimana scorsa deve continuare
        # a essere monitorata se il prezzo ci torna -- non essere dimenticata
        # perche' uscita dalla finestra di lookback H1/M30 (12h/8h).
        # Economico: solo un update di stato, nessuna notifica per queste.
        try:
            all_active = lh_db.get_all_active_recurrence(conn, asset)
            zone_refs_current = {z.get("zone_ref") for z in zones}
            max_age_days = rec_params.get("recurrence_max_age_days", 14)
            for hist_state in all_active:
                if hist_state["zone_ref"] in zone_refs_current:
                    continue  # gia' aggiornata nel giro sopra

                # Eta' massima: zona mai rivisitata dopo N giorni ->
                # il mercato e' andato avanti, non spreca piu' cicli.
                first_seen = hist_state.get("first_seen_ts", "")
                if first_seen:
                    try:
                        age = (now - datetime.fromisoformat(first_seen)).days
                        if age > max_age_days and hist_state.get("visits", 0) == 0:
                            hist_state["status"] = "STALE"
                            lh_db.upsert_zone_recurrence(conn, hist_state)
                            logger.info("LH Recurrence [%s]: zona %s STALE (mai rivisitata dopo %d giorni)",
                                       asset, hist_state["zone_ref"], age)
                            continue
                    except Exception:
                        pass

                new_state = _update_zone_recurrence(
                    hist_state, current_price,
                    df_m15, min_impulse_atr_m15, confirmation_bars, invalidate_after, now_iso,
                )
                try:
                    lh_db.upsert_zone_recurrence(conn, new_state)
                except Exception as e:
                    logger.warning("LH Recurrence storica [%s]: %s", asset, e)
                if new_state["status"] == "INVALIDATED" and hist_state["status"] != "INVALIDATED":
                    logger.info("LH Recurrence storica [%s]: zona %s INVALIDATA",
                               asset, hist_state["zone_ref"])
        except Exception as e:
            logger.warning("LH Recurrence storica [%s]: errore (non-blocking): %s", asset, e)

        for zone in zones:
            zref = zone.get("zone_ref")
            tier = "NEAR" if zone.get("is_near") else "WATCH"
            if zref and lh_db.has_recent_zone_alert(conn, asset, zone["direction"], zref, tier=tier):
                logger.debug("LH ZoneScan [%s %s %s]: zona %s gia' notificata, skip.",
                            asset, zone["direction"], tier, zref)
                continue
            try:
                lh_db.insert_zone_alert(conn, asset, zone, tier=tier)
            except Exception as e:
                logger.warning("LH ZoneScan [%s]: insert fallito: %s", asset, e)
                continue
            logger.info(
                "LH ZoneScan [%s]: zona %s [%s] dist=%.2fATR/%.1fpt larghezza=%.2f refined=%s score=%.1f (zone=%s)",
                asset, zone["zone_kind"], tier, zone["distance_atr"],
                zone.get("distance_points", 0), zone.get("zone_width", 0),
                zone.get("m5_refined"), zone["restart_score"], zref,
            )
            if tier == "NEAR":
                # NEAR: non mando la notifica informativa qui -- sara'
                # generate_lh_signal (che gira subito dopo e vede le
                # stesse zone) a decidere se mandare un trade con
                # entry/SL/TP. Evita il doppio messaggio sulla stessa
                # zona nello stesso momento.
                logger.info(
                    "LH ZoneScan [%s]: zona %s NEAR (score=%.0f), delegata al segnale di trading.",
                    asset, zone["zone_kind"], zone["restart_score"],
                )
            else:
                _notify_zone(asset, zone, config)
    except Exception as e:
        # exc_info=True stampa il traceback completo -- prima si vedeva
        # solo str(e), spesso vuoto o poco utile per capire DOVE falliva.
        logger.error("LH ZoneScan [%s]: errore (non-blocking): %s", asset, e, exc_info=True)

    try:
        last_candle = df_m5.iloc[-1] if df_m5 is not None and len(df_m5) > 0 else df_m15.iloc[-1]
        current_high_m = float(last_candle["high"])
        current_low_m  = float(last_candle["low"])

        atr_m15 = mie_context.get("mie_volatility_atr_m15", 0) or 0
        be_threshold = 0.3 * atr_m15 if atr_m15 > 0 else 0

        if be_threshold > 0:
            open_rows = conn.execute(
                "SELECT signal_id, direction, entry, stop_loss, mfe "
                "FROM lh_signals WHERE final_outcome='OPEN' AND asset=? "
                "AND COALESCE(order_status, 'FILLED') = 'FILLED'",
                (asset,)
            ).fetchall()
            for sid, d, entry_p, sl_p, mfe_p in open_rows:
                if entry_p is None or sl_p is None:
                    continue
                fav = max(current_high_m - entry_p, 0) if d == "BUY" else max(entry_p - current_low_m, 0)
                cur_mfe = max(float(mfe_p or 0), fav)
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
            pass

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

    try:
        result = generate_lh_signal(asset, df_m15, now,
                                    mie_context=mie_context, df_m5=df_m5,
                                    restart_zones=zones, swing_zones=swing_zones)
    except Exception as e:
        logger.error("LH [%s]: errore generazione: %s", asset, e)
        return

    signal = result["signal"]
    diag   = result["diagnostics"]

    if signal is None:
        logger.info("LH [%s]: no signal — %s", asset, diag.get("rejection", "UNKNOWN"))
        return

    direction = signal["direction"]

    if lh_db.has_open_lh_signal(conn, asset, direction):
        logger.info(
            "LH [%s %s]: segnale OPEN già presente, skip.",
            asset, direction,
        )
        return

    entry = signal.get("entry", 0)
    sl = signal.get("stop_loss", 0)
    if entry and sl:
        risk_abs = abs(entry - sl)
        atr_m15 = mie_context.get("mie_volatility_atr_m15", 0) or 0
        if atr_m15 > 0:
            if risk_abs < 0.25 * atr_m15:
                logger.info(
                    "LH [%s %s]: REJECT RISK_TOO_TIGHT (%.4f = %.2f ATR < 0.25)",
                    asset, direction, risk_abs, risk_abs / atr_m15,
                )
                return
        else:
            if abs(entry - sl) / entry < 0.0002:
                logger.info(
                    "LH [%s %s]: REJECT RISK_TOO_TIGHT (%.5f, no ATR)",
                    asset, direction, abs(entry - sl) / entry,
                )
                return

    ob_ref = signal.get("swept_level_label", "")
    if ob_ref and lh_db.has_recent_lh_signal(
        conn, asset, direction, ob_ref, hours=4
    ):
        logger.info(
            "LH [%s %s]: duplicato OB=%s, skip.",
            asset, direction, ob_ref,
        )
        return

    signal["market_snapshot"] = json.dumps(mie_context, default=str)

    try:
        signal_id = lh_db.insert_lh_signal(conn, signal)
    except Exception as e:
        logger.error("LH [%s]: errore inserimento: %s", asset, e)
        return

    if signal.get("setup_state") == "WATCHING":
        logger.info(
            "LH [%s %s]: ordine PENDENTE — non inviato al Decision Ledger "
            "(sara\' catturato al riempimento)", asset, direction,
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


def _notify_zone_near(asset: str, zone: dict, config: dict):
    """
    Secondo livello, piu' urgente del primo avviso "sorvegliala": il
    prezzo e' a pochi punti dalla Restart Zone (soglia per asset, non ATR
    -- qui conta la precisione assoluta di prezzo). Stessa filosofia del
    Metodo Gold Edge (Fase 5, M5): "il mercato entra nella tua area.
    Non comprare. Non vendere. Guarda." — promemoria di conferma incluso.
    """
    try:
        from notifications import telegram_bot, ntfy_bot

        kind = zone["zone_kind"]
        emoji = "\U0001f7e2" if kind == "BULLISH" else "\U0001f534"
        kind_it = "rialzista" if kind == "BULLISH" else "ribassista"
        precision_note = "" if zone.get("m5_refined") else " (non raffinata, M5 assente)"

        def fp(v):
            if v is None: return "N/A"
            return f"{v:,.2f}" if float(v) > 1000 else f"{v:.4f}"

        trade = zone.get("suggested_trade")
        trade_line = ""
        if trade:
            trade_line = (
                f"\n\U0001f4a1 *Possibile trade* ({trade['rr']:.0f}R)\n"
                f"Entry: `{fp(trade['entry'])}`  SL: `{fp(trade['stop_loss'])}`  "
                f"TP: `{fp(trade['take_profit'])}`\n"
            )

        text = (
            f"{emoji} \U0001f6a8 *Restart Zone {kind_it} VICINISSIMA*\n"
            f"{asset.replace('_',' ')} — siamo dentro l'area, a {zone.get('distance_points',0):.1f} punti.\n\n"
            f"Zona: `{fp(zone.get('zone_low'))}` - `{fp(zone.get('zone_high'))}` "
            f"({zone.get('zone_width',0):.2f} ampiezza{precision_note})\n"
            f"Forza: {zone.get('zone_strength','?')} ({zone.get('restart_score',0):.0f}/100)\n"
            + trade_line +
            f"\n_Prima di agire, verifica tu:_\n"
            f"_- Ha preso liquidita'?_\n"
            f"_- Ha rotto la microstruttura?_\n"
            f"_- Il movimento e' deciso o debole?_\n\n"
            f"_Informativo \u2014 non e' un segnale di trading._"
        )

        bot_token  = config.get("TELEGRAM_BOT_TOKEN", "")
        chat_id    = config.get("TELEGRAM_CHAT_ID", "")
        ntfy_topic = config.get("NTFY_TOPIC", "")

        if bot_token and chat_id:
            telegram_bot.send_message(bot_token, chat_id, text)
        if ntfy_topic:
            title = f"VICINISSIMA: Restart Zone {kind_it} {asset.replace('_',' ')}"
            ntfy_bot.send_message(ntfy_topic, title, text.replace("*","").replace("`","").replace("_",""))

    except Exception as e:
        logger.warning("LH _notify_zone_near: %s", e)


def _notify_zone(asset: str, zone: dict, config: dict):
    """
    Notifica INFORMATIVA di Restart Zone — non un trade. Nessun
    entry/SL/TP operativo: solo "guarda qui", il resto lo decide il trader.

    Il messaggio guida con la frase in chiaro (zona interessante, da
    sorvegliare, ci si sta avvicinando) — i numeri tecnici restano come
    dettaglio di supporto sotto, non in testa.
    """
    try:
        from notifications import telegram_bot, ntfy_bot

        kind = zone["zone_kind"]  # BULLISH / BEARISH
        emoji = "\U0001f7e2" if kind == "BULLISH" else "\U0001f534"
        kind_it = "rialzista" if kind == "BULLISH" else "ribassista"

        strength = zone.get("zone_strength")
        if strength == "STRONG":
            headline = f"Restart Zone {kind_it} molto interessante — sorvegliala."
        elif strength == "MODERATE":
            headline = f"Restart Zone {kind_it} da tenere d'occhio."
        else:  # WEAK
            headline = f"Restart Zone {kind_it} (impulso trovato, poche conferme)."

        confirmations = zone.get("confirmations") or []
        conf_line = ", ".join(confirmations) if confirmations else "nessuna conferma SMC"
        precision_note = "" if zone.get("m5_refined") else " \u26a0\ufe0f non raffinata (M5 assente)"

        def fp(v):
            if v is None: return "N/A"
            return f"{v:,.2f}" if float(v) > 1000 else f"{v:.4f}"

        trade = zone.get("suggested_trade")
        trade_line = ""
        if trade:
            trade_line = (
                f"\n\U0001f4a1 *Possibile trade* ({trade['rr']:.0f}R)\n"
                f"Entry: `{fp(trade['entry'])}`  SL: `{fp(trade['stop_loss'])}`  "
                f"TP: `{fp(trade['take_profit'])}`\n"
            )

        text = (
            f"{emoji} *{headline}*\n"
            f"{asset.replace('_',' ')} — ci si sta avvicinando alla zona.\n\n"
            f"Zona: `{fp(zone.get('zone_low'))}` - `{fp(zone.get('zone_high'))}` "
            f"({zone.get('zone_width',0):.2f} ampiezza{precision_note})\n"
            f"Distanza: {zone['distance_atr']} ATR \u2014 punteggio: {zone.get('restart_score',0):.0f}/100\n"
            f"Confermata da: {conf_line}\n"
            + trade_line +
            f"\n_Informativo \u2014 non e' un segnale di trading._"
        )

        bot_token  = config.get("TELEGRAM_BOT_TOKEN", "")
        chat_id    = config.get("TELEGRAM_CHAT_ID", "")
        ntfy_topic = config.get("NTFY_TOPIC", "")

        if bot_token and chat_id:
            telegram_bot.send_message(bot_token, chat_id, text)
        if ntfy_topic:
            title = f"{headline} {asset.replace('_',' ')}"
            ntfy_bot.send_message(ntfy_topic, title, text.replace("*","").replace("`",""))

    except Exception as e:
        logger.warning("LH _notify_zone: %s", e)


def _notify(signal: dict, config: dict):
    try:
        from notifications import telegram_bot, ntfy_bot

        direction = signal["direction"]
        asset     = signal["asset"]
        emoji     = "\U0001f7e2" if direction == "BUY" else "\U0001f534"
        dir_it    = "LONG" if direction == "BUY" else "SHORT"

        def fp(v):
            if v is None: return "N/A"
            return f"{v:,.2f}" if float(v) > 1000 else f"{v:.4f}"

        # Stelle dal restart_score
        score = signal.get("quality_score", 0)
        n_stars = 5 if score >= 90 else (4 if score >= 70 else (3 if score >= 50 else (2 if score >= 30 else 1)))
        stars = "\u2605" * n_stars + "\u2606" * (5 - n_stars)

        # Tag brevi dalle conferme
        confs = signal.get("confirmations", [])
        tag_map = {"ORDER_BLOCK":"OB","FVG":"FVG","BOS":"BOS","SWEEP":"LIQ",
                   "REACTION_MAP":"RM","TREND_ALLINEATO":"TREND","DECELERAZIONE":"DECEL",
                   "OB-FRESH+FVG":"OB FRESH","SWING-H4":"SWING H4","SWING-D1":"SWING D1"}
        tags = []
        for c in confs:
            t = tag_map.get(c)
            if t and t not in tags:
                tags.append(t)
            if len(tags) >= 3:
                break
        tags_str = " + ".join(tags) if tags else "impulso puro"

        text = (
            f"{emoji} *{dir_it} — Restart Zone*\n"
            f"*{asset.replace('_',' ')}*\n\n"
            f"{stars}\n"
            f"{tags_str}\n\n"
            f"Entry: `{fp(signal['entry'])}`\n"
            f"SL: `{fp(signal['stop_loss'])}`\n"
            f"TP: `{fp(signal['tp'])}`\n"
            f"RR: {signal['rr']:.1f}\n\n"
            f"Sessione: {signal.get('session','?')}"
        )

        bot_token  = config.get("TELEGRAM_BOT_TOKEN", "")
        chat_id    = config.get("TELEGRAM_CHAT_ID", "")
        ntfy_topic = config.get("NTFY_TOPIC", "")

        if bot_token and chat_id:
            telegram_bot.send_message(bot_token, chat_id, text)
        if ntfy_topic:
            title = (f"LH {dir_it} {asset.replace('_',' ')} | "
                     f"{stars} {tags_str}")
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


# ============================================================
# Riepilogo di fine giornata (v3.8) -- "Overnight Trading Plan"
# ============================================================
#
# DEFAULT applicati in assenza di risposta esplicita -- facilmente
# modificabili, segnalati chiaramente:
#   - Focus BUY/SELL: punteggio migliore vince; se la differenza e'
#     sotto 5 punti, "nessuna priorita' netta" (vedi format_zone_digest
#     in liquidity_hunter.py). Se il criterio reale e' diverso (es. zona
#     piu' vicina al prezzo, o conteggio zone), va cambiato li'.
#   - Orario di invio: pensato per le 20:00 UTC (prima della chiusura
#     europea/apertura Asia) -- il trigger orario va nello scan.yml,
#     stesso pattern gia' usato per il Daily Brief (vedi sotto).

def _format_digest_message(asset: str, digest: dict) -> str:
    """Compone il testo del messaggio nello stile esatto del mockup fornito."""
    lines = [f"*{asset.replace('_',' ')}*", ""]

    if digest["buy_lines"]:
        lines.append("\U0001f7e2 *BUY WATCH*")
        lines.append("")
        for l in digest["buy_lines"]:
            lines.append(l["range"])
            lines.append(l["stars"])
            lines.append(l["tags"])
            lines.append("")

    if digest["sell_lines"]:
        lines.append("\U0001f534 *SELL WATCH*")
        lines.append("")
        for l in digest["sell_lines"]:
            lines.append(l["range"])
            lines.append(l["stars"])
            lines.append(l["tags"])
            lines.append("")

    if not digest["buy_lines"] and not digest["sell_lines"]:
        lines.append("_Nessuna Restart Zone di qualita' sufficiente oggi._")
        lines.append("")

    lines.append("\U0001f3af *Focus di domani*")
    lines.append(digest["focus"])

    return "\n".join(lines)


def _get_macro_events_for_tomorrow() -> list:
    """
    Variante di _get_macro_events() (core/daily_brief.py) per il digest
    SERALE: filtra per DOMANI invece che per oggi. Il digest gira alle
    19:00 UTC per pianificare la notte/il giorno successivo -- mostrare
    "gli eventi di oggi" a quell'ora significa mostrare eventi delle
    14:30 gia' passati da 4h e mezza, inutili per chi deve decidere se
    lasciare ordini pendenti overnight.

    Stessa fonte dati (Forex Factory, nessuna API key), stessa mappa
    valute/paesi e stesso filtro "solo impatto alto" del Daily Brief --
    cambia solo la data target del confronto.
    """
    import requests
    tomorrow_str = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")
    MACRO_COUNTRIES = ("US", "EU", "JP", "CN", "DE", "UK")
    try:
        resp = requests.get(
            "https://nfs.faireconomy.media/ff_calendar_thisweek.json", timeout=10,
        )
        if resp.status_code != 200:
            return []
        events = resp.json()
        if not isinstance(events, list):
            return []

        currency_map = {
            "USD": "US", "EUR": "EU", "GBP": "UK", "JPY": "JP",
            "CNY": "CN", "CHF": "CH", "AUD": "AU", "CAD": "CA",
        }

        filtered = []
        for ev in events:
            if ev.get("impact") not in ("High",):
                continue
            currency = ev.get("country", "")
            country = currency_map.get(currency, currency)
            if country not in MACRO_COUNTRIES:
                continue

            date_str = ev.get("date", "")
            if not date_str:
                continue
            try:
                dt_utc = datetime.fromisoformat(date_str).astimezone(timezone.utc)
                if dt_utc.strftime("%Y-%m-%d") != tomorrow_str:
                    continue
                h_local = dt_utc.hour + 2
                if h_local >= 24:
                    h_local -= 24
                time_local = f"{h_local:02d}:{dt_utc.minute:02d}"
            except Exception:
                continue

            filtered.append({
                "time": time_local, "event": ev.get("title", "?"),
                "country": country, "impact": "High",
            })

        filtered.sort(key=lambda e: e["time"])
        return filtered
    except Exception:
        return []


def send_zone_digest(config: dict):
    """
    Riepilogo serale delle Restart Zone ancora valide -- "Overnight
    Trading Plan". Chiamato una volta al giorno (trigger orario nello
    scan.yml, stesso pattern del Daily Brief).
    """
    conn = core_db.get_connection(config["DB_PATH"])
    lh_db.init_lh_schema(conn)

    now = datetime.now(timezone.utc)
    assets = config.get("LH_SCANNER", {}).get("assets", LH_ASSETS)

    parts = []
    for asset in assets:
        try:
            zones = lh_db.get_zones_for_digest(conn, asset, hours=24)
        except Exception as e:
            logger.error("LH Digest [%s]: errore lettura zone: %s", asset, e)
            continue

        buy_zones  = [z for z in zones if z["zone_kind"] == "BULLISH"]
        sell_zones = [z for z in zones if z["zone_kind"] == "BEARISH"]

        if not buy_zones and not sell_zones:
            logger.info("LH Digest [%s]: nessuna zona valida, skip.", asset)
            continue

        digest = format_zone_digest(asset, buy_zones, sell_zones)
        parts.append(_format_digest_message(asset, digest))

    conn.close()

    if not parts:
        logger.info("LH Digest: nessuna zona valida su nessun asset, nessun invio.")
        return

    header = "\U0001f319 *GOLD EDGE AI*\nOvernight Trading Plan\n\n"
    body = "\n\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n\n".join(parts)
    full_message = header + body

    # ── Eventi macro -- FIX (13/08, bug trovato dall'utente): il digest
    # serale girava a 19:00 UTC e riusava _get_macro_events() del Daily
    # Brief, che filtra per "oggi". Alle 19:00 un evento delle 14:30 e'
    # gia' successo 4h e mezza prima -- inutile per pianificare la notte.
    # Il digest serale deve mostrare gli eventi di DOMANI (il giorno per
    # cui si pianificano gli ordini overnight), non quelli di oggi gia'
    # trascorsi. Non tocco daily_brief.py (corretto per le 08:00, dove
    # "oggi" ha senso) -- variante dedicata qui, stessa fonte dati
    # (Forex Factory, nessuna API key), stessa logica di filtro/mappa
    # valute, solo la data target cambia.
    try:
        macro_events = _get_macro_events_for_tomorrow()
        if macro_events:
            macro_lines = ["", "\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500",
                          "", "\U0001f4c5 *High Impact Events (domani)*"]
            for ev in macro_events:
                flag = {"US": "\U0001f1fa\U0001f1f8", "EU": "\U0001f1ea\U0001f1fa",
                       "JP": "\U0001f1ef\U0001f1f5", "CN": "\U0001f1e8\U0001f1f3",
                       "UK": "\U0001f1ec\U0001f1e7", "DE": "\U0001f1e9\U0001f1ea"}.get(ev["country"], "")
                macro_lines.append(f"{flag} {ev['event']} \u2022 {ev['time']}")
            full_message += "\n".join(macro_lines)
    except Exception as e:
        logger.warning("LH Digest: eventi macro non disponibili (non-blocking): %s", e)

    bot_token  = config.get("TELEGRAM_BOT_TOKEN", "")
    chat_id    = config.get("TELEGRAM_CHAT_ID", "")
    ntfy_topic = config.get("NTFY_TOPIC", "")

    if bot_token and chat_id:
        telegram_bot_sent = None
        try:
            from notifications import telegram_bot
            telegram_bot_sent = telegram_bot.send_message(bot_token, chat_id, full_message)
        except Exception as e:
            logger.error("LH Digest: invio Telegram fallito: %s", e)
        logger.info("LH Digest Telegram: %s", telegram_bot_sent)

    if ntfy_topic:
        try:
            from notifications import ntfy_bot
            title = f"Gold Edge AI — Overnight Plan {now.strftime('%d %b %Y')}"
            plain = full_message.replace("*", "").replace("_", "")
            ntfy_bot.send_message(ntfy_topic, title, plain)
            logger.info("LH Digest ntfy inviato")
        except Exception as e:
            logger.error("LH Digest: invio ntfy fallito: %s", e)
