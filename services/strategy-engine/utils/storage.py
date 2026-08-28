import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb


def execute_migrations(postgres_dsn: str):
    possible_paths = [
        Path(os.getenv("SCHEMA_SQL_PATH", "/app/infra/schema.sql")),
    ]
    resolved_file = Path(__file__).resolve()
    for parent in resolved_file.parents:
        possible_paths.append(parent / "infra" / "schema.sql")
        possible_paths.append(parent / "schema.sql")

    schema_path = None
    for p in possible_paths:
        if p.exists():
            schema_path = p
            break

    if not schema_path:
        raise FileNotFoundError(
            f"schema.sql not found in any of the searched locations: {[str(x) for x in possible_paths]}"
        )

    with open(schema_path, "r", encoding="utf-8") as f:
        schema_sql = f.read()

    with psycopg.connect(postgres_dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(hashtext('trading_bot_schema_migration'))")
            cur.execute(schema_sql)


class TradeStore:
    def __init__(self, log_path: str, postgres_dsn: str = "", order_log_path: str = ""):
        self.log_path = Path(log_path)
        resolved_order_path = order_log_path or f"{log_path}.orders"
        self.order_log_path = Path(resolved_order_path)
        self.postgres_dsn = postgres_dsn
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.order_log_path.parent.mkdir(parents=True, exist_ok=True)
        if self.postgres_dsn:
            execute_migrations(self.postgres_dsn)

    @classmethod
    def from_env(cls):
        return cls(
            log_path=os.getenv("TRADE_LOG_PATH", "/app/logs/trades.jsonl"),
            postgres_dsn=os.getenv("POSTGRES_DSN", ""),
            order_log_path=os.getenv("ORDER_LOG_PATH", "/app/logs/orders.jsonl"),
        )

    def log_trade_event(self, event: dict):
        enriched = {
            "event_id": str(uuid.uuid4()),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **event,
        }
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(enriched) + "\n")
        if self.postgres_dsn:
            self._write_postgres(enriched)
        return enriched

    def log_order_event(self, event: dict):
        enriched = {
            "order_event_id": str(uuid.uuid4()),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **event,
        }
        with self.order_log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(enriched) + "\n")
        if self.postgres_dsn:
            self._write_order_postgres(enriched)
        return enriched

    def reserve_order_intent(self, record: dict) -> bool:
        if not self.postgres_dsn:
            raise RuntimeError("Durable order-intent reservation requires PostgreSQL")
        with psycopg.connect(self.postgres_dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO order_intents (
                        intent_id, idempotency_key, created_at, updated_at, strategy,
                        symbol, action, quantity, order_value, signal_timestamp,
                        config_version, status
                    ) VALUES (
                        %(intent_id)s, %(idempotency_key)s, %(created_at)s, %(updated_at)s,
                        %(strategy)s, %(symbol)s, %(action)s, %(quantity)s, %(order_value)s,
                        %(signal_timestamp)s, %(config_version)s, %(status)s
                    )
                    ON CONFLICT DO NOTHING
                    RETURNING intent_id
                    """,
                    record,
                )
                return cur.fetchone() is not None

    def update_order_intent(self, intent_id: str, status: str, **fields):
        if not self.postgres_dsn:
            raise RuntimeError("Durable order-intent updates require PostgreSQL")
        allowed_fields = {"broker_order_id", "error_message", "request_payload", "response_payload"}
        unexpected = set(fields) - allowed_fields
        if unexpected:
            raise ValueError(f"Unsupported order-intent fields: {sorted(unexpected)}")
        assignments = ["status = %(status)s", "updated_at = %(updated_at)s"]
        params = {"intent_id": intent_id, "status": status, "updated_at": datetime.now(timezone.utc).isoformat()}
        for name, value in fields.items():
            assignments.append(f"{name} = %({name})s")
            params[name] = Jsonb(value) if name in {"request_payload", "response_payload"} and value is not None else value
        with psycopg.connect(self.postgres_dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"UPDATE order_intents SET {', '.join(assignments)} WHERE intent_id = %(intent_id)s",
                    params,
                )
                if cur.rowcount != 1:
                    raise RuntimeError(f"Order intent not found: {intent_id}")

    def list_unfinished_order_intents(self) -> list[dict]:
        if not self.postgres_dsn:
            return []
        with psycopg.connect(self.postgres_dsn) as conn:
            with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
                cur.execute(
                    """
                    SELECT intent_id, idempotency_key, strategy, symbol, action, quantity,
                           order_value, signal_timestamp, config_version, status,
                           broker_order_id, error_message, request_payload, response_payload
                    FROM order_intents
                    WHERE status IN (
                        'reserved', 'submitting', 'submitted', 'accepted', 'pending_new',
                        'partially_filled', 'filled_protected', 'unprotected_filled',
                        'unknown_requires_reconciliation'
                    )
                    ORDER BY created_at ASC
                    """
                )
                return list(cur.fetchall())

    def upsert_fractional_protection(self, record: dict):
        if not self.postgres_dsn:
            raise RuntimeError("Durable fractional protection requires PostgreSQL")
        now = datetime.now(timezone.utc).isoformat()
        payload = {"created_at": now, "updated_at": now, **record}
        with psycopg.connect(self.postgres_dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO fractional_protections (
                        symbol, entry_intent_id, quantity, stop_price, take_profit_price,
                        entry_order_id, stop_order_id, status, session_date,
                        created_at, updated_at, last_error
                    ) VALUES (
                        %(symbol)s, %(entry_intent_id)s, %(quantity)s, %(stop_price)s,
                        %(take_profit_price)s, %(entry_order_id)s, %(stop_order_id)s,
                        %(status)s, %(session_date)s, %(created_at)s, %(updated_at)s,
                        %(last_error)s
                    )
                    ON CONFLICT (symbol) DO UPDATE SET
                        entry_intent_id = EXCLUDED.entry_intent_id,
                        quantity = EXCLUDED.quantity,
                        stop_price = EXCLUDED.stop_price,
                        take_profit_price = EXCLUDED.take_profit_price,
                        entry_order_id = EXCLUDED.entry_order_id,
                        stop_order_id = EXCLUDED.stop_order_id,
                        status = EXCLUDED.status,
                        session_date = EXCLUDED.session_date,
                        updated_at = EXCLUDED.updated_at,
                        last_error = EXCLUDED.last_error
                    """,
                    payload,
                )

    def update_fractional_protection(self, symbol: str, **fields):
        allowed = {"quantity", "entry_order_id", "stop_order_id", "status", "session_date", "last_error"}
        unexpected = set(fields) - allowed
        if unexpected:
            raise ValueError(f"Unsupported fractional-protection fields: {sorted(unexpected)}")
        assignments = ["updated_at = %(updated_at)s"]
        params = {"symbol": symbol.upper(), "updated_at": datetime.now(timezone.utc).isoformat()}
        for name, value in fields.items():
            assignments.append(f"{name} = %({name})s")
            params[name] = value
        with psycopg.connect(self.postgres_dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"UPDATE fractional_protections SET {', '.join(assignments)} WHERE symbol = %(symbol)s",
                    params,
                )
                if cur.rowcount != 1:
                    raise RuntimeError(f"Fractional protection not found: {symbol}")

    def list_fractional_protections(self, active_only: bool = True) -> list[dict]:
        if not self.postgres_dsn:
            return []
        where = "WHERE status NOT IN ('closed', 'canceled')" if active_only else ""
        with psycopg.connect(self.postgres_dsn) as conn:
            with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
                cur.execute(f"SELECT * FROM fractional_protections {where} ORDER BY created_at")
                return list(cur.fetchall())

    def _write_postgres(self, event: dict):
        with psycopg.connect(self.postgres_dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO trades (
                        event_id, timestamp, intent_id, strategy, symbol, action, quantity,
                        order_value, estimated_risk_value, account_nav_used,
                        current_position_value_used, state_source, broker_state_error,
                        allowed, reason
                    ) VALUES (%(event_id)s, %(timestamp)s, %(intent_id)s, %(strategy)s,
                              %(symbol)s, %(action)s, %(quantity)s, %(order_value)s,
                              %(estimated_risk_value)s, %(account_nav_used)s,
                              %(current_position_value_used)s, %(state_source)s,
                              %(broker_state_error)s, %(allowed)s, %(reason)s)
                    """,
                    event,
                )

    def _write_order_postgres(self, event: dict):
        event = dict(event)
        for field in ("request_payload", "response_payload"):
            if event.get(field) is not None:
                event[field] = Jsonb(event[field])
        with psycopg.connect(self.postgres_dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO order_events (
                        order_event_id, timestamp, intent_id, strategy, symbol, action, quantity,
                        requested_order_type, requested_time_in_force, dry_run, status,
                        broker, broker_order_id, request_payload, response_payload, error_message
                    ) VALUES (
                        %(order_event_id)s, %(timestamp)s, %(intent_id)s, %(strategy)s, %(symbol)s,
                        %(action)s, %(quantity)s, %(requested_order_type)s,
                        %(requested_time_in_force)s, %(dry_run)s, %(status)s, %(broker)s,
                        %(broker_order_id)s, %(request_payload)s, %(response_payload)s,
                        %(error_message)s
                    )
                    """,
                    event,
                )
