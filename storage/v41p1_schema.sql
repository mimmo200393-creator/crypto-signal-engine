-- storage/v41p1_schema.sql
-- Schema isolato per Institutional Scanner V4.1 Phase 1
-- Money Flow & Intraday Edge Validation
-- Completamente separato da v41_signals per tracking statistico indipendente.

CREATE TABLE IF NOT EXISTS v41p1_signals (
    signal_id TEXT PRIMARY KEY,
    timestamp_setup DATETIME NOT NULL,
    timestamp_closed DATETIME,
    asset TEXT NOT NULL,
    direction TEXT NOT NULL CHECK(direction IN ('BUY','SELL')),

    -- Prezzi
    entry REAL NOT NULL,
    stop_loss REAL NOT NULL,
    take_profit REAL,
    tp1 REAL,
    tp2 REAL,
    tp1_hit BOOLEAN DEFAULT 0,
    tp2_hit BOOLEAN DEFAULT 0,
    rr REAL,

    -- Trigger
    trigger_types TEXT,           -- JSON list: BOS / CHOCH / LIQUIDITY_SWEEP
    sweep_direction TEXT,
    bos_direction TEXT,
    choch_direction TEXT,

    -- Money Flow Map
    nearest_above_label TEXT,
    nearest_above_price REAL,
    nearest_above_priority TEXT,
    nearest_above_score REAL,
    nearest_below_label TEXT,
    nearest_below_price REAL,
    nearest_below_priority TEXT,
    nearest_below_score REAL,

    -- Liquidity Source e Target
    liquidity_source TEXT,
    liquidity_source_price REAL,
    liquidity_source_priority TEXT,
    liquidity_source_score REAL,
    liquidity_target TEXT,
    liquidity_target_price REAL,
    liquidity_target_priority TEXT,
    liquidity_target_score REAL,

    -- Tradeability
    expected_move_points REAL,
    expected_move_pct REAL,
    expected_move_barrier TEXT,
    distance_to_nearest_above_pct REAL,
    distance_to_nearest_below_pct REAL,

    -- Quality Score
    quality_score INTEGER NOT NULL,
    quality_label TEXT NOT NULL CHECK(quality_label IN ('HIGH','MEDIUM','LOW')),

    -- Contesto
    ema_h4 TEXT,
    ema_h1 TEXT,
    dow_theory_h4 TEXT,
    momentum TEXT,
    session TEXT,
    in_h4_zone BOOLEAN DEFAULT 0,
    sr_reaction BOOLEAN DEFAULT 0,
    ote_present BOOLEAN DEFAULT 0,
    ote_entry_low REAL,
    ote_entry_high REAL,

    -- Outcome Tracking
    trader_decision TEXT DEFAULT 'unknown'
        CHECK(trader_decision IN ('unknown','taken','skipped')),
    final_outcome TEXT
        CHECK(final_outcome IN (NULL,'TP','SL','EXPIRED','OPEN')),
    mae REAL,
    mfe REAL,
    time_to_tp_minutes INTEGER,
    time_to_sl_minutes INTEGER,

    -- Snapshot
    market_snapshot TEXT
);

CREATE INDEX IF NOT EXISTS idx_v41p1_signals_asset_status
    ON v41p1_signals(asset, final_outcome);
CREATE INDEX IF NOT EXISTS idx_v41p1_signals_timestamp
    ON v41p1_signals(timestamp_setup);
CREATE INDEX IF NOT EXISTS idx_v41p1_signals_quality
    ON v41p1_signals(quality_label);
CREATE INDEX IF NOT EXISTS idx_v41p1_signals_session
    ON v41p1_signals(session);
CREATE INDEX IF NOT EXISTS idx_v41p1_signals_trigger
    ON v41p1_signals(trigger_types);

-- Watchlist Alert: preparatori, non operativi
CREATE TABLE IF NOT EXISTS v41p1_watchlist_alerts (
    alert_id TEXT PRIMARY KEY,
    timestamp_alert DATETIME NOT NULL,
    asset TEXT NOT NULL,
    level_label TEXT NOT NULL,
    level_price REAL NOT NULL,
    level_priority TEXT NOT NULL,
    level_score REAL NOT NULL,
    distance_pct REAL NOT NULL,
    potential_direction TEXT NOT NULL CHECK(potential_direction IN ('BUY','SELL')),
    historical_touches INTEGER DEFAULT 0,
    followed_by_trade_alert BOOLEAN DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_v41p1_watchlist_asset
    ON v41p1_watchlist_alerts(asset, timestamp_alert);

-- Stato di prossimita' per Watchlist Alert (evita ripetizioni)
CREATE TABLE IF NOT EXISTS v41p1_watchlist_state (
    asset TEXT NOT NULL,
    level_label TEXT NOT NULL,
    is_inside_proximity BOOLEAN NOT NULL DEFAULT 0,
    last_updated DATETIME NOT NULL,
    PRIMARY KEY (asset, level_label)
);

-- Ultimo alert operativo per Duplicate Signal Protection
CREATE TABLE IF NOT EXISTS v41p1_last_alert_state (
    asset TEXT PRIMARY KEY,
    direction TEXT NOT NULL,
    trigger_type TEXT NOT NULL,
    liquidity_source TEXT,
    last_updated DATETIME NOT NULL
);

-- Money Flow Map snapshot: registra la mappa ad ogni scan per analisi storica
CREATE TABLE IF NOT EXISTS v41p1_mfm_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    timestamp_snapshot DATETIME NOT NULL,
    asset TEXT NOT NULL,
    current_price REAL NOT NULL,
    nearest_above_label TEXT,
    nearest_above_price REAL,
    nearest_above_priority TEXT,
    nearest_above_score REAL,
    nearest_below_label TEXT,
    nearest_below_price REAL,
    nearest_below_priority TEXT,
    nearest_below_score REAL,
    levels_json TEXT    -- JSON completo di tutti i livelli con score
);

CREATE INDEX IF NOT EXISTS idx_v41p1_mfm_asset_ts
    ON v41p1_mfm_snapshots(asset, timestamp_snapshot);
