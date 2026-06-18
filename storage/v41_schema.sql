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
    take_profit REAL,       -- TP2 (2R), per compatibilità con il campo originale
    tp1 REAL,               -- 1R
    tp2 REAL,               -- 2R (identico a take_profit, campo esplicito per le statistiche)
    tp1_hit BOOLEAN DEFAULT 0,
    tp2_hit BOOLEAN DEFAULT 0,
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

    ote_entry_low REAL,
    ote_entry_high REAL,
    ote_in_zone_now BOOLEAN DEFAULT 0,

    expected_move_points REAL,
    expected_move_pct REAL,
    expected_move_barrier TEXT,
    expected_move_barrier_price REAL,

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

-- Stato di prossimità per asset+livello, usato per evitare di rigenerare
-- un Watchlist Alert ad ogni scan mentre il prezzo resta nella stessa
-- fascia. Un nuovo alert scatta solo sulla transizione fuori -> dentro.
CREATE TABLE IF NOT EXISTS v41_watchlist_state (
    asset TEXT NOT NULL,
    level_label TEXT NOT NULL,
    is_inside_proximity BOOLEAN NOT NULL DEFAULT 0,
    last_updated DATETIME NOT NULL,
    PRIMARY KEY (asset, level_label)
);

-- Storico dei Watchlist Alert effettivamente inviati, per analisi
-- successiva (es. quanti Watchlist Alert sono poi seguiti da un
-- Trade Alert reale sullo stesso asset/livello/direzione).
CREATE TABLE IF NOT EXISTS v41_watchlist_alerts (
    alert_id TEXT PRIMARY KEY,
    timestamp_alert DATETIME NOT NULL,
    asset TEXT NOT NULL,
    level_label TEXT NOT NULL,
    level_price REAL NOT NULL,
    distance_pct REAL NOT NULL,
    potential_direction TEXT NOT NULL CHECK(potential_direction IN ('BUY','SELL')),
    followed_by_trade_alert BOOLEAN DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_v41_watchlist_alerts_asset
    ON v41_watchlist_alerts(asset, timestamp_alert);

-- Riferimento persistente all'ultimo alert OPERATIVO inviato per
-- ciascun asset, usato dalla Duplicate Signal Protection per
-- confrontare il nuovo setup con l'ultimo effettivamente notificato
-- (sopravvive a riavvii ed esecuzioni indipendenti del workflow).
CREATE TABLE IF NOT EXISTS v41_last_alert_state (
    asset TEXT PRIMARY KEY,
    direction TEXT NOT NULL,
    trigger_type TEXT NOT NULL,
    liquidity_source TEXT,
    last_updated DATETIME NOT NULL
);
