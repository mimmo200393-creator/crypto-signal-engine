"""
core/decision_ledger/ote_integration.py
Aggancio di OTE Fase A al Decision Ledger.

Stesso pattern di lh_integration.py/tt_integration.py. La differenza:
OTE registra sia i CANDIDATE (neutri, senza direzione — decision_type
CANDIDATE_OBSERVED) sia i SIGNAL (direzionali — decision_type EXECUTED).
Questo permette all'Engine Edge Lab di analizzare anche le situazioni
che non sono diventate trade, per scoprire dove sta l'edge.

IMPORTANTE: le chiavi di `snapshots` passate a collect_decision devono
corrispondere ESATTAMENTE ai nomi in ENGINE_REPORTERS (decision_collector.py):
structure, trend_health, volatility, displacement, order_block, fvg,
liquidity, session_sweep, reaction_map, candlestick, macro, market_state,
money_flow. I dati PROPRI di OTE (la sua zona, la sua liquidity map
neutra) NON vanno in questo dict — userebbero lo stesso nome delle chiavi
riservate e verrebbero letti dal reporter sbagliato producendo un
falso "neutro" silenzioso invece del vero stato dell'engine MIE
(bug trovato e corretto il 22/08).
"""

from __future__ import annotations

import logging
from typing import Optional

from core.decision_ledger import decision_collector as dc
from core.decision_ledger import ledger_writer

logger = logging.getLogger("ote_integration")

STRATEGY = "OTE"


def capture_candidate(candidate_id: str, asset: str,
                      mie_snapshots: dict = None,
                      ledger_path: str = ledger_writer.DEFAULT_LEDGER_PATH) -> None:
    """
    Registra un candidate NEUTRO nel Ledger (nessuna direzione).

    mie_snapshots: dict con le chiavi standard degli engine MIE (es.
    {"structure": {...}, "reaction_map": {...}}), letti dal runner dalle
    tabelle *_snapshots. Passare solo quello che si ha -- il resto
    viene registrato come "dati insufficienti", nessun crash.
    """
    try:
        dc.collect_decision(
            decision_id=candidate_id,
            asset=asset,
            strategy=STRATEGY,
            direction=None,
            decision_type="CANDIDATE_OBSERVED",
            snapshots=mie_snapshots or {},
            trade=None,
            ledger_path=ledger_path,
        )
    except Exception as e:
        logger.warning("OTE capture_candidate fallito (non-blocking): %s", e)


def capture_executed(signal_id: str, asset: str, direction: str,
                     signal_data: dict, mie_snapshots: dict = None,
                     ledger_path: str = ledger_writer.DEFAULT_LEDGER_PATH) -> None:
    """Registra un segnale OTE ESEGUITO (direzione confermata dal mercato)."""
    try:
        trade = {
            "entry": signal_data.get("planned_entry"),
            "stop_loss": signal_data.get("planned_sl"),
            "take_profit": signal_data.get("planned_tp"),
            "rr": signal_data.get("planned_rr"),
            "quality_score": signal_data.get("quality_score"),
            "quality_label": signal_data.get("quality_label"),
            "trigger_types": [signal_data.get("trigger_type")] if signal_data.get("trigger_type") else None,
        }
        dc.collect_decision(
            decision_id=signal_id,
            asset=asset,
            strategy=STRATEGY,
            direction=direction,
            decision_type="EXECUTED",
            snapshots=mie_snapshots or {},
            trade=trade,
            ledger_path=ledger_path,
        )
    except Exception as e:
        logger.warning("OTE capture_executed fallito (non-blocking): %s", e)


def link_outcome(decision_id: str, outcome: str, entry: float, stop_loss: float,
                 mae: float = None, mfe: float = None,
                 duration_bars: int = None, rr_planned: float = None,
                 ledger_path: str = ledger_writer.DEFAULT_LEDGER_PATH) -> None:
    """Collega l'esito di un trade OTE chiuso al Ledger."""
    try:
        risk = abs(entry - stop_loss) if (entry and stop_loss) else None
        be_moved = (risk is not None and risk < 1e-9)

        ledger_outcome = {"SL": "SL", "TP": "TP", "EXPIRED": "EXPIRED"}.get(outcome, "EXPIRED")

        r_realized = None
        mfe_r = None
        mae_r = None

        if be_moved:
            if ledger_outcome == "TP":
                r_realized = rr_planned if rr_planned else None
            elif ledger_outcome == "SL":
                r_realized = 0.0
                ledger_outcome = "BE"
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
        logger.warning("OTE link_outcome fallito (non-blocking): %s", e)
