from pathlib import Path
import sys


SERVICE_DIR = Path(__file__).resolve().parents[1] / "services" / "strategy-engine"
sys.path.insert(0, str(SERVICE_DIR))

from strategies.base import TradeIntent
from strategies.opening_swing import OpeningThenSwingStrategy
from utils.broker_state import BrokerPosition, BrokerState


class StubData:
    def __init__(self, source):
        self.observations = [{"source": source}]


class StubStrategy:
    def __init__(self, intent, source):
        self.intent = intent
        self.source = source
        self.data = StubData(source)

    def run_cycle(self, _broker_state=None):
        self.data.observations.append({"source": self.source})
        return self.intent

    def owned_position_symbols(self, _broker_state=None):
        return {"ANF"}


def intent(strategy, action):
    return TradeIntent(strategy, "ANF", action, 0.25 if action == "buy" else 0, 35 if action == "buy" else 0, 500)


def test_opening_signal_has_priority_and_preserves_its_provenance():
    combined = object.__new__(OpeningThenSwingStrategy)
    combined.opening = StubStrategy(intent("tier4_opening", "buy"), "opening")
    combined.swing = StubStrategy(intent("tier2_swing", "buy"), "swing")
    combined.data = type("Buffer", (), {"observations": []})()
    result = combined.run_cycle()
    assert result.strategy == "tier4_opening"
    assert combined.data.observations == [{"source": "opening"}]


def test_swing_runs_when_opening_has_no_actionable_signal():
    combined = object.__new__(OpeningThenSwingStrategy)
    combined.opening = StubStrategy(intent("tier4_opening", "hold"), "opening")
    combined.swing = StubStrategy(intent("tier2_swing", "buy"), "swing")
    combined.data = type("Buffer", (), {"observations": []})()
    result = combined.run_cycle()
    assert result.strategy == "tier2_swing"
    assert combined.data.observations == [{"source": "swing"}]


def test_swing_never_receives_opening_owned_positions():
    opening = StubStrategy(intent("tier4_opening", "hold"), "opening")

    class CapturingSwing(StubStrategy):
        received = None

        def run_cycle(self, broker_state=None):
            self.received = broker_state
            return super().run_cycle(broker_state)

    swing = CapturingSwing(intent("tier2_swing", "hold"), "swing")
    combined = object.__new__(OpeningThenSwingStrategy)
    combined.opening = opening
    combined.swing = swing
    combined.data = type("Buffer", (), {"observations": []})()
    state = BrokerState(
        timestamp="now", account_id="a", account_nav=500, buying_power=300,
        trading_blocked=False,
        positions={
            "ANF": BrokerPosition("ANF", 0.5, 75, 145, 150),
            "AAPL": BrokerPosition("AAPL", 0.2, 60, 300, 310),
        },
    )
    combined.run_cycle(state)
    assert set(swing.received.positions) == {"AAPL"}
