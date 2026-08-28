import datetime
import os
import sys
import unittest
from pathlib import Path

# Add strategy-engine to path
SERVICE_DIR = Path(__file__).resolve().parents[1] / "services" / "strategy-engine"
if str(SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(SERVICE_DIR))

from strategies.tier1_wheel import (
    WheelStrategy,
    parse_occ_symbol,
    calculate_delta,
    normal_cdf,
)
from utils.broker_state import BrokerState, BrokerPosition


class FakeBrokerState:
    def __init__(self, nav, positions):
        self.account_nav = nav
        self.positions = positions

    def get_position_market_value(self, symbol):
        pos = self.positions.get(symbol.upper())
        return pos.market_value if pos else 0.0


class WheelStrategyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._prev_provider = os.environ.get("MARKET_DATA_PROVIDER")
        os.environ["MARKET_DATA_PROVIDER"] = "mock"

    @classmethod
    def tearDownClass(cls):
        if cls._prev_provider is None:
            os.environ.pop("MARKET_DATA_PROVIDER", None)
        else:
            os.environ["MARKET_DATA_PROVIDER"] = cls._prev_provider

    def test_parse_occ_symbol(self):
        exp, opt_type, strike = parse_occ_symbol("SPY260609P00250000", "SPY")
        self.assertEqual(exp, datetime.date(2026, 6, 9))
        self.assertEqual(opt_type, "put")
        self.assertAlmostEqual(strike, 250.0)

        exp2, opt_type2, strike2 = parse_occ_symbol("AAPL261219C00150000", "AAPL")
        self.assertEqual(exp2, datetime.date(2026, 12, 19))
        self.assertEqual(opt_type2, "call")
        self.assertAlmostEqual(strike2, 150.0)

    def test_calculate_delta(self):
        # Stock=100, Strike=100, DTE=30 days (t=30/365), r=4.5%, vol=25%
        t = 30.0 / 365.0
        c_delta = calculate_delta(100.0, 100.0, t, 0.045, 0.25, "call")
        p_delta = calculate_delta(100.0, 100.0, t, 0.045, 0.25, "put")
        
        self.assertTrue(0.0 < c_delta < 1.0)
        self.assertTrue(-1.0 < p_delta < 0.0)
        self.assertAlmostEqual(c_delta - p_delta, 1.0, places=4)

    def test_wheel_strategy_opens_put_when_no_positions(self):
        # Empty portfolio state -> should sell Out-of-The-Money CSP (Put)
        broker_state = FakeBrokerState(
            nav=100000.0,
            positions={}
        )
        strategy = WheelStrategy()
        # Mock providers are used by default because MARKET_DATA_PROVIDER is mock/hybrid in tests
        intent = strategy.run_cycle(broker_state)
        
        self.assertEqual(intent.action, "sell_to_open")
        self.assertTrue("P" in intent.symbol) # OCC symbol for Put
        self.assertEqual(intent.quantity, 1)

    def test_wheel_strategy_opens_call_when_owning_stock(self):
        # Assigned portfolio state (has 100 shares of SPY) -> should sell Covered Call
        broker_state = FakeBrokerState(
            nav=100000.0,
            positions={
                "SPY": BrokerPosition(symbol="SPY", qty=100.0, market_value=50000.0)
            }
        )
        strategy = WheelStrategy()
        strategy.watchlist = ["SPY"] # force target symbol to be SPY
        intent = strategy.run_cycle(broker_state)
        
        self.assertEqual(intent.action, "sell_to_open")
        self.assertTrue("C" in intent.symbol) # OCC symbol for Call
        self.assertEqual(intent.quantity, 1)

    def test_wheel_strategy_manages_active_option_positions(self):
        # Portfolio contains active short Put that has lost 80% premium (take profit)
        exp_date = (datetime.date.today() + datetime.timedelta(days=35)).strftime("%y%m%d")
        opt_symbol = f"SPY{exp_date}P00250000"
        
        broker_state = FakeBrokerState(
            nav=100000.0,
            positions={
                opt_symbol: BrokerPosition(
                    symbol=opt_symbol,
                    qty=-1.0,
                    market_value=-100.0,
                    avg_entry_price=6.00,
                    current_price=1.00,
                )
            }
        )
        
        strategy = WheelStrategy()
        strategy.watchlist = ["SPY"]
        intent = strategy.run_cycle(broker_state)
        
        self.assertEqual(intent.action, "buy_to_close")
        self.assertEqual(intent.symbol, opt_symbol)
        self.assertEqual(intent.quantity, 1)
        self.assertTrue("Profit target reached" in intent.notes)


if __name__ == "__main__":
    unittest.main()
