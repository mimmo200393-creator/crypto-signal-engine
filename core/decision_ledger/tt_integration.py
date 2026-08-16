"""
core/decision_ledger/tt_integration.py
Aggancio di TT (Direction/Location/Liquidity) al Decision Ledger.

Replica il pattern di lh_integration.py -- stesso collector generico
(decision_collector) e writer (ledger_writer), qui solo l'adattatore
specifico per TT.

Differenza rispetto a LH: TT non dipende dai 13 engine MIE (isolamento
esplicito da TRB/LH, vedi strategies/tt/). I suoi "snapshot" sono i 5
componenti che evaluate_setup() gia' produce dentro
signal["context_snapshot"] (direction/poi/liquidity/premium_discount/
context_15m) -- niente da riassemblare, si riusa direttamente.

── QUANDO SI REGISTRA ────────────────────────────────────────────
capture_executed() viene chiamata SOLO quando l'ENTRY e' confermata
(5M sweep+reaction+[structure]), non alla creazione dell'Early Signal
(WAITING_CONFIRMATION) -- stesso principio di LH ("ordine pendente non
ancora nel Ledger, catturato al riempimento"). decision_id = signal_id
di TT, cosi' link_outcome puo' richiuderlo quando il trade si chiude.

── NON-BLOCKING ──────────────────────────────────────────────────
Ogni funzione cattura le eccezioni e logga un warning: se il Ledger
fallisce, TT continua a funzionare. Stesso principio di lh_integration.

── NOTA ──────────────────────────────────────────────────────────
Non ho verificato l'implementazione interna di decision_collector.py
(non l'ho vista) -- questo file replica solo l'INTERFACCIA gia'
confermata funzionante da lh_integration.py. Essendo tutto avvolto in
try/except non-blocking, un eventuale disallineamento si manifesta
come un warning nei log, mai come un errore che blocca TT.
"""

from __future__ import annotations

import logging
from typing import Optional

from core.decision_ledger import decision_collector as dc
from core.decision_ledger import ledger_writer

logger = logging.getLogger("tt_integration")

STRATEGY = "TT"

# Rifiuti di TT che vale la pena registrare per l'analisi futura ("il
# setup era quasi valido, cosa lo ha fermato?") -- non i rifiuti banali
# (dati insufficienti, nessuna direzione chiara), che gonfierebbero il
# Ledger senza aggiungere informazione utile.
SIGNIFICANT_REJECT_GATES = {
    "RR_INSUFFICIENT",              # POI+liquidity trovati, ma il reward non bastava
    "NO_DYNAMIC_TARGET_AVAILABLE",  # nessun target raggiungibile con RR sufficiente
    "NO_LIQUIDITY_TARGET",          # POI valida ma nessuna liquidity collegata
}


def build_snapshots_dict(context_snapshot: dict) -> dict:
    """
    A differenza di LH (13 engine MIE esterni da assemblare), TT ha gia'
    tutto dentro context_snapshot (prodotto da evaluate_setup() in
    strategies/tt/liquidity_engine.py) -- direction/poi/liquidity/
    premium_discount/context_15m. Nessun riassemblaggio necessario.
    """
    context_snapshot = context_snapshot or {}
    return {
        "direction_4h":     context_snapshot.get("direction"),
        "poi":              context_snapshot.get("poi"),
        "liquidity":        context_snapshot.get("liquidity"),
        "premium_discount": context_snapshot.get("premium_discount"),
        "context_15m":      context_snapshot.get("context_15m"),
    }


def _trade_dict(signal: dict) -> dict:
    """
    Estrae i campi del trade dal signal TT per il Ledger. Include i
    campi specifici di TT (poi_type, pd_zone, quality) oltre a quelli
    standard, cosi' l'analisi futura puo' incrociare i due.
    """
    return {
        "entry":         signal.get("planned_entry"),
        "stop_loss":      signal.get("planned_sl"),
        "take_profit":    signal.get("planned_tp"),
        "rr":             signal.get("planned_rr"),
        "quality_score":  signal.get("quality_score"),
        "quality_label":  signal.get("quality_label"),
        # Campi specifici TT
        "poi_type":         signal.get("poi_type"),
        "poi_quality":      signal.get("poi_quality"),
        "pd_zone":          signal.get("pd_zone"),
        "pd_pct":           signal.get("pd_pct"),
        "ctx_15m_structure": signal.get("ctx_15m_structure"),
        "ctx_15m_momentum":  signal.get("ctx_15m_momentum"),
        "proximity_points":  signal.get("proximity_points"),
        "setup_type":        signal.get("setup_type"),
        "planned_tp_type":   signal.get("planned_tp_type"),
    }


def capture_executed(decision_id: str, asset: str, signal: dict,
                     ledger_path: str = ledger_writer.DEFAULT_LEDGER_PATH) -> None:
    """
    Registra un segnale TT ESEGUITO (ENTRY confermata) nel Ledger.
    decision_id = signal_id di TT. snapshots costruiti direttamente da
    signal["context_snapshot"] -- nessun parametro esterno da passare,
    a differenza di LH.
    """
    try:
        snapshots = build_snapshots_dict(signal.get("context_snapshot"))
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
        logger.warning("TT capture_executed fallito (non-blocking): %s", e)


def capture_rejected(decision_id: str, asset: str, direction: Optional[str],
                     reject_gate: str, signal: Optional[dict] = None,
                     ledger_path: str = ledger_writer.DEFAULT_LEDGER_PATH) -> None:
    """Registra un rifiuto TT significativo (solo i gate rilevanti)."""
    try:
        if reject_gate not in SIGNIFICANT_REJECT_GATES:
            return
        snapshots = build_snapshots_dict(signal.get("context_snapshot") if signal else None)
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
        logger.warning("TT capture_rejected fallito (non-blocking): %s", e)


def link_outcome(decision_id: str, outcome: str, entry: float, stop_loss: float,
                 mae: float = None, mfe: float = None,
                 duration_bars: int = None,
                 rr_planned: float = None,
                 ledger_path: str = ledger_writer.DEFAULT_LEDGER_PATH) -> None:
    """
    Collega l'esito di un trade TT chiuso al Ledger. Idempotente.
    Stessa logica di lh_integration.link_outcome (replica esatta,
    incluso il caso breakeven) -- TT non ha ancora un concetto di
    breakeven attivo (nessun meccanismo di spostamento SL a entry come
    LH), ma la protezione risk=0 resta utile come guardia difensiva.
    """
    try:
        risk = abs(entry - stop_loss) if (entry and stop_loss) else None
        be_moved = (risk is not None and risk < 1e-9)

        ledger_outcome = {
            "SL": "SL", "TP": "TP",
            "EXPIRED": "EXPIRED", "BE": "BE",
        }.get(outcome, "EXPIRED")

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
        logger.warning("TT link_outcome fallito (non-blocking): %s", e)
