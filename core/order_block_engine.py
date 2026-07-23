"""
core/order_block_engine.py
Order Block Engine V2 — Sprint 14

Mappa persistente degli Order Block con ciclo di vita completo.

Ogni scan:
  1. Rileva nuovi OB dalla finestra storica M15
  2. Dedup contro la mappa esistente (sovrapposizione zona >50%)
  3. Aggiorna il ciclo di vita di tutti gli OB attivi
  4. Calcola quality score usando Structure Engine context
  5. Produce snapshot ordinato per qualità × distanza

Ciclo di vita:
  FRESH       → appena formato, non ancora testato dal prezzo
  TESTED      → prezzo ha toccato il bordo della zona (wick)
  MITIGATED   → prezzo ha chiuso dentro la zona (parziale)
  INVALIDATED → prezzo ha chiuso oltre la zona (attraversata)
  BREAKER     → OB invalidato che agisce come S/R nella direzione opposta

Tabelle:
  order_blocks          — mappa persistente (1 riga per OB)
  order_block_snapshots — snapshot per scan (consumato da tutte le strategie)
"""

from __future__ import annotations

import json
import logging
import uuid
import sqlite3
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("order_block_engine")

# ============================================================
# Parametri
# ============================================================

DEFAULT_CONFIG = {
    "detection_lookback": 50,        # candele da analizzare per trovare nuovi OB
    "min_displacement_atr": 1.0,     # impulso minimo per considerare un OB valido
    "min_body_ratio": 0.5,           # body/range minimo della candela OB
    # Sweep prima dell'OB (doc 005). Rilevato dalle SOLE candele M15 del
    # modulo: nessuna dipendenza da Liquidity/Structure Engine.
    # Alimenta solo la quality (+2), NON la detection.
    "sweep_lookback_bars": 22,       # finestra in cui cercare l'estremo di swing
    "sweep_check_bars": 5,           # candele prima dell'OB in cui cercare la presa
    # Aggiornato da 20/3 a 22/5 dopo test A/B su dati reali (18/07/2026):
    # finestra di riferimento tenuta stabile a 17 barre (22-5), finestra di
    # verifica estesa da 45 a 75 minuti. Hit rate su 32 OB reali: 3.1% -> 9.4%.
    # Monitorare 1-2 settimane prima di valutare ulteriore estensione a 7.
    "zone_overlap_threshold": 0.5,   # sovrapposizione minima per dedup (50%)
    "max_age_bars": 500,             # oltre questa età, OB diventa STALE
    "max_tracked": 30,               # OB massimi per asset nella mappa
    "invalidation_close_through": True,  # True = chiusura oltre zona; False = wick
    "breaker_enabled": True,         # abilita conversione a BREAKER
    # BOS richiesto (doc 005, passo 2): l'impulso che forma l'OB deve aver
    # ROTTO la struttura, non solo essere ampio. Rilevato dalle SOLE candele
    # M15 del modulo, come lo sweep: nessuna dipendenza da altri engine.
    # Calibrazione su dati reali: definendo il BOS come rottura dell'ULTIMO
    # swing (non del massimo assoluto), passa il 33% degli impulsi su BTC e
    # il 48% su XAU -> riduce i falsi OB di circa due terzi.
    # OSSERVAZIONE (deciso 23/07/2026): has_bos viene SEMPRE registrato ma
    # NON e' ancora un requisito. Test retroattivo su candele reali:
    #   H1 aggregato  CON BOS 65.5% reazione vs 53.8% senza (n=29 vs 39)
    #   MA permutation test p=0.234 -> NON distinguibile dal caso
    #   E XAU inverte segno tra timeframe (M15 +13.6pt, H1 -3.2pt)
    # Attivarlo ora taglierebbe ~75% degli OB sulla base di rumore.
    # Passare a True SOLO dopo il confronto sugli esiti reali dei trade.
    "require_bos": False,            # True = BOS obbligatorio in detection
    "bos_lookback_bars": 20,         # finestra in cui cercare l'ultimo swing
    "bos_swing_k": 2,                # candele per lato che confermano lo swing
    # CALIBRAZIONE (analisi di sensibilita' su 546 candele BTC / 547 XAU M15):
    #   k=1 -> risultato IDENTICO per ogni lookback (8..30): con una sola
    #          candela per lato uno "swing" si trova sempre 2-3 barre indietro,
    #          quindi non si misura una rottura strutturale ma solo "ha chiuso
    #          sopra il massimo recente". Scartato: misura rumore.
    #   k=2 -> si STABILIZZA da lookback 16 in su (BTC 22%, XAU 39%, costante
    #          fino a 30): plateau, quindi parametro robusto.
    #   k=3 -> piu' severo (BTC 22%, XAU 36% a lb=20), stabile da lb=20.
    # Scelti k=2 / lookback=20 perche' dentro il plateau: una piccola
    # variazione del parametro non ribalta il risultato.
    # ATTENZIONE: criterio = ROBUSTEZZA, non performance. Nessuno di questi
    # valori e' stato validato sugli ESITI dei trade — manca il dato.
}


# ============================================================
# Schema
# ============================================================

_CREATE_ORDER_BLOCKS = """
CREATE TABLE IF NOT EXISTS order_blocks (
    ob_id TEXT PRIMARY KEY,
    asset TEXT NOT NULL,
    direction TEXT NOT NULL,
    timeframe TEXT DEFAULT 'M15',
    zone_high REAL NOT NULL,
    zone_low REAL NOT NULL,
    formation_ts DATETIME,
    formation_bar_index INTEGER,
    status TEXT DEFAULT 'FRESH',
    quality_score INTEGER DEFAULT 0,
    has_fvg BOOLEAN DEFAULT 0,
    has_sweep_before BOOLEAN DEFAULT 0,
    has_bos BOOLEAN DEFAULT 0,
    is_last_ob BOOLEAN DEFAULT 0,
    session_quality TEXT,
    displacement_atr REAL DEFAULT 0,
    has_displacement BOOLEAN DEFAULT 0,
    mitigation_count INTEGER DEFAULT 0,
    test_count INTEGER DEFAULT 0,
    first_test_ts DATETIME,
    first_mitigation_ts DATETIME,
    invalidation_ts DATETIME,
    breaker_ts DATETIME,
    breaker_test_count INTEGER DEFAULT 0,
    age_bars INTEGER DEFAULT 0,
    trend_at_formation TEXT DEFAULT 'UNKNOWN',
    in_discount BOOLEAN DEFAULT 0,
    in_premium BOOLEAN DEFAULT 0,
    structure_confidence INTEGER DEFAULT 0,
    volume_at_formation TEXT DEFAULT 'NORMAL',
    last_updated DATETIME
);
"""

_CREATE_OB_SNAPSHOTS = """
CREATE TABLE IF NOT EXISTS order_block_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    asset TEXT NOT NULL,
    timestamp_snapshot DATETIME NOT NULL,
    snapshot_version TEXT DEFAULT '2.0.0',
    fresh_bullish INTEGER DEFAULT 0,
    fresh_bearish INTEGER DEFAULT 0,
    total_tracked INTEGER DEFAULT 0,
    total_active INTEGER DEFAULT 0,
    total_breaker INTEGER DEFAULT 0,
    snapshot_json TEXT
);
"""


def init_ob_schema(conn: sqlite3.Connection):
    """Crea tabelle e migra colonne mancanti."""
    conn.executescript(_CREATE_ORDER_BLOCKS)
    conn.executescript(_CREATE_OB_SNAPSHOTS)

    # Migrazione colonne V2 (se tabella esiste da V1)
    for col, col_type in [
        ("has_bos", "BOOLEAN DEFAULT 0"),
        ("test_count", "INTEGER DEFAULT 0"),
        ("first_test_ts", "DATETIME"),
        ("breaker_ts", "DATETIME"),
        ("breaker_test_count", "INTEGER DEFAULT 0"),
        ("formation_bar_index", "INTEGER"),
        ("structure_confidence", "INTEGER DEFAULT 0"),
        ("volume_at_formation", "TEXT DEFAULT 'NORMAL'"),
        ("last_updated", "DATETIME"),
        ("total_active", "INTEGER DEFAULT 0"),
        ("total_breaker", "INTEGER DEFAULT 0"),
    ]:
        try:
            table = "order_block_snapshots" if col in ("total_active", "total_breaker") else "order_blocks"
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}")
        except sqlite3.OperationalError:
            pass

    conn.commit()


# ============================================================
# Detection: trova nuovi OB nella finestra M15
# ============================================================

def _has_sweep_before_ob(data, ob_idx: int, direction: str, cfg: dict) -> bool:
    """
    C'e' stata una presa di liquidita' poco prima della formazione dell'OB?

    Doc 005: un Order Block di qualita' si forma DOPO una presa di liquidita'.
    Il prezzo buca un estremo di swing recente — dove stanno gli stop — e vi
    rientra; solo allora parte l'impulso. Senza quella presa, la candela
    prima del movimento e' una candela qualsiasi.

    AUTONOMO: usa SOLO le candele M15 che il modulo ha gia' in mano.
    Nessuna lettura da Liquidity Engine, Structure Engine o altri moduli.
    Prima questa informazione arrivava da structure_snapshot["events"], che
    non emette MAI eventi SWEEP/LIQUIDITY (verificato: 0 su 500 snapshot):
    has_sweep_before era quindi sempre False e il +2 della quality non veniva
    mai assegnato — il criterio principale del doc 005 era di fatto spento.

    NON influenza la detection: l'OB viene rilevato comunque, esattamente
    come prima. Alimenta solo _compute_quality.

    Definizione (solo candele):
      - livello di liquidita' = estremo delle candele nella finestra
        precedente, esclusa la coda di sweep_check_bars candele
      - sweep = nella coda, una candela buca quel livello e CHIUDE rientrando
      Per un OB BULLISH si cerca la presa SOTTO (sell-side liquidity),
      per un OB BEARISH quella SOPRA (buy-side liquidity).
    """
    lookback = int(cfg.get("sweep_lookback_bars",
                           DEFAULT_CONFIG["sweep_lookback_bars"]))
    check_bars = int(cfg.get("sweep_check_bars",
                             DEFAULT_CONFIG["sweep_check_bars"]))

    start = max(0, ob_idx - lookback)
    ref_end = ob_idx - check_bars + 1     # fine (esclusa) della finestra di riferimento

    # Serve storia sufficiente per definire un livello E una coda in cui bucarlo
    if ref_end - start < 3:
        return False

    lows = data["low"].values
    highs = data["high"].values
    closes = data["close"].values

    if direction == "BULLISH":
        prior_low = float(lows[start:ref_end].min())
        for k in range(ref_end, ob_idx + 1):
            if float(lows[k]) < prior_low and float(closes[k]) > prior_low:
                return True
    else:
        prior_high = float(highs[start:ref_end].max())
        for k in range(ref_end, ob_idx + 1):
            if float(highs[k]) > prior_high and float(closes[k]) < prior_high:
                return True

    return False


def _impulse_broke_structure(data, impulse_idx: int, direction: str,
                              cfg: dict) -> bool:
    """
    L'impulso ha prodotto un BOS (Break of Structure)?

    Doc 005, passo 2: un Order Block valido non nasce da una candela ampia
    qualsiasi — nasce da un impulso che ROMPE la struttura. Il criterio
    precedente (corpo >= 1 ATR) misura solo l'ampiezza: una candela grande
    dentro un range non rompe nulla e non lascia un OB significativo.

    AUTONOMO: usa SOLO le candele M15 che il modulo ha gia' in mano,
    esattamente come _has_sweep_before_ob. Nessuna lettura da Structure
    Engine o altri moduli — la specializzazione dell'engine resta intatta.
    (Lo structure_snapshot non sarebbe comunque utilizzabile qui: emette
    eventi per lo scan CORRENTE, mentre la detection scorre impulsi passati
    nella finestra storica.)

    Definizione: BOS = la candela di impulso CHIUDE oltre l'ULTIMO swing
    confermato nella direzione del movimento.
      impulso rialzista  -> chiusura sopra l'ultimo swing high
      impulso ribassista -> chiusura sotto l'ultimo swing low

    Si usa l'ULTIMO swing, non il massimo/minimo assoluto della finestra:
    il BOS e' la rottura dell'ultimo punto strutturale. Misurato su dati
    reali: col massimo assoluto passerebbe solo l'11% degli impulsi su BTC
    (troppo severo), con l'ultimo swing il 33% su BTC e il 48% su XAU.

    Nota: il BOS conferma anche la direzione strutturale locale — un impulso
    che rompe al rialzo E' la prova che la struttura sta girando verso l'alto.
    Copre quindi in buona parte il requisito "trend corretto" senza dover
    dipendere dallo Structure Engine.
    """
    lookback = int(cfg.get("bos_lookback_bars",
                           DEFAULT_CONFIG["bos_lookback_bars"]))
    kk = int(cfg.get("bos_swing_k", DEFAULT_CONFIG["bos_swing_k"]))

    start = max(0, impulse_idx - lookback)
    if impulse_idx - start < 2 * kk + 1:
        return False

    highs = data["high"].values
    lows = data["low"].values
    closes = data["close"].values
    impulse_close = float(closes[impulse_idx])

    if direction == "BULLISH":
        for i in range(impulse_idx - kk - 1, start + kk - 1, -1):
            before = highs[i - kk:i]
            after = highs[i + 1:i + kk + 1]
            if len(before) == 0 or len(after) == 0:
                continue
            # swing STRETTO (come find_pivots in indicators.py): con il
            # confronto di uguaglianza, in una zona piatta OGNI barra
            # risultava uno swing e il BOS era quasi sempre vero.
            if float(highs[i]) > float(before.max()) and \
               float(highs[i]) > float(after.max()):
                return impulse_close > float(highs[i])
    else:
        for i in range(impulse_idx - kk - 1, start + kk - 1, -1):
            before = lows[i - kk:i]
            after = lows[i + 1:i + kk + 1]
            if len(before) == 0 or len(after) == 0:
                continue
            if float(lows[i]) < float(before.min()) and \
               float(lows[i]) < float(after.min()):
                return impulse_close < float(lows[i])

    return False


def _detect_order_blocks(df_m15, structure_snapshot: dict,
                          atr_m15: float, config: dict) -> list:
    """
    Scansiona la finestra M15 cercando OB:
    - Un OB BULLISH è l'ultima candela bearish prima di un impulso rialzista
    - Un OB BEARISH è l'ultima candela bullish prima di un impulso ribassista
    - L'impulso deve essere >= min_displacement_atr × ATR

    Ritorna lista di dict con i dati del nuovo OB.
    """
    lookback = config.get("detection_lookback", DEFAULT_CONFIG["detection_lookback"])
    min_disp = config.get("min_displacement_atr", DEFAULT_CONFIG["min_displacement_atr"])
    min_body = config.get("min_body_ratio", DEFAULT_CONFIG["min_body_ratio"])
    require_bos = config.get("require_bos", DEFAULT_CONFIG["require_bos"])

    if len(df_m15) < lookback + 3 or atr_m15 <= 0:
        return []

    candidates = []
    data = df_m15.iloc[-lookback:].reset_index(drop=True)

    for i in range(2, len(data) - 1):
        curr = data.iloc[i]
        prev = data.iloc[i - 1]
        c_open, c_close = float(curr["open"]), float(curr["close"])
        c_high, c_low = float(curr["high"]), float(curr["low"])
        c_range = c_high - c_low

        if c_range <= 0:
            continue

        c_body = abs(c_close - c_open)
        c_body_ratio = c_body / c_range

        # Candela di impulso: body ampio e movimento >= threshold
        is_bullish_impulse = (c_close > c_open and c_body >= min_disp * atr_m15
                              and c_body_ratio >= min_body)
        is_bearish_impulse = (c_close < c_open and c_body >= min_disp * atr_m15
                              and c_body_ratio >= min_body)

        # ── BOS: l'impulso ha rotto la struttura? (doc 005, passo 2) ──
        # Registrato sempre; usato come requisito solo se require_bos=True.
        has_bos = False
        if is_bullish_impulse or is_bearish_impulse:
            has_bos = _impulse_broke_structure(
                data, i, "BULLISH" if is_bullish_impulse else "BEARISH", config)
            if require_bos and not has_bos:
                continue     # impulso ampio ma senza rottura -> non e' un OB

        if is_bullish_impulse:
            # Cerca l'ultima candela bearish PRIMA dell'impulso
            for j in range(i - 1, max(i - 5, 0) - 1, -1):
                ob_candle = data.iloc[j]
                ob_open, ob_close = float(ob_candle["open"]), float(ob_candle["close"])
                if ob_close < ob_open:  # candela bearish
                    candidates.append({
                        "direction": "BULLISH",
                        "zone_high": float(ob_candle["high"]),
                        "zone_low": float(ob_candle["low"]),
                        "formation_bar_index": i - 1,
                        "formation_ts": int(ob_candle.get("timestamp", 0)),
                        "displacement_atr": round(c_body / atr_m15, 2),
                        "impulse_bar_index": i,
                        "has_bos": has_bos,
                        "has_sweep_before": _has_sweep_before_ob(
                            data, j, "BULLISH", config),
                    })
                    break

        elif is_bearish_impulse:
            # Cerca l'ultima candela bullish PRIMA dell'impulso
            for j in range(i - 1, max(i - 5, 0) - 1, -1):
                ob_candle = data.iloc[j]
                ob_open, ob_close = float(ob_candle["open"]), float(ob_candle["close"])
                if ob_close > ob_open:  # candela bullish
                    candidates.append({
                        "direction": "BEARISH",
                        "zone_high": float(ob_candle["high"]),
                        "zone_low": float(ob_candle["low"]),
                        "formation_bar_index": i - 1,
                        "formation_ts": int(ob_candle.get("timestamp", 0)),
                        "displacement_atr": round(c_body / atr_m15, 2),
                        "impulse_bar_index": i,
                        "has_bos": has_bos,
                        "has_sweep_before": _has_sweep_before_ob(
                            data, j, "BEARISH", config),
                    })
                    break

    return candidates


# ============================================================
# Dedup: controlla sovrapposizione con OB esistenti
# ============================================================

def _zone_overlap_pct(h1, l1, h2, l2) -> float:
    """Calcola la percentuale di sovrapposizione tra due zone."""
    overlap = max(0, min(h1, h2) - max(l1, l2))
    size1 = h1 - l1
    size2 = h2 - l2
    if size1 <= 0 or size2 <= 0:
        return 0
    return overlap / min(size1, size2)


def _is_duplicate(candidate: dict, existing_obs: list, threshold: float) -> bool:
    """Controlla se il candidato si sovrappone a un OB esistente."""
    for ob in existing_obs:
        if ob["direction"] != candidate["direction"]:
            continue
        overlap = _zone_overlap_pct(
            candidate["zone_high"], candidate["zone_low"],
            ob["zone_high"], ob["zone_low"],
        )
        if overlap >= threshold:
            return True
    return False


# ============================================================
# Lifecycle: aggiorna stato di tutti gli OB
# ============================================================

def _update_lifecycle(conn: sqlite3.Connection, asset: str,
                       current_high: float, current_low: float,
                       current_close: float, now: datetime,
                       config: dict):
    """
    Per ogni OB attivo dell'asset, aggiorna:
    - age_bars += 1
    - FRESH → TESTED se wick tocca il bordo
    - FRESH/TESTED → MITIGATED se close dentro la zona
    - MITIGATED → INVALIDATED se close oltre la zona
    - INVALIDATED → BREAKER se il prezzo torna e testa dall'altro lato
    """
    close_through = config.get("invalidation_close_through",
                                DEFAULT_CONFIG["invalidation_close_through"])
    breaker_enabled = config.get("breaker_enabled",
                                  DEFAULT_CONFIG["breaker_enabled"])
    max_age = config.get("max_age_bars", DEFAULT_CONFIG["max_age_bars"])
    now_iso = now.isoformat()

    rows = conn.execute("""
        SELECT ob_id, direction, zone_high, zone_low, status,
               mitigation_count, test_count, age_bars,
               breaker_test_count
        FROM order_blocks
        WHERE asset = ? AND status NOT IN ('EXPIRED')
    """, (asset,)).fetchall()

    for row in rows:
        (ob_id, direction, zh, zl, status,
         mit_count, test_count, age, breaker_tests) = row

        new_status = status
        new_mit = mit_count
        new_test = test_count
        new_age = age + 1
        new_breaker_tests = breaker_tests or 0
        updates = {"age_bars": new_age, "last_updated": now_iso}

        # ── Aging: troppo vecchio senza test → EXPIRED ───────
        if new_age > max_age and status in ("FRESH",) and test_count == 0:
            new_status = "EXPIRED"
            updates["status"] = new_status
            _apply_updates(conn, ob_id, updates)
            continue

        # ── FRESH / TESTED lifecycle ─────────────────────────
        if status in ("FRESH", "TESTED"):
            # Check: prezzo ha toccato il bordo? (TESTED)
            if direction == "BULLISH":
                touched = current_low <= zh  # prezzo scende verso la zona
                entered = current_close <= zh and current_close >= zl
                broke_through = current_close < zl if close_through else current_low < zl
            else:  # BEARISH
                touched = current_high >= zl  # prezzo sale verso la zona
                entered = current_close >= zl and current_close <= zh
                broke_through = current_close > zh if close_through else current_high > zh

            if broke_through:
                new_status = "INVALIDATED"
                new_mit += 1
                updates["invalidation_ts"] = now_iso
            elif entered:
                new_status = "MITIGATED"
                new_mit += 1
                if mit_count == 0:
                    updates["first_mitigation_ts"] = now_iso
            elif touched and status == "FRESH":
                new_status = "TESTED"
                new_test += 1
                if test_count == 0:
                    updates["first_test_ts"] = now_iso

        # ── MITIGATED lifecycle ──────────────────────────────
        elif status == "MITIGATED":
            if direction == "BULLISH":
                broke_through = current_close < zl if close_through else current_low < zl
            else:
                broke_through = current_close > zh if close_through else current_high > zh

            if broke_through:
                new_status = "INVALIDATED"
                updates["invalidation_ts"] = now_iso

        # ── INVALIDATED → BREAKER ────────────────────────────
        elif status == "INVALIDATED" and breaker_enabled:
            # Un OB bullish invalidato diventa resistenza (bearish breaker)
            # Un OB bearish invalidato diventa supporto (bullish breaker)
            if direction == "BULLISH":
                # Prezzo torna a testare la zona dal basso (ora è resistenza)
                if current_high >= zl and current_close < zh:
                    new_status = "BREAKER"
                    updates["breaker_ts"] = now_iso
            else:
                # Prezzo torna a testare la zona dall'alto (ora è supporto)
                if current_low <= zh and current_close > zl:
                    new_status = "BREAKER"
                    updates["breaker_ts"] = now_iso

        # ── BREAKER lifecycle ────────────────────────────────
        elif status == "BREAKER":
            if direction == "BULLISH":
                # Breaker bearish: se il prezzo chiude sopra → breaker fallito
                if current_close > zh:
                    new_status = "EXPIRED"
                elif current_high >= zl:
                    new_breaker_tests += 1
            else:
                # Breaker bullish: se il prezzo chiude sotto → breaker fallito
                if current_close < zl:
                    new_status = "EXPIRED"
                elif current_low <= zh:
                    new_breaker_tests += 1

        updates["status"] = new_status
        updates["mitigation_count"] = new_mit
        updates["test_count"] = new_test
        updates["breaker_test_count"] = new_breaker_tests

        _apply_updates(conn, ob_id, updates)

    conn.commit()


def _apply_updates(conn, ob_id, updates):
    """Applica un dizionario di aggiornamenti a un OB."""
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values()) + [ob_id]
    conn.execute(
        f"UPDATE order_blocks SET {set_clause} WHERE ob_id = ?",
        values,
    )


# ============================================================
# Quality Score: 0-10
# ============================================================

def _compute_quality(ob: dict, structure_snapshot: dict,
                      session: str) -> int:
    """
    Quality score 0-10:
      +2  displacement forte (>= 2 ATR)
      +1  displacement presente (>= 1 ATR)
      +1  FVG associato (dalla FVG Engine)
      +2  sweep prima della formazione
      +1  trend H4 allineato
      +1  premium/discount corretto
      +1  sessione qualità (London/NY)
      +1  ultimo OB del movimento (is_last_ob)
    """
    score = 0
    disp_atr = ob.get("displacement_atr", 0)

    if disp_atr >= 2.0:
        score += 2
    elif disp_atr >= 1.0:
        score += 1

    if ob.get("has_fvg"):
        score += 1

    if ob.get("has_sweep_before"):
        score += 2

    # Trend alignment
    if structure_snapshot:
        h4_cls = structure_snapshot.get("structure_h4", {}).get("classification", "NEUTRAL")
        if (ob["direction"] == "BULLISH" and h4_cls == "BULLISH") or \
           (ob["direction"] == "BEARISH" and h4_cls == "BEARISH"):
            score += 1

        # Premium/Discount
        pd_zone = structure_snapshot.get("premium_discount", {}).get("zone", "EQUILIBRIUM")
        if (ob["direction"] == "BULLISH" and pd_zone == "DISCOUNT") or \
           (ob["direction"] == "BEARISH" and pd_zone == "PREMIUM"):
            score += 1
            ob["in_discount"] = ob["direction"] == "BULLISH"
            ob["in_premium"] = ob["direction"] == "BEARISH"

        ob["structure_confidence"] = structure_snapshot.get("structure_confidence", 0)

    # Session quality
    if session in ("LONDON", "NEW_YORK"):
        score += 1

    if ob.get("is_last_ob"):
        score += 1

    return min(score, 10)


# ============================================================
# FVG check: cerca FVG che si sovrappone alla zona OB
# ============================================================

def _check_fvg_overlap(conn, asset: str, zone_high: float, zone_low: float) -> bool:
    """Controlla se esiste una FVG aperta che si sovrappone alla zona OB."""
    try:
        row = conn.execute(
            "SELECT snapshot_json FROM fvg_snapshots "
            "WHERE asset = ? ORDER BY timestamp_snapshot DESC LIMIT 1",
            (asset,)
        ).fetchone()
        if not row or not row[0]:
            return False
        snapshot = json.loads(row[0])
        for fvg in snapshot.get("fvgs", []):
            if fvg.get("status") in ("OPEN", "PARTIALLY_FILLED"):
                overlap = _zone_overlap_pct(
                    zone_high, zone_low,
                    fvg.get("zone_high", 0), fvg.get("zone_low", 0),
                )
                if overlap > 0.1:
                    return True
    except Exception:
        pass
    return False


# ============================================================
# Main entry point
# ============================================================

def produce_ob_snapshot(asset: str, df_m15, structure_snapshot: dict,
                         conn: sqlite3.Connection,
                         session: str = "ASIA",
                         now: datetime = None,
                         config: dict = None) -> dict:
    """
    Punto di ingresso principale. Chiamato da v41p1_runner.py ad ogni scan.

    1. Rileva nuovi OB
    2. Dedup
    3. Inserisci nuovi
    4. Aggiorna lifecycle di tutti
    5. Ricalcola quality
    6. Produci snapshot
    """
    if now is None:
        now = datetime.now(timezone.utc)
    if config is None:
        config = dict(DEFAULT_CONFIG)
    cfg = {**DEFAULT_CONFIG, **config}

    now_iso = now.isoformat()
    atr_m15 = float(df_m15.iloc[-1]["atr"]) if "atr" in df_m15.columns and len(df_m15) > 0 else 0
    current_price = float(df_m15.iloc[-1]["close"]) if len(df_m15) > 0 else 0
    current_high = float(df_m15.iloc[-1]["high"]) if len(df_m15) > 0 else 0
    current_low = float(df_m15.iloc[-1]["low"]) if len(df_m15) > 0 else 0

    # ── Step 1: Rileva candidati ─────────────────────────────
    candidates = _detect_order_blocks(df_m15, structure_snapshot, atr_m15, cfg)

    # ── Step 2: Carica mappa esistente ───────────────────────
    existing = conn.execute(
        "SELECT ob_id, direction, zone_high, zone_low, status "
        "FROM order_blocks WHERE asset = ? AND status NOT IN ('INVALIDATED', 'EXPIRED')",
        (asset,)
    ).fetchall()
    existing_list = [{"ob_id": r[0], "direction": r[1], "zone_high": r[2],
                       "zone_low": r[3], "status": r[4]} for r in existing]

    # ── Step 3: Inserisci nuovi (dedup) ──────────────────────
    overlap_threshold = cfg.get("zone_overlap_threshold",
                                 DEFAULT_CONFIG["zone_overlap_threshold"])
    new_count = 0
    for cand in candidates:
        if _is_duplicate(cand, existing_list, overlap_threshold):
            continue

        ob_id = str(uuid.uuid4())[:8]
        has_fvg = _check_fvg_overlap(conn, asset, cand["zone_high"], cand["zone_low"])

        # Sweep prima della formazione: rilevato da _detect_order_blocks
        # sulle sole candele M15 del modulo (vedi _has_sweep_before_ob).
        # Prima si leggeva structure_snapshot["events"] — dipendenza esterna
        # che non produceva mai eventi SWEEP/LIQUIDITY, quindi sempre False.
        has_sweep = bool(cand.get("has_sweep_before", False))

        # Trend at formation
        trend = "UNKNOWN"
        if structure_snapshot:
            trend = structure_snapshot.get("structure_h4", {}).get(
                "classification", "UNKNOWN")

        ob_data = {
            **cand,
            "ob_id": ob_id,
            "asset": asset,
            "timeframe": "M15",
            "status": "FRESH",
            "has_fvg": has_fvg,
            "has_sweep_before": has_sweep,
            "is_last_ob": True,
            "session_quality": session,
            "has_displacement": cand["displacement_atr"] >= 1.0,
            "trend_at_formation": trend,
            "mitigation_count": 0,
            "test_count": 0,
            "age_bars": 0,
            "in_discount": False,
            "in_premium": False,
            "structure_confidence": structure_snapshot.get(
                "structure_confidence", 0) if structure_snapshot else 0,
            "volume_at_formation": structure_snapshot.get(
                "volume_classification", "NORMAL") if structure_snapshot else "NORMAL",
            "last_updated": now_iso,
        }

        # Quality score
        ob_data["quality_score"] = _compute_quality(ob_data, structure_snapshot, session)

        # Mark previous OBs of same direction as not "last"
        conn.execute(
            "UPDATE order_blocks SET is_last_ob = 0 "
            "WHERE asset = ? AND direction = ? AND is_last_ob = 1",
            (asset, cand["direction"]),
        )

        conn.execute("""
            INSERT OR IGNORE INTO order_blocks (
                ob_id, asset, direction, timeframe,
                zone_high, zone_low, formation_ts, formation_bar_index,
                status, quality_score,
                has_fvg, has_sweep_before, has_bos, is_last_ob, session_quality,
                displacement_atr, has_displacement,
                mitigation_count, test_count, age_bars,
                trend_at_formation, in_discount, in_premium,
                structure_confidence, volume_at_formation, last_updated
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            ob_data["ob_id"], asset, ob_data["direction"], "M15",
            ob_data["zone_high"], ob_data["zone_low"],
            ob_data["formation_ts"], ob_data.get("formation_bar_index"),
            "FRESH", ob_data["quality_score"],
            ob_data["has_fvg"], ob_data["has_sweep_before"],
            bool(ob_data.get("has_bos", False)),
            True, session,
            ob_data["displacement_atr"], ob_data["has_displacement"],
            0, 0, 0,
            ob_data["trend_at_formation"],
            ob_data["in_discount"], ob_data["in_premium"],
            ob_data["structure_confidence"], ob_data["volume_at_formation"],
            now_iso,
        ))

        existing_list.append(ob_data)
        new_count += 1

    # ── Step 4: Aggiorna lifecycle di TUTTI gli OB ───────────
    _update_lifecycle(conn, asset, current_high, current_low,
                       current_price, now, cfg)

    # ── Step 5: Pulizia — limita il numero di OB tracciati ───
    max_tracked = cfg.get("max_tracked", DEFAULT_CONFIG["max_tracked"])
    total = conn.execute(
        "SELECT COUNT(*) FROM order_blocks WHERE asset = ? AND status != 'EXPIRED'",
        (asset,)
    ).fetchone()[0]

    if total > max_tracked:
        # Rimuovi i più vecchi INVALIDATED prima
        excess = total - max_tracked
        conn.execute(f"""
            DELETE FROM order_blocks WHERE ob_id IN (
                SELECT ob_id FROM order_blocks
                WHERE asset = ? AND status = 'INVALIDATED'
                ORDER BY age_bars DESC
                LIMIT ?
            )
        """, (asset, excess))

    conn.commit()

    # ── Step 6: Produci snapshot ─────────────────────────────
    all_obs = conn.execute("""
        SELECT ob_id, direction, zone_high, zone_low, status,
               quality_score, displacement_atr, has_fvg, has_sweep_before,
               has_bos, is_last_ob, session_quality, has_displacement,
               mitigation_count, test_count, age_bars,
               trend_at_formation, in_discount, in_premium,
               structure_confidence, volume_at_formation,
               formation_ts, first_test_ts, first_mitigation_ts,
               breaker_test_count
        FROM order_blocks
        WHERE asset = ? AND status != 'EXPIRED'
        ORDER BY quality_score DESC, age_bars ASC
    """, (asset,)).fetchall()

    ob_list = []
    for r in all_obs:
        zone_mid = (r[2] + r[3]) / 2
        distance_pct = abs(current_price - zone_mid) / current_price if current_price > 0 else 0
        ob_list.append({
            "id": r[0], "direction": r[1],
            "zone_high": r[2], "zone_low": r[3],
            "zone_midpoint": round(zone_mid, 4),
            "status": r[4], "quality_score": r[5],
            "displacement_atr": r[6],
            "has_fvg": bool(r[7]), "has_sweep_before": bool(r[8]),
            "has_bos": bool(r[9]),
            "is_last_ob_of_move": bool(r[10]),
            "session_quality": r[11], "has_displacement": bool(r[12]),
            "mitigation_count": r[13], "test_count": r[14],
            "age_bars": r[15],
            "trend_at_formation": r[16],
            "in_discount": bool(r[17]), "in_premium": bool(r[18]),
            "structure_confidence": r[19],
            "volume_at_formation": r[20],
            "formation_timestamp": str(r[21]),
            "first_test_ts": r[22], "first_mitigation_ts": r[23],
            "breaker_test_count": r[24] or 0,
            "distance_from_price_pct": round(distance_pct, 6),
            "timeframe": "M15",
        })

    # Conteggi
    active_statuses = ("FRESH", "TESTED", "MITIGATED")
    active_obs = [ob for ob in ob_list if ob["status"] in active_statuses]
    breaker_obs = [ob for ob in ob_list if ob["status"] == "BREAKER"]
    fresh_bull = [ob for ob in ob_list if ob["status"] == "FRESH" and ob["direction"] == "BULLISH"]
    fresh_bear = [ob for ob in ob_list if ob["status"] == "FRESH" and ob["direction"] == "BEARISH"]

    # Nearest per direction (solo attivi)
    def _nearest(obs, direction):
        filtered = [ob for ob in obs if ob["direction"] == direction
                     and ob["status"] in active_statuses]
        if not filtered:
            return None
        return min(filtered, key=lambda ob: ob["distance_from_price_pct"])

    snapshot = {
        "asset": asset,
        "timestamp": now_iso,
        "snapshot_version": "2.0.0",
        "order_blocks": ob_list,
        "fresh_bullish_count": len(fresh_bull),
        "fresh_bearish_count": len(fresh_bear),
        "total_tracked": len(ob_list),
        "total_active": len(active_obs),
        "total_breaker": len(breaker_obs),
        "nearest_fresh_bullish": _nearest(ob_list, "BULLISH"),
        "nearest_fresh_bearish": _nearest(ob_list, "BEARISH"),
        "nearest_breaker": min(breaker_obs, key=lambda ob: ob["distance_from_price_pct"]) if breaker_obs else None,
        "new_obs_this_scan": new_count,
    }

    # Salva snapshot
    snapshot_id = str(uuid.uuid4())
    conn.execute("""
        INSERT INTO order_block_snapshots (
            snapshot_id, asset, timestamp_snapshot, snapshot_version,
            fresh_bullish, fresh_bearish, total_tracked,
            total_active, total_breaker, snapshot_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        snapshot_id, asset, now_iso, "2.0.0",
        len(fresh_bull), len(fresh_bear), len(ob_list),
        len(active_obs), len(breaker_obs),
        json.dumps(snapshot, default=str),
    ))
    conn.commit()

    if new_count > 0 or len(active_obs) > 0:
        logger.info(
            "OB Engine V2 [%s]: new=%d active=%d (F=%d T=%d M=%d) "
            "breaker=%d invalidated=%d total=%d",
            asset, new_count,
            len(active_obs),
            sum(1 for ob in active_obs if ob["status"] == "FRESH"),
            sum(1 for ob in active_obs if ob["status"] == "TESTED"),
            sum(1 for ob in active_obs if ob["status"] == "MITIGATED"),
            len(breaker_obs),
            sum(1 for ob in ob_list if ob["status"] == "INVALIDATED"),
            len(ob_list),
        )

    return snapshot
