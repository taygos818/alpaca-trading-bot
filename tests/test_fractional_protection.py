import importlib
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo


SERVICE_DIR = Path(__file__).resolve().parents[1] / "services" / "strategy-engine"
sys.path.insert(0, str(SERVICE_DIR))
module = importlib.import_module("utils.fractional_protection")


class FakeStore:
    def __init__(self, records=None):
        self.records = {row["symbol"]: dict(row) for row in (records or [])}

    def upsert_fractional_protection(self, record):
        self.records[record["symbol"]] = dict(record)

    def update_fractional_protection(self, symbol, **fields):
        self.records[symbol].update(fields)

    def list_fractional_protections(self, active_only=True):
        values = list(self.records.values())
        return [row for row in values if not active_only or row.get("status") not in {"closed", "canceled"}]


class FakeExecutor:
    def __init__(self, order=None, open_orders=None):
        self.order = order or {"id": "entry-1", "status": "filled", "filled_qty": "0.25"}
        self.open_orders = list(open_orders or [])
        self.stops = []
        self.canceled = []

    def get_order(self, order_id):
        return self.order

    def submit_fractional_stop(self, symbol, quantity, stop_price, client_order_id):
        response = {
            "id": f"stop-{len(self.stops) + 1}", "symbol": symbol,
            "qty": str(quantity), "side": "sell", "type": "stop",
        }
        self.stops.append((symbol, quantity, stop_price, client_order_id))
        self.open_orders = [response]
        return response

    def list_open_orders(self, symbol=""):
        return list(self.open_orders)

    def cancel_order(self, order_id):
        self.canceled.append(order_id)
        self.open_orders = [order for order in self.open_orders if order.get("id") != order_id]


def record(symbol="AAPL"):
    return {
        "symbol": symbol, "entry_intent_id": "tier2-entry-1", "quantity": 0.25,
        "stop_price": 95.0, "take_profit_price": 110.0,
        "entry_order_id": "entry-1", "stop_order_id": "", "status": "filled_unprotected",
    }


def broker_state(qty=0.25):
    return SimpleNamespace(positions={
        "AAPL": SimpleNamespace(qty=qty),
    })


def test_fractional_entry_fill_immediately_gets_day_stop():
    store = FakeStore([record()])
    executor = FakeExecutor()
    coordinator = module.FractionalProtectionCoordinator(store, executor)
    intent = SimpleNamespace(symbol="AAPL", quantity=0.25, stop_loss_price=95.0)

    coordinator.protect_entry_fill(intent, "tier2-entry-1", "entry-1", timeout_seconds=1)

    assert executor.stops[0][:3] == ("AAPL", 0.25, 95.0)
    assert store.records["AAPL"]["status"] == "protected"


def test_reconcile_renews_missing_stop_during_submission_window(monkeypatch):
    store = FakeStore([record()])
    executor = FakeExecutor(open_orders=[])
    coordinator = module.FractionalProtectionCoordinator(store, executor)
    monkeypatch.setattr(coordinator, "_stop_submission_window", lambda: True)

    failures = coordinator.reconcile(broker_state())

    assert failures == []
    assert len(executor.stops) == 1
    assert store.records["AAPL"]["status"] == "protected"


def test_each_stop_renewal_uses_a_unique_client_order_id(monkeypatch):
    store = FakeStore([record()])
    executor = FakeExecutor()
    coordinator = module.FractionalProtectionCoordinator(store, executor)
    nonces = iter((11111111, 22222222))
    monkeypatch.setattr(module.time, "time_ns", lambda: next(nonces))

    coordinator._submit_stop("AAPL", 0.25, 95.0, "tier2-entry-1")
    coordinator._submit_stop("AAPL", 0.25, 95.0, "tier2-entry-1")

    assert executor.stops[0][3] != executor.stops[1][3]


def test_reconcile_fails_closed_for_fractional_position_without_plan():
    coordinator = module.FractionalProtectionCoordinator(FakeStore(), FakeExecutor())

    failures = coordinator.reconcile(broker_state())

    assert failures == ["AAPL fractional position has no durable protection plan"]


def test_close_cancels_fractional_stop_first():
    stop = {"id": "stop-1", "symbol": "AAPL", "qty": "0.25", "side": "sell", "type": "stop"}
    store = FakeStore([record()])
    executor = FakeExecutor(open_orders=[stop])
    coordinator = module.FractionalProtectionCoordinator(store, executor)

    coordinator.cancel_stop_before_close("AAPL")

    assert executor.canceled == ["stop-1"]
    assert store.records["AAPL"]["status"] == "closing"


def test_stop_submission_window_opens_before_regular_session():
    et = ZoneInfo("America/New_York")
    assert module.FractionalProtectionCoordinator._stop_submission_window(
        datetime(2026, 8, 21, 9, 25, tzinfo=et)
    ) is True
    assert module.FractionalProtectionCoordinator._stop_submission_window(
        datetime(2026, 8, 21, 9, 24, tzinfo=et)
    ) is False
