-- storage/v3_schema.sql
-- Schema isolato per Institutional Scanner Framework V3.2 Frozen.
-- Tabelle completamente separate da candles_cache/signals esistenti
-- e dalle eventuali tabelle gold_* di tentativi precedenti.
--
-- Multi-asset: PAXG_USDT e BTC_USDT, tracciati indipendentemente.

-- ============================================================
-- Cache candele D1/M30/M15 multi-asset
-- ============================================================

CREATE TABLE IF NOT EXISTS v3_candles_cache (
    asset TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    timestamp INTEGER NOT NULL,
    open REAL,
    high REAL,
    low REAL,
    close REAL,
    volume REAL,
    PRIMARY KEY (asset, timeframe, timestamp)
);

CREATE INDEX IF NOT EXISTS idx_v3_candles_asset_tf_ts
    ON v3_candles_cache(asset, timeframe, timestamp);

-- ============================================================
-- Segnali Institutional Scanner V3.2
-- ============================================================

CREATE TABLE IF NOT EXISTS v3_signals (
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

CREATE INDEX IF NOT EXISTS idx_v3_signals_asset_status
    ON v3_signals(asset, final_outcome);
CREATE INDEX IF NOT EXISTS idx_v3_signals_timestamp
    ON v3_signals(timestamp_setup);

-- ============================================================
-- Stato strutturale H4 per asset (per pullback invalidation tracking)
-- ============================================================

CREATE TABLE IF NOT EXISTS v3_structure_state (
    asset TEXT PRIMARY KEY,
    trend_direction TEXT,              -- BULLISH / BEARISH / NEUTRAL
    last_higher_low REAL,
    last_lower_high REAL,
    last_swing_timestamp INTEGER,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
