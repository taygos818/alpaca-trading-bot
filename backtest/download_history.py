import argparse
import datetime
import json
import os
from pathlib import Path
import requests

def load_env(env_path: Path):
    if not env_path.exists():
        return
    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, sep, val = line.partition("=")
            if sep:
                os.environ[key.strip()] = val.strip()

def download_alpaca_bars(symbol: str, api_key: str, secret_key: str, start_date: str, end_date: str) -> list[dict]:
    url = f"https://data.alpaca.markets/v2/stocks/{symbol}/bars"
    headers = {
        "APCA-API-KEY-ID": api_key,
        "APCA-API-SECRET-KEY": secret_key,
        "accept": "application/json"
    }
    params = {
        "timeframe": "1Day",
        "start": start_date,
        "end": end_date,
        "adjustment": "raw",
        "feed": "iex",
        "sort": "asc",
        "limit": 1000
    }
    
    bars = []
    page_token = None
    
    while True:
        if page_token:
            params["page_token"] = page_token
        
        response = requests.get(url, headers=headers, params=params, timeout=15)
        response.raise_for_status()
        data = response.json()
        
        chunk = data.get("bars") or []
        bars.extend(chunk)
        
        page_token = data.get("next_page_token")
        if not page_token:
            break
            
    return bars

def main():
    parser = argparse.ArgumentParser(description="Download historical data for backtesting")
    parser.add_argument("--days", type=int, default=180, help="Number of lookback days")
    args = parser.parse_args()

    # Locate project root and load env files
    project_root = Path(__file__).resolve().parents[1]
    load_env(project_root / ".env.secrets")
    load_env(project_root / ".env")

    api_key = os.getenv("ALPACA_API_KEY")
    secret_key = os.getenv("ALPACA_SECRET_KEY")

    if not api_key or not secret_key:
        print("Error: ALPACA_API_KEY and ALPACA_SECRET_KEY must be configured in env files.")
        return

    # Calculate dates
    end_dt = datetime.datetime.now(datetime.timezone.utc)
    start_dt = end_dt - datetime.timedelta(days=args.days)
    
    start_str = start_dt.strftime("%Y-%m-%d")
    end_str = end_dt.strftime("%Y-%m-%d")
    
    output_dir = project_root / "data" / "historical"
    output_dir.mkdir(parents=True, exist_ok=True)

    watchlist = [item.strip() for item in os.getenv("WATCHLIST", "SPY,QQQ,MSFT,AAPL").split(",") if item.strip()]
    
    print(f"Downloading historical daily bars from {start_str} to {end_str}...")
    for symbol in watchlist:
        try:
            print(f" -> Downloading {symbol}...")
            bars = download_alpaca_bars(symbol, api_key, secret_key, start_str, end_str)
            output_file = output_dir / f"{symbol.upper()}_daily.json"
            with open(output_file, "w", encoding="utf-8") as f:
                json.dump(bars, f, indent=2)
            print(f"    Saved {len(bars)} bars to {output_file.name}")
        except Exception as exc:
            print(f"    Failed to download {symbol}: {exc}")

if __name__ == "__main__":
    main()
