"""Promotion of reviewed commands into a tightly bounded paper lifecycle."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import threading
from typing import Any
from zoneinfo import ZoneInfo

from agent_contracts import AuthorizedExecution, AuthorizedExit, OrderEvent, OrderStatus
from execution_gateway import AlpacaCliError, AlpacaCliGateway, CliResponse


class BrokerStateUnresolved(RuntimeError):
    pass


FINAL_STATUSES = {"filled", "canceled", "cancelled", "expired", "rejected"}
OPEN_STATUSES = {"new", "accepted", "pending_new", "partially_filled", "pending_cancel", "pending_replace"}


@dataclass(frozen=True, slots=True)
class PaperLaunchPolicy:
    submission_enabled: bool = False
    entry_submission_enabled: bool = True
    dry_run: bool = True
    bounded_ack: str = ""
    max_submissions_per_day: int = 30
    max_authorized_loss_usd: Decimal = Decimal("5000")
    max_open_orders: int = 8
    symbol_cooldown_minutes: int = 10

    def __post_init__(self) -> None:
        if self.submission_enabled and self.dry_run:
            raise ValueError("paper submission and dry-run cannot both be enabled")
        if self.submission_enabled and self.bounded_ack != "paper-contest":
            raise ValueError("bounded paper acknowledgement is required")
        if self.max_submissions_per_day <= 0 or self.max_open_orders < 0 or self.symbol_cooldown_minutes < 0:
            raise ValueError("invalid paper launch count limit")
        if self.max_authorized_loss_usd <= 0:
            raise ValueError("paper launch risk limit must be positive")

    @classmethod
    def from_env(cls) -> "PaperLaunchPolicy":
        enabled = _flag("PAPER_ORDER_SUBMISSION_ENABLED", False)
        return cls(
            submission_enabled=enabled,
            entry_submission_enabled=_flag("PAPER_ENTRY_SUBMISSION_ENABLED", enabled),
            dry_run=_flag("PAPER_ORDER_DRY_RUN", True),
            bounded_ack=os.getenv("M6_BOUNDED_SUBMISSION_ACK", "").strip(),
            max_submissions_per_day=int(os.getenv("PAPER_MAX_SUBMISSIONS_PER_DAY", "30")),
            max_authorized_loss_usd=Decimal(os.getenv("M6_MAX_AUTHORIZED_LOSS_USD", "5000")),
            max_open_orders=int(os.getenv("M6_MAX_OPEN_ORDERS", "8")),
            symbol_cooldown_minutes=int(os.getenv("PAPER_SYMBOL_COOLDOWN_MINUTES", "10")),
        )


class JsonlSubmissionLedger:
    """Restart-safe count of accepted paper submissions by New York trading date."""

    def __init__(self, path: str = "") -> None:
        self.path = Path(path) if path else None
        self._lock = threading.Lock()

    def count(self, trading_date: str, kind: str | None = None) -> int:
        if self.path is None or not self.path.exists():
            return 0
        with self._lock, self.path.open("r", encoding="utf-8") as handle:
            return sum(
                1
                for line in handle
                if line.strip()
                and (row := json.loads(line)).get("trading_date") == trading_date
                and (kind is None or row.get("kind") == kind)
            )

    def latest_entry_at(self, underlying: str) -> datetime | None:
        if self.path is None or not self.path.exists():
            return None
        symbol = underlying.strip().upper()
        latest = None
        with self._lock, self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                row = json.loads(line)
                if row.get("kind") != "entry" or str(row.get("underlying", "")).upper() != symbol:
                    continue
                submitted_at = datetime.fromisoformat(str(row["submitted_at"]))
                latest = submitted_at if latest is None or submitted_at > latest else latest
        return latest

    def append(
        self,
        client_order_id: str,
        kind: str,
        submitted_at: datetime,
        *,
        underlying: str = "",
    ) -> None:
        if self.path is None:
            return
        trading_date = submitted_at.astimezone(ZoneInfo("America/New_York")).date().isoformat()
        payload = {
            "client_order_id": client_order_id,
            "kind": kind,
            "submitted_at": submitted_at.isoformat(),
            "trading_date": trading_date,
        }
        if underlying:
            payload["underlying"] = underlying.strip().upper()
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")


@dataclass(frozen=True, slots=True)
class BrokerOrderSnapshot:
    broker_order_id: str
    client_order_id: str
    status: str
    filled_quantity: int
    average_fill_price: Decimal
    broker_timestamp: datetime

    @property
    def is_final(self) -> bool:
        return self.status in FINAL_STATUSES


@dataclass(frozen=True, slots=True)
class LaunchResult:
    mode: str
    response: CliResponse | None
    snapshot: BrokerOrderSnapshot | None
    duplicate: bool = False


class BoundedPaperLauncher:
    """Daily bounded paper submission with duplicate and broker-state checks."""

    def __init__(
        self,
        gateway: AlpacaCliGateway,
        policy: PaperLaunchPolicy | None = None,
        ledger: JsonlSubmissionLedger | None = None,
    ) -> None:
        self.gateway = gateway
        self.policy = policy or PaperLaunchPolicy.from_env()
        self.ledger = ledger or JsonlSubmissionLedger(os.getenv("PAPER_SUBMISSION_LEDGER_PATH", ""))

    def _check_daily_limit(self, now: datetime) -> None:
        trading_date = now.astimezone(ZoneInfo("America/New_York")).date().isoformat()
        if self.ledger.count(trading_date, "entry") >= self.policy.max_submissions_per_day:
            raise BrokerStateUnresolved("daily paper submission limit reached")

    def _check_symbol_cooldown(self, underlying: str, now: datetime) -> None:
        if self.policy.symbol_cooldown_minutes == 0:
            return
        latest = self.ledger.latest_entry_at(underlying)
        if latest is not None and now - latest < timedelta(minutes=self.policy.symbol_cooldown_minutes):
            raise BrokerStateUnresolved("per-underlying paper entry cooldown is active")

    def launch(self, execution: AuthorizedExecution) -> LaunchResult:
        existing = self._lookup_existing(execution.command.client_order_id)
        if existing is not None:
            return LaunchResult("duplicate", None, existing, True)
        open_orders = _orders(self.gateway.open_orders().payload)
        duplicate = next(
            (item for item in open_orders if item.client_order_id == execution.command.client_order_id),
            None,
        )
        if duplicate is not None:
            return LaunchResult("duplicate", None, duplicate, True)
        if len([item for item in open_orders if not item.is_final]) >= self.policy.max_open_orders:
            raise BrokerStateUnresolved("maximum open-order count reached")

        preview = self.gateway.preview(execution)
        if not self.policy.submission_enabled or not self.policy.entry_submission_enabled:
            return LaunchResult("dry_run", preview, None)
        now = datetime.now(timezone.utc)
        self._check_daily_limit(now)
        self._check_symbol_cooldown(execution.proposal.underlying, now)
        if execution.authorization.authorized_maximum_loss > self.policy.max_authorized_loss_usd:
            raise BrokerStateUnresolved("authorization exceeds bounded paper loss limit")

        response = self.gateway.submit(execution)
        snapshot = normalize_broker_order(response.payload)
        self.ledger.append(
            execution.command.client_order_id,
            "entry",
            now,
            underlying=execution.proposal.underlying,
        )
        if snapshot.client_order_id != execution.command.client_order_id:
            raise BrokerStateUnresolved("broker response client order ID mismatch")
        return LaunchResult("submitted", response, snapshot)

    def launch_exit(self, execution: AuthorizedExit) -> LaunchResult:
        existing = self._lookup_existing(execution.client_order_id)
        if existing is not None:
            return LaunchResult("duplicate", None, existing, True)
        open_orders = _orders(self.gateway.open_orders().payload)
        if len([item for item in open_orders if not item.is_final]) >= self.policy.max_open_orders:
            raise BrokerStateUnresolved("maximum open-order count reached")
        preview = self.gateway.preview_exit(execution)
        if not self.policy.submission_enabled:
            return LaunchResult("dry_run", preview, None)
        now = datetime.now(timezone.utc)
        response = self.gateway.submit_exit(execution)
        snapshot = normalize_broker_order(response.payload)
        self.ledger.append(execution.client_order_id, "exit", now)
        if snapshot.client_order_id != execution.client_order_id:
            raise BrokerStateUnresolved("broker exit response client order ID mismatch")
        return LaunchResult("submitted", response, snapshot)

    def _lookup_existing(self, client_order_id: str) -> BrokerOrderSnapshot | None:
        try:
            response = self.gateway.order_by_client_id(client_order_id)
        except AlpacaCliError as exc:
            if "status=404" in str(exc):
                return None
            raise BrokerStateUnresolved("client-order-id lookup failed closed") from exc
        snapshot = normalize_broker_order(response.payload)
        if snapshot.client_order_id != client_order_id:
            raise BrokerStateUnresolved("client-order-id lookup returned a different order")
        return snapshot

    def reconcile(self, execution: AuthorizedExecution) -> tuple[BrokerOrderSnapshot, OrderEvent]:
        response = self.gateway.order_by_client_id(execution.command.client_order_id)
        snapshot = normalize_broker_order(response.payload)
        if snapshot.client_order_id != execution.command.client_order_id:
            raise BrokerStateUnresolved("reconciliation returned a different client order ID")
        status = _contract_status(snapshot.status)
        event = OrderEvent(
            record_id=f"event.{execution.command.record_id}.{snapshot.status}.{snapshot.filled_quantity}",
            trace_id=execution.command.trace_id,
            command_id=execution.command.record_id,
            broker_order_id=snapshot.broker_order_id,
            status=status,
            filled_quantity=snapshot.filled_quantity,
            average_fill_price=snapshot.average_fill_price,
            broker_timestamp=snapshot.broker_timestamp,
            created_at=max(snapshot.broker_timestamp, execution.command.created_at),
        )
        return snapshot, event

    def cancel(self, execution: AuthorizedExecution, snapshot: BrokerOrderSnapshot) -> CliResponse:
        if snapshot.client_order_id != execution.command.client_order_id:
            raise BrokerStateUnresolved("refusing to cancel an unrelated order")
        if snapshot.is_final:
            raise BrokerStateUnresolved("final order cannot be canceled")
        return self.gateway.cancel_order(snapshot.broker_order_id)


def normalize_broker_order(payload: Any) -> BrokerOrderSnapshot:
    if isinstance(payload, dict) and isinstance(payload.get("order"), dict):
        payload = payload["order"]
    if not isinstance(payload, dict):
        raise BrokerStateUnresolved("broker order payload is invalid")
    order_id = str(payload.get("id") or payload.get("order_id") or "").strip()
    client_id = str(payload.get("client_order_id") or "").strip()
    status = str(payload.get("status") or "").strip().lower()
    if not order_id or not client_id or status not in FINAL_STATUSES | OPEN_STATUSES:
        raise BrokerStateUnresolved("broker order identity or status is unresolved")
    filled = int(Decimal(str(payload.get("filled_qty") or payload.get("filled_quantity") or "0")))
    average = Decimal(str(payload.get("filled_avg_price") or payload.get("average_fill_price") or "0"))
    if filled == 0:
        average = Decimal("0")
    timestamp_value = payload.get("updated_at") or payload.get("filled_at") or payload.get("created_at")
    if not timestamp_value:
        raise BrokerStateUnresolved("broker order timestamp is missing")
    timestamp = datetime.fromisoformat(str(timestamp_value).replace("Z", "+00:00")).astimezone(timezone.utc)
    return BrokerOrderSnapshot(order_id, client_id, status, filled, average, timestamp)


def _orders(payload: Any) -> tuple[BrokerOrderSnapshot, ...]:
    if isinstance(payload, dict):
        if "orders" in payload:
            payload = payload["orders"]
        elif "data" in payload:
            payload = payload["data"]
        else:
            raise BrokerStateUnresolved("open-order payload is invalid")
    if payload in (None, ""):
        return ()
    if not isinstance(payload, list):
        raise BrokerStateUnresolved("open-order payload is invalid")
    return tuple(normalize_broker_order(item) for item in payload)


def _contract_status(status: str) -> OrderStatus:
    mapping = {
        "new": OrderStatus.ACCEPTED,
        "accepted": OrderStatus.ACCEPTED,
        "pending_new": OrderStatus.REQUESTED,
        "partially_filled": OrderStatus.PARTIALLY_FILLED,
        "filled": OrderStatus.FILLED,
        "canceled": OrderStatus.CANCELED,
        "cancelled": OrderStatus.CANCELED,
        "expired": OrderStatus.CANCELED,
        "rejected": OrderStatus.REJECTED,
        "pending_cancel": OrderStatus.ACCEPTED,
        "pending_replace": OrderStatus.ACCEPTED,
    }
    return mapping[status]


def _flag(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}
