import os
import sys
import unittest
from pathlib import Path

# Add strategy-engine to path
SERVICE_DIR = Path(__file__).resolve().parents[1] / "services" / "strategy-engine"
if str(SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(SERVICE_DIR))

from utils.data_feed import (
    AlpacaMarketDataProvider,
    IndicatorSettings,
)


@unittest.skipUnless(
    os.environ.get("RUN_LIVE_INTEGRATION_TESTS", "").strip().lower() == "true",
    "Set RUN_LIVE_INTEGRATION_TESTS=true to opt in to external API tests",
)
class IntegrationFeedsTests(unittest.TestCase):

    def test_alpaca_real_fetch(self):
        api_key = os.environ.get("ALPACA_API_KEY")
        secret_key = os.environ.get("ALPACA_SECRET_KEY")
        if not api_key or not secret_key:
            self.skipTest("Alpaca credentials not found in env")

        provider = AlpacaMarketDataProvider(
            api_key=api_key,
            secret_key=secret_key,
            base_url="https://data.alpaca.markets",
            data_feed="iex",
            indicator_settings=IndicatorSettings(
                fast_ema_period=3,
                slow_ema_period=5,
                rsi_period=3,
                intraday_roc_minutes=5,
            ),
        )
        try:
            aapl_level = provider.get_index_level("AAPL")
        except Exception as exc:
            self.skipTest(f"Alpaca API request unavailable ({type(exc).__name__})")
        self.assertIsInstance(aapl_level, float)
        self.assertGreater(aapl_level, 0.0)
        print(f"\n[Integration Test] Real Alpaca AAPL close price fetched: {aapl_level}")

        rsi = provider.get_rsi("AAPL")
        self.assertIsInstance(rsi, float)
        self.assertTrue(0.0 <= rsi <= 100.0)
        print(f"[Integration Test] Real Alpaca AAPL RSI (3-period) fetched: {rsi}")
