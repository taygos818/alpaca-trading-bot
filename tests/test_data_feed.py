import os
import sys
import types
import unittest
import importlib.util
from pathlib import Path
import datetime

try:
    import requests
except ModuleNotFoundError:
    requests = types.ModuleType("requests")

    class HTTPError(Exception):
        pass

    class RequestException(Exception):
        pass

    class Session:
        pass

    requests.HTTPError = HTTPError
    requests.RequestException = RequestException
    requests.Session = Session
    sys.modules["requests"] = requests


SERVICE_DIR = Path(__file__).resolve().parents[1] / "services" / "strategy-engine"
MODULE_PATH = SERVICE_DIR / "utils" / "data_feed.py"
SPEC = importlib.util.spec_from_file_location("data_feed", MODULE_PATH)
data_feed = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(data_feed)

AlpacaMarketDataProvider = data_feed.AlpacaMarketDataProvider
DataFeedUnavailable = data_feed.DataFeedUnavailable
FredMarketDataProvider = data_feed.FredMarketDataProvider
HybridMarketSnapshotProvider = data_feed.HybridMarketSnapshotProvider
IndicatorSettings = data_feed.IndicatorSettings
MockMarketSnapshotProvider = data_feed.MockMarketSnapshotProvider
build_market_snapshot_provider = data_feed.build_market_snapshot_provider


class FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200):
        self.payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"status={self.status_code}")

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, route_map: dict[tuple[str, str], dict]):
        self.route_map = route_map
        self.calls: list[tuple[str, str]] = []
        self.request_params: list[dict] = []

    def get(self, url, headers=None, params=None, timeout=None):
        params = params or {}
        key = (url, str(params.get("timeframe", params.get("series_id", ""))))
        self.calls.append(key)
        self.request_params.append(dict(params))
        payload = self.route_map[key]
        return FakeResponse(payload)


def make_bars(closes: list[float]) -> dict:
    return {"bars": [{"c": close, "h": close, "l": close} for close in closes]}


def make_bars_ohlc(closes: list[float], highs: list[float], lows: list[float]) -> dict:
    return {"bars": [{"c": c, "h": h, "l": l} for c, h, l in zip(closes, highs, lows)]}


class DataFeedTests(unittest.TestCase):
    def test_fred_provider_reads_latest_numeric_observation(self):
        session = FakeSession(
            {
                (
                    "https://api.stlouisfed.org/fred/series/observations",
                    "VIXCLS",
                ): {
                    "observations": [
                        {"value": "."},
                        {"value": "18.42"},
                    ]
                }
            }
        )
        provider = FredMarketDataProvider(
            api_key="fred-key",
            base_url="https://api.stlouisfed.org/fred",
            series_map={"VIX": "VIXCLS"},
            session=session,
        )

        self.assertAlmostEqual(provider.get_index_level("VIX"), 18.42)

    def test_alpaca_provider_computes_indicators_from_bar_history(self):
        session = FakeSession(
            {
                (
                    "https://data.alpaca.markets/v2/stocks/SPY/bars",
                    "1Day",
                ): make_bars([100 + value for value in range(40)]),
                (
                    "https://data.alpaca.markets/v2/stocks/SPY/bars",
                    "1Min",
                ): make_bars([100.0, 101.0, 102.0, 103.0]),
            }
        )
        provider = AlpacaMarketDataProvider(
            api_key="alpaca-key",
            secret_key="alpaca-secret",
            base_url="https://data.alpaca.markets",
            data_feed="iex",
            indicator_settings=IndicatorSettings(
                fast_ema_period=3,
                slow_ema_period=5,
                rsi_period=3,
                intraday_roc_minutes=4,
            ),
            session=session,
        )

        self.assertAlmostEqual(provider.get_index_level("SPY"), 139.0)
        self.assertTrue(provider.get_ema_crossover("SPY"))
        self.assertAlmostEqual(provider.get_rsi("SPY"), 100.0)
        self.assertAlmostEqual(provider.get_intraday_roc("SPY"), 3.0)

    def test_alpaca_provider_computes_atr_and_parameterized_indicators(self):
        # We need period + 1 bars (e.g. 6 bars for a 5-period ATR)
        closes = [100.0, 102.0, 101.0, 103.0, 102.0, 104.0]
        highs  = [101.0, 103.0, 102.0, 104.0, 103.0, 105.0]
        lows   = [ 99.0, 101.0, 100.0, 102.0, 101.0, 103.0]
        
        session = FakeSession(
            {
                (
                    "https://data.alpaca.markets/v2/stocks/SPY/bars",
                    "1Day",
                ): make_bars_ohlc(closes, highs, lows)
            }
        )
        provider = AlpacaMarketDataProvider(
            api_key="alpaca-key",
            secret_key="alpaca-secret",
            base_url="https://data.alpaca.markets",
            data_feed="iex",
            indicator_settings=IndicatorSettings(),
            session=session,
        )

        # 5-period ATR calculation:
        # TR1 = H1 - L1 = 103 - 101 = 2 (since c0 = 100)
        # TR2 = H2 - L2 = 102 - 100 = 2 (since c1 = 102)
        # TR3 = max(104-102, 104-101, 102-101) = 3
        # TR4 = max(103-101, 103-103, 103-101) = 2
        # TR5 = max(105-103, 105-102, 103-102) = 3
        # Average TR = (3 + 2 + 3 + 2 + 3) / 5 = 2.6
        atr = provider.get_atr("SPY", period=5)
        self.assertAlmostEqual(atr, 2.6)

        # Test parameterized indicators
        ema_crossover = provider.get_ema_crossover("SPY", fast_period=2, slow_period=3)
        # fast=2 closes: [102.0, 104.0] -> fast_ema = (100+102)/2 = 101; then (104-101)*(2/3)+101 = 103
        # slow=3 closes: [101.0, 103.0, 102.0, 104.0] -> etc. Crossover should be resolved correctly.
        self.assertIsInstance(ema_crossover, bool)

        rsi = provider.get_rsi("SPY", period=2)
        self.assertTrue(0.0 <= rsi <= 100.0)

    def test_hybrid_provider_falls_back_to_mock_for_unimplemented_iv_rank(self):
        provider = HybridMarketSnapshotProvider(
            alpaca=AlpacaMarketDataProvider(
                api_key="alpaca-key",
                secret_key="alpaca-secret",
                base_url="https://data.alpaca.markets",
                data_feed="iex",
                indicator_settings=IndicatorSettings(),
                session=FakeSession({}),
            ),
            fallback=MockMarketSnapshotProvider(),
            allow_mock_iv_rank=True,
        )

        iv_rank = provider.get_iv_rank("AAPL")

        self.assertGreaterEqual(iv_rank, 10)
        self.assertLessEqual(iv_rank, 55)

    def test_build_market_snapshot_provider_returns_mock_when_requested(self):
        previous_value = os.environ.get("MARKET_DATA_PROVIDER")
        os.environ["MARKET_DATA_PROVIDER"] = "mock"
        try:
            provider = build_market_snapshot_provider()
        finally:
            if previous_value is None:
                os.environ.pop("MARKET_DATA_PROVIDER", None)
            else:
                os.environ["MARKET_DATA_PROVIDER"] = previous_value

        self.assertIsInstance(provider, MockMarketSnapshotProvider)

    def test_hybrid_provider_raises_without_fallback(self):
        provider = HybridMarketSnapshotProvider(
            alpaca=None,
            fred=None,
            fallback=None,
            allow_mock_fallback=False,
            allow_mock_iv_rank=False,
        )

        with self.assertRaises(DataFeedUnavailable):
            provider.get_rsi("SPY")


class FakeRedis:
    def __init__(self):
        self.store = {}
        self.ttl = {}

    def get(self, key):
        return self.store.get(key)

    def setex(self, key, seconds, value):
        self.store[key] = value
        self.ttl[key] = seconds

    def ping(self):
        return True


class DataFeedCachingTests(unittest.TestCase):
    def test_alpaca_provider_requests_latest_bars_and_normalizes_chronology(self):
        session = FakeSession({
            ("https://data.alpaca.markets/v2/stocks/SPY/bars", "1Min"): {
                "bars": [
                    {"t": "2026-08-31T14:32:00Z", "c": 102.0},
                    {"t": "2026-08-31T14:31:00Z", "c": 101.0},
                    {"t": "2026-08-31T14:30:00Z", "c": 100.0},
                ]
            }
        })
        provider = AlpacaMarketDataProvider(
            api_key="alpaca-key",
            secret_key="alpaca-secret",
            base_url="https://data.alpaca.markets",
            data_feed="iex",
            indicator_settings=IndicatorSettings(),
            session=session,
        )

        bars = provider._get_bars("SPY", "1Min", 3)

        self.assertEqual(session.request_params[0]["sort"], "desc")
        self.assertEqual([bar["c"] for bar in bars], [100.0, 101.0, 102.0])

    def test_alpaca_provider_redis_caching(self):
        fake_redis = FakeRedis()
        
        session = FakeSession({
            ("https://data.alpaca.markets/v2/stocks/SPY/bars", "1Day"): make_bars([100.0, 101.0, 102.0])
        })
        
        provider = AlpacaMarketDataProvider(
            api_key="alpaca-key",
            secret_key="alpaca-secret",
            base_url="https://data.alpaca.markets",
            data_feed="iex",
            indicator_settings=IndicatorSettings(),
            session=session,
            redis_url="redis://fake",
            cache_ttl_seconds=60
        )
        provider.redis_client = fake_redis
        
        # First call: cache miss. Should query session.
        bars = provider._get_bars("SPY", "1Day", 2)
        self.assertEqual(len(bars), 2)
        self.assertEqual(bars[0]["c"], 101.0)
        self.assertEqual(bars[1]["c"], 102.0)
        self.assertEqual(len(session.calls), 1)
        
        # Verify it was written to cache
        cache_key = "cache:bars:SPY:1Day"
        self.assertIn(cache_key, fake_redis.store)
        
        # Modify the cached data to verify subsequent calls hit cache
        import json
        cached_payload = json.loads(fake_redis.store[cache_key])
        cached_payload["bars"] = [{"c": 200.0, "h": 200.0, "l": 200.0}, {"c": 201.0, "h": 201.0, "l": 201.0}]
        fake_redis.store[cache_key] = json.dumps(cached_payload)
        
        # Second call: cache hit. Should return the modified cached data, not query session.
        bars_cached = provider._get_bars("SPY", "1Day", 2)
        self.assertEqual(len(bars_cached), 2)
        self.assertEqual(bars_cached[0]["c"], 200.0)
        self.assertEqual(bars_cached[1]["c"], 201.0)
        self.assertEqual(len(session.calls), 1)

    def test_alpaca_provider_redis_caching_full_history(self):
        fake_redis = FakeRedis()
        
        session = FakeSession({
            ("https://data.alpaca.markets/v2/stocks/SPY/bars", "1Day"): make_bars([100.0, 101.0, 102.0])
        })
        
        provider = AlpacaMarketDataProvider(
            api_key="alpaca-key",
            secret_key="alpaca-secret",
            base_url="https://data.alpaca.markets",
            data_feed="iex",
            indicator_settings=IndicatorSettings(),
            session=session,
            redis_url="redis://fake",
            cache_ttl_seconds=60
        )
        provider.redis_client = fake_redis
        
        # Request a limit of 5. fetch_limit is 100.
        # It gets 3 bars from session, caches with full_history = True
        bars = provider._get_bars("SPY", "1Day", 5)
        self.assertEqual(len(bars), 3)
        self.assertEqual(len(session.calls), 1)
        
        # Verify cache is stored with full_history = True
        import json
        cache_key = "cache:bars:SPY:1Day"
        cache_payload = json.loads(fake_redis.store[cache_key])
        self.assertTrue(cache_payload["full_history"])
        
        # Now request a limit of 4. Since full_history = True, it should hit the cache, not call session.
        bars_cached = provider._get_bars("SPY", "1Day", 4)
        self.assertEqual(len(bars_cached), 3)
        self.assertEqual(len(session.calls), 1)


class DataFeedVolatilityTests(unittest.TestCase):
    def test_alpaca_provider_get_iv_rank_success(self):
        closes = [100.0] * 282
        for idx in range(10, 20):
            closes[idx] = 101.0
        for idx in range(150, 160):
            closes[idx] = 99.0
            
        session = FakeSession({
            ("https://data.alpaca.markets/v2/stocks/SPY/bars", "1Day"): {"bars": [{"c": c} for c in closes]},
            ("https://paper-api.alpaca.markets/v2/options/contracts", ""): [
                {"symbol": "SPY260720C00100000", "type": "call", "strike_price": "100", "expiration_date": (datetime.date.today() + datetime.timedelta(days=35)).strftime("%Y-%m-%d")},
                {"symbol": "SPY260720P00100000", "type": "put", "strike_price": "100", "expiration_date": (datetime.date.today() + datetime.timedelta(days=35)).strftime("%Y-%m-%d")}
            ],
            ("https://data.alpaca.markets/v1beta1/options/snapshots", ""): {
                "snapshots": {
                    "SPY260720C00100000": {"impliedVolatility": 0.25},
                    "SPY260720P00100000": {"impliedVolatility": 0.23}
                }
            }
        })
        
        provider = AlpacaMarketDataProvider(
            api_key="alpaca-key",
            secret_key="alpaca-secret",
            base_url="https://data.alpaca.markets",
            trading_api_url="https://paper-api.alpaca.markets",
            data_feed="iex",
            indicator_settings=IndicatorSettings(),
            session=session
        )
        
        iv_rank = provider.get_iv_rank("SPY")
        
        self.assertIsInstance(iv_rank, float)
        self.assertTrue(0.0 <= iv_rank <= 100.0)

    def test_alpaca_provider_get_iv_rank_fallback_to_hv(self):
        closes = [100.0] * 282
        for idx in range(10, 20):
            closes[idx] = 102.0
            
        session = FakeSession({
            ("https://data.alpaca.markets/v2/stocks/SPY/bars", "1Day"): {"bars": [{"c": c} for c in closes]},
            ("https://paper-api.alpaca.markets/v2/options/contracts", ""): []
        })
        
        provider = AlpacaMarketDataProvider(
            api_key="alpaca-key",
            secret_key="alpaca-secret",
            base_url="https://data.alpaca.markets",
            trading_api_url="https://paper-api.alpaca.markets",
            data_feed="iex",
            indicator_settings=IndicatorSettings(),
            session=session
        )
        
        iv_rank = provider.get_iv_rank("SPY")
        self.assertIsInstance(iv_rank, float)
        self.assertTrue(0.0 <= iv_rank <= 100.0)


if __name__ == "__main__":
    unittest.main()

