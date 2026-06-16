-- storage/v4_schema.sql
-- Schema isolato per Institutional Scanner Framework V4.0 Daily Edition.
-- Completamente separato da v3_signals per garantire statistiche
-- indipendenti tra V3.2 Frozen e V4.0 Daily Edition.

CREATE TABLE IF NOT EXISTS v4_signals (
    signal_id TEXT PRIMARY KEY,
    timestamp_setup DATETIME NOT NULL,
    timestamp_closed DATETIME,
    asset TEXT NOT NULL,
    direction TEXT NOT NULL CHECK(direction IN ('BUY','SELL')),

    entry REAL NOT NULL,
    stop_loss REAL NOT NULL,
    tp1 REAL,
    tp2 REAL,
    tp3 REAL,

    rr REAL NOT NULL,
    signal_quality REAL NOT NULL,
    quality_label TEXT,

    daily_context_status TEXT,
    h4_structure_status TEXT,
    h4_zone_status TEXT,
    ote_present BOOLEAN DEFAULT 0,
    pullback_type TEXT,
    pullback_invalidated BOOLEAN DEFAULT 0,
    m30_transition_status TEXT,
    m15_bos_confirmed BOOLEAN DEFAULT 0,
    session TEXT,

    trader_decision TEXT DEFAULT 'unknown'
        CHECK(trader_decision IN ('unknown','taken','skipped')),
    final_outcome TEXT
        CHECK(final_outcome IN (NULL,'TP1','TP2','TP3','SL','EXPIRED','OPEN')),
    mae REAL,
    mfe REAL,

    market_snapshot TEXT
);

CREATE INDEX IF NOT EXISTS idx_v4_signals_asset_status
    ON v4_signals(asset, final_outcome);
CREATE INDEX IF NOT EXISTS idx_v4_signals_timestamp
    ON v4_signals(timestamp_setup);
CREATE INDEX IF NOT EXISTS idx_v4_signals_quality
    ON v4_signals(quality_label);
