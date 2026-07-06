-- ============================================================
-- storage/decision_ledger_schema.sql
-- Sprint 0 — Decision Ledger
-- Infrastruttura di raccolta dati per l'apprendimento statistico.
--
-- Filosofia: "Collect first, calibrate later".
-- Raccoglie in modo PASSIVO. Non modifica nessuna logica decisionale.
--
-- Modifiche dalla Design Review (v2) tutte integrate:
--   M1 — Decision ID = ULID (univoco in concorrenza, ordinabile)
--   M2 — File separato + WAL (impostato dal writer, non qui)
--   M3 — raw_features_json per il ML futuro (non ricostruibile dopo)
--   M4 — last_checked_ts + stato terminale idempotente
--   M5 — code_version per decisione
--   M6 — regime catturato all'ingresso
--   M7 — budget/archiviazione (gestito da policy esterna)
--
-- IMPORTANTE: questo schema vive in un FILE SEPARATO (decision_ledger.db),
-- non in signals.db, per isolare la concorrenza e le corruzioni.
-- ============================================================

-- ============================================================
-- TABELLA PRINCIPALE
-- ============================================================
CREATE TABLE IF NOT EXISTS decision_ledger (

    -- ─── SEZIONE A: IDENTITÀ ───────────────────────────────
    decision_id       TEXT PRIMARY KEY,        -- M1: ULID (26 char, ordinabile)
    ts_micro          INTEGER NOT NULL,        -- timestamp Unix in microsecondi (point-in-time)
    ts_iso            TEXT NOT NULL,            -- ISO leggibile (ridondante ma comodo per debug)

    code_version      TEXT,                    -- M5: git short hash / numero Sprint
    regime            TEXT,                    -- M6: TRENDING/RANGING/TRANSITIONAL/UNKNOWN

    asset             TEXT NOT NULL,           -- BTC_USDT / PAXG_USDT
    strategy          TEXT NOT NULL,           -- V41P1 / EDGE_LAB / TRB / LH
    direction         TEXT CHECK(direction IN ('BUY','SELL')),

    decision_type     TEXT NOT NULL            -- EXECUTED / REJECTED
                      CHECK(decision_type IN ('EXECUTED','REJECTED')),
    reject_gate       TEXT,                    -- NULL se eseguito; nome del gate se rifiutato

    -- ─── SEZIONE B: VETTORE ENGINE (tripletta per engine) ──
    -- state: -1 contrario, 0 neutro, +1 favorevole (rispetto a direction)
    -- conf:  0..100
    -- value: metrica nativa dell'engine (REAL, non scalata: lo spazio non è critico)
    structure_state       INTEGER, structure_conf INTEGER, structure_value REAL,
    structure_value2      REAL,     -- eccezione: premium/discount position
    trend_health_state    INTEGER, trend_health_conf INTEGER, trend_health_value REAL,
    volatility_state      INTEGER, volatility_conf INTEGER, volatility_value REAL,
    displacement_state    INTEGER, displacement_conf INTEGER, displacement_value REAL,
    order_block_state     INTEGER, order_block_conf INTEGER, order_block_value REAL,
    fvg_state             INTEGER, fvg_conf INTEGER, fvg_value REAL,
    liquidity_state       INTEGER, liquidity_conf INTEGER, liquidity_value REAL,
    session_sweep_state   INTEGER, session_sweep_conf INTEGER, session_sweep_value REAL,
    reaction_map_state    INTEGER, reaction_map_conf INTEGER, reaction_map_value REAL,
    candlestick_state     INTEGER, candlestick_conf INTEGER, candlestick_value REAL,
    macro_state           INTEGER, macro_conf INTEGER, macro_value REAL,
    market_state_state    INTEGER, market_state_conf INTEGER, market_state_value REAL,
    market_state_value2   REAL,     -- eccezione: bias_confidence
    money_flow_state      INTEGER, money_flow_conf INTEGER, money_flow_value REAL,

    -- M3: feature grezze per il ML futuro (blob JSON compatto).
    -- NON ricostruibile a posteriori → raccolto ORA anche se non usato subito.
    raw_features_json     TEXT,

    -- ─── SEZIONE C: DECISIONE DI TRADE ─────────────────────
    entry             REAL,
    stop_loss         REAL,
    take_profit       REAL,
    rr_planned        REAL,
    quality_score     INTEGER,                 -- score della strategia (per confronto col nuovo)
    quality_label     TEXT,
    session           TEXT,                    -- LONDON/NY/ASIA/OVERLAP
    trigger_types     TEXT,                    -- JSON list

    -- ─── SEZIONE D: ESITO (popolato alla chiusura) ─────────
    outcome           TEXT NOT NULL DEFAULT 'PENDING'
                      CHECK(outcome IN ('PENDING','TP','SL','BE','EXPIRED','VIRTUAL_TP','VIRTUAL_SL')),
    r_realized        REAL,
    mfe_r             REAL,
    mae_r             REAL,
    duration_bars     INTEGER,
    outcome_ts_iso    TEXT,                    -- quando è stato chiuso
    last_checked_ts   INTEGER,                 -- M4: ultimo scan che ha valutato questo PENDING

    -- ─── META ──────────────────────────────────────────────
    created_ts_iso    TEXT NOT NULL,           -- quando il record è stato inserito
    ledger_version    TEXT DEFAULT 'sprint0-v1'
);

-- ============================================================
-- INDICI — pensati per le query di analisi previste
-- ============================================================
-- "Quali engine favorevoli prima dei TP, per asset?"
CREATE INDEX IF NOT EXISTS idx_dl_outcome_asset
    ON decision_ledger(outcome, asset);

-- "Come cambia il framework dopo ogni Sprint?"
CREATE INDEX IF NOT EXISTS idx_dl_version
    ON decision_ledger(code_version);

-- Segmentazione per strategia/regime
CREATE INDEX IF NOT EXISTS idx_dl_strategy_regime
    ON decision_ledger(strategy, regime);

-- M4: trovare velocemente i PENDING da riconciliare
CREATE INDEX IF NOT EXISTS idx_dl_pending
    ON decision_ledger(outcome, last_checked_ts)
    WHERE outcome = 'PENDING';

-- Ordinamento temporale (ULID già ordinabile, ma ts_micro per range query)
CREATE INDEX IF NOT EXISTS idx_dl_ts
    ON decision_ledger(ts_micro);

-- Analisi dei rifiutati per gate
CREATE INDEX IF NOT EXISTS idx_dl_reject
    ON decision_ledger(decision_type, reject_gate);
