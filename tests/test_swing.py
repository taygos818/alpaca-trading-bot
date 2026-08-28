import os
import sys
import unittest
from unittest.mock import patch
from pathlib import Path

# Add strategy-engine to path
SERVICE_DIR = Path(__file__).resolve().parents[1] / "services" / "strategy-engine"
if str(SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(SERVICE_DIR))

from strategies.tier2_swing import SwingStrategy
from utils.broker_state import BrokerPosition


class FakeBrokerState:
    def __init__(self, nav, positions, cash=None):
        self.account_nav = nav
        self.positions = positions
        self.cash = nav if cash is None else cash

    def get_position_market_value(self, symbol):
        pos = self.positions.get(symbol.upper())
        return pos.market_value if pos else 0.0


class FakeMarketSnapshotProvider:
    def __init__(self, index_level=100.0, ema_crossover=True, rsi=10.0, atr=2.0):
        self.index_level = index_level
        self.ema_crossover = ema_crossover
        self.rsi = rsi
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
        return 0.0

    def get_atr(self, symbol: str, period: int = 14) -> float:
        return self.atr


class SwingStrategyTests(unittest.TestCase):
    def test_dynamic_discovery_fails_closed_when_shortlist_is_missing(self):
        with patch.dict(os.environ, {"DISCOVERY_MODE": "dynamic", "DISCOVERY_OUTPUT_PATH": "/tmp/definitely-missing-shortlist.json"}):
            strategy = SwingStrategy()
        strategy.data = FakeMarketSnapshotProvider()
        intent = strategy.run_cycle(FakeBrokerState(nav=100000.0, positions={}))
        self.assertEqual(intent.action, "hold")
        self.assertIn("entries blocked", intent.notes)

    def test_swing_strategy_defaults(self):
        strategy = SwingStrategy()
        self.assertEqual(strategy.name, "tier2_swing")
        self.assertEqual(strategy.ema_fast, 9)
        self.assertEqual(strategy.ema_slow, 21)
        self.assertEqual(strategy.rsi_period, 2)
        self.assertEqual(strategy.rsi_entry_threshold, 15.0)

    def test_swing_strategy_buy_signal_with_sizing(self):
        # Setup: No position, EMA Crossover is True, RSI is 10 (oversold)
        broker_state = FakeBrokerState(nav=100000.0, positions={})
        strategy = SwingStrategy()
        strategy.max_trade_risk_pct = 0.01
        strategy.max_concentration_pct = 0.15
        strategy.watchlist = ["AAPL"]
        strategy.data = FakeMarketSnapshotProvider(index_level=150.0, ema_crossover=True, rsi=10.0, atr=3.0)

        intent = strategy.run_cycle(broker_state)

        # Sizing calculations:
        # Target risk USD = 100000.0 * 0.01 = 1000.0
        # Risk per share = 2.0 (atr_stop_multiple) * 3.0 (atr) = 6.0
        # Continuous risk sizing is capped by the default $10,000 order ceiling.
        self.assertEqual(intent.action, "buy")
        self.assertEqual(intent.symbol, "AAPL")
        self.assertEqual(intent.quantity, 66.6666)
        self.assertAlmostEqual(intent.order_value, 150.0 * 66.6666)
        self.assertAlmostEqual(intent.estimated_risk_value, 6.0 * 66.6666)
        self.assertTrue("Swing BUY entry" in intent.notes or "Market Scan #1 Opportunity" in intent.notes)

    def test_swing_strategy_buy_signal_capped_by_concentration(self):
        # Setup: NAV is small, concentration limit should cap the order size
        broker_state = FakeBrokerState(nav=10000.0, positions={})
        strategy = SwingStrategy()
        strategy.max_trade_risk_pct = 0.01
        strategy.max_concentration_pct = 0.15
        strategy.watchlist = ["AAPL"]
        # High ATR (20.0), stock price 100.0
        strategy.data = FakeMarketSnapshotProvider(index_level=100.0, ema_crossover=True, rsi=10.0, atr=20.0)

        intent = strategy.run_cycle(broker_state)

        # Sizing calculations:
        # Target risk USD = 10000.0 * 0.01 = 100.0
        # Risk per share = 2.0 * 20.0 = 40.0
        # Continuous qty by risk = 100.0 / 40.0 = 2.5 shares.
        # Concentration limit = 10000.0 * 0.15 = 1500.0
        # Qty is 2.5 (less than concentration limit of 15 shares)
        self.assertEqual(intent.action, "buy")
        self.assertEqual(intent.quantity, 2.5)

        # Now test with very low ATR (0.1), which would result in large qty by risk:
        strategy.data = FakeMarketSnapshotProvider(index_level=100.0, ema_crossover=True, rsi=10.0, atr=0.1)
        # Target risk USD = 100.0, Risk per share = 2.0 * 0.1 = 0.2
        # Qty by risk = 100.0 // 0.2 = 500 shares (order value = 50000.0)
        # Concentration limit = 10000.0 * 0.15 = 1500.0 -> max qty by concentration = 15 shares
        intent_capped = strategy.run_cycle(broker_state)
        self.assertEqual(intent_capped.action, "buy")
        self.assertEqual(intent_capped.quantity, 15)  # capped by concentration

    def test_small_account_sizes_fractionally_by_bankroll_limits(self):
        broker_state = FakeBrokerState(nav=508.33, cash=508.33, positions={})
        strategy = SwingStrategy()
        strategy.max_trade_risk_pct = 0.01
        strategy.max_concentration_pct = 0.15
        strategy.max_order_quantity = 1.0
        strategy.max_order_notional = 250.0
        strategy.cash_buffer_pct = 0.10
        strategy.watchlist = ["EXPENSIVE"]
        strategy.data = FakeMarketSnapshotProvider(index_level=400.0, ema_crossover=True, rsi=10.0, atr=8.0)

        intent = strategy.run_cycle(broker_state)

        self.assertEqual(intent.action, "buy")
        self.assertEqual(intent.quantity, 0.1906)
        self.assertLessEqual(intent.quantity, 1.0)
        self.assertLessEqual(intent.order_value, broker_state.account_nav * 0.15)
        self.assertLessEqual(intent.estimated_risk_value, broker_state.account_nav * 0.01)

    def test_fractional_position_exit_does_not_truncate_quantity(self):
        broker_state = FakeBrokerState(
            nav=508.33,
            positions={
                "AAPL": BrokerPosition(
                    symbol="AAPL", qty=0.2375, avg_entry_price=100.0,
                    current_price=89.0, market_value=21.1375,
                )
            },
        )
        strategy = SwingStrategy()
        strategy.watchlist = ["AAPL"]
        strategy.data = FakeMarketSnapshotProvider(index_level=89.0, ema_crossover=False, rsi=50.0, atr=5.0)

        intent = strategy.run_cycle(broker_state)

        self.assertEqual(intent.action, "sell")
        self.assertEqual(intent.quantity, 0.2375)

    def test_swing_strategy_holding_position_stop_loss_hit(self):
        # We hold stock at average entry 100.0. ATR is 5.0. Stop loss is 100.0 - 2.0 * 5.0 = 90.0
        # Current price drops to 89.0 -> should sell
        broker_state = FakeBrokerState(
            nav=100000.0,
            positions={
                "AAPL": BrokerPosition(symbol="AAPL", qty=50.0, avg_entry_price=100.0, current_price=89.0, market_value=4450.0)
            }
        )
        strategy = SwingStrategy()
        strategy.watchlist = ["AAPL"]
        strategy.data = FakeMarketSnapshotProvider(index_level=89.0, ema_crossover=True, rsi=50.0, atr=5.0)

        intent = strategy.run_cycle(broker_state)

        self.assertEqual(intent.action, "sell")
        self.assertEqual(intent.symbol, "AAPL")
        self.assertEqual(intent.quantity, 50)
        self.assertTrue("Stop loss hit" in intent.notes)

    def test_swing_strategy_holding_position_take_profit_hit(self):
        # We hold stock at average entry 100.0. ATR is 5.0. Take profit is 100.0 + 2.0 * 5.0 * 3.0 = 130.0
        # Current price rises to 131.0 -> should sell
        broker_state = FakeBrokerState(
            nav=100000.0,
            positions={
                "AAPL": BrokerPosition(symbol="AAPL", qty=50.0, avg_entry_price=100.0, current_price=131.0, market_value=6550.0)
            }
        )
        strategy = SwingStrategy()
        strategy.watchlist = ["AAPL"]
        strategy.data = FakeMarketSnapshotProvider(index_level=131.0, ema_crossover=False, rsi=80.0, atr=5.0)

        intent = strategy.run_cycle(broker_state)

        self.assertEqual(intent.action, "sell")
        self.assertEqual(intent.symbol, "AAPL")
        self.assertEqual(intent.quantity, 50)
        self.assertTrue("Take profit hit" in intent.notes)

    def test_swing_strategy_holding_position_no_exit(self):
        # Current price is 100.5 (between 90.0 and 130.0, not deep enough dip for DCA nor high enough profit for pyramid) -> should hold
        broker_state = FakeBrokerState(
            nav=100000.0,
            positions={
                "AAPL": BrokerPosition(symbol="AAPL", qty=50.0, avg_entry_price=100.0, current_price=100.5, market_value=5025.0)
            }
        )
        strategy = SwingStrategy()
        strategy.watchlist = ["AAPL"]
        strategy.data = FakeMarketSnapshotProvider(index_level=100.5, ema_crossover=True, rsi=10.0, atr=5.0)

        intent = strategy.run_cycle(broker_state)

        self.assertEqual(intent.action, "hold")
        self.assertEqual(intent.quantity, 0)
        self.assertTrue("Holding swing position" in intent.notes)

    def test_swing_strategy_dca_dip_buy(self):
        # Position held at 100.0. Current price drops to 97.0 (3% dip >= 2.5% DCA threshold) with oversold RSI
        broker_state = FakeBrokerState(
            nav=100000.0,
            positions={
                "AAPL": BrokerPosition(symbol="AAPL", qty=50.0, avg_entry_price=100.0, current_price=97.0, market_value=4850.0)
            }
        )
        strategy = SwingStrategy()
        strategy.max_trade_risk_pct = 0.01
        strategy.max_concentration_pct = 0.15
        strategy.add_cooldown_seconds = 60.0
        strategy.watchlist = ["AAPL"]
        strategy.data = FakeMarketSnapshotProvider(index_level=97.0, ema_crossover=True, rsi=10.0, atr=2.0)

        intent = strategy.run_cycle(broker_state)

        self.assertEqual(intent.action, "buy")
        self.assertEqual(intent.symbol, "AAPL")
        self.assertTrue("DCA Dip-Buy" in intent.notes)

    def test_swing_strategy_pyramid_momentum_add(self):
        # Position held at 100.0. Current price rises to 103.0 (3% profit >= 2.0% Pyramid threshold) with momentum setup
        broker_state = FakeBrokerState(
            nav=100000.0,
            positions={
                "AAPL": BrokerPosition(symbol="AAPL", qty=50.0, avg_entry_price=100.0, current_price=103.0, market_value=5150.0)
            }
        )
        strategy = SwingStrategy()
        strategy.max_trade_risk_pct = 0.01
        strategy.max_concentration_pct = 0.15
        strategy.add_cooldown_seconds = 60.0
        strategy.watchlist = ["AAPL"]
        strategy.data = FakeMarketSnapshotProvider(index_level=103.0, ema_crossover=True, rsi=10.0, atr=2.0)

        intent = strategy.run_cycle(broker_state)

        self.assertEqual(intent.action, "buy")
        self.assertEqual(intent.symbol, "AAPL")
        self.assertTrue("Pyramid Momentum Add" in intent.notes)

        # Immediate consecutive cycle should hit cooldown guard and return hold
        intent_cooldown = strategy.run_cycle(broker_state)
        self.assertEqual(intent_cooldown.action, "hold")

    def test_swing_strategy_stateless_fallback_loop(self):
        # No broker state, ENABLE_SAMPLE_SIGNALS=True -> should trigger sample buy signal
        os.environ["ENABLE_SAMPLE_SIGNALS"] = "true"
        try:
            strategy = SwingStrategy()
            strategy.watchlist = ["AAPL"]
            strategy.data = FakeMarketSnapshotProvider(index_level=100.0, ema_crossover=True, rsi=10.0, atr=5.0)
            intent = strategy.run_cycle(None)
            self.assertEqual(intent.action, "buy")
            self.assertEqual(intent.quantity, 10)
        finally:
            os.environ.pop("ENABLE_SAMPLE_SIGNALS", None)


if __name__ == "__main__":
    unittest.main()
