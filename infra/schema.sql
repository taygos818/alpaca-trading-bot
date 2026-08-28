-- Database schema for trading bot storage

CREATE TABLE IF NOT EXISTS trades (
    id BIGSERIAL PRIMARY KEY,
    event_id UUID,
    timestamp TIMESTAMPTZ NOT NULL,
    intent_id TEXT,
    strategy TEXT NOT NULL,
    symbol TEXT NOT NULL,
    action TEXT NOT NULL,
    quantity DOUBLE PRECISION NOT NULL,
    order_value DOUBLE PRECISION NOT NULL,
    estimated_risk_value DOUBLE PRECISION,
    account_nav_used DOUBLE PRECISION,
    current_position_value_used DOUBLE PRECISION,
    state_source TEXT,
    broker_state_error TEXT,
    allowed BOOLEAN NOT NULL,
    reason TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS order_events (
    id BIGSERIAL PRIMARY KEY,
    order_event_id UUID NOT NULL,
    timestamp TIMESTAMPTZ NOT NULL,
    intent_id TEXT NOT NULL,
    strategy TEXT NOT NULL,
    symbol TEXT NOT NULL,
    action TEXT NOT NULL,
    quantity DOUBLE PRECISION NOT NULL,
    requested_order_type TEXT NOT NULL,
    requested_time_in_force TEXT NOT NULL,
    dry_run BOOLEAN NOT NULL,
    status TEXT NOT NULL,
    broker TEXT NOT NULL,
    broker_order_id TEXT NOT NULL,
    request_payload JSONB,
    response_payload JSONB,
    error_message TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS order_intents (
    intent_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    strategy TEXT NOT NULL,
    symbol TEXT NOT NULL,
    action TEXT NOT NULL,
    quantity DOUBLE PRECISION NOT NULL,
    order_value DOUBLE PRECISION NOT NULL,
    signal_timestamp TIMESTAMPTZ NOT NULL,
    config_version TEXT NOT NULL,
    status TEXT NOT NULL,
    broker_order_id TEXT NOT NULL DEFAULT '',
    error_message TEXT NOT NULL DEFAULT '',
    request_payload JSONB,
    response_payload JSONB
);

CREATE INDEX IF NOT EXISTS order_intents_status_idx ON order_intents (status, updated_at);
DROP INDEX IF EXISTS order_intents_active_lane_idx;
CREATE UNIQUE INDEX order_intents_active_lane_idx
ON order_intents (strategy, symbol, action)
WHERE status IN ('reserved', 'submitting', 'submitted', 'accepted', 'pending_new', 'partially_filled', 'filled_protected', 'unprotected_filled', 'unknown_requires_reconciliation');

ALTER TABLE trades ALTER COLUMN quantity TYPE DOUBLE PRECISION;
ALTER TABLE order_events ALTER COLUMN quantity TYPE DOUBLE PRECISION;

CREATE TABLE IF NOT EXISTS fractional_protections (
    symbol TEXT PRIMARY KEY,
    entry_intent_id TEXT NOT NULL,
    quantity DOUBLE PRECISION NOT NULL,
    stop_price DOUBLE PRECISION NOT NULL,
    take_profit_price DOUBLE PRECISION NOT NULL,
    entry_order_id TEXT NOT NULL DEFAULT '',
    stop_order_id TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    session_date DATE,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    last_error TEXT NOT NULL DEFAULT ''
);
