import argparse
import datetime
import os
import sys
from pathlib import Path

# Add strategy-engine to path
ROOT_DIR = Path(__file__).resolve().parents[1]
SERVICE_DIR = ROOT_DIR / "services" / "strategy-engine"
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
if str(SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(SERVICE_DIR))

from backtest.engine import BacktestSimulator
from strategies.tier2_swing import SwingStrategy
from strategies.tier3_intraday import IntradayStrategy

def main():
    parser = argparse.ArgumentParser(description="Strategy Parameter Sweep Optimizer")
    parser.add_argument("--strategy", default="tier2_swing", choices=["tier2_swing", "tier3_intraday"], help="Strategy to optimize")
    parser.add_argument("--symbols", default="SPY,QQQ,MSFT,AAPL", help="Watchlist symbols")
    parser.add_argument("--start", default="2026-01-01", help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end", default="2026-06-01", help="End date (YYYY-MM-DD)")
    
    # Swing Strategy parameters
    parser.add_argument("--ema-pairs", default="9:21,12:26,10:30", help="Swing: Comma-separated fast:slow EMA pairs")
    
    # Shared / Intraday Strategy parameters
    parser.add_argument("--rsi-thresholds", default="10,15,20" , help="RSI entry thresholds")
    parser.add_argument("--roc-thresholds", default="0.3,0.5,0.8", help="Intraday: Comma-separated ROC entry thresholds")
    args = parser.parse_args()

    start_date = datetime.datetime.strptime(args.start, "%Y-%m-%d").date()
    end_date = datetime.datetime.strptime(args.end, "%Y-%m-%d").date()
    watchlist = [x.strip().upper() for x in args.symbols.split(",") if x.strip()]
    
    rsi_thresholds = [float(x.strip()) for x in args.rsi_thresholds.split(",") if x.strip()]

    # Setup environment defaults
    os.environ["ENABLE_SAMPLE_SIGNALS"] = "false"
    # Force use of env overrides instead of strategy.toml to test sweep values
    os.environ["STRATEGY_CONFIG_PATH"] = "nonexistent_file_to_force_env.toml"

    results = []

    if args.strategy == "tier2_swing":
        # Parse EMA pairs
        ema_pairs = []
        for pair in args.ema_pairs.split(","):
            fast_str, sep, slow_str = pair.partition(":")
            if sep:
                ema_pairs.append((int(fast_str.strip()), int(slow_str.strip())))
        
        print("=" * 75)
        print(" RUNNING PARAMETER SWEEP FOR SWING STRATEGY ")
        print("=" * 75)
        print(f"Period:    {start_date} to {end_date}")
        print(f"Watchlist: {watchlist}")
        print(f"EMA Pairs: {ema_pairs}")
        print(f"RSI Thrs:  {rsi_thresholds}\n")

        for fast, slow in ema_pairs:
            for rsi_thr in rsi_thresholds:
                # Set environment variables for this strategy run
                os.environ["SWING_EMA_FAST"] = str(fast)
                os.environ["SWING_EMA_SLOW"] = str(slow)
                os.environ["SWING_RSI_ENTRY_THRESHOLD"] = str(rsi_thr)
                
                simulator = BacktestSimulator(initial_equity=100000.0)
                try:
                    simulator.run(SwingStrategy, watchlist, start_date, end_date)
                    metrics = simulator.get_metrics()
                    results.append({
                        "param1": f"{fast}:{slow}",
                        "param2": rsi_thr,
                        "total_return_pct": metrics.get("total_return_pct", 0.0),
                        "max_drawdown_pct": metrics.get("max_drawdown_pct", 0.0),
                        "sharpe_ratio": metrics.get("sharpe_ratio", 0.0),
                        "total_trades": metrics.get("total_trades", 0)
                    })
                except Exception as exc:
                    print(f"Failed sweep for EMA={fast}:{slow} RSI={rsi_thr}: {exc}")

        # Sort results by total return (descending)
        results.sort(key=lambda x: x["total_return_pct"], reverse=True)

        print("\n" + "=" * 90)
        print(f"{'Rank':<5} | {'EMA Pair':<10} | {'RSI Threshold':<13} | {'Total Return':<12} | {'Max Drawdown':<12} | {'Sharpe':<8} | {'Trades'}")
        print("-" * 90)
        for i, res in enumerate(results, 1):
            print(f"{i:<5} | {res['param1']:<10} | {res['param2']:<13.1f} | {res['total_return_pct']:<11.2f}% | {res['max_drawdown_pct']:<11.2f}% | {res['sharpe_ratio']:<8.2f} | {res['total_trades']}")
        print("=" * 90)

    elif args.strategy == "tier3_intraday":
        roc_thresholds = [float(x.strip()) for x in args.roc_thresholds.split(",") if x.strip()]
        
        # Override default rsi threshold if user didn't specify custom ones for intraday
        if args.rsi_thresholds == "10,15,20":
            # For intraday, default entry boundaries are usually higher (e.g. < 70)
            rsi_thresholds = [60.0, 70.0, 80.0]

        print("=" * 75)
        print(" RUNNING PARAMETER SWEEP FOR INTRADAY STRATEGY ")
        print("=" * 75)
        print(f"Period:    {start_date} to {end_date}")
        print(f"Watchlist: {watchlist}")
        print(f"ROC Thrs:  {roc_thresholds}")
        print(f"RSI Thrs:  {rsi_thresholds}\n")

        for roc_thr in roc_thresholds:
            for rsi_thr in rsi_thresholds:
                # Set environment variables for this strategy run
                os.environ["INTRADAY_ROC_THRESHOLD"] = str(roc_thr)
                os.environ["INTRADAY_RSI_ENTRY_THRESHOLD"] = str(rsi_thr)
                
                simulator = BacktestSimulator(initial_equity=100000.0)
                try:
                    simulator.run(IntradayStrategy, watchlist, start_date, end_date)
                    metrics = simulator.get_metrics()
                    results.append({
                        "param1": roc_thr,
                        "param2": rsi_thr,
                        "total_return_pct": metrics.get("total_return_pct", 0.0),
                        "max_drawdown_pct": metrics.get("max_drawdown_pct", 0.0),
                        "sharpe_ratio": metrics.get("sharpe_ratio", 0.0),
                        "total_trades": metrics.get("total_trades", 0)
                    })
                except Exception as exc:
                    print(f"Failed sweep for ROC={roc_thr} RSI={rsi_thr}: {exc}")

        # Sort results by total return (descending)
        results.sort(key=lambda x: x["total_return_pct"], reverse=True)

        print("\n" + "=" * 90)
        print(f"{'Rank':<5} | {'ROC Thr (%)':<11} | {'RSI Threshold':<13} | {'Total Return':<12} | {'Max Drawdown':<12} | {'Sharpe':<8} | {'Trades'}")
        print("-" * 90)
        for i, res in enumerate(results, 1):
            print(f"{i:<5} | {res['param1']:<11.2f} | {res['param2']:<13.1f} | {res['total_return_pct']:<11.2f}% | {res['max_drawdown_pct']:<11.2f}% | {res['sharpe_ratio']:<8.2f} | {res['total_trades']}")
        print("=" * 90)

if __name__ == "__main__":
    main()
