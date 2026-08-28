import datetime
import os
import sys
import unittest
from pathlib import Path

# Add strategy-engine to path
SERVICE_DIR = Path(__file__).resolve().parents[1] / "services" / "strategy-engine"
if str(SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(SERVICE_DIR))

from strategies.tier3_intraday import IntradayStrategy
from utils.broker_state import BrokerPosition


class FakeBrokerState:
    def __init__(self, nav, positions, timestamp=None):
        self.account_nav = nav
        self.positions = positions
        self.timestamp = timestamp

    def get_position_market_value(self, symbol):
        pos = self.positions.get(symbol.upper())
        return pos.market_value if pos else 0.0


class FakeMarketSnapshotProvider:
    def __init__(self, index_level=100.0, ema_crossover=True, rsi=45.0, roc=1.0, atr=2.0):
        self.index_level = index_level
        self.ema_crossover = ema_crossover
        self.rsi = rsi
        self.roc = roc
        self.atr = atr

    def get_index_level(self, symbol: str) -> float:
        return self.index_level

    def get_iv_rank(self, symbol: str) -> float:
        return 20.0

    def get_ema_crossover(self, symbol: str, fast_period: int | None = None, slow_period: int | None = None) -> bool:
        return self.ema_crossover

    def get_rsi(self, symbol: str, period: int | None = None) -> float:
        return self.rsi

    def get_intraday_roc(self, symbol: str) -> float:
        return self.roc

    def get_atr(self, symbol: str, period: int = 14) -> float:
        return self.atr


class IntradayStrategyTests(unittest.TestCase):
    def test_intraday_strategy_defaults(self):
        strategy = IntradayStrategy()
        self.assertEqual(strategy.name, "tier3_intraday")
        self.assertEqual(strategy.roc_threshold, 0.5)
        self.assertEqual(strategy.rsi_period, 14)
        self.assertEqual(strategy.rsi_entry_threshold, 70.0)
        self.assertEqual(strategy.atr_stop_multiple, 1.5)
        self.assertEqual(strategy.risk_reward_ratio, 2.0)
        self.assertEqual(strategy.flatten_hour_et, 15)
        self.assertEqual(strategy.flatten_minute_et, 50)

    def test_intraday_strategy_buy_signal_with_sizing(self):
        # Setup: No position, ROC is 1.0 (exceeds 0.5), EMA is True, RSI is 45 (below 70), current price 100.0, ATR is 2.0
        # Time is 10:00 AM ET (before flatten time)
        broker_state = FakeBrokerState(
            nav=100000.0,
            positions={},
            timestamp="2026-06-10T10:00:00Z"
        )
        strategy = IntradayStrategy()
        strategy.max_trade_risk_pct = 0.01
        strategy.max_concentration_pct = 0.15
        strategy.watchlist = ["AAPL"]
        strategy.data = FakeMarketSnapshotProvider(index_level=100.0, ema_crossover=True, rsi=45.0, roc=1.0, atr=2.0)

        intent = strategy.run_cycle(broker_state)

        # Sizing calculations:
        # Target risk USD = 100000.0 * 0.01 = 1000.0
        # Risk per share = 1.5 (atr_stop_multiple) * 2.0 (atr) = 3.0
        # Qty by risk = 1000.0 // 3.0 = 333 shares
        # Concentration cap = 100000.0 * 0.15 = 15000.0 USD
        # Qty by concentration = 15000 // 100.0 = 150 shares
        # Final qty = min(333, 150) = 150
        self.assertEqual(intent.action, "buy")
        self.assertEqual(intent.symbol, "AAPL")
        self.assertEqual(intent.quantity, 150)
        self.assertAlmostEqual(intent.order_value, 15000.0)
        self.assertAlmostEqual(intent.estimated_risk_value, 3.0 * 150)
        self.assertTrue("Intraday BUY entry" in intent.notes)

    def test_intraday_strategy_holding_position_no_exit_before_flatten(self):
        # Setup: We hold AAPL at avg entry 100.0. ATR is 2.0. Stop loss is 100.0 - 1.5 * 2.0 = 97.0. Take profit is 100 + 1.5 * 2.0 * 2.0 = 106.0
        # Current price is 102.0 (no exit triggered). Time is 11:00 AM (before flatten)
        broker_state = FakeBrokerState(
            nav=100000.0,
            positions={
                "AAPL": BrokerPosition(symbol="AAPL", qty=100.0, avg_entry_price=100.0, current_price=102.0, market_value=10200.0)
            },
            timestamp="2026-06-10T11:00:00Z"
        )
        strategy = IntradayStrategy()
        strategy.watchlist = ["AAPL"]
        strategy.data = FakeMarketSnapshotProvider(index_level=102.0, atr=2.0)

        intent = strategy.run_cycle(broker_state)

        self.assertEqual(intent.action, "hold")
        self.assertEqual(intent.quantity, 0)
        self.assertTrue("Holding intraday position" in intent.notes)

    def test_intraday_strategy_holding_position_stop_loss_hit(self):
        # Current price is 96.0 (below stop 97.0). Time is 11:00 AM
        broker_state = FakeBrokerState(
            nav=100000.0,
            positions={
                "AAPL": BrokerPosition(symbol="AAPL", qty=100.0, avg_entry_price=100.0, current_price=96.0, market_value=9600.0)
            },
            timestamp="2026-06-10T11:00:00Z"
        )
        strategy = IntradayStrategy()
        strategy.watchlist = ["AAPL"]
        strategy.data = FakeMarketSnapshotProvider(index_level=96.0, atr=2.0)

        intent = strategy.run_cycle(broker_state)

        self.assertEqual(intent.action, "sell")
        self.assertEqual(intent.quantity, 100)
        self.assertTrue("Stop loss hit" in intent.notes)

    def test_intraday_strategy_holding_position_take_profit_hit(self):
        # Current price is 107.0 (above target 106.0). Time is 11:00 AM
        broker_state = FakeBrokerState(
            nav=100000.0,
            positions={
                "AAPL": BrokerPosition(symbol="AAPL", qty=100.0, avg_entry_price=100.0, current_price=107.0, market_value=10700.0)
            },
            timestamp="2026-06-10T11:00:00Z"
        )
        strategy = IntradayStrategy()
        strategy.watchlist = ["AAPL"]
        strategy.data = FakeMarketSnapshotProvider(index_level=107.0, atr=2.0)

        intent = strategy.run_cycle(broker_state)

        self.assertEqual(intent.action, "sell")
        self.assertEqual(intent.quantity, 100)
        self.assertTrue("Take profit hit" in intent.notes)

    def test_intraday_strategy_holding_position_eod_flatten(self):
        # Setup: We hold position. Time is 3:55 PM (15:55 ET), which is past flatten time 3:50 PM (15:50 ET)
        # Should return a sell/flatten intent regardless of stop/target
        broker_state = FakeBrokerState(
            nav=100000.0,
            positions={
                "AAPL": BrokerPosition(symbol="AAPL", qty=100.0, avg_entry_price=100.0, current_price=102.0, market_value=10200.0)
            },
            timestamp="2026-06-10T15:55:00-04:00"
        )
        strategy = IntradayStrategy()
        strategy.watchlist = ["AAPL"]
        strategy.data = FakeMarketSnapshotProvider(index_level=102.0, atr=2.0)

        intent = strategy.run_cycle(broker_state)

        self.assertEqual(intent.action, "sell")
        self.assertEqual(intent.quantity, 100)
        self.assertTrue("Intraday EOD FLATTEN" in intent.notes)


if __name__ == "__main__":
    unittest.main()
