"""
core/market_radar_runner.py
Market Radar V1 — Runner (SOLA REGISTRAZIONE)

OBIETTIVO V1: verificare se il radar riconosce automaticamente le stesse
configurazioni di esaurimento che il trader riconosce a occhio. NON genera
BUY/SELL. Genera "Entry Zone" = eventi «da questo momento osserva il grafico».

FLUSSO:
    Mercato → Sensori (candele M15 + zone gia' calcolate) → Feature Engine
    → State Machine → Evento Entry Zone → Ledger (registra + monitora MAE/MFE)

── SORGENTI DATI (verificate sul DB reale, 2026-07-13) ────────────
Il radar RIUSA i dati che il sistema gia' produce, invece di ricalcolarli:

  IMPULSE
    - ampiezza  : structure_state.impulses_json → amplitude_atr  (OK, fresco)
    - velocita' : CALCOLATA dalle candele M15   (duration_bars in impulses_json
                  e' SEMPRE 0 nel DB reale → inutilizzabile, verificato su tutti
                  gli asset. La velocita' va quindi calcolata a mano.)
  CONTEXT
    - zone      : order_blocks / fvg_zones → zone_high/zone_low, ob_id/fvg_id.
                  ob_id/fvg_id fanno da zone_ref STABILE per la dedup.
  EXHAUSTION
    - CALCOLATA dalle candele M15 (contrazione ATR + corpi decrescenti).
      Unico pezzo interamente nuovo.

── CASI LIMITE VERIFICATI (da gestire) ────────────────────────────
  - impulses_json puo' essere [] (es. PAXG dismesso) → nessun impulso, RIPOSO.
  - duration_bars == 0 sempre → NON usarlo, velocita' dalle candele.
  - order_blocks/fvg_zones hanno status (MITIGATED/active/...) → filtrare.

── DEDUP ──────────────────────────────────────────────────────────
Una configurazione = una Entry Zone. zone_ref = ob_id/fvg_id della zona su
cui l'impulso e' atterrato. Finche' quella zona ha un evento "aperto" in
monitoraggio, non se ne emette un altro. Evita il problema OTE-SC.

── NON-BLOCKING ───────────────────────────────────────────────────
Ogni aggancio al Ledger e' in try/except: se fallisce, il radar continua.

── NOTA SOGLIE ────────────────────────────────────────────────────
Le soglie in RADAR_CFG servono SOLO alla macchina a stati per decidere le
transizioni, e sono volutamente LARGHE (catturare troppo, restringere coi
dati). NON sono lo "score" del setup: velocity/extension/exhaustion vanno
salvati GREZZI nel Ledger. Le soglie di "buon setup" si decidono DOPO
300-500 zone, dai dati.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from storage import db as core_db
from core import v3_db
from core import market_radar_db
from core import radar_features as rf
# from core.decision_ledger import radar_integration as ledger_link  # da creare sul modello trb_integration

logger = logging.getLogger("market_radar.runner")

RADAR_ASSETS     = ["BTC_USDT", "XAU_USD"]
RADAR_TIMEFRAMES = {"M15": "15m"}

RADAR_CFG = {
    # Impulse — soglie di transizione (larghe all'inizio)
    "IMPULSE_AMPLITUDE_ATR_MIN": 2.0,   # amplitude_atr da impulses_json
    "IMPULSE_VELOCITY_MIN":      0.6,   # calibrato sui dati M15 reali (p90 ~0.6, non 1.0)
    "IMPULSE_LOOKBACK_BARS":     6,     # finestra per calcolo velocita' su M15
    # Context
    "CONTEXT_ZONE_MAX_ATR":      1.0,   # prezzo entro N ATR da una zona
    "CONTEXT_MIN_ZONE_QUALITY":  0,     # quality_score minimo dell'OB (0 = tutti)
    # Exhaustion
    "EXHAUSTION_ATR_RATIO":      0.7,   # ATR(recenti)/ATR(picco) sotto questo
    "EXHAUSTION_LOOKBACK":       3,     # candele su cui misurare la contrazione
    # Monitoraggio outcome
    "OBSERVE_WINDOW_BARS":       30,
    "ATR_PERIOD":                14,
    "EMA_PERIOD":                20,
    # Invalidazione: se dopo l'Osservazione il prezzo prosegue nella direzione
    # dell'impulso oltre N ATR, il rimbalzo atteso non arriva → torna a RIPOSO.
    # Stop loss EQUILIBRATO (in ATR sotto/sopra l'entry). In V1 e' solo
    # REGISTRATO: sapere se e quando sarebbe stato toccato, senza mai
    # interrompere la misura del respiro (MFE) — il rimbalzo puo' arrivare
    # DOPO un tocco dello stop, ed e' proprio quello che da' il guadagno.
    # In futuro (trading reale) questo livello diventera' lo stop operativo.
    "STOP_LOSS_ATR":             1.0,    # ne' troppo stretto ne' troppo largo
    # "Fotografia" allo scatto: livelli suggeriti per le notifiche future e
    # per validare la gestione. Tarati sul trading reale del trader:
    #   TP scalp veloce = 1 ATR (rimbalzo tecnico, "qualche punto")
    #   BE trigger      = 1 ATR (a quel punto: incassa o proteggi e traila)
    # Anche questi sono SOLO registrati: il monitor dice se il respiro li ha
    # raggiunti, ma non chiude nulla. Servono a capire, sui dati, se lo scalp
    # bastava o se conveniva lasciar correre (trailing).
    "TP_SCALP_ATR":              1.0,
    "BE_TRIGGER_ATR":            1.0,
    "NOTIFY":                    False,   # V1: sola registrazione
}


# ============================================================
# FEATURE ENGINE — descrive il MOVIMENTO (grezzo, no soglie)
# ============================================================
# TODO(dev): definire insieme i corpi. Firme e semantica sotto.

def _atr(df, period: int):
    """ATR su `period` candele chiuse (da radar_features)."""
    return rf.atr(df, period)

def read_last_impulse(conn, asset: str):
    """
    Legge l'ultimo impulso da structure_state.impulses_json.
    Ritorna dict {direction, amplitude_atr, timestamp_end} oppure None.

    ATTENZIONE (verificato sul DB): duration_bars e' SEMPRE 0 → NON usarlo.
    Usiamo solo direction + amplitude_atr da qui; la velocita' la calcola
    feat_velocity dalle candele. La lista puo' essere vuota (asset fermi).
    """
    try:
        row = conn.execute(
            "SELECT impulses_json FROM structure_state WHERE asset=?", (asset,)
        ).fetchone()
        if not row or not row[0]:
            return None
        impulses = json.loads(row[0])
        if not impulses:
            return None
        last = impulses[-1]
        return {
            "direction":     last.get("direction"),        # UP / DOWN
            "amplitude_atr": last.get("amplitude_atr"),
            "timestamp_end": last.get("timestamp_end"),
        }
    except Exception as e:
        logger.debug("read_last_impulse [%s]: %s", asset, e)
        return None

def feat_velocity(df_m15, cfg):
    """Velocita' normalizzata dalle candele M15 (vedi radar_features)."""
    a = rf.atr(df_m15, cfg["ATR_PERIOD"])
    return rf.velocity(df_m15, cfg["IMPULSE_LOOKBACK_BARS"], a)

def feat_exhaustion(df_m15, cfg):
    """Contrazione ATR recente vs periodo (vedi radar_features)."""
    return rf.exhaustion(df_m15, cfg["EXHAUSTION_LOOKBACK"], cfg["ATR_PERIOD"])


# ============================================================
# CONTEXT — dove si trova il movimento (zone gia' calcolate)
# ============================================================

def nearest_zone(conn, asset: str, price: float, direction: str, atr: float, cfg):
    """
    Trova la zona tecnica piu' vicina al prezzo NELLA direzione attesa del
    rimbalzo, entro CONTEXT_ZONE_MAX_ATR * atr.

    Dopo un impulso DOWN il rimbalzo atteso e' UP → cerchiamo supporti
    (zone BULLISH) su cui il prezzo e' atterrato.

    Ritorna (zone_ref, zone_dist_atr, zone_kind) o (None, None, None).
    zone_ref = ob_id / fvg_id → identificatore STABILE per la dedup.

    ── ATTENZIONE (verificato sul DB 2026-07-13) ──────────────────────
    Su XAU_USD NON esistono order_blocks BULLISH (0 righe): se il Context
    guarda SOLO gli OB, su XAU il radar non emette mai — ma XAU e' proprio
    l'asset target. DECISIONE APERTA COL DEV: il Context deve guardare anche
    fvg_zones e/o i livelli SR (sr_engine), non solo order_blocks. Sotto e'
    implementato solo il ramo OB; il ramo FVG e' un TODO da aggiungere con
    la stessa logica (fvg_id come zone_ref).

    Status OB reali nel DB: FRESH, MITIGATED, BREAKER, INVALIDATED.
    FRESH = zona mai testata (la piu' forte per un rimbalzo). Escludiamo
    INVALIDATED e BREAKER; MITIGATED incluso ma piu' debole (da valutare).
    """
    try:
        want = "BULLISH" if direction == "BUY" else "BEARISH"
        max_dist = cfg["CONTEXT_ZONE_MAX_ATR"] * atr if atr else None
        # accumula candidati (zone_ref, dist, kind) dalle TRE fonti, poi
        # sceglie il piu' vicino entro max_dist.
        candidates = []

        # ── Fonte 1: order blocks ──────────────────────────────────
        for ob_id, zh, zl, q in conn.execute("""
            SELECT ob_id, zone_high, zone_low, quality_score FROM order_blocks
            WHERE asset=? AND direction=? AND status IN ('FRESH','MITIGATED')
        """, (asset, want)).fetchall():
            if q is not None and q < cfg["CONTEXT_MIN_ZONE_QUALITY"]:
                continue
            dist = 0.0 if zl <= price <= zh else min(abs(price - zh), abs(price - zl))
            candidates.append((f"ob:{ob_id}", dist, "order_block"))

        # ── Fonte 2: FVG zones ─────────────────────────────────────
        for fvg_id, zh, zl in conn.execute("""
            SELECT fvg_id, zone_high, zone_low FROM fvg_zones
            WHERE asset=? AND direction=? AND status NOT IN ('INVALIDATED')
              AND (is_invalidated IS NULL OR is_invalidated=0)
        """, (asset, want)).fetchall():
            if zh is None or zl is None:
                continue
            dist = 0.0 if zl <= price <= zh else min(abs(price - zh), abs(price - zl))
            candidates.append((f"fvg:{fvg_id}", dist, "fvg"))

        # ── Fonte 3: livelli di liquidita' (da liquidity_snapshots) ─
        # Per un rimbalzo BUY servono supporti SOTTO (kind='low'); per SELL
        # resistenze SOPRA (kind='high'). Non swept, ancora ACTIVE.
        import json as _json
        row = conn.execute("""
            SELECT snapshot_json FROM liquidity_snapshots
            WHERE asset=? ORDER BY timestamp_snapshot DESC LIMIT 1
        """, (asset,)).fetchone()
        if row and row[0]:
            want_kind = "low" if direction == "BUY" else "high"
            for lv in _json.loads(row[0]).get("levels", []):
                if lv.get("kind") != want_kind or lv.get("swept") or lv.get("status") != "ACTIVE":
                    continue
                lp = lv.get("price")
                if lp is None:
                    continue
                # supporto valido solo se sotto/uguale al prezzo (BUY), sopra (SELL)
                if direction == "BUY" and lp > price:
                    continue
                if direction == "SELL" and lp < price:
                    continue
                dist = abs(price - lp)
                zref = ("liq:%s:%s:%s" % (lv.get("label",""), lv.get("timeframe",""),
                                          round(lp, 1))).replace(" ", "_")
                candidates.append((zref, dist, "liquidity"))

        # ── scegli il candidato piu' vicino entro la soglia ────────
        best = None
        for zref, dist, kind in candidates:
            if max_dist is not None and dist > max_dist:
                continue
            if best is None or dist < best[1]:
                best = (zref, dist, kind)
        if best:
            return (best[0], best[1] / atr if atr else None, best[2])
        return (None, None, None)
    except Exception as e:
        logger.debug("nearest_zone [%s]: %s", asset, e)
        return (None, None, None)


def compute_features(conn, asset, df_m15, price, cfg) -> dict:
    imp = read_last_impulse(conn, asset)
    direction = None
    if imp and imp.get("direction"):
        # rimbalzo atteso OPPOSTO all'impulso
        direction = "BUY" if imp["direction"] == "DOWN" else "SELL"

    atr = _atr(df_m15, cfg["ATR_PERIOD"])
    zone_ref, zone_dist_atr, zone_kind = (None, None, None)
    if direction and atr:
        zone_ref, zone_dist_atr, zone_kind = nearest_zone(
            conn, asset, price, direction, atr, cfg)

    return {
        "direction":       direction,
        "amplitude_atr":   imp.get("amplitude_atr") if imp else None,
        "velocity":        feat_velocity(df_m15, cfg),
        "extension":       rf.extension(df_m15, cfg["EMA_PERIOD"], atr),
        "exhaustion":      feat_exhaustion(df_m15, cfg),
        "body_shrinking":  rf.body_shrinking(df_m15, cfg["EXHAUSTION_LOOKBACK"]),
        "zone_ref":        zone_ref,
        "zone_dist_atr":   zone_dist_atr,
        "zone_kind":       zone_kind,
        "atr":             atr,
        "price":           price,
    }


# ============================================================
# STATE MACHINE — interpreta le feature, decide lo stato
# ============================================================

STATE_REST     = "RIPOSO"
STATE_EXTENDED = "MERCATO_ESTESO"
STATE_OBSERVE  = "OSSERVAZIONE"

def next_state(cur_state: str, f: dict, cfg) -> tuple[str, bool]:
    """
    Ritorna (nuovo_stato, emetti_entry_zone).
      RIPOSO         → MERCATO_ESTESO  se impulso ampio E veloce
      MERCATO_ESTESO → OSSERVAZIONE    se il prezzo e' su una zona tecnica
                     → RIPOSO          se l'impulso e' svanito
      OSSERVAZIONE   → [Entry Zone]    se esaurimento in corso
                     → RIPOSO          se il prezzo continua forte oltre
                                       l'invalidazione (rimbalzo non arrivato)
    """
    amp = f.get("amplitude_atr")
    vel = f.get("velocity")
    # impulso valido = ampio (da structure_state) E veloce (da candele M15)
    impulse_ok = (amp is not None and amp >= cfg["IMPULSE_AMPLITUDE_ATR_MIN"]
                  and vel is not None and vel >= cfg["IMPULSE_VELOCITY_MIN"])
    context_ok = (f.get("zone_ref") is not None)   # nearest_zone ha gia' filtrato per distanza
    exhausted  = (f.get("exhaustion") is not None
                  and f["exhaustion"] <= cfg["EXHAUSTION_ATR_RATIO"])

    if cur_state == STATE_REST:
        return (STATE_EXTENDED, False) if impulse_ok else (STATE_REST, False)
    if cur_state == STATE_EXTENDED:
        if not impulse_ok:
            return (STATE_REST, False)            # impulso svanito senza zona
        return (STATE_OBSERVE, False) if context_ok else (STATE_EXTENDED, False)
    if cur_state == STATE_OBSERVE:
        if exhausted:
            return (STATE_OBSERVE, True)          # EMETTI Entry Zone, resta in osservazione
        # Nota: NON invalidiamo su continuazione del prezzo. Il respiro puo'
        # arrivare anche dopo che il prezzo e' andato un po' contro. Lo stop
        # loss viene REGISTRATO nel monitor, ma non interrompe la misura.
        return (STATE_OBSERVE, False)
    return (STATE_REST, False)


# ============================================================
# RUNNER per asset
# ============================================================

def _merge_cfg(config: dict) -> dict:
    """Fonde RADAR_CFG coi valori di config['MARKET_RADAR'], mappando le
    chiavi snake_case del config alle chiavi di RADAR_CFG."""
    cfg = dict(RADAR_CFG)
    mr = config.get("MARKET_RADAR", {}) or {}
    key_map = {
        "velocity_min":         "IMPULSE_VELOCITY_MIN",
        "amplitude_atr_min":    "IMPULSE_AMPLITUDE_ATR_MIN",
        "context_zone_max_atr": "CONTEXT_ZONE_MAX_ATR",
        "exhaustion_atr_ratio": "EXHAUSTION_ATR_RATIO",
        "observe_window_bars":  "OBSERVE_WINDOW_BARS",
        "stop_loss_atr":        "STOP_LOSS_ATR",
        "notify":               "NOTIFY",
    }
    for k_cfg, k_int in key_map.items():
        if k_cfg in mr:
            cfg[k_int] = mr[k_cfg]
    return cfg


def _run_for_asset(conn, asset: str, config: dict, now: datetime):
    logger.info("Radar: inizio ciclo per %s", asset)
    cfg = _merge_cfg(config)

    limit  = config.get("BOOTSTRAP_TARGET_CANDLES", 300)
    df_m15 = v3_db.get_v3_candles_df(conn, asset, RADAR_TIMEFRAMES["M15"], limit=limit)
    if len(df_m15) < 30:
        logger.warning("Radar [%s]: dati M15 insufficienti (%d), skip.", asset, len(df_m15))
        return

    # 1) Monitora zone aperte → aggiorna MAE/MFE, chiude a fine finestra
    try:
        last = df_m15.iloc[-1]
        updated = market_radar_db.monitor_open_zones(
            conn, asset,
            current_high=float(last["high"]), current_low=float(last["low"]),
            now_iso=now.isoformat(), window_bars=cfg["OBSERVE_WINDOW_BARS"],
        )
        for u in updated:
            logger.info("Radar Monitor [%s]: zona %s mae=%.4f mfe=%.4f bars=%d %s",
                        asset, u["zone_id"][:8], u["mae"], u["mfe"], u["bars"],
                        "CHIUSA" if u["closed"] else "aperta")
            # TODO: se u["closed"] → ledger_link.link_outcome(... MAE/MFE grezzi ...)
    except Exception as e:
        logger.error("Radar Monitor [%s]: errore: %s", asset, e)

    # 2) Stato corrente della macchina
    state = market_radar_db.get_state(conn, asset) or STATE_REST

    # 3) Feature (impulso da structure_state, zona da order_blocks, resto da M15)
    price = float(df_m15.iloc[-1]["close"])
    open_refs = market_radar_db.get_open_zone_refs(conn, asset)
    f = compute_features(conn, asset, df_m15, price, cfg)

    # 4) Avanza la macchina; registra SEMPRE le transizioni (funnel)
    new_state, emit = next_state(state, f, cfg)
    if new_state != state:
        try:
            market_radar_db.log_transition(conn, asset, from_state=state,
                                           to_state=new_state, features=f,
                                           now_iso=now.isoformat())
        except Exception as e:
            logger.warning("Radar [%s]: log_transition fallito: %s", asset, e)
    market_radar_db.set_state(conn, asset, new_state)

    # 5) Emissione Entry Zone (con DEDUP su zone_ref)
    if emit:
        zref = f.get("zone_ref")
        if zref and zref in open_refs:
            logger.info("Radar [%s]: zona %s gia' aperta, skip (no doppione).", asset, zref)
            return
        # Stop loss EQUILIBRATO: STOP_LOSS_ATR ATR nella direzione avversa
        # (sotto l'entry per BUY, sopra per SELL). Solo registrato in V1.
        atr = f.get("atr")
        stop_atr = cfg.get("STOP_LOSS_ATR", 1.0)
        tp_atr   = cfg.get("TP_SCALP_ATR", 1.0)
        be_atr   = cfg.get("BE_TRIGGER_ATR", 1.0)
        stop_loss = tp_scalp = be_trigger = None
        if atr:
            if f["direction"] == "BUY":
                stop_loss   = price - stop_atr * atr
                tp_scalp    = price + tp_atr * atr
                be_trigger  = price + be_atr * atr
            else:
                stop_loss   = price + stop_atr * atr
                tp_scalp    = price - tp_atr * atr
                be_trigger  = price - be_atr * atr
        f["stop_loss"]  = stop_loss
        f["tp_scalp"]   = tp_scalp
        f["be_trigger"] = be_trigger

        try:
            zone_id = market_radar_db.insert_zone(
                conn, asset, direction=f["direction"], price=price,
                features=f, zone_ref=zref, now_iso=now.isoformat())
        except Exception as e:
            logger.error("Radar [%s]: insert_zone fallito: %s", asset, e)
            return

        logger.info("Radar [%s]: ⚡ ENTRY ZONE dir=%s price=%.4f zone=%s "
                    "amp=%.2f vel=%.2f exh=%.2f (id=%s)",
                    asset, f["direction"], price, zref or "-",
                    f.get("amplitude_atr") or 0, f.get("velocity") or 0,
                    f.get("exhaustion") or 0, zone_id)

        # TODO: ledger_link.capture_zone(zone_id, asset, f, ...) — salva feature grezze
        if cfg.get("NOTIFY"):
            _notify(asset, f, price, config)


def _notify(asset: str, f: dict, price: float, config: dict):
    """Notifica 'zona da osservare' — NON un BUY/SELL."""
    try:
        from notifications import telegram_bot, ntfy_bot
        text = (
            f"👀 *MARKET RADAR — Zona da osservare*\n\n"
            f"*{asset.replace('_',' ')}*\n"
            f"Possibile esaurimento · rimbalzo atteso: {f.get('direction')}\n\n"
            f"Prezzo: `{price:.4f}`\n"
            f"amp={f.get('amplitude_atr') or 0:.2f} vel={f.get('velocity') or 0:.2f} "
            f"exh={f.get('exhaustion') or 0:.2f}\n\n"
            f"_Da questo momento osserva il grafico. Decide il trader._"
        )
        bot = config.get("TELEGRAM_BOT_TOKEN",""); chat = config.get("TELEGRAM_CHAT_ID","")
        topic = config.get("NTFY_TOPIC","")
        if bot and chat: telegram_bot.send_message(bot, chat, text)
        if topic: ntfy_bot.send_message(topic, f"Radar {asset} {f.get('direction')}",
                                        text.replace("*","").replace("`",""))
    except Exception as e:
        logger.warning("Radar _notify: %s", e)


def run_radar_scan(config: dict):
    """
    Entry point. Legge candele M15 e zone dal DB (nessun fetch).
    Nota: a differenza di TRB non serve market_contexts — le zone le legge
    direttamente da order_blocks/fvg_zones.
    """
    conn = core_db.get_connection(config["DB_PATH"])
    market_radar_db.init_radar_schema(conn)

    now    = datetime.now(timezone.utc)
    assets = config.get("MARKET_RADAR", {}).get("assets", RADAR_ASSETS)

    logger.info("=== Market Radar: inizio ciclo (%s) ===", ", ".join(assets))
    for asset in assets:
        try:
            _run_for_asset(conn, asset, config, now)
        except Exception as e:
            logger.error("Radar [%s]: errore non gestito: %s", asset, e)
    conn.close()
    logger.info("=== Market Radar: fine ciclo ===")
