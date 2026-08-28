import importlib
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest


SERVICE_DIR = Path(__file__).resolve().parents[1] / "services" / "strategy-engine"
sys.path.insert(0, str(SERVICE_DIR))
module = importlib.import_module("utils.order_lifecycle")


class FakeStore:
    def __init__(self, reserve=True, unfinished=None):
        self.should_reserve = reserve
        self.unfinished = unfinished or []
        self.reserved = []
        self.updates = []

    def reserve_order_intent(self, record):
        self.reserved.append(record)
        return self.should_reserve

    def update_order_intent(self, intent_id, status, **fields):
        self.updates.append((intent_id, status, fields))

    def list_unfinished_order_intents(self):
        return self.unfinished


class FakeExecutor:
    def __init__(self, result=None, error=None, recovered=None, reconciliation_error=None, position=None):
        self.result = result
        self.error = error
        self.recovered = recovered
        self.reconciliation_error = reconciliation_error
        self.position = position
        self.execute_calls = []

    def execute(self, intent, intent_id):
        self.execute_calls.append(intent_id)
        if self.error:
            raise self.error
        return self.result

    def get_order_by_client_id(self, intent_id):
        if self.reconciliation_error:
            raise self.reconciliation_error
        return self.recovered

    def result_from_reconciliation(self, intent, intent_id, payload):
        return SimpleNamespace(broker_order_id=payload["id"], request_payload={"type": "market"})

    def get_position(self, symbol):
        return self.position


def make_intent():
    return SimpleNamespace(
        strategy="tier2_swing",
        symbol="SPY",
        action="buy",
        quantity=1,
        order_value=500.0,
        signal_timestamp=datetime(2026, 8, 10, 16, 0, tzinfo=timezone.utc),
        config_version="v3",
    )


def test_deterministic_intent_id_is_stable_within_window():
    first = module.deterministic_intent_id(make_intent(), window_seconds=60)
    second = module.deterministic_intent_id(make_intent(), window_seconds=60)
    assert first == second
    assert first.startswith("tier2_swing-")


def test_duplicate_reservation_never_calls_broker():
    store = FakeStore(reserve=False)
    executor = FakeExecutor()
    result = module.OrderLifecycleCoordinator(store, executor).execute(make_intent(), "intent-1")
    assert result.status == "duplicate_blocked"
    assert executor.execute_calls == []


def test_preflight_rejection_is_definitive_and_does_not_reconcile():
    execution = importlib.import_module("utils.execution")
    store = FakeStore()
    executor = FakeExecutor(error=execution.OrderPreflightError("quote moved"))
    with pytest.raises(execution.OrderPreflightError, match="quote moved"):
        module.OrderLifecycleCoordinator(store, executor).execute(make_intent(), "intent-preflight")
    assert store.updates[-1][1] == "rejected_preflight"


def test_ambiguous_submission_recovers_by_client_order_id():
    execution = importlib.import_module("utils.execution")
    store = FakeStore()
    executor = FakeExecutor(
        error=execution.OrderExecutionError("timeout"),
        recovered={"id": "broker-1", "status": "accepted"},
    )
    result = module.OrderLifecycleCoordinator(store, executor).execute(make_intent(), "intent-1")
    assert result.status == "recovered_submitted"
    assert any(status == "accepted" for _, status, _ in store.updates)


def test_unresolved_submission_blocks_further_operation():
    execution = importlib.import_module("utils.execution")
    store = FakeStore()
    executor = FakeExecutor(error=execution.OrderExecutionError("timeout"), recovered=None)
    with pytest.raises(module.OrderReconciliationRequired):
        module.OrderLifecycleCoordinator(store, executor).execute(make_intent(), "intent-1")
    assert store.updates[-1][1] == "unknown_requires_reconciliation"


def test_startup_blocks_on_unresolved_intent():
    store = FakeStore(unfinished=[{"intent_id": "intent-lost"}])
    executor = FakeExecutor(recovered=None)
    with pytest.raises(module.OrderReconciliationRequired):
        module.OrderLifecycleCoordinator(store, executor).reconcile_startup()


def test_startup_preserves_partially_filled_broker_state():
    store = FakeStore(unfinished=[{"intent_id": "intent-partial"}])
    executor = FakeExecutor(recovered={"id": "broker-2", "status": "partially_filled", "filled_qty": "1"})
    module.OrderLifecycleCoordinator(store, executor).reconcile_startup()
    assert store.updates[-1][1] == "partially_filled"


def test_reconciliation_tracks_filled_bracket_while_stop_is_active():
    store = FakeStore(unfinished=[{
        "intent_id": "intent-protected",
        "request_payload": {"order_class": "bracket"},
    }])
    executor = FakeExecutor(recovered={
        "id": "broker-3", "status": "filled",
        "legs": [{"type": "limit", "status": "new"}, {"type": "stop", "status": "new"}],
    })
    module.OrderLifecycleCoordinator(store, executor).reconcile_once()
    assert store.updates[-1][1] == "filled_protected"


def test_reconciliation_blocks_unprotected_filled_bracket():
    store = FakeStore(unfinished=[{
        "intent_id": "intent-unprotected",
        "symbol": "SPY",
        "request_payload": {"order_class": "bracket"},
    }])
    executor = FakeExecutor(position={"symbol": "SPY"}, recovered={
        "id": "broker-4", "status": "filled",
        "legs": [{"type": "limit", "status": "expired"}, {"type": "stop", "status": "canceled"}],
    })
    with pytest.raises(module.OrderReconciliationRequired, match="unprotected"):
        module.OrderLifecycleCoordinator(store, executor).reconcile_once()
    assert store.updates[-1][1] == "unprotected_filled"


def test_reconciliation_releases_old_bracket_after_position_is_closed():
    store = FakeStore(unfinished=[{
        "intent_id": "intent-flat",
        "symbol": "SPY",
        "request_payload": {"order_class": "bracket"},
    }])
    executor = FakeExecutor(position=None, recovered={
        "id": "broker-flat", "status": "filled",
        "legs": [{"type": "limit", "status": "expired"}, {"type": "stop", "status": "canceled"}],
    })
    module.OrderLifecycleCoordinator(store, executor).reconcile_once()
    assert store.updates[-1][1] == "closed_reconciled"


def test_reconciliation_closes_bracket_when_an_exit_leg_fills():
    store = FakeStore(unfinished=[{
        "intent_id": "intent-closed",
        "request_payload": {"order_class": "bracket"},
    }])
    executor = FakeExecutor(recovered={
        "id": "broker-5", "status": "filled",
        "legs": [{"type": "limit", "status": "filled"}, {"type": "stop", "status": "canceled"}],
    })
    module.OrderLifecycleCoordinator(store, executor).reconcile_once()
    assert store.updates[-1][1] == "closed"
