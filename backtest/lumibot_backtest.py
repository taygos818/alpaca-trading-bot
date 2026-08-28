import argparse
import datetime
import json
import os
import sys
from pathlib import Path

# Add strategy-engine and root to path
ROOT_DIR = Path(__file__).resolve().parents[1]
SERVICE_DIR = ROOT_DIR / "services" / "strategy-engine"
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
if str(SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(SERVICE_DIR))

from backtest.engine import BacktestSimulator
from strategies.tier1_wheel import WheelStrategy
from strategies.tier2_swing import SwingStrategy
from strategies.tier3_intraday import IntradayStrategy

def main():
    parser = argparse.ArgumentParser(description="Lumibot Execution-Parity Backtest Runner")
    parser.add_argument("--strategy", default="tier2_swing", choices=["tier1_wheel", "tier2_swing", "tier3_intraday"], help="Strategy to run")
    parser.add_argument("--start", default="2026-01-01", help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end", default="2026-06-01", help="End date (YYYY-MM-DD)")
    parser.add_argument("--watchlist", default="SPY,QQQ,MSFT,AAPL", help="Comma-separated watchlist")
    parser.add_argument("--initial-equity", type=float, default=100000.0, help="Starting portfolio equity")
    args = parser.parse_args()

    # Parse inputs
    start_date = datetime.datetime.strptime(args.start, "%Y-%m-%d").date()
    end_date = datetime.datetime.strptime(args.end, "%Y-%m-%d").date()
    watchlist = [x.strip().upper() for x in args.watchlist.split(",") if x.strip()]

    # Load strategy
    strategy_map = {
        "tier1_wheel": WheelStrategy,
        "tier2_swing": SwingStrategy,
        "tier3_intraday": IntradayStrategy
    }
    strategy_cls = strategy_map[args.strategy]

    # Setup environment defaults
    os.environ["ENABLE_SAMPLE_SIGNALS"] = "false"
    os.environ["STRATEGY_CONFIG_PATH"] = str(Path(__file__).resolve().parents[1] / "strategy.toml")

    # Run backtest
    simulator = BacktestSimulator(initial_equity=args.initial_equity)
    simulator.run(strategy_cls, watchlist, start_date, end_date)

    # Output metrics
    metrics = simulator.get_metrics()
    
    print("\n" + "=" * 50)
    print(f" BACKTEST REPORT: {args.strategy.upper()} ")
    print("=" * 50)
    print(f"Period:         {start_date} to {end_date}")
    print(f"Initial Equity: ${metrics.get('initial_equity', 0.0):,.2f}")
    print(f"Ending Equity:  ${metrics.get('ending_equity', 0.0):,.2f}")
    print(f"Total Return:   {metrics.get('total_return_pct', 0.0):.2f}%")
    print(f"Max Drawdown:   {metrics.get('max_drawdown_pct', 0.0):.2f}%")
    print(f"Sharpe Ratio:   {metrics.get('sharpe_ratio', 0.0):.2f}")
    print(f"Total Trades:   {metrics.get('total_trades', 0)}")
    print("=" * 50)
    
    if simulator.trades:
        print("\nTRADES LIST:")
        print(f"{'Date':<12} | {'Symbol':<15} | {'Action':<12} | {'Qty':<6} | {'Price':<10} | {'Notes'}")
        print("-" * 100)
        for t in simulator.trades[-30:]: # Show last 30 trades
            print(f"{t['date']:<12} | {t['symbol']:<15} | {t['action']:<12} | {t['qty']:<6} | ${t['price']:<9.2f} | {t['notes']}")
        if len(simulator.trades) > 30:
            print(f"... and {len(simulator.trades) - 30} more trades.")
    else:
        print("\nNo trades executed during the backtest period.")

    # Save results to a file for the dashboard
    try:
        output_data = {
            "strategy": args.strategy,
            "start_date": str(start_date),
            "end_date": str(end_date),
            "metrics": metrics,
            "daily_history": [
                {
                    "date": str(h["date"]),
                    "equity": h["equity"],
                    "cash": h["cash"],
                    "positions_value": h["positions_value"]
                }
                for h in simulator.daily_history
            ],
            "trades": simulator.trades
        }
        backtest_out_dir = ROOT_DIR / "logs" / "strategy-engine"
        backtest_out_dir.mkdir(parents=True, exist_ok=True)
        with open(backtest_out_dir / "last_backtest.json", "w", encoding="utf-8") as f:
            json.dump(output_data, f, indent=2)
        print(f"\nSaved backtest results to {backtest_out_dir.name}/last_backtest.json")
    except Exception as exc:
        print(f"Failed to save backtest results: {exc}")

if __name__ == "__main__":
    main()

