import importlib.util
import os
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

# Add service paths to sys.path
DASHBOARD_DIR = Path(__file__).resolve().parents[1] / "services" / "monitor-dash"
if str(DASHBOARD_DIR) not in sys.path:
    sys.path.insert(0, str(DASHBOARD_DIR))

# Dynamic imports
APP_SPEC = importlib.util.spec_from_file_location("app", DASHBOARD_DIR / "app.py")
app = importlib.util.module_from_spec(APP_SPEC)
assert APP_SPEC.loader is not None
APP_SPEC.loader.exec_module(app)
compute_fifo_trades = app.compute_fifo_trades


class PerformanceAnalysisTests(unittest.TestCase):
    def test_simple_long_trade(self):
        # 1. Buy 10 QQQ at $400 ($4,000 value)
        # 2. Sell 10 QQQ at $410 ($4,100 value)
        # Realized P&L = +$100
        trades = [
            {
                "timestamp": datetime(2026, 6, 11, 9, 30, tzinfo=timezone.utc),
                "strategy": "tier2_swing",
                "symbol": "QQQ",
                "action": "buy",
                "quantity": 10,
                "order_value": 4000.0,
            },
            {
                "timestamp": datetime(2026, 6, 11, 10, 0, tzinfo=timezone.utc),
                "strategy": "tier2_swing",
                "symbol": "QQQ",
                "action": "sell",
                "quantity": 10,
                "order_value": 4100.0,
            }
        ]

        matched = compute_fifo_trades(trades)
        self.assertEqual(len(matched), 1)
        
        trade = matched[0]
        self.assertEqual(trade["symbol"], "QQQ")
        self.assertEqual(trade["direction"], "Long")
        self.assertEqual(trade["quantity"], 10)
        self.assertEqual(trade["entry_price"], 400.0)
        self.assertEqual(trade["exit_price"], 410.0)
        self.assertEqual(trade["realized_pnl"], 100.0)

    def test_partial_exit_trade(self):
        # 1. Buy 10 MSFT at $350 ($3,500 value)
        # 2. Sell 4 MSFT at $360 ($1,440 value)
        # 3. Sell 6 MSFT at $340 ($2,040 value)
        trades = [
            {
                "timestamp": datetime(2026, 6, 11, 9, 30, tzinfo=timezone.utc),
                "strategy": "tier2_swing",
                "symbol": "MSFT",
                "action": "buy",
                "quantity": 10,
                "order_value": 3500.0,
            },
            {
                "timestamp": datetime(2026, 6, 11, 10, 0, tzinfo=timezone.utc),
                "strategy": "tier2_swing",
                "symbol": "MSFT",
                "action": "sell",
                "quantity": 4,
                "order_value": 1440.0,
            },
            {
                "timestamp": datetime(2026, 6, 11, 11, 0, tzinfo=timezone.utc),
                "strategy": "tier2_swing",
                "symbol": "MSFT",
                "action": "sell",
                "quantity": 6,
                "order_value": 2040.0,
            }
        ]

        matched = compute_fifo_trades(trades)
        self.assertEqual(len(matched), 2)

        # First matched chunk: 4 units
        t1 = matched[0]
        self.assertEqual(t1["quantity"], 4)
        self.assertEqual(t1["entry_price"], 350.0)
        self.assertEqual(t1["exit_price"], 360.0)
        self.assertEqual(t1["realized_pnl"], 40.0) # 4 * 10

        # Second matched chunk: 6 units
        t2 = matched[1]
        self.assertEqual(t2["quantity"], 6)
        self.assertEqual(t2["entry_price"], 350.0)
        self.assertEqual(t2["exit_price"], 340.0)
        self.assertEqual(t2["realized_pnl"], -60.0) # 6 * -10

    def test_simple_short_trade(self):
        # 1. Sell Short 5 SPY at $500 ($2,500 value)
        # 2. Buy to Cover 5 SPY at $490 ($2,450 value)
        # Realized P&L = +$50
        trades = [
            {
                "timestamp": datetime(2026, 6, 11, 9, 30, tzinfo=timezone.utc),
                "strategy": "tier3_intraday",
                "symbol": "SPY",
                "action": "sell_to_open",
                "quantity": 5,
                "order_value": 2500.0,
            },
            {
                "timestamp": datetime(2026, 6, 11, 10, 0, tzinfo=timezone.utc),
                "strategy": "tier3_intraday",
                "symbol": "SPY",
                "action": "buy_to_close",
                "quantity": 5,
                "order_value": 2450.0,
            }
        ]

        matched = compute_fifo_trades(trades)
        self.assertEqual(len(matched), 1)

        trade = matched[0]
        self.assertEqual(trade["symbol"], "SPY")
        self.assertEqual(trade["direction"], "Short")
        self.assertEqual(trade["quantity"], 5)
        self.assertEqual(trade["entry_price"], 500.0)
        self.assertEqual(trade["exit_price"], 490.0)
        self.assertEqual(trade["realized_pnl"], 50.0)

    def test_multi_symbol_isolation(self):
        # Interleaved trades of different symbols should match independently
        trades = [
            {
                "timestamp": datetime(2026, 6, 11, 9, 30, tzinfo=timezone.utc),
                "strategy": "tier2_swing",
                "symbol": "AAPL",
                "action": "buy",
                "quantity": 10,
                "order_value": 1800.0, # $180/share
            },
            {
                "timestamp": datetime(2026, 6, 11, 9, 45, tzinfo=timezone.utc),
                "strategy": "tier2_swing",
                "symbol": "MSFT",
                "action": "buy",
                "quantity": 10,
                "order_value": 3500.0, # $350/share
            },
            {
                "timestamp": datetime(2026, 6, 11, 10, 0, tzinfo=timezone.utc),
                "strategy": "tier2_swing",
                "symbol": "AAPL",
                "action": "sell",
                "quantity": 10,
                "order_value": 1900.0, # $190/share (+100 P&L)
            },
            {
                "timestamp": datetime(2026, 6, 11, 10, 15, tzinfo=timezone.utc),
                "strategy": "tier2_swing",
                "symbol": "MSFT",
                "action": "sell",
                "quantity": 10,
                "order_value": 3400.0, # $340/share (-100 P&L)
            }
        ]

        matched = compute_fifo_trades(trades)
        self.assertEqual(len(matched), 2)
        
        # Sort matched trades by symbol to assert reliably
        matched_sorted = sorted(matched, key=lambda t: t["symbol"])
        
        aapl_trade = matched_sorted[0]
        self.assertEqual(aapl_trade["symbol"], "AAPL")
        self.assertEqual(aapl_trade["realized_pnl"], 100.0)

        msft_trade = matched_sorted[1]
        self.assertEqual(msft_trade["symbol"], "MSFT")
        self.assertEqual(msft_trade["realized_pnl"], -100.0)


if __name__ == "__main__":
    unittest.main()
