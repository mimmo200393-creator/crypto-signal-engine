"""
core/decision_ledger/v41p1_integration.py
Sprint 0 — Integrazione Decision Ledger ↔ V41P1

Modulo PONTE. Isola tutta la logica di cattura in un unico posto,
così il runner e il db V41P1 vengono toccati il minimo indispensabile.

Principio: raccolta passiva. Ogni funzione qui:
  - è in try/except totale (non solleva mai)
  - non modifica MAI signal, entry, sl, tp o la decisione
  - se fallisce, il trade prosegue identico

Ponte signal_id ↔ decision_id:
  Il runner genera un decision_id (ULID) PRIMA di decidere.
  Se il segnale viene emesso, lo stesso decision_id viene usato
  come signal_id di V41P1 (chiave condivisa → nessuna colonna extra
  necessaria, ma manteniamo anche il bridge esplicito per sicurezza).
"""

from __future__ import annotations

import logging
from typing import Optional

from core.decision_ledger import decision_collector as dc
from core.decision_ledger import ledger_writer

logger = logging.getLogger("decision_ledger.v41p1")

# Gate di rifiuto "significativi": trigger esisteva ma un filtro ha bloccato.
# Solo questi vengono registrati come REJECTED (i rifiuti banali no).
SIGNIFICANT_REJECT_GATES = {
    "SESSION_OVERLAP",
    "BUY_DOW_NEUTRAL",
    "RISK_TOO_TIGHT",
    "DUPLICATE_SIGNAL",
}


def build_snapshots_dict(structure_snapshot, vol_snapshot, ob_snapshot,
                         fvg_snapshot, liq_snapshot, ss_snapshot,
                         rm_snapshot, cs_snapshot, macro_snapshot,
                         ms_snapshot, mfm) -> dict:
    """
    Assembla il dict di snapshot nel formato che il collector si aspetta.
    Mappa i nomi degli snapshot del runner ai nomi engine del Ledger.
    """
    return {
        "structure":    structure_snapshot,
        "trend_health": structure_snapshot,   # trend_health è dentro structure_snapshot
        "volatility":   vol_snapshot,
        "displacement": structure_snapshot,   # displacement è dentro structure_snapshot
        "order_block":  ob_snapshot,
        "fvg":          fvg_snapshot,
        "liquidity":    liq_snapshot,
        "session_sweep": ss_snapshot,
        "reaction_map": rm_snapshot,
        "candlestick":  cs_snapshot,
        "macro":        macro_snapshot,
        "market_state": ms_snapshot,
        "money_flow":   mfm,
    }


def capture_executed(decision_id: str, asset: str, signal: dict,
                     snapshots: dict,
                     ledger_path: str = ledger_writer.DEFAULT_LEDGER_PATH) -> None:
    """
    Registra una decisione ESEGUITA nel Ledger.
    Chiamata dopo l'emissione del segnale, con lo stesso decision_id
    usato come signal_id.
    """
    try:
        dc.collect_decision(
            decision_id=decision_id,
            asset=asset,
            strategy="V41P1",
            direction=signal.get("direction"),
            decision_type="EXECUTED",
            snapshots=snapshots,
            trade={
                "entry":         signal.get("entry"),
                "stop_loss":     signal.get("stop_loss"),
                "take_profit":   signal.get("take_profit") or signal.get("tp2"),
                "rr":            signal.get("rr"),
                "quality_score": signal.get("quality_score"),
                "quality_label": signal.get("quality_label"),
                "session":       signal.get("session"),
                "trigger_types": signal.get("trigger_types"),
            },
            ledger_path=ledger_path,
        )
    except Exception as e:
        logger.warning("capture_executed fallito (non-blocking): %s", e)


def capture_rejected(decision_id: str, asset: str, direction: Optional[str],
                     reject_gate: str, snapshots: dict,
                     signal: Optional[dict] = None,
                     ledger_path: str = ledger_writer.DEFAULT_LEDGER_PATH) -> None:
    """
    Registra un rifiuto SIGNIFICATIVO nel Ledger.
    Solo i gate in SIGNIFICANT_REJECT_GATES vengono salvati.
    """
    try:
        if reject_gate not in SIGNIFICANT_REJECT_GATES:
            return  # rifiuto banale: non si salva
        trade = None
        if signal:
            trade = {
                "entry":         signal.get("entry"),
                "stop_loss":     signal.get("stop_loss"),
                "take_profit":   signal.get("take_profit") or signal.get("tp2"),
                "rr":            signal.get("rr"),
                "quality_score": signal.get("quality_score"),
                "quality_label": signal.get("quality_label"),
                "session":       signal.get("session"),
                "trigger_types": signal.get("trigger_types"),
            }
        dc.collect_decision(
            decision_id=decision_id,
            asset=asset,
            strategy="V41P1",
            direction=direction,
            decision_type="REJECTED",
            reject_gate=reject_gate,
            snapshots=snapshots,
            trade=trade,
            ledger_path=ledger_path,
        )
    except Exception as e:
        logger.warning("capture_rejected fallito (non-blocking): %s", e)


def link_outcome(decision_id: str, outcome: str, entry: float, stop_loss: float,
                 mae: float = None, mfe: float = None,
                 duration_bars: int = None,
                 ledger_path: str = ledger_writer.DEFAULT_LEDGER_PATH) -> None:
    """
    Collega l'esito di un trade chiuso al Ledger (M4: idempotente).
    Chiamata dal monitor quando un trade V41P1 si chiude.

    Converte l'outcome V41P1 (SL/TP/EXPIRED) nel formato Ledger e
    calcola r_realized/mfe_r/mae_r in unità R.
    """
    try:
        risk = abs(entry - stop_loss) if (entry and stop_loss) else None
        # Mappa outcome V41P1 → Ledger
        ledger_outcome = {
            "SL": "SL", "TP": "TP", "TP2": "TP",
            "EXPIRED": "EXPIRED", "BE": "BE",
        }.get(outcome, "EXPIRED")

        r_realized = None
        mfe_r = None
        mae_r = None
        # BE ha sempre r_realized=0, anche se risk=0 (sl spostato a entry)
        if ledger_outcome == "BE":
            r_realized = 0.0
        if risk and risk > 0:
            if ledger_outcome == "TP":
                r_realized = round((mfe or 0) / risk, 3) if mfe else None
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
        logger.warning("link_outcome fallito (non-blocking): %s", e)
