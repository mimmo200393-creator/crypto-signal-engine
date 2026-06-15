-- storage/schema_v2.sql
-- Schema V2.2 per Crypto Signal Engine Institutional Adaptive Framework
-- Mantiene la tabella `trades` V1 per retrocompatibilita' e
-- aggiunge la tabella `signals` V2 con schema esteso.

-- ============================================================
-- V1: tabella trades originale (invariata, per retrocompatibilita')
-- ============================================================

CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_name TEXT NOT NULL,
    strategy_version TEXT NOT NULL,
    timestamp_alert DATETIME,
    timestamp_setup DATETIME NOT NULL,
    timestamp_closed DATETIME,
    asset TEXT NOT NULL,
    setup TEXT NOT NULL,
    direzione TEXT NOT NULL CHECK(direzione IN ('LONG','SHORT')),
    entry REAL NOT NULL,
    stop_loss REAL NOT NULL,
    take_profit REAL NOT NULL,
    rr REAL NOT NULL,
    score INTEGER NOT NULL,
    stato TEXT NOT NULL CHECK(stato IN ('ACTIVE','TP','SL','EXPIRED')),
    atr_h1 REAL,
    support_level REAL,
    resistance_level REAL,
    trigger_type TEXT,
    macro_event_active BOOLEAN DEFAULT 0,
    macro_event_type TEXT,
    macro_event_minutes_to_release INTEGER,
    mae REAL,
    mfe REAL,
    bars_open INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_trades_asset_dir_stato ON trades(asset, direzione, stato);
CREATE INDEX IF NOT EXISTS idx_trades_stato ON trades(stato);

-- ============================================================
-- V2: tabella signals (schema esteso V2.2)
-- ============================================================

CREATE TABLE IF NOT EXISTS signals (
    signal_id TEXT PRIMARY KEY,
    strategy_name TEXT NOT NULL,
    strategy_version TEXT NOT NULL,
    asset TEXT NOT NULL,
    direction TEXT NOT NULL CHECK(direction IN ('LONG','SHORT')),
    entry REAL NOT NULL,
    stop_loss REAL NOT NULL,
    take_profit REAL NOT NULL,
    rr REAL NOT NULL,
    raw_score REAL NOT NULL,
    final_score REAL NOT NULL,
    market_regime TEXT,
    timestamp_setup DATETIME NOT NULL,
    timestamp_alert DATETIME,
    timestamp_closed DATETIME,
    trade_status TEXT NOT NULL DEFAULT 'GENERATED'
        CHECK(trade_status IN ('GENERATED','REJECTED','APPROVED','NOTIFIED','OPEN','TP','SL','EXPIRED')),
    rejection_reason TEXT,
    mae REAL,
    mfe REAL,
    bars_open INTEGER DEFAULT 0,
    time_to_tp INTEGER,
    time_to_sl INTEGER,
    market_snapshot TEXT,
    macro_event_active BOOLEAN DEFAULT 0,
    macro_event_type TEXT,
    macro_event_minutes_to_release INTEGER,
    macro_risk TEXT,
    zone_level REAL,
    zone_touches INTEGER,
    session TEXT,
    momentum_direction TEXT,
    atr_daily REAL
);

CREATE INDEX IF NOT EXISTS idx_signals_asset_dir_status
    ON signals(asset, direction, trade_status);
CREATE INDEX IF NOT EXISTS idx_signals_status
    ON signals(trade_status);
CREATE INDEX IF NOT EXISTS idx_signals_strategy
    ON signals(strategy_name, strategy_version);
CREATE INDEX IF NOT EXISTS idx_signals_timestamp
    ON signals(timestamp_setup);

-- ============================================================
-- Cache candele (invariata dalla V1)
-- ============================================================

CREATE TABLE IF NOT EXISTS candles_cache (
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

CREATE INDEX IF NOT EXISTS idx_candles_asset_tf_ts
    ON candles_cache(asset, timeframe, timestamp);
