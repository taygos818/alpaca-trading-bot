import datetime
import json
import sys
import unittest
from pathlib import Path

# Add root and strategy-engine to path
ROOT_DIR = Path(__file__).resolve().parents[1]
SERVICE_DIR = ROOT_DIR / "services" / "strategy-engine"
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
if str(SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(SERVICE_DIR))

from backtest.engine import (
    BacktestSimulator,
    HistoricalSnapshotProvider,
    calculate_bs_price,
    calculate_bs_delta,
    parse_occ_symbol,
    generate_occ_symbol
)
from strategies.base import BaseStrategy, TradeIntent


class DummyStrategy(BaseStrategy):
    name = "dummy_strat"
    
    def __init__(self):
        self.watchlist = ["AAPL"]
        self.cycle_count = 0
        
    def run_cycle(self, broker_state=None) -> TradeIntent:
        self.cycle_count += 1
        if self.cycle_count == 1:
            # First cycle: Buy 10 shares of AAPL
            return TradeIntent(
                strategy=self.name,
                symbol="AAPL",
                action="buy",
                quantity=10,
                order_value=1500.0,
                estimated_risk_value=300.0,
                current_position_value=0.0,
                account_nav=100000.0,
                notes="Dummy buy"
            )
        elif self.cycle_count == 2:
            # Second cycle: Sell 5 shares of AAPL
            return TradeIntent(
                strategy=self.name,
                symbol="AAPL",
                action="sell",
                quantity=5,
                order_value=750.0,
                estimated_risk_value=0.0,
                current_position_value=1500.0,
                account_nav=100000.0,
                notes="Dummy partial sell"
            )
        return TradeIntent(
            strategy=self.name,
            symbol="AAPL",
            action="hold",
            quantity=0,
            order_value=0.0,
            account_nav=100000.0
        )


class BacktestEngineTests(unittest.TestCase):
    def test_black_scholes_pricing(self):
        # Call pricing check: S=100, K=100, t=30/365, r=4.5%, vol=25%
        price_c = calculate_bs_price(100.0, 100.0, 30.0/365.0, 0.045, 0.25, "call")
        delta_c = calculate_bs_delta(100.0, 100.0, 30.0/365.0, 0.045, 0.25, "call")
        
        self.assertGreater(price_c, 0.0)
        self.assertTrue(0.0 < delta_c < 1.0)
        
        # Put pricing check
        price_p = calculate_bs_price(100.0, 100.0, 30.0/365.0, 0.045, 0.25, "put")
        delta_p = calculate_bs_delta(100.0, 100.0, 30.0/365.0, 0.045, 0.25, "put")
        
        self.assertGreater(price_p, 0.0)
        self.assertTrue(-1.0 < delta_p < 0.0)

    def test_occ_symbol_conversions(self):
        exp = datetime.date(2026, 6, 9)
        symbol = generate_occ_symbol("MSFT", exp, "put", 425.0)
        self.assertEqual(symbol, "MSFT260609P00425000")
        
        p_exp, p_otype, p_strike = parse_occ_symbol(symbol, "MSFT")
        self.assertEqual(p_exp, exp)
        self.assertEqual(p_otype, "put")
        self.assertEqual(p_strike, 425.0)

    def test_simulator_stock_execution(self):
        # Setup fake data: 3 days of AAPL prices
        history = {
            "AAPL": [
                {"t": "2026-01-02T00:00:00Z", "c": 150.0, "h": 151.0, "l": 149.0},
                {"t": "2026-01-05T00:00:00Z", "c": 160.0, "h": 161.0, "l": 159.0},
                {"t": "2026-01-06T00:00:00Z", "c": 155.0, "h": 156.0, "l": 154.0}
            ]
        }
        
        # Mock Path.exists to return True for our fake file and mock open
        original_exists = Path.exists
        original_open = open
        
        def fake_exists(path):
            if "AAPL_daily.json" in str(path) or "VIX_daily.json" in str(path):
                return True
            return original_exists(path)
            
        def fake_open(file, mode='r', *args, **kwargs):
            if "AAPL_daily.json" in str(file):
                class MockFile:
                    def read(self):
                        return json.dumps(history["AAPL"])
                    def __enter__(self):
                        return self
                    def __exit__(self, exc_type, exc_val, exc_tb):
                        pass
                return MockFile()
            if "VIX_daily.json" in str(file):
                class MockVixFile:
                    def read(self):
                        return json.dumps([])
                    def __enter__(self):
                        return self
                    def __exit__(self, exc_type, exc_val, exc_tb):
                        pass
                return MockVixFile()
            return original_open(file, mode, *args, **kwargs)

        import json
        from unittest.mock import patch
        
        sim = BacktestSimulator(initial_equity=100000.0)
        
        with patch.object(Path, "exists", new=fake_exists), \
             patch("builtins.open", new=fake_open):
            sim.run(DummyStrategy, ["AAPL"], datetime.date(2026, 1, 2), datetime.date(2026, 1, 6))

        # Check trades
        self.assertEqual(len(sim.trades), 2)
        
        # Trade 1: Buy 10 shares of AAPL @ 150.0
        t1 = sim.trades[0]
        self.assertEqual(t1["action"], "buy")
        self.assertEqual(t1["qty"], 10)
        self.assertEqual(t1["price"], 150.0)
        
        # Trade 2: Sell 5 shares of AAPL @ 160.0
        t2 = sim.trades[1]
        self.assertEqual(t2["action"], "sell")
        self.assertEqual(t2["qty"], 5)
        self.assertEqual(t2["price"], 160.0)

        # End state checks:
        # Cash starts at 100000.0
        # Day 1: Buy 10 AAPL @ 150 -> Cash = 100000 - 1500 = 98500. AAPL pos = 10. NAV = 98500 + 1500 = 100000.
        # Day 2: Sell 5 AAPL @ 160 -> Cash = 98500 + 800 = 99300. AAPL pos = 5. AAPL close = 160. NAV = 99300 + 800 = 100100.
        # Day 3: AAPL close = 155. AAPL pos = 5 (market value = 5 * 155 = 775). Cash = 99300. NAV = 99300 + 775 = 100075.
        metrics = sim.get_metrics()
        self.assertEqual(metrics["total_trades"], 2)
        self.assertAlmostEqual(metrics["ending_equity"], 100075.0)

    def test_simulator_parameter_sweeps(self):
        history = {
            "AAPL": [
                {"t": "2026-01-02T00:00:00Z", "c": 150.0, "h": 151.0, "l": 149.0},
                {"t": "2026-01-05T00:00:00Z", "c": 160.0, "h": 161.0, "l": 159.0},
                {"t": "2026-01-06T00:00:00Z", "c": 155.0, "h": 156.0, "l": 154.0}
            ]
        }
        
        class ParametricDummyStrategy(BaseStrategy):
            name = "parametric_dummy"
            def __init__(self):
                self.watchlist = ["AAPL"]
                self.param_val = float(os.getenv("DUMMY_PARAM", "10.0"))
                self.cycle_count = 0
            def run_cycle(self, broker_state=None) -> TradeIntent:
                self.cycle_count += 1
                if self.cycle_count == 1:
                    return TradeIntent(
                        strategy=self.name,
                        symbol="AAPL",
                        action="buy",
                        quantity=int(self.param_val),
                        order_value=150.0 * self.param_val,
                        estimated_risk_value=30.0 * self.param_val,
                        account_nav=100000.0
                    )
                return TradeIntent(strategy=self.name, symbol="AAPL", action="hold", quantity=0, order_value=0.0, account_nav=100000.0)

        original_exists = Path.exists
        original_open = open
        def fake_exists(path):
            if "AAPL_daily.json" in str(path) or "VIX_daily.json" in str(path):
                return True
            return original_exists(path)
        def fake_open(file, mode='r', *args, **kwargs):
            if "AAPL_daily.json" in str(file):
                class MockFile:
                    def read(self):
                        return json.dumps(history["AAPL"])
                    def __enter__(self):
                        return self
                    def __exit__(self, exc_type, exc_val, exc_tb):
                        pass
                return MockFile()
            if "VIX_daily.json" in str(file):
                class MockVixFile:
                    def read(self):
                        return json.dumps([])
                    def __enter__(self):
                        return self
                    def __exit__(self, exc_type, exc_val, exc_tb):
                        pass
                return MockVixFile()
            return original_open(file, mode, *args, **kwargs)

        from unittest.mock import patch
        import os

        os.environ["DUMMY_PARAM"] = "10"
        sim1 = BacktestSimulator(initial_equity=100000.0)
        with patch.object(Path, "exists", new=fake_exists), patch("builtins.open", new=fake_open):
            sim1.run(ParametricDummyStrategy, ["AAPL"], datetime.date(2026, 1, 2), datetime.date(2026, 1, 6))
        
        os.environ["DUMMY_PARAM"] = "5"
        sim2 = BacktestSimulator(initial_equity=100000.0)
        with patch.object(Path, "exists", new=fake_exists), patch("builtins.open", new=fake_open):
            sim2.run(ParametricDummyStrategy, ["AAPL"], datetime.date(2026, 1, 2), datetime.date(2026, 1, 6))
            
        os.environ.pop("DUMMY_PARAM", None)

        self.assertEqual(sim1.trades[0]["qty"], 10)
        self.assertEqual(sim2.trades[0]["qty"], 5)


if __name__ == "__main__":
    unittest.main()
