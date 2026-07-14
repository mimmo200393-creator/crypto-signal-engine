"""
fix_trb_ledger_outcomes.py
Ripara gli outcome storici di TRB nel Decision Ledger.

── IL BUG ────────────────────────────────────────────────────────
trb_integration.link_outcome mappava gli outcome con questo dict:

    {"SL":"SL", "TP":"TP", "TP2":"TP", "EXPIRED":"EXPIRED", "BE":"BE"}
        .get(outcome, "EXPIRED")          ← default silenzioso

Ma TRB usa gli outcome col suffisso _HIT: "TP2_HIT", "SL_HIT".
Non essendo nel dict, cadevano TUTTI sul default "EXPIRED".

Risultato verificato su decision_ledger.db:
    trb_signals (verita'):  TP2_HIT 13 | SL_HIT 20 | EXPIRED 2
    decision_ledger:        EXPIRED 14 | PENDING 1     ← tutto sbagliato

Con 0 TP, il win rate di TRB era 0% in ogni gruppo → edge = 0 per tutti
i 13 engine → TRB spariva dall'Engine Edge Lab (mentre LH, che usa
"TP"/"SL" puliti, funzionava).

── COSA FA QUESTO SCRIPT ─────────────────────────────────────────
Per ogni decisione TRB EXECUTED nel Ledger, ritrova il segnale originale
in trb_signals (match su asset + entry) e riscrive outcome + r_realized
con i valori VERI:
    TP2_HIT / TP1_HIT → TP,  r_realized = rr2 (o rr1)
    SL_HIT            → SL,  r_realized = -1.0
    EXPIRED           → EXPIRED, r_realized = None
    OPEN              → lasciato PENDING (trade ancora aperto)

── SICUREZZA ─────────────────────────────────────────────────────
- DRY-RUN di default: mostra cosa farebbe, non scrive nulla.
- Backup automatico del Ledger prima di ogni scrittura reale.
- Match ambigui (2+ segnali con stesso asset+entry) → SALTATI e segnalati,
  mai indovinati.

USO:
    python3 fix_trb_ledger_outcomes.py            # dry-run (default)
    python3 fix_trb_ledger_outcomes.py --apply    # applica davvero
"""

import argparse
import os
import shutil
import sqlite3
import sys
from datetime import datetime

SIGNALS_DB = os.environ.get("DB_PATH", "data/signals.db")
LEDGER_DB  = os.environ.get("LEDGER_DB_PATH", "data/decision_ledger.db")

# La stessa mappa corretta di trb_integration.py (dopo la fix)
OUTCOME_MAP = {
    "TP2_HIT": "TP", "TP1_HIT": "TP", "SL_HIT": "SL", "BE_HIT": "BE",
    "TP": "TP", "TP2": "TP", "TP1": "TP", "SL": "SL", "BE": "BE",
    "EXPIRED": "EXPIRED",
}


def r_for(ledger_outcome, sig):
    """R realizzato coerente con trb_integration: TP → rr2, SL → -1, BE → 0."""
    if ledger_outcome == "TP":
        rr = sig["rr2"] if sig["rr2"] else None
        return float(rr) if rr else None
    if ledger_outcome == "SL":
        return -1.0
    if ledger_outcome == "BE":
        return 0.0
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="applica davvero (default: dry-run)")
    args = ap.parse_args()

    for p in (SIGNALS_DB, LEDGER_DB):
        if not os.path.exists(p):
            print(f"ERRORE: file non trovato: {p}")
            sys.exit(1)

    sig = sqlite3.connect(SIGNALS_DB); sig.row_factory = sqlite3.Row
    led = sqlite3.connect(LEDGER_DB);  led.row_factory = sqlite3.Row

    rows = led.execute("""
        SELECT decision_id, asset, entry, stop_loss, outcome, r_realized
        FROM decision_ledger
        WHERE strategy='TRB' AND decision_type='EXECUTED'
    """).fetchall()

    print(f"Decisioni TRB EXECUTED nel Ledger: {len(rows)}\n")

    updates, skipped, ambiguous, unchanged = [], [], [], []

    for r in rows:
        # Match su asset + entry + stop_loss.
        # Lo stop_loss serve a DISAMBIGUARE: puo' capitare che due segnali
        # diversi condividano lo stesso entry (stessa zona ritestata) ma con
        # esiti opposti — verificato su XAU entry=4014.2868, un SL_HIT e un
        # TP2_HIT. Lo stop_loss e' calcolato dall'ATR del momento, quindi
        # distingue i due in modo affidabile.
        matches = sig.execute("""
            SELECT final_outcome, rr2, entry FROM trb_signals
            WHERE asset=? AND ABS(entry - ?) < 0.01
              AND ABS(stop_loss - ?) < 0.01
        """, (r["asset"], r["entry"], r["stop_loss"])).fetchall()

        # Fallback: se lo stop_loss non combacia (es. spostato a BE dopo
        # l'apertura), riprova col solo entry.
        if not matches:
            matches = sig.execute("""
                SELECT final_outcome, rr2, entry FROM trb_signals
                WHERE asset=? AND ABS(entry - ?) < 0.01
            """, (r["asset"], r["entry"])).fetchall()

        if not matches:
            skipped.append((r["decision_id"], r["asset"], r["entry"],
                            "nessun segnale corrispondente"))
            continue
        # se piu' segnali hanno lo stesso entry ma esiti DIVERSI → ambiguo
        distinct = {m["final_outcome"] for m in matches}
        if len(distinct) > 1:
            ambiguous.append((r["decision_id"], r["asset"], r["entry"], distinct))
            continue

        s = matches[0]
        real = s["final_outcome"]

        if real == "OPEN":
            continue  # ancora aperto: resta PENDING, corretto

        new_outcome = OUTCOME_MAP.get(real)
        if new_outcome is None:
            skipped.append((r["decision_id"], r["asset"], r["entry"],
                            f"outcome sconosciuto '{real}'"))
            continue

        new_r = r_for(new_outcome, s)

        if new_outcome == r["outcome"] and (new_r == r["r_realized"]):
            unchanged.append(r["decision_id"])
            continue

        updates.append({
            "decision_id": r["decision_id"], "asset": r["asset"],
            "entry": r["entry"], "old": r["outcome"], "new": new_outcome,
            "old_r": r["r_realized"], "new_r": new_r, "real": real,
        })

    # ── Report ──
    print("=" * 66)
    print("CORREZIONI DA APPLICARE")
    print("=" * 66)
    for u in updates:
        print(f"  {u['asset']:<10} entry={u['entry']:<11.2f} "
              f"{u['old']:<8} → {u['new']:<4} (reale: {u['real']:<8}) "
              f"R: {u['old_r']} → {u['new_r']}")

    print(f"\n  Da correggere: {len(updates)}")
    print(f"  Gia' corretti: {len(unchanged)}")
    if ambiguous:
        print(f"  AMBIGUI (saltati, stesso entry con esiti diversi): {len(ambiguous)}")
        for a in ambiguous:
            print(f"     {a[1]} entry={a[2]} → esiti {a[3]}")
    if skipped:
        print(f"  Saltati: {len(skipped)}")
        for s_ in skipped[:5]:
            print(f"     {s_[1]} entry={s_[2]} — {s_[3]}")

    if not updates:
        print("\nNessuna correzione necessaria.")
        return

    # distribuzione risultante
    from collections import Counter
    dist = Counter(u["new"] for u in updates)
    print(f"\n  Distribuzione dopo la fix: {dict(dist)}")
    tp = dist.get("TP", 0); tot = sum(dist.values())
    if tot:
        print(f"  → win rate TRB recuperato: {tp}/{tot} = {tp/tot*100:.1f}%")

    if not args.apply:
        print("\n" + "=" * 66)
        print("DRY-RUN: nessuna modifica scritta.")
        print("Per applicare davvero:  python3 fix_trb_ledger_outcomes.py --apply")
        print("=" * 66)
        return

    # ── Applica (con backup) ──
    backup = f"{LEDGER_DB}.backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    shutil.copy2(LEDGER_DB, backup)
    print(f"\nBackup creato: {backup}")

    for u in updates:
        led.execute("""
            UPDATE decision_ledger SET outcome=?, r_realized=?
            WHERE decision_id=?
        """, (u["new"], u["new_r"], u["decision_id"]))
    led.commit()
    print(f"Applicate {len(updates)} correzioni al Ledger.")
    print("Rigenera le dashboard per vedere TRB nell'Engine Edge Lab.")


if __name__ == "__main__":
    main()
