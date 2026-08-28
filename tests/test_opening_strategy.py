import json
import os
import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import patch


SERVICE_DIR = Path(__file__).resolve().parents[1] / "services" / "strategy-engine"
sys.path.insert(0, str(SERVICE_DIR))

from strategies.tier4_opening import OpeningOpportunityStrategy


class FakeState:
    account_nav = 100_000.0
    cash = 100_000.0
    positions = {}

    @staticmethod
    def get_position_market_value(_symbol):
        return 0.0


class FakeData:
    observations = []

    def get_index_level(self, _symbol):
        return 103.0

    def get_atr(self, _symbol):
        return 2.0

    def get_latest_price(self, _symbol):
        return 103.0

    def get_opening_range(self, _symbol, _session, _minutes):
        return {
            "open": 100.0, "high": 102.0, "low": 99.0, "last": 103.0,
            "vwap": 101.0, "volume": 100000, "bar_count": 16,
            "last_timestamp": "2026-08-13T09:46:00-04:00",
        }


def test_opening_strategy_requires_post_bell_confirmation(tmp_path):
    artifact = tmp_path / "premarket.json"
    artifact.write_text(json.dumps({
        "status": "passed",
        "session_date": "2026-08-13",
        "lanes": {"long": [{"symbol": "MOVE", "price": 102.0}]},
    }))
    with patch.dict(os.environ, {
        "MARKET_DATA_PROVIDER": "mock",
        "PREMARKET_DISCOVERY_OUTPUT_PATH": str(artifact),
        "OPENING_MAX_NOTIONAL": "1000",
    }):
        strategy = OpeningOpportunityStrategy()
    artifact.write_text(json.dumps({
        "status": "passed", "kind": "opening_research",
        "session_date": "2026-08-13", "generated_at": "2026-08-13T09:20:00-04:00",
        "config_hash": strategy.discovery_config_hash,
        "lanes": {"long": [{"symbol": "MOVE", "price": 102.0}]},
    }))
    strategy.data = FakeData()
    fixed = datetime.fromisoformat("2026-08-13T09:46:30-04:00")
    with patch("strategies.tier4_opening.current_time_et", return_value=fixed):
        intent = strategy.run_cycle(FakeState())
    assert intent.action == "buy"
    assert intent.symbol == "MOVE"
    assert intent.order_value <= 1000


def test_opening_strategy_sizes_fractionally_with_small_live_bankroll(tmp_path):
    artifact = tmp_path / "premarket.json"
    with patch.dict(os.environ, {
        "MARKET_DATA_PROVIDER": "mock",
        "PREMARKET_DISCOVERY_OUTPUT_PATH": str(artifact),
        "OPENING_MAX_NOTIONAL": "250",
        "MAX_ORDER_QUANTITY": "1",
        "MAX_CONCENTRATION_PCT": "0.15",
        "MIN_CASH_BUFFER_PCT": "0.10",
        "MAX_TRADE_RISK_PCT": "0.01",
    }):
        strategy = OpeningOpportunityStrategy()
    artifact.write_text(json.dumps({
        "status": "passed", "kind": "opening_research",
        "session_date": "2026-08-13", "generated_at": "2026-08-13T09:35:00-04:00",
        "config_hash": strategy.discovery_config_hash,
        "lanes": {"long": [{"symbol": "ANF", "price": 140.0}]},
    }))

    class SmallState(FakeState):
        account_nav = 508.0
        cash = 432.0

    class AnfData(FakeData):
        def get_opening_range(self, *_args):
            return {"open": 137, "high": 140, "low": 136, "last": 141,
                    "vwap": 139, "volume": 100000, "bar_count": 16,
                    "last_timestamp": "2026-08-13T09:46:00-04:00"}

    strategy.data = AnfData()
    fixed = datetime.fromisoformat("2026-08-13T09:46:30-04:00")
    with patch("strategies.tier4_opening.current_time_et", return_value=fixed):
        intent = strategy.run_cycle(SmallState())
    assert intent.action == "buy"
    assert 0 < intent.quantity < 1
    assert intent.order_value <= SmallState.account_nav * 0.15


def test_blocked_intent_does_not_suppress_a_later_opening_retry(tmp_path):
    artifact = tmp_path / "premarket.json"
    with patch.dict(os.environ, {
        "MARKET_DATA_PROVIDER": "mock",
        "PREMARKET_DISCOVERY_OUTPUT_PATH": str(artifact),
        "OPENING_MAX_NOTIONAL": "250",
    }):
        strategy = OpeningOpportunityStrategy()
    artifact.write_text(json.dumps({
        "status": "passed", "kind": "opening_research",
        "session_date": "2026-08-13", "generated_at": "2026-08-13T09:35:00-04:00",
        "config_hash": strategy.discovery_config_hash,
        "lanes": {"long": [{"symbol": "MOVE", "price": 102.0}]},
    }))
    strategy.data = FakeData()
    fixed = datetime.fromisoformat("2026-08-13T09:46:30-04:00")
    with patch("strategies.tier4_opening.current_time_et", return_value=fixed):
        first = strategy.run_cycle(FakeState())
        retry = strategy.run_cycle(FakeState())
    assert first.action == "buy"
    assert retry.action == "buy"


def test_opening_strategy_enforces_persisted_target_outside_entry_window(tmp_path):
    with patch.dict(os.environ, {"MARKET_DATA_PROVIDER": "mock"}):
        strategy = OpeningOpportunityStrategy()

    class OwnedStore:
        @staticmethod
        def list_fractional_protections():
            return [{
                "symbol": "ANF",
                "entry_intent_id": "tier4_opening-owned",
                "stop_price": 138.19,
                "take_profit_price": 156.83,
            }]

    class TargetData(FakeData):
        def get_latest_price(self, _symbol):
            return 157.0

    class Position:
        qty = 0.5283
        market_value = 82.94

    class OwnedState(FakeState):
        positions = {"ANF": Position()}

    strategy.store = OwnedStore()
    strategy.data = TargetData()
    intent = strategy.run_cycle(OwnedState())
    assert intent.action == "sell"
    assert intent.strategy == "tier4_opening"
    assert intent.symbol == "ANF"
    assert intent.quantity == 0.5283


def test_opening_strategy_does_not_duplicate_broker_stop(tmp_path):
    with patch.dict(os.environ, {"MARKET_DATA_PROVIDER": "mock"}):
        strategy = OpeningOpportunityStrategy()

    class OwnedStore:
        @staticmethod
        def list_fractional_protections():
            return [{
                "symbol": "ANF", "entry_intent_id": "tier4_opening-owned",
                "stop_price": 138.19, "take_profit_price": 156.83,
            }]

    class StopData(FakeData):
        def get_latest_price(self, _symbol):
            return 138.0

    class Position:
        qty = 0.5283
        market_value = 72.91

    class OwnedState(FakeState):
        positions = {"ANF": Position()}

    strategy.store = OwnedStore()
    strategy.data = StopData()
    intent = strategy.run_cycle(OwnedState())
    assert intent.action == "hold"
    assert "awaiting broker execution" in intent.notes


def test_opening_strategy_never_uses_short_research_lane(tmp_path):
    artifact = tmp_path / "premarket.json"
    artifact.write_text(json.dumps({
        "status": "passed",
        "session_date": "2026-08-13",
        "lanes": {"long": [], "short_research_only": [{"symbol": "DROP", "price": 90.0}]},
    }))
    with patch.dict(os.environ, {
        "MARKET_DATA_PROVIDER": "mock",
        "PREMARKET_DISCOVERY_OUTPUT_PATH": str(artifact),
    }):
        strategy = OpeningOpportunityStrategy()
    artifact.write_text(json.dumps({
        "status": "passed", "kind": "opening_research",
        "session_date": "2026-08-13", "generated_at": "2026-08-13T09:20:00-04:00",
        "config_hash": strategy.discovery_config_hash,
        "lanes": {"long": [], "short_research_only": [{"symbol": "DROP", "price": 90.0}]},
    }))
    strategy.data = FakeData()
    fixed = datetime.fromisoformat("2026-08-13T09:46:30-04:00")
    with patch("strategies.tier4_opening.current_time_et", return_value=fixed):
        intent = strategy.run_cycle(FakeState())
    assert intent.action == "hold"


def test_short_requires_both_isolated_lane_and_explicit_flag(tmp_path):
    artifact = tmp_path / "premarket.json"
    with patch.dict(os.environ, {
        "MARKET_DATA_PROVIDER": "mock", "PREMARKET_DISCOVERY_OUTPUT_PATH": str(artifact),
        "TRADING_LANE": "stock_short_paper", "OPENING_SHORTS_ENABLED": "true",
    }):
        strategy = OpeningOpportunityStrategy()
    artifact.write_text(json.dumps({
        "status": "passed", "kind": "opening_research", "session_date": "2026-08-13",
        "generated_at": "2026-08-13T09:31:00-04:00", "config_hash": strategy.discovery_config_hash,
        "lanes": {"long": [], "short_research_only": [{"symbol": "DROP", "price": 95.0}]},
    }))

    class ShortData(FakeData):
        def get_opening_range(self, *_args):
            return {"open": 100, "high": 101, "low": 98, "last": 97, "vwap": 99,
                    "volume": 100000, "bar_count": 16,
                    "last_timestamp": "2026-08-13T09:46:00-04:00"}

    strategy.data = ShortData()
    with patch("strategies.tier4_opening.current_time_et", return_value=datetime.fromisoformat("2026-08-13T09:46:30-04:00")):
        intent = strategy.run_cycle(FakeState())
    assert intent.action == "sell_short"
    assert intent.stop_loss_price > intent.reference_price
    assert intent.take_profit_price < intent.reference_price
