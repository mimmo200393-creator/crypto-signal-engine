"""
core/decision_ledger/decision_collector.py
Sprint 0 — Decision Collector

Genera il Decision ID, interroga ogni Engine tramite report() ESTERNO
(legge gli snapshot già prodotti, senza toccare il codice degli Engine),
assembla il record e lo passa al Ledger Writer.

Le strategie e gli Engine NON conoscono questo modulo.
È il collettore che li osserva.

Modifiche Design Review integrate:
  M1 — Decision ID = ULID
  M3 — raw_features_json (feature grezze per ML futuro)
  M5 — code_version
  M6 — regime (classificatore minimale che usa engine esistenti)

Filosofia: raccolta passiva. Non modifica MAI la decisione di trading.
Ogni operazione è in try/except: un fallimento del collector non
deve mai impedire un trade.
"""

from __future__ import annotations

import json
import logging
import os
import time
import secrets
from datetime import datetime, timezone
from typing import Optional

from core.decision_ledger import ledger_writer

logger = logging.getLogger("decision_ledger.collector")

# ============================================================
# M1 — ULID (Universally Unique Lexicographically Sortable ID)
# Implementazione minimale senza dipendenze esterne.
# 48 bit timestamp (ms) + 80 bit random → 26 char Crockford base32.
# Univoco in concorrenza, ordinabile temporalmente.
# ============================================================
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def generate_ulid() -> str:
    ts_ms = int(time.time() * 1000)
    # 48 bit timestamp
    ts_part = ""
    for _ in range(10):
        ts_part = _CROCKFORD[ts_ms & 0x1F] + ts_part
        ts_ms >>= 5
    # 80 bit random
    rand = secrets.randbits(80)
    rand_part = ""
    for _ in range(16):
        rand_part = _CROCKFORD[rand & 0x1F] + rand_part
        rand >>= 5
    return ts_part + rand_part


def get_code_version() -> str:
    """M5 — legge il git short hash, con fallback."""
    try:
        head = os.popen("git rev-parse --short HEAD 2>/dev/null").read().strip()
        if head:
            return head
    except Exception:
        pass
    return os.environ.get("MIE_CODE_VERSION", "unknown")


# ============================================================
# M6 — Regime classifier minimale (usa engine ESISTENTI)
# Non è il Market Regime Layer completo: è una classificazione
# a-priori robusta che cattura il regime all'ingresso, così è
# analizzabile a posteriori. TRENDING / RANGING / TRANSITIONAL.
# ============================================================

def classify_regime(structure_snapshot: dict, vol_snapshot: dict) -> str:
    try:
        if not structure_snapshot:
            return "UNKNOWN"
        th = structure_snapshot.get("trend_health", {})
        # campo reale: current_trend (non "phase")
        current_trend = th.get("current_trend", "NEUTRAL")
        impulse_count = th.get("impulse_count", 0)
        h4 = structure_snapshot.get("structure_h4", {}).get("classification", "NEUTRAL")
        vol_regime = (vol_snapshot or {}).get("regime", "NORMAL")
        expanding = (vol_snapshot or {}).get("expanding", False)
        contracting = (vol_snapshot or {}).get("contracting", False)

        # Trending: struttura H4 direzionale + trend attivo con impulsi
        if h4 in ("BULLISH", "BEARISH") and current_trend == h4 and impulse_count >= 1:
            return "TRENDING"
        # Ranging: struttura neutra o volatilità contratta
        if h4 == "NEUTRAL" or contracting or vol_regime == "CONTRACTING":
            return "RANGING"
        # Transitional: tutto il resto (disaccordo trend/struttura, cambio in corso)
        return "TRANSITIONAL"
    except Exception:
        return "UNKNOWN"


# ============================================================
# report() ESTERNO — deriva la tripletta dagli snapshot esistenti
# Nessun file Engine viene toccato. Ogni funzione legge lo snapshot
# che l'Engine ha già prodotto e ne deriva {state, conf, value}.
# state: +1 favorevole / 0 neutro / -1 contrario RISPETTO a direction.
# ============================================================

def _vote_from_classification(cls: str, direction: str) -> int:
    """Helper: BULLISH/BEARISH/NEUTRAL → voto rispetto a direction."""
    if cls == "BULLISH":
        return 1 if direction == "BUY" else -1
    if cls == "BEARISH":
        return 1 if direction == "SELL" else -1
    return 0


def report_structure(snap: dict, direction: str) -> dict:
    if not snap:
        return {"state": 0, "conf": 0, "value": None, "value2": None}
    h4 = snap.get("structure_h4", {}).get("classification", "NEUTRAL")
    conf = snap.get("structure_confidence", 0)
    pd_pos = snap.get("premium_discount", {}).get("position", 0.5)
    return {
        "state": _vote_from_classification(h4, direction),
        "conf": int(conf),
        "value": conf,
        "value2": pd_pos,   # eccezione: premium/discount position
    }


def report_trend_health(snap: dict, direction: str) -> dict:
    if not snap:
        return {"state": 0, "conf": 0, "value": None}
    th = snap.get("trend_health", {})
    trend = th.get("current_trend", "NEUTRAL")
    impulse_count = th.get("impulse_count", 0)
    state = _vote_from_classification(trend, direction)
    # Confidence proporzionale al numero di impulsi confermati
    conf = min(100, impulse_count * 25)
    return {"state": state, "conf": int(conf), "value": impulse_count}


def report_volatility(snap: dict, direction: str) -> dict:
    if not snap:
        return {"state": 0, "conf": 0, "value": None}
    regime = snap.get("regime", "NORMAL")
    atr_ratio = snap.get("atr_ratio_m15", 1.0)
    expanding = snap.get("expanding", False)
    contracting = snap.get("contracting", False)
    # La volatilità non ha direzione: favorevole se espande, ostile se spike/contrae
    state = 0
    if expanding or regime == "EXPANDING":
        state = 1
    elif regime == "SPIKE":
        state = -1
    elif contracting or regime == "CONTRACTING":
        state = -1
    return {"state": state, "conf": 60, "value": atr_ratio}


def report_displacement(snap: dict, direction: str) -> dict:
    if not snap:
        return {"state": 0, "conf": 0, "value": None}
    disp = snap.get("displacement", {})
    confirmed = disp.get("confirmed", False)
    disp_dir = disp.get("direction")
    mag = disp.get("magnitude_atr", 0)
    if not confirmed:
        return {"state": 0, "conf": 0, "value": mag}
    state = _vote_from_classification(
        "BULLISH" if disp_dir == "BULLISH" else "BEARISH" if disp_dir == "BEARISH" else "NEUTRAL",
        direction)
    return {"state": state, "conf": min(100, int(mag * 50)), "value": mag}


def report_order_block(snap: dict, direction: str) -> dict:
    if not snap:
        return {"state": 0, "conf": 0, "value": None}
    fresh = snap.get("fresh_bullish_count", 0) + snap.get("fresh_bearish_count", 0)
    nb = snap.get("nearest_fresh_bullish")
    nbe = snap.get("nearest_fresh_bearish")
    quality = 0
    state = 0
    if direction == "BUY" and nb:
        state = 1
        quality = nb.get("quality_score", 0)
    elif direction == "SELL" and nbe:
        state = 1
        quality = nbe.get("quality_score", 0)
    return {"state": state, "conf": int(quality * 10), "value": fresh}


def report_fvg(snap: dict, direction: str) -> dict:
    if not snap:
        return {"state": 0, "conf": 0, "value": None}
    # Prima votava 1 per l'ESISTENZA di un FVG aperto (conf fissa 40) → quasi
    # sempre 1, non discriminava. Ora usa il gap aperto PIU VICINO nella
    # direzione e ne pesa la qualità: un gap non riempito, non invalidato,
    # formato durante displacement e agganciato a struttura vale di più.
    open_count = snap.get("open_bullish_count", 0) + snap.get("open_bearish_count", 0)
    nearest = (snap.get("nearest_open_bullish") if direction == "BUY"
               else snap.get("nearest_open_bearish"))
    if not nearest or not isinstance(nearest, dict):
        return {"state": 0, "conf": 0, "value": open_count}
    if nearest.get("is_invalidated"):
        return {"state": 0, "conf": 0, "value": open_count}
    fill = nearest.get("fill_percentage", 0) or 0
    unfilled = max(0.0, 1.0 - float(fill))
    conf = 30 * unfilled
    if nearest.get("during_displacement"):
        conf += 25
    if nearest.get("associated_bos") or nearest.get("associated_choch"):
        conf += 15
    return {"state": 1, "conf": int(min(100, conf)), "value": open_count}


def report_liquidity(snap: dict, direction: str) -> dict:
    if not snap:
        return {"state": 0, "conf": 0, "value": None}
    n_levels = snap.get("total_levels", 0)
    # target nella direzione del trade: BUY punta a liquidità sopra, SELL sotto
    if direction == "BUY":
        target = snap.get("nearest_above")
    else:
        target = snap.get("nearest_below")
    has_target = target is not None
    score = 0
    if has_target and isinstance(target, dict):
        score = int(target.get("structural_score", 0) * 100)
    return {"state": 1 if has_target else 0,
            "conf": score if score else (40 if has_target else 0),
            "value": n_levels}


def report_session_sweep(snap: dict, direction: str) -> dict:
    if not snap:
        return {"state": 0, "conf": 0, "value": None}
    la = snap.get("london_action", {})
    true_dir = la.get("true_direction")
    reversed_ = la.get("sweep_reversed", False)
    state = _vote_from_classification(
        "BULLISH" if true_dir == "BULLISH" else "BEARISH" if true_dir == "BEARISH" else "NEUTRAL",
        direction)
    return {"state": state, "conf": 60 if reversed_ else 20,
            "value": 1 if reversed_ else 0}


def report_reaction_map(snap: dict, direction: str) -> dict:
    if not snap:
        return {"state": 0, "conf": 0, "value": None}
    in_zone = snap.get("in_high_confluence_zone", False)
    # zona rilevante per la direzione: BUY reagisce da una zona sotto (supporto),
    # SELL da una zona sopra (resistenza)
    zone = snap.get("strongest_below") if direction == "BUY" else snap.get("strongest_above")
    conf_score = 0
    state = 0
    if zone and isinstance(zone, dict):
        conf_score = zone.get("confluence_score", 0)
        expected = zone.get("expected_reaction")  # valori reali: BOUNCE_UP/BOUNCE_DOWN/UNKNOWN
        # Semantica verificata sui dati (95% coerenza):
        #   zona sotto (supporto) → BOUNCE_UP  = rimbalzo verso l'alto → favorevole BUY
        #   zona sopra (resistenza) → BOUNCE_DOWN = respinta verso il basso → favorevole SELL
        if expected == "BOUNCE_UP" and direction == "BUY":
            state = 1
        elif expected == "BOUNCE_DOWN" and direction == "SELL":
            state = 1
        elif expected in ("BOUNCE_UP", "BOUNCE_DOWN"):
            state = -1  # la zona attende la reazione opposta alla direzione del trade
    return {"state": state, "conf": int(conf_score), "value": conf_score}


def report_candlestick(snap: dict, direction: str) -> dict:
    if not snap:
        return {"state": 0, "conf": 0, "value": None}
    has_conf = snap.get("has_confirmation", False)
    cs_dir = snap.get("strongest_direction")
    state = _vote_from_classification(
        "BULLISH" if cs_dir == "BULLISH" else "BEARISH" if cs_dir == "BEARISH" else "NEUTRAL",
        direction)
    if not has_conf:
        state = 0
    conf_score = snap.get("zone_confluence_score", 0) or 0
    return {"state": state,
            "conf": int(conf_score) if has_conf else 0,
            "value": 1 if has_conf else 0}


def report_macro(snap: dict, direction: str) -> dict:
    # Macro oggi è quasi inerte (feed esterni spesso None), ma raccolto per il futuro.
    if not snap:
        return {"state": 0, "conf": 0, "value": None}
    sentiment = snap.get("news_sentiment", 0) or 0
    is_blackout = snap.get("is_blackout", False)
    # blackout = ostile (evento macro imminente)
    state = -1 if is_blackout else 0
    return {"state": state, "conf": 30 if is_blackout else 0, "value": sentiment}


def report_market_state(snap: dict, direction: str) -> dict:
    if not snap:
        return {"state": 0, "conf": 0, "value": None, "value2": None}
    bias = snap.get("bias", "NEUTRAL")
    bias_conf = snap.get("bias_confidence", 0)
    quality = snap.get("market_quality_score", 0)  # campo reale
    state = _vote_from_classification(bias, direction)
    return {"state": state, "conf": int(bias_conf),
            "value": quality, "value2": bias_conf}  # eccezione: bias_confidence


def report_money_flow(snap: dict, direction: str) -> dict:
    if not snap:
        return {"state": 0, "conf": 0, "value": None}
    # MFM: c'è un target di liquidità nella direzione? La MFM ha quasi sempre
    # un livello sopra E sotto, quindi lo STATE resta poco discriminante finché
    # non avremo dati per una soglia; ma la CONFIDENCE ora riflette la priorità
    # reale del livello (che varia), così il Ledger registra la vera forza del
    # target invece di un valore piatto. Nessuna soglia arbitraria oggi.
    above = snap.get("nearest_above")
    below = snap.get("nearest_below")
    target = above if direction == "BUY" else below
    if target and isinstance(target, dict):
        prio = target.get("priority_score", 0) or 0
        return {"state": 1, "conf": int(prio * 100), "value": prio}
    return {"state": 0, "conf": 0, "value": None}


# Registro degli engine: nome → funzione report
ENGINE_REPORTERS = {
    "structure": report_structure,
    "trend_health": report_trend_health,
    "volatility": report_volatility,
    "displacement": report_displacement,
    "order_block": report_order_block,
    "fvg": report_fvg,
    "liquidity": report_liquidity,
    "session_sweep": report_session_sweep,
    "reaction_map": report_reaction_map,
    "candlestick": report_candlestick,
    "macro": report_macro,
    "market_state": report_market_state,
    "money_flow": report_money_flow,
}


# ============================================================
# COLLECTOR — assembla il record completo
# ============================================================

def collect_decision(
    *,
    decision_id: Optional[str] = None,
    asset: str,
    strategy: str,
    direction: Optional[str],
    decision_type: str,              # EXECUTED / REJECTED
    reject_gate: Optional[str] = None,
    snapshots: dict,                 # {"structure": {...}, "volatility": {...}, ...}
    trade: Optional[dict] = None,    # entry/sl/tp/rr/quality/session/trigger
    ledger_path: str = ledger_writer.DEFAULT_LEDGER_PATH,
) -> Optional[str]:
    """
    Assembla e persiste un Decision Snapshot. Ritorna il decision_id
    (nuovo o passato) oppure None se la raccolta è fallita.

    NON solleva mai eccezioni: la raccolta è passiva e non deve
    poter bloccare il runner.
    """
    try:
        if decision_id is None:
            decision_id = generate_ulid()

        # Direzione può mancare in alcuni rifiuti pre-direzione: default BUY per il voto
        dir_for_vote = direction or "BUY"

        now = datetime.now(timezone.utc)
        record = {
            "decision_id": decision_id,
            "ts_micro": int(time.time() * 1_000_000),
            "ts_iso": now.isoformat(),
            "code_version": get_code_version(),
            "regime": classify_regime(
                snapshots.get("structure"), snapshots.get("volatility")),
            "asset": asset,
            "strategy": strategy,
            "direction": direction,
            "decision_type": decision_type,
            "reject_gate": reject_gate,
            "outcome": "PENDING",
            "created_ts_iso": now.isoformat(),
            "last_checked_ts": int(time.time() * 1_000_000),
        }

        # ── Vettore engine: tripletta per ognuno ──────────────
        for eng_name, reporter in ENGINE_REPORTERS.items():
            snap = snapshots.get(eng_name)
            try:
                r = reporter(snap, dir_for_vote)
            except Exception as e:
                logger.warning("report %s fallito: %s", eng_name, e)
                r = {"state": 0, "conf": 0, "value": None}
            record[f"{eng_name}_state"] = r.get("state", 0)
            record[f"{eng_name}_conf"] = r.get("conf", 0)
            record[f"{eng_name}_value"] = r.get("value")
            if "value2" in r:
                record[f"{eng_name}_value2"] = r.get("value2")

        # M3: raw features per il ML futuro.
        # NON duplica la tripletta (già in colonne). Salva solo i valori
        # grezzi aggiuntivi che un modello ML richiederebbe e che NON sono
        # ricostruibili a posteriori — chiavi compatte per minimizzare spazio.
        # Passato esplicitamente dal runner via snapshots["_raw_ml"] se presente,
        # altrimenti vuoto (raccolta attivabile quando serve, senza refactoring).
        raw_ml = snapshots.get("_raw_ml")
        record["raw_features_json"] = json.dumps(raw_ml, default=str, separators=(",", ":")) if raw_ml else None

        # ── Decisione di trade ────────────────────────────────
        if trade:
            record["entry"] = trade.get("entry")
            record["stop_loss"] = trade.get("stop_loss")
            record["take_profit"] = trade.get("take_profit")
            record["rr_planned"] = trade.get("rr")
            record["quality_score"] = trade.get("quality_score")
            record["quality_label"] = trade.get("quality_label")
            record["session"] = trade.get("session")
            tt = trade.get("trigger_types")
            record["trigger_types"] = json.dumps(tt) if tt is not None else None

        ok = ledger_writer.write_decision(record, ledger_path)
        return decision_id if ok else None

    except Exception as e:
        logger.error("collect_decision fallito (non-blocking): %s", e)
        return None
