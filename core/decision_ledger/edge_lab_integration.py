"""
core/decision_ledger/edge_lab_integration.py
Sprint 0 — Integrazione Decision Ledger ↔ Edge Lab (OTE-SC)

Modulo PONTE, sul modello di v41p1_integration.py.

ARCHITETTURA (decisa a mente fredda, "Strada 1.5"):
  - Livello 1 — Common Context (contratto ufficiale, uniforme per TUTTE
    le strategie): i 13 trittici engine MIE, popolati dallo STESSO
    mie_context che Edge Lab già legge dagli snapshot MIE. Questo mantiene
    il Ledger confrontabile tra V41P1 / OTE-SC / TRB / LH.
  - Livello 2 — Strategy Payload (buffer temporaneo): i campi SPECIFICI di
    OTE-SC (fibonacci, OTE zone, sr_score, ...) salvati come JSON grezzo e
    strutturato dentro raw_features_json. NON è la destinazione definitiva:
    è un contenitore di raccolta finché, con dati sufficienti, non si
    deciderà se meritano una colonna dedicata. Nessun ALTER TABLE oggi.

Principio invariato: raccolta passiva. Ogni funzione qui:
  - è in try/except totale (non solleva mai)
  - non modifica MAI signal, entry, sl, tp o la decisione
  - se fallisce, il trade prosegue identico

NOTE TECNICHE:
  Il mie_context prodotto dal runner Edge Lab è APPIATTITO:
      { "mie_structure_<campo>": val, "mie_structure_available": True, ... }
  Il collector invece si aspetta snapshot ANNIDATI per engine:
      { "structure": {<campo>: val, ...}, "volatility": {...}, ... }
  Quindi qui ricostruiamo gli snapshot annidati dal dict piatto PRIMA di
  passarli al collector, così riusiamo gli STESSI report_* di V41P1 senza
  duplicare logica. Questo è ciò che mantiene il Common Context davvero
  uniforme: stessi engine, stessi reporter, stesso significato dei voti.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from core.decision_ledger import decision_collector as dc
from core.decision_ledger import ledger_writer

logger = logging.getLogger("decision_ledger.edge_lab")

STRATEGY_NAME = "OTE-SC"

# Prefissi MIE nel mie_context appiattito (identici a quelli del runner).
_MIE_PREFIXES = [
    "structure", "volatility", "order_block", "fvg", "liquidity",
    "session_sweep", "reaction_map", "candlestick", "macro", "market_state",
]

# Gate di rifiuto "significativi" per OTE-SC: il setup esisteva ma un filtro
# l'ha bloccato. Solo questi vengono registrati come REJECTED (non i banali).
# Ricavati dai log/runner reali di Edge Lab.
SIGNIFICANT_REJECT_GATES = {
    "SESSION_OVERLAP",
    "RISK_TOO_TIGHT",
    "TRADEABILITY_FLAGS",
    "RECENT_DUPLICATE",
    "QUALITY_TOO_LOW",
    "TREND_NOT_ALIGNED",
    "NO_LIQUIDITY_TARGET",
}


# ============================================================
# Common Context — ricostruzione snapshot annidati dal mie_context piatto
# ============================================================

def _rebuild_nested_snapshots(mie_context: dict) -> dict:
    """
    Dal mie_context APPIATTITO ({mie_<prefix>_<campo>: val}) ricostruisce
    il dict di snapshot ANNIDATI ({<engine>: {<campo>: val}}) nel formato
    che il collector si aspetta.

    Rispetta lo stesso mapping "un engine può derivare da un altro snapshot"
    di V41P1: trend_health e displacement vivono dentro structure.
    money_flow non è tra gli snapshot MIE di Edge Lab (V41P1 lo passa a
    parte come mfm) → resta None qui, il report gestisce None ritornando 0.
    """
    if not mie_context:
        return {}

    nested: dict = {p: {} for p in _MIE_PREFIXES}

    for key, value in mie_context.items():
        if not key.startswith("mie_"):
            continue
        if key.endswith("_available"):
            continue
        # key = "mie_<prefix>_<campo>"; prefix può contenere '_' (session_sweep)
        body = key[len("mie_"):]
        matched = None
        for p in _MIE_PREFIXES:
            if body == p or body.startswith(p + "_"):
                matched = p
                break
        if matched is None:
            continue
        field = body[len(matched):].lstrip("_")
        if field:
            nested[matched][field] = value

    # Snapshot vuoto → None, così il report_* ritorna neutro invece di 0 forzato
    def _or_none(d):
        return d if d else None

    structure_snap = _or_none(nested["structure"])

    return {
        "structure":     structure_snap,
        "trend_health":  structure_snap,          # come V41P1: dentro structure
        "volatility":    _or_none(nested["volatility"]),
        "displacement":  structure_snap,          # come V41P1: dentro structure
        "order_block":   _or_none(nested["order_block"]),
        "fvg":           _or_none(nested["fvg"]),
        "liquidity":     _or_none(nested["liquidity"]),
        "session_sweep": _or_none(nested["session_sweep"]),
        "reaction_map":  _or_none(nested["reaction_map"]),
        "candlestick":   _or_none(nested["candlestick"]),
        "macro":         _or_none(nested["macro"]),
        "market_state":  _or_none(nested["market_state"]),
        "money_flow":    None,   # non presente negli snapshot MIE di Edge Lab
    }


# ============================================================
# Strategy Payload — campi specifici OTE-SC (buffer in raw_features_json)
# ============================================================

def _build_strategy_payload(signal: dict, market_ctx: dict) -> dict:
    """
    Estrae i campi SPECIFICI di OTE-SC in un payload grezzo e strutturato.
    Valori reali, NON interpretazioni (così l'analisi futura è libera).
    Tutto opzionale: se un campo manca, semplicemente non c'è.
    """
    payload = {"_schema": "ote_sc.v1", "_strategy": STRATEGY_NAME}

    # Fibonacci / OTE zone
    for k in ("ote_low", "ote_high", "ote_source", "fib_618", "fib_786"):
        if signal.get(k) is not None:
            payload[k] = signal.get(k)

    # S/R (dal market_ctx, direzione-specifico)
    direction = signal.get("direction")
    sr = (market_ctx or {}).get("sr_scores", {})
    if direction == "BUY":
        payload["sr_score"] = sr.get("score_buy")
        payload["sr_reaction"] = sr.get("reaction_buy")
    elif direction == "SELL":
        payload["sr_score"] = sr.get("score_sell")
        payload["sr_reaction"] = sr.get("reaction_sell")

    # Trend combinato e volatilità di contesto
    trend = (market_ctx or {}).get("trend", {})
    if trend:
        payload["trend_combined"] = trend.get("combined")
        payload["trend_h4"] = (trend.get("h4") or {}).get("direction")
        payload["trend_h1"] = (trend.get("h1") or {}).get("direction")

    # Liquidity target (utile per l'analisi TP su liquidità)
    for k in ("liquidity_target", "liquidity_target_price",
              "liquidity_target_priority", "liquidity_target_score"):
        if signal.get(k) is not None:
            payload[k] = signal.get(k)

    # Riferimenti sessione
    for k in ("session", "ref_session", "vol_regime_m15"):
        if signal.get(k) is not None:
            payload[k] = signal.get(k)

    return payload


def _attach_payload(snapshots: dict, signal: dict, market_ctx: dict) -> None:
    """
    Inserisce il payload nel canale _raw_ml, che il collector serializza in
    raw_features_json. USO COME BUFFER TEMPORANEO (vedi docstring modulo).
    """
    try:
        snapshots["_raw_ml"] = _build_strategy_payload(signal, market_ctx)
    except Exception as e:
        logger.debug("payload OTE-SC non costruito: %s", e)


# ============================================================
# Cattura decisioni
# ============================================================

def _trade_from_signal(signal: dict) -> dict:
    return {
        "entry":         signal.get("entry"),
        "stop_loss":     signal.get("stop_loss"),
        "take_profit":   signal.get("tp"),
        "rr":            signal.get("rr"),
        "quality_score": signal.get("quality_score"),
        "quality_label": signal.get("quality_label"),
        "session":       signal.get("session"),
        "trigger_types": signal.get("ote_source"),  # OTE non ha BOS/CHOCH: usa ote_source
    }


def capture_executed(decision_id: str, asset: str, signal: dict,
                     mie_context: dict, market_ctx: dict,
                     ledger_path: str = ledger_writer.DEFAULT_LEDGER_PATH) -> None:
    """Registra una decisione OTE-SC ESEGUITA (Common Context + payload)."""
    try:
        snapshots = _rebuild_nested_snapshots(mie_context)
        _attach_payload(snapshots, signal, market_ctx)
        dc.collect_decision(
            decision_id=decision_id,
            asset=asset,
            strategy=STRATEGY_NAME,
            direction=signal.get("direction"),
            decision_type="EXECUTED",
            snapshots=snapshots,
            trade=_trade_from_signal(signal),
            ledger_path=ledger_path,
        )
    except Exception as e:
        logger.warning("capture_executed OTE-SC fallito (non-blocking): %s", e)


def capture_rejected(decision_id: str, asset: str, direction: Optional[str],
                     reject_gate: str, mie_context: dict,
                     market_ctx: Optional[dict] = None,
                     signal: Optional[dict] = None,
                     ledger_path: str = ledger_writer.DEFAULT_LEDGER_PATH) -> None:
    """Registra un rifiuto SIGNIFICATIVO OTE-SC."""
    try:
        if reject_gate not in SIGNIFICANT_REJECT_GATES:
            return
        snapshots = _rebuild_nested_snapshots(mie_context)
        if signal:
            _attach_payload(snapshots, signal, market_ctx or {})
        dc.collect_decision(
            decision_id=decision_id,
            asset=asset,
            strategy=STRATEGY_NAME,
            direction=direction,
            decision_type="REJECTED",
            reject_gate=reject_gate,
            snapshots=snapshots,
            trade=_trade_from_signal(signal) if signal else None,
            ledger_path=ledger_path,
        )
    except Exception as e:
        logger.warning("capture_rejected OTE-SC fallito (non-blocking): %s", e)


def link_outcome(decision_id: str, outcome: str, entry: float, stop_loss: float,
                 mae: float = None, mfe: float = None,
                 duration_bars: int = None, rr_planned: float = None,
                 ledger_path: str = ledger_writer.DEFAULT_LEDGER_PATH) -> None:
    """
    Collega l'esito di un trade OTE-SC chiuso al Ledger.
    OTE-SC usa outcome 'TP'/'SL'/'EXPIRED' (schema edge_lab_signals),
    già compatibili con la normalizzazione del writer. Delega a update_outcome,
    che ricostruisce R dai valori ORIGINALI salvati nel record (immune al
    breakeven), esattamente come per V41P1.
    """
    try:
        risk = abs(entry - stop_loss) if (entry and stop_loss) else None
        ledger_outcome = {"SL": "SL", "TP": "TP", "EXPIRED": "EXPIRED",
                          "BE": "BE"}.get(outcome, "EXPIRED")

        r_realized = None
        mfe_r = mae_r = None
        if ledger_outcome == "BE":
            r_realized = 0.0
        elif risk and risk > 0:
            if ledger_outcome == "TP":
                r_realized = rr_planned if rr_planned else (
                    round((mfe or 0) / risk, 3) if mfe else None)
            elif ledger_outcome == "SL":
                r_realized = -1.0
            mfe_r = round((mfe or 0) / risk, 3) if mfe is not None else None
            mae_r = round((mae or 0) / risk, 3) if mae is not None else None

        ledger_writer.update_outcome(
            decision_id=decision_id,
            outcome=ledger_outcome,
            r_realized=r_realized,
            mfe_r=mfe_r,
            mae_r=mae_r,
            duration_bars=duration_bars,
            ledger_path=ledger_path,
        )
    except Exception as e:
        logger.warning("link_outcome OTE-SC fallito (non-blocking): %s", e)
