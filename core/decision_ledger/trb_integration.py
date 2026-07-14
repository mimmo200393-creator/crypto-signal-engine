"""
core/decision_ledger/trb_integration.py
Aggancio di Trend Rider (TRB) al Decision Ledger.

Replica il pattern di v41p1_integration.py, adattato a TRB. Il collector
(decision_collector) e il writer (ledger_writer) sono generici: accettano
`strategy` come parametro. Qui c'e' solo l'adattatore specifico per TRB.

ATTENZIONE: TRB usa outcome col suffisso _HIT (TP2_HIT, SL_HIT), a
differenza di LH/V41P1 che usano "TP"/"SL". Vedi _OUTCOME_MAP in
link_outcome: e' proprio li' che il copy-paste da LH aveva introdotto
un bug che azzerava tutti i TP di TRB nel Ledger.

── MODALITA' SOLO-REGISTRAZIONE ──────────────────────────────────
TRB scrive nel Ledger i voti dei 13 engine MIE per ogni segnale, MA i
dati NON vanno analizzati finche' non ce ne sono abbastanza. Con ~19
fattori (6 confluenze TRB + 13 engine) servono ~150-180 trade chiusi per
un'analisi robusta: sotto quella soglia qualsiasi combinazione "vincente"
e' overfitting. La registrazione parte ora per non perdere dati (come
successe con entry_zone_type del Trend Rider), l'analisi arriva dopo.

── NON-BLOCKING ──────────────────────────────────────────────────
Ogni funzione cattura le eccezioni e logga un warning: se il Ledger
fallisce, TRB continua a funzionare. La registrazione e' passiva e non
deve MAI rompere la generazione dei segnali.
"""

from __future__ import annotations

import logging
from typing import Optional

from core.decision_ledger import decision_collector as dc
from core.decision_ledger import ledger_writer

logger = logging.getLogger("trb_integration")

STRATEGY = "TRB"

# Gate di TRB che vale la pena registrare come REJECTED (per l'analisi futura
# "quali engine erano attivi quando TRB ha rifiutato"). I rifiuti banali
# (dati insufficienti) non si salvano per non gonfiare il Ledger.
SIGNIFICANT_REJECT_GATES = {
    "NO_ENTRY_ZONE",            # prezzo non in nessuna zona (OB/FVG/EMA)
    "ZONE_ALREADY_SIGNALED",    # dedup: zona gia' segnalata
    "TREND_NOT_ALIGNED",        # trend H1 contro la direzione
}


def build_snapshots_dict(structure_snapshot, vol_snapshot, ob_snapshot,
                         fvg_snapshot, liq_snapshot, ss_snapshot,
                         rm_snapshot, cs_snapshot, macro_snapshot,
                         ms_snapshot, mfm) -> dict:
    """
    Assembla il dict di snapshot nel formato che il collector si aspetta.
    Identico a v41p1_integration: i 13 engine mappati ai loro snapshot.
    trend_health e displacement sono dentro structure_snapshot.
    """
    return {
        "structure":     structure_snapshot,
        "trend_health":  structure_snapshot,
        "volatility":    vol_snapshot,
        "displacement":  structure_snapshot,
        "order_block":   ob_snapshot,
        "fvg":           fvg_snapshot,
        "liquidity":     liq_snapshot,
        "session_sweep": ss_snapshot,
        "reaction_map":  rm_snapshot,
        "candlestick":   cs_snapshot,
        "macro":         macro_snapshot,
        "market_state":  ms_snapshot,
        "money_flow":    mfm,
    }


def _trade_dict(signal: dict) -> dict:
    """
    Estrae i campi del trade dal signal TRB per il Ledger.
    Include le CONFLUENZE della teoria SMC (oltre ai campi standard), cosi'
    l'analisi futura potra' combinare engine MIE + confluenze in un colpo.
    """
    return {
        "entry":         signal.get("entry"),
        "stop_loss":     signal.get("stop_loss"),
        "take_profit":   signal.get("tp2") or signal.get("tp1"),
        "rr":            signal.get("rr"),
        "quality_score": signal.get("quality_score"),
        "quality_label": signal.get("quality_label"),
        "session":       signal.get("session"),
        # Confluenze TRB (Entry Zone Finder) — arricchiscono i filtri del Ledger
        "entry_zone_type":       signal.get("entry_zone_type"),
        "zone_ref":              signal.get("zone_ref"),
        "flag_adx_ok":           signal.get("flag_adx_ok"),
        "flag_trigger_present":  signal.get("flag_trigger_present"),
        "flag_volatility_ok":    signal.get("flag_volatility_ok"),
        "flag_sl_widened":       signal.get("flag_sl_widened"),
    }


def capture_executed(decision_id: str, asset: str, signal: dict,
                     snapshots: dict,
                     ledger_path: str = ledger_writer.DEFAULT_LEDGER_PATH) -> None:
    """Registra un segnale TRB ESEGUITO nel Ledger. decision_id = signal_id."""
    try:
        dc.collect_decision(
            decision_id=decision_id,
            asset=asset,
            strategy=STRATEGY,
            direction=signal.get("direction"),
            decision_type="EXECUTED",
            snapshots=snapshots,
            trade=_trade_dict(signal),
            ledger_path=ledger_path,
        )
    except Exception as e:
        logger.warning("TRB capture_executed fallito (non-blocking): %s", e)


def capture_rejected(decision_id: str, asset: str, direction: Optional[str],
                     reject_gate: str, snapshots: dict,
                     signal: Optional[dict] = None,
                     ledger_path: str = ledger_writer.DEFAULT_LEDGER_PATH) -> None:
    """Registra un rifiuto TRB significativo (solo i gate rilevanti)."""
    try:
        if reject_gate not in SIGNIFICANT_REJECT_GATES:
            return
        trade = _trade_dict(signal) if signal else None
        dc.collect_decision(
            decision_id=decision_id,
            asset=asset,
            strategy=STRATEGY,
            direction=direction,
            decision_type="REJECTED",
            reject_gate=reject_gate,
            snapshots=snapshots,
            trade=trade,
            ledger_path=ledger_path,
        )
    except Exception as e:
        logger.warning("TRB capture_rejected fallito (non-blocking): %s", e)


def link_outcome(decision_id: str, outcome: str, entry: float, stop_loss: float,
                 mae: float = None, mfe: float = None,
                 duration_bars: int = None,
                 rr_planned: float = None,
                 ledger_path: str = ledger_writer.DEFAULT_LEDGER_PATH) -> None:
    """
    Collega l'esito di un trade TRB chiuso al Ledger. Idempotente.

    Replica la logica di v41p1_integration.link_outcome (ledger_writer
    espone update_outcome, non link_outcome: la conversione outcome->R
    va fatta qui). Gestisce il caso breakeven (SL spostato a entry,
    risk=0) evitando divisioni per zero.
    """
    try:
        risk = abs(entry - stop_loss) if (entry and stop_loss) else None
        be_moved = (risk is not None and risk < 1e-9)

        # ── MAPPA OUTCOME TRB → LEDGER ────────────────────────────────
        # BUG FIX (verificato su decision_ledger.db, 2026-07-14):
        # questa mappa era stata copiata da lh_integration.py SENZA adattarla.
        # LH usa gli outcome "TP"/"SL"; TRB usa il formato col suffisso
        # "TP2_HIT"/"SL_HIT". Non essendo nel dict, cadevano tutti sul
        # default silenzioso "EXPIRED":
        #     trb_signals (verita'):  TP2_HIT 13 | SL_HIT 20 | EXPIRED 2
        #     decision_ledger:        EXPIRED 14                ← tutto perso
        # Con 0 TP il win rate era 0% in ogni gruppo → edge=0 per tutti i 13
        # engine → TRB spariva dall'Engine Edge Lab.
        # Se aggiungi un nuovo outcome a TRB, aggiungilo QUI.
        _OUTCOME_MAP = {
            # formato TRB (con suffisso _HIT)
            "TP2_HIT": "TP", "TP1_HIT": "TP", "SL_HIT": "SL", "BE_HIT": "BE",
            # formato generico/LH (senza suffisso) — retrocompatibilita'
            "TP": "TP", "TP2": "TP", "TP1": "TP", "SL": "SL", "BE": "BE",
            "EXPIRED": "EXPIRED",
        }
        ledger_outcome = _OUTCOME_MAP.get(outcome)
        if ledger_outcome is None:
            # NON silenziare: un outcome sconosciuto e' un bug, non un EXPIRED.
            # E' il default silenzioso che ha nascosto il problema per settimane.
            logger.warning(
                "TRB link_outcome: outcome sconosciuto '%s' (decision %s) "
                "→ registrato come EXPIRED. Aggiungerlo a _OUTCOME_MAP.",
                outcome, decision_id)
            ledger_outcome = "EXPIRED"

        r_realized = None
        mfe_r = None
        mae_r = None

        if ledger_outcome == "BE":
            r_realized = 0.0
        elif be_moved:
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
        logger.warning("TRB link_outcome fallito (non-blocking): %s", e)
