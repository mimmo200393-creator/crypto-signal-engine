-- storage/v41_schema.sql
-- Schema isolato per Institutional Scanner Framework V4.1 Intraday Wave Edition.
-- Completamente separato da v3_signals e v4_signals per garantire
-- statistiche indipendenti tra i tre framework.

CREATE TABLE IF NOT EXISTS v41_signals (
    signal_id TEXT PRIMARY KEY,
    timestamp_setup DATETIME NOT NULL,
    timestamp_closed DATETIME,
    asset TEXT NOT NULL,
    direction TEXT NOT NULL CHECK(direction IN ('BUY','SELL')),

    entry REAL NOT NULL,
    stop_loss REAL NOT NULL,
    take_profit REAL,
    rr REAL,

    trigger_types TEXT,           -- JSON list: BOS / CHOCH / LIQUIDITY_SWEEP
    sweep_direction TEXT,
    bos_direction TEXT,
    choch_direction TEXT,

    quality_score INTEGER NOT NULL,
    quality_label TEXT NOT NULL CHECK(quality_label IN ('HIGH','MEDIUM','LOW')),

    ema_h4 TEXT,
    ema_h1 TEXT,
    dow_theory_h4 TEXT,
    momentum TEXT,
    in_h4_zone BOOLEAN DEFAULT 0,
    sr_reaction BOOLEAN DEFAULT 0,
    ote_present BOOLEAN DEFAULT 0,
    session TEXT,

    liquidity_source TEXT,
    liquidity_target TEXT,
    liquidity_target_price REAL,

    trader_decision TEXT DEFAULT 'unknown'
        CHECK(trader_decision IN ('unknown','taken','skipped')),
    final_outcome TEXT
        CHECK(final_outcome IN (NULL,'TP','SL','EXPIRED','OPEN')),
    mae REAL,
    mfe REAL,

    market_snapshot TEXT
);

CREATE INDEX IF NOT EXISTS idx_v41_signals_asset_status
    ON v41_signals(asset, final_outcome);
CREATE INDEX IF NOT EXISTS idx_v41_signals_timestamp
    ON v41_signals(timestamp_setup);
CREATE INDEX IF NOT EXISTS idx_v41_signals_quality
    ON v41_signals(quality_label);
CREATE INDEX IF NOT EXISTS idx_v41_signals_session
    ON v41_signals(session);
