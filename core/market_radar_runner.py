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
    - ampiezza  : CALCOLATA dalle candele M15 (= velocity x lookback).
                  NON piu' da structure_state: quella fonte si aggiorna ogni
                  4-6 ore mentre impulso→calma dura ~2h, e ampiezza/velocita'
                  non hanno mai coinciso (0 su 13 impulsi in 46h di dati).
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
    # NON PIU' USATA: il gate e' la sola velocity, che implica gia' un
    # movimento di 3.6 ATR (0.6 x 6 barre) — piu' severo di questi 2.0.
    # Lasciata per non rompere config.yaml che la referenzia.
    "IMPULSE_AMPLITUDE_ATR_MIN": 2.0,   # (inattiva)
    "IMPULSE_VELOCITY_MIN":      0.6,   # calibrato sui dati M15 reali (p90 ~0.6, non 1.0)
    "IMPULSE_LOOKBACK_BARS":     6,     # finestra per calcolo velocita' su M15
    # Freschezza dell'impulso: amplitude_atr arriva da structure_state (che lo
    # aggiorna quando vuole), velocity dalle candele M15 delle ultime
    # IMPULSE_LOOKBACK_BARS barre. Se le due misure guardano momenti diversi,
    # la congiunzione "ampio E veloce" non scatta mai per pura asincronia.
    # Verificato sui dati del 15/07: BTC ha avuto 4 impulsi (vel 0.6-0.89) ma
    # l'ampiezza in structure_state era timbrata 16:31 mentre l'ultimo picco
    # di velocita' era alle 13:00-13:30 → mai coincidenti → BTC sempre in
    # RIPOSO, zero zone, nonostante amplitude_atr=2.935 superasse il gate.
    # Default = la stessa finestra della velocity (6 barre x 15 min = 90 min):
    # cosi' le due misure parlano dello stesso movimento.
    # NON PIU' USATA: serviva a non accoppiare un'ampiezza vecchia di
    # structure_state con la velocity di adesso. Ora entrambe escono dalle
    # stesse candele, nella stessa finestra: l'asincronia non esiste piu'.
    "IMPULSE_MAX_AGE_MIN":       90,    # (inattiva)
    # Context
    "CONTEXT_ZONE_MAX_ATR":      1.0,   # prezzo entro N ATR da una zona
    "CONTEXT_MIN_ZONE_QUALITY":  0,     # quality_score minimo dell'OB (0 = tutti)
    # Exhaustion
    "EXHAUSTION_ATR_RATIO":      0.7,   # ATR(recenti)/ATR(picco) sotto questo
    "EXHAUSTION_LOOKBACK":       3,     # candele su cui misurare la contrazione
    # Uscita dallo stato OSSERVAZIONE (la macchina non ne usciva MAI: una
    # sola osservazione ha prodotto 13 zone fantasma in 6 ore, verificato
    # sui dati del 15/07). Scaduta la finestra si torna a RIPOSO e serve un
    # NUOVO impulso valido per ripartire.
    # Finestra di osservazione, tarata sul ritmo REALE misurato (53 impulsi):
    # dopo l'impulso la calma arriva in mediana 9 candele M15 (BTC 135 min,
    # XAU 105 min); oltre le 30 candele si trova solo in 3 casi su 41.
    # 16 candele (4h) coprono la mediana con margine e liberano la macchina
    # in tempo per l'impulso successivo, invece di tenerla agganciata a uno
    # gia' morto.
    "OBSERVE_MAX_BARS":          16,    # 16 barre M15 = 4h
    "BAR_MINUTES":               15,
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

def read_last_impulse(conn, asset: str, now=None, max_age_min: float = None):
    """
    NON PIU' USATA dalla state machine (resta per compatibilita'/debug).

    Il radar ricava impulso, direzione e ampiezza dalle proprie candele M15
    (vedi feat_impulse_direction / feat_velocity / compute_features). Questa
    funzione leggeva structure_state, che si aggiorna ogni 4-6 ore: troppo
    lenta per un fenomeno che dura ~2 ore.

    Legge l'ultimo impulso da structure_state.impulses_json.
    Ritorna dict {direction, amplitude_atr, timestamp_end, age_min} oppure None.

    ATTENZIONE (verificato sul DB): duration_bars e' SEMPRE 0 → NON usarlo.
    Usiamo solo direction + amplitude_atr da qui; la velocita' la calcola
    feat_velocity dalle candele. La lista puo' essere vuota (asset fermi).

    FRESCHEZZA (max_age_min): structure_state tiene UN SOLO impulso corrente,
    sovrascritto quando l'engine lo aggiorna — non uno storico. Se e' piu'
    vecchio di max_age_min rispetto alla finestra su cui misuriamo la
    velocita', le due condizioni del gate descrivono movimenti diversi e la
    loro congiunzione e' priva di significato. In quel caso l'impulso viene
    scartato (ritorna None) e la macchina resta a RIPOSO: e' il
    comportamento corretto, meglio nessun segnale che un segnale spurio.
    Con max_age_min=None il controllo e' disattivato (comportamento vecchio).
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

        age_min = None
        ts_end = last.get("timestamp_end")
        if max_age_min and now is not None and ts_end:
            try:
                t_end = datetime.fromisoformat(ts_end)
                if t_end.tzinfo is None:
                    t_end = t_end.replace(tzinfo=timezone.utc)
                age_min = (now - t_end).total_seconds() / 60.0
            except Exception:
                age_min = None          # timestamp illeggibile → non scartare
            if age_min is not None and age_min > max_age_min:
                logger.info("Radar [%s]: impulso stantio (%.0f min > %.0f), "
                            "ampiezza ignorata.", asset, age_min, max_age_min)
                return None

        return {
            "direction":     last.get("direction"),        # UP / DOWN
            "amplitude_atr": last.get("amplitude_atr"),
            "timestamp_end": ts_end,
            "age_min":       round(age_min, 1) if age_min is not None else None,
        }
    except Exception as e:
        logger.debug("read_last_impulse [%s]: %s", asset, e)
        return None

def feat_impulse_direction(df_m15, cfg):
    """
    Direzione dell'impulso (UP/DOWN) dalle SOLE candele M15, misurata sulla
    STESSA finestra della velocity: il segno di close[-1] - close[-1-lookback].

    Prima arrivava da structure_state["impulses"][-1]["direction"]. Due
    problemi, entrambi verificati sui dati:
      - la fonte si aggiorna ogni 4-6 ore, mentre impulso→calma dura ~2h:
        la direzione descriveva un movimento gia' concluso
      - con il controllo di freschezza l'impulso stantio veniva scartato,
        quindi direction restava None e la macchina non poteva mai raggiungere
        OSSERVAZIONE (nearest_zone richiede una direzione)

    Ora direzione e velocita' escono dalla stessa misura, sulla stessa
    finestra, nello stesso istante. Nessuna asincronia possibile.
    """
    lb = int(cfg.get("IMPULSE_LOOKBACK_BARS", 6))
    if len(df_m15) < lb + 1:
        return None
    try:
        delta = float(df_m15["close"].iloc[-1]) - float(df_m15["close"].iloc[-1 - lb])
    except Exception:
        return None
    if delta > 0:
        return "UP"
    if delta < 0:
        return "DOWN"
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


def compute_features(conn, asset, df_m15, price, cfg, now=None,
                     cur_state=None) -> dict:
    # Impulso rilevato dalle SOLE candele M15 del modulo: direzione e
    # velocita' escono dalla stessa finestra, nello stesso istante.
    # structure_state non viene piu' letto (vedi feat_impulse_direction e
    # next_state per il perche', misurato sui dati).
    imp_dir_now = feat_impulse_direction(df_m15, cfg)
    vel_now = feat_velocity(df_m15, cfg)

    # ── DIREZIONE CONGELATA ──────────────────────────────────
    # Fuori da RIPOSO la direzione NON si ricalcola: resta quella
    # dell'impulso che ha avviato la macchina, letta dal funnel
    # (radar_transitions, transizione RIPOSO → MERCATO_ESTESO).
    #
    # Perche': durante l'esaurimento il prezzo oscilla e la velocita' crolla,
    # quindi il segno delle ultime 6 candele diventa RUMORE. Misurato sul
    # caso reale di BTC del 16/07: impulso DOWN alle 07:15 (vel 0.6979), poi
    # in osservazione la direzione e' passata a UP con vel 0.267 e 0.018 —
    # il prezzo era fermo. Il radar ha emesso due zone SELL contro il proprio
    # impulso: non misuravano il rimbalzo, lo misuravano al contrario.
    #
    # La direzione si sblocca SOLO con un impulso nuovo e vero (vedi
    # next_state): stessa soglia che avvia la macchina, nessun numero inventato.
    imp_dir = imp_dir_now
    if cur_state and cur_state != STATE_REST:
        try:
            frozen = market_radar_db.get_last_impulse_features(conn, asset)
            if frozen and frozen.get("impulse_direction"):
                imp_dir = frozen["impulse_direction"]
        except Exception as e:
            logger.debug("Radar [%s]: direzione congelata non letta: %s", asset, e)

    direction = None
    if imp_dir:
        # rimbalzo atteso OPPOSTO all'impulso
        direction = "BUY" if imp_dir == "DOWN" else "SELL"

    atr = _atr(df_m15, cfg["ATR_PERIOD"])
    zone_ref, zone_dist_atr, zone_kind = (None, None, None)
    if direction and atr:
        zone_ref, zone_dist_atr, zone_kind = nearest_zone(
            conn, asset, price, direction, atr, cfg)

    vel = vel_now
    # Ampiezza dell'impulso ricavata dalle STESSE candele e dalla stessa
    # finestra della velocity — non piu' da structure_state:
    #     velocity = |close[-1] - close[-1-lb]| / (lb * ATR)
    #  => |close[-1] - close[-1-lb]| / ATR = velocity * lb
    # E' quindi il movimento in ATR sulla finestra dell'impulso. Resta come
    # informazione registrata (utile in analisi), NON come gate: il gate e'
    # la sola velocity, che questa grandezza contiene per costruzione.
    lb = int(cfg.get("IMPULSE_LOOKBACK_BARS", 6))

    return {
        "direction":       direction,
        "impulse_direction": imp_dir,          # congelata fuori da RIPOSO
        "impulse_direction_now": imp_dir_now,  # ricalcolata: solo informativa
        "amplitude_atr":   round(vel * lb, 3) if vel is not None else None,
        "velocity":        vel,
        "extension":       rf.extension(df_m15, cfg["EMA_PERIOD"], atr),
        "exhaustion":      feat_exhaustion(df_m15, cfg),
        "body_shrinking":  rf.body_shrinking(df_m15, cfg["EXHAUSTION_LOOKBACK"]),
        "zone_ref":        zone_ref,
        "zone_dist_atr":   zone_dist_atr,
        "zone_kind":       zone_kind,
        "atr":             atr,
        "price":           price,
        "m5_reversal":     None,  # popolato dal runner se df_m5 disponibile
    }


# ============================================================
# STATE MACHINE — interpreta le feature, decide lo stato
# ========================================================

STATE_REST     = "RIPOSO"
STATE_EXTENDED = "MERCATO_ESTESO"
STATE_OBSERVE  = "OSSERVAZIONE"

def next_state(cur_state: str, f: dict, cfg,
               bars_in_observe: float | None = None) -> tuple[str, bool]:
    """
    Ritorna (nuovo_stato, emetti_entry_zone).
      RIPOSO         → MERCATO_ESTESO  se impulso ampio E veloce
      MERCATO_ESTESO → OSSERVAZIONE    se il prezzo e' su una zona tecnica
                     → RIPOSO          se l'impulso e' svanito
      OSSERVAZIONE   → [Entry Zone]    se esaurimento IN CORSO SU UNA ZONA
                     → RIPOSO          quando la finestra di osservazione scade

    Due difetti corretti (diagnosticati sui dati reali del 15/07):

    1. NESSUNA USCITA da OSSERVAZIONE. La macchina, una volta entrata, non
       tornava mai a RIPOSO: il gate dell'impulso (amplitude>=2 E velocity>=0.6)
       veniva quindi verificato UNA VOLTA SOLA, all'inizio. Nel DB: una sola
       transizione RIPOSO→MERCATO_ESTESO, poi 16 zone emesse per ore da
       quell'unico impulso. Ora la finestra scade (OBSERVE_MAX_BARS).

    2. CONTESTO NON RICONTROLLATO all'emissione. context_ok era richiesto solo
       per ENTRARE in osservazione; poi il prezzo si allontanava dalle zone e
       zone_ref diventava None, ma si emetteva lo stesso. Peggio: la dedup a
       valle e' `if zref and zref in open_refs`, quindi con zref=None andava in
       corto e NON deduplicava nulla. Risultato: 13 zone su 16 con zone_ref
       NULL, tutte lo stesso setup. Ora senza zona non si emette.
    """
    amp = f.get("amplitude_atr")
    vel = f.get("velocity")
    # Impulso valido = VELOCE. Basta questo, e il gate e' piu' severo di
    # quanto sembri:
    #
    #     velocity = |close[-1] - close[-6]| / (6 * ATR)
    #     velocity >= 0.6  <=>  il prezzo ha percorso >= 3.6 ATR in 6 candele
    #
    # Il gate ampiezza chiedeva >= 2.0 ATR, cioe' MENO di quanto la velocita'
    # garantisce gia': non filtrava nulla. In compenso portava una dipendenza
    # da structure_state, e i dati dicono che gli costava tutto:
    #
    #   - ampiezza e velocita' non hanno MAI coinciso: 0 su 13 impulsi in 46h
    #   - perche' misurano cose diverse su finestre diverse: amplitude_atr e'
    #     una gamba strutturale (svolta anche in ore, duration_bars e' sempre 0),
    #     velocity sono le ultime 6 candele M15
    #   - esempio reale: amp=8.86 ATR con velocity=0.203 nello stesso istante
    #   - e structure_state produce un impulso ogni 4-6 ore (gap mediano 374 min
    #     su BTC), mentre impulso→calma dura ~2 ore: la fonte arriva DOPO che
    #     il fenomeno e' finito
    #
    # Ora il radar vede l'impulso MENTRE accade, dalle sole candele M15.
    impulse_ok = (vel is not None and vel >= cfg["IMPULSE_VELOCITY_MIN"])
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
        # Inversione VERA: un impulso nuovo, in direzione opposta a quella
        # congelata, riporta a RIPOSO — al giro dopo la macchina ripartira'
        # con la direzione nuova. Serve la STESSA soglia che avvia il radar
        # (IMPULSE_VELOCITY_MIN): se 0.6 basta per far partire la macchina da
        # RIPOSO, basta anche per fargli cambiare idea. Sotto quella soglia
        # e' rumore di esaurimento e viene ignorato: verificato su BTC del
        # 16/07, dove i "cambi di direzione" avevano velocita' 0.267 e 0.018
        # mentre tutti i momenti sopra 0.6 erano concordi con l'impulso.
        dir_now = f.get("impulse_direction_now")
        dir_frozen = f.get("impulse_direction")
        if (impulse_ok and dir_now and dir_frozen and dir_now != dir_frozen):
            return (STATE_REST, False)

        # Scadenza: l'osservazione non puo' durare all'infinito, altrimenti
        # un impulso di ore fa continua a legittimare nuove zone.
        max_bars = cfg.get("OBSERVE_MAX_BARS")
        if (max_bars and bars_in_observe is not None
                and bars_in_observe >= max_bars):
            return (STATE_REST, False)
        # Emette SOLO se l'esaurimento avviene SU una zona tecnica
        # E il M5 conferma l'inversione (se disponibile).
        if exhausted and context_ok:
            m5r = f.get("m5_reversal")
            if m5r is None or m5r is True:  # None = M5 non disponibile, non bloccare
                return (STATE_OBSERVE, True)          # EMETTI Entry Zone, resta in osservazione
        # Nota: NON invalidiamo su continuazione del prezzo. Il respiro puo'
        # arrivare anche dopo che il prezzo e' andato un po' contro. Lo stop
        # loss viene REGISTRATO nel monitor, ma non interrompe la misura.
        return (STATE_OBSERVE, False)
    return (STATE_REST, False)


# ============================================================
# RUNNER per asset
# ============================================================

def _bars_since_state(conn, asset: str, state: str, now, cfg) -> float | None:
    """
    Quante barre M15 sono passate dall'ingresso nello stato corrente.
    Letto da radar_transitions (ultima transizione VERSO quello stato).
    Ritorna None se non c'e' storico: in quel caso la finestra non scade
    (fail-open, non blocca il radar).
    """
    ts = market_radar_db.get_last_transition_ts(conn, asset, state)
    if not ts:
        return None
    try:
        t0 = datetime.fromisoformat(ts)
        if t0.tzinfo is None:
            t0 = t0.replace(tzinfo=timezone.utc)
        minutes = (now - t0).total_seconds() / 60.0
        return minutes / float(cfg.get("BAR_MINUTES", 15))
    except Exception:
        return None

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
        "observe_max_bars":     "OBSERVE_MAX_BARS",
        "impulse_max_age_min":  "IMPULSE_MAX_AGE_MIN",
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
    f = compute_features(conn, asset, df_m15, price, cfg, now=now,
                         cur_state=state)

    # ── M5 reversal confirmation (solo XAU, dove M5 è disponibile) ──
    if asset == "XAU_USD":
        try:
            df_m5 = v3_db.get_v3_candles_df(conn, asset, "5m", limit=20)
            if df_m5 is not None and len(df_m5) >= 3 and f.get("direction"):
                # Le ultime 3 candele M5 confermano la direzione del rimbalzo?
                # BUY: 3 close consecutive crescenti. SELL: 3 close decrescenti.
                closes = df_m5["close"].astype(float).values[-3:]
                if f["direction"] == "BUY":
                    f["m5_reversal"] = bool(closes[2] > closes[1] > closes[0])
                else:
                    f["m5_reversal"] = bool(closes[2] < closes[1] < closes[0])
        except Exception as e:
            logger.debug("Radar [%s]: M5 reversal check fallito: %s", asset, e)

    # 4) Da quanto tempo siamo in OSSERVAZIONE (per la scadenza della finestra)
    bars_in_observe = None
    if state == STATE_OBSERVE:
        bars_in_observe = _bars_since_state(conn, asset, STATE_OBSERVE, now, cfg)

    # 5) Avanza la macchina; registra SEMPRE le transizioni (funnel)
    new_state, emit = next_state(state, f, cfg, bars_in_observe=bars_in_observe)
    if new_state != state:
        try:
            market_radar_db.log_transition(conn, asset, from_state=state,
                                           to_state=new_state, features=f,
                                           now_iso=now.isoformat())
        except Exception as e:
            logger.warning("Radar [%s]: log_transition fallito: %s", asset, e)
        if new_state == STATE_REST and state == STATE_OBSERVE:
            logger.info("Radar [%s]: finestra di osservazione scaduta "
                        "(%.1f barre) → RIPOSO, serve un nuovo impulso.",
                        asset, bars_in_observe or 0)
    # updated_ts veniva passato NULL a ogni scan: ora si registra davvero.
    market_radar_db.set_state(conn, asset, new_state, now_iso=now.isoformat())

    # 6) Emissione Entry Zone (con DEDUP su zone_ref)
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

        # Velocita' dell'IMPULSO (non dell'esaurimento). Senza questo il
        # dashboard classifica ogni zona come "lenta": le feature qui sopra
        # sono misurate durante l'esaurimento, quando la velocita' e' bassa
        # per definizione (0.046-0.269 nei dati reali, contro il gate >=0.6).
        # Cosi' l'ipotesi "impulso piu' veloce → rimbalzo maggiore" diventa
        # testabile sul numero giusto.
        imp_f = market_radar_db.get_last_impulse_features(conn, asset)
        f["impulse_velocity"]      = imp_f.get("velocity")
        f["impulse_amplitude_atr"] = imp_f.get("amplitude_atr")
        f["exhaustion_velocity"]   = f.get("velocity")   # esplicito: e' l'altra

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
