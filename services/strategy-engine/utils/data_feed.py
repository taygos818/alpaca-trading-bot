from datetime import datetime, timedelta, timezone
import json
import logging
import os
from dataclasses import dataclass, field
from random import Random
from typing import Protocol

import redis
import requests
from utils.alpaca_credentials import alpaca_credentials


LOGGER = logging.getLogger(__name__)


class DataFeedUnavailable(RuntimeError):
    pass


class MarketSnapshotProvider(Protocol):
    def get_index_level(self, symbol: str) -> float:
        ...

    def get_latest_price(self, symbol: str) -> float:
        ...

    def get_iv_rank(self, symbol: str) -> float:
        ...

    def get_ema_crossover(self, symbol: str, fast_period: int | None = None, slow_period: int | None = None) -> bool:
        ...

    def get_rsi(self, symbol: str, period: int | None = None) -> float:
        ...

    def get_intraday_roc(self, symbol: str) -> float:
        ...

    def get_atr(self, symbol: str, period: int = 14) -> float:
        ...

    def get_opening_range(self, symbol: str, session_date: str, range_minutes: int = 15) -> dict:
        ...

    def get_option_contracts(self, symbol: str) -> list[dict]:
        ...

    def get_option_chain_snapshots(self, symbol: str) -> dict[str, dict]:
        ...


def _parse_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _parse_series_map(raw_value: str | None) -> dict[str, str]:
    if not raw_value:
        return {"VIX": "VIXCLS"}

    mapping: dict[str, str] = {}
    for item in raw_value.split(","):
        key, separator, value = item.partition(":")
        if separator and key.strip() and value.strip():
            mapping[key.strip().upper()] = value.strip()
    return mapping or {"VIX": "VIXCLS"}


def _calculate_ema(values: list[float], period: int) -> float:
    if len(values) < period:
        raise DataFeedUnavailable(f"Need at least {period} values for EMA")
    ema = sum(values[:period]) / period
    multiplier = 2 / (period + 1)
    for price in values[period:]:
        ema = ((price - ema) * multiplier) + ema
    return ema


def _calculate_rsi(values: list[float], period: int) -> float:
    if len(values) < period + 1:
        raise DataFeedUnavailable(f"Need at least {period + 1} values for RSI")

    gains: list[float] = []
    losses: list[float] = []
    for previous, current in zip(values, values[1:]):
        change = current - previous
        gains.append(max(change, 0.0))
        losses.append(abs(min(change, 0.0)))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for gain, loss in zip(gains[period:], losses[period:]):
        avg_gain = ((avg_gain * (period - 1)) + gain) / period
        avg_loss = ((avg_loss * (period - 1)) + loss) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def _calculate_atr(bars: list[dict], period: int) -> float:
    if len(bars) < period + 1:
        raise DataFeedUnavailable(f"Need at least {period + 1} bars for ATR calculation")
    
    tr_list = []
    for i in range(1, len(bars)):
        high = float(bars[i].get("h") or 0.0)
        low = float(bars[i].get("l") or 0.0)
        prev_close = float(bars[i-1].get("c") or 0.0)
        
        tr = max(
            high - low,
            abs(high - prev_close),
            abs(low - prev_close)
        )
        tr_list.append(tr)
        
    atr = sum(tr_list[:period]) / period
    for tr in tr_list[period:]:
        atr = (atr * (period - 1) + tr) / period
    return atr


def _calculate_historical_volatility(closes: list[float], window: int = 30) -> list[float]:
    import math
    if len(closes) < window + 1:
        raise ValueError(f"Need at least {window + 1} closes to calculate HV")
    
    log_returns = []
    for i in range(1, len(closes)):
        prev = closes[i-1]
        curr = closes[i]
        if prev <= 0 or curr <= 0:
            log_returns.append(0.0)
        else:
            log_returns.append(math.log(curr / prev))
            
    hv_series = []
    for start_idx in range(len(log_returns) - window + 1):
        window_returns = log_returns[start_idx : start_idx + window]
        mean_ret = sum(window_returns) / window
        variance = sum((r - mean_ret) ** 2 for r in window_returns) / (window - 1) if window > 1 else 0.0
        daily_vol = math.sqrt(variance)
        annual_vol = daily_vol * math.sqrt(252)
        hv_series.append(annual_vol)
        
    return hv_series


@dataclass(frozen=True)
class IndicatorSettings:
    fast_ema_period: int = 12
    slow_ema_period: int = 26
    rsi_period: int = 14
    intraday_roc_minutes: int = 15

    @classmethod
    def from_env(cls):
        return cls(
            fast_ema_period=int(os.getenv("FAST_EMA_PERIOD", "12")),
            slow_ema_period=int(os.getenv("SLOW_EMA_PERIOD", "26")),
            rsi_period=int(os.getenv("RSI_PERIOD", "14")),
            intraday_roc_minutes=int(os.getenv("INTRADAY_ROC_MINUTES", "15")),
        )


@dataclass(frozen=True)
class DataFeedSettings:
    provider: str = "hybrid"
    alpaca_data_api_url: str = "https://data.alpaca.markets"
    alpaca_trading_api_url: str = "https://paper-api.alpaca.markets"
    alpaca_data_feed: str = "iex"
    options_market_data_feed: str = "indicative"
    options_min_dte: int = 7
    options_max_dte: int = 21
    alpaca_api_key: str = ""
    alpaca_secret_key: str = ""
    fred_api_url: str = "https://api.stlouisfed.org/fred"
    fred_api_key: str = ""
    fred_series_map: dict[str, str] = field(default_factory=lambda: {"VIX": "VIXCLS"})
    allow_mock_fallback: bool = True
    allow_mock_iv_rank: bool = True
    request_timeout_seconds: float = 10.0
    redis_url: str = ""
    cache_ttl_seconds: int = 60

    @classmethod
    def from_env(cls):
        paper_trade = os.getenv("ALPACA_PAPER_TRADE", "true").strip().lower() == "true"
        api_key, secret_key = alpaca_credentials(paper_trade)
        default_trading_url = "https://paper-api.alpaca.markets"
        trading_api_url = os.getenv("ALPACA_TRADING_API_URL", default_trading_url).rstrip("/")
        if trading_api_url != default_trading_url:
            raise ValueError("alpaca-trading-bot accepts only the Alpaca paper endpoint")
        return cls(
            provider=os.getenv("MARKET_DATA_PROVIDER", "hybrid").strip().lower(),
            alpaca_data_api_url=os.getenv("ALPACA_DATA_API_URL", "https://data.alpaca.markets").rstrip("/"),
            alpaca_trading_api_url=trading_api_url,
            alpaca_data_feed=os.getenv("ALPACA_DATA_FEED", "iex"),
            options_market_data_feed=os.getenv("OPTIONS_MARKET_DATA_FEED", "indicative"),
            options_min_dte=int(os.getenv("OPTIONS_MIN_DTE", "7")),
            options_max_dte=int(os.getenv("OPTIONS_MAX_DTE", "21")),
            alpaca_api_key=api_key,
            alpaca_secret_key=secret_key,
            fred_api_url=os.getenv("FRED_API_URL", "https://api.stlouisfed.org/fred").rstrip("/"),
            fred_api_key=os.getenv("FRED_API_KEY", ""),
            fred_series_map=_parse_series_map(os.getenv("FRED_SERIES_MAP")),
            allow_mock_fallback=_parse_bool(os.getenv("ALLOW_MOCK_DATA_FALLBACK"), True),
            allow_mock_iv_rank=_parse_bool(os.getenv("ALLOW_MOCK_IV_RANK"), True),
            request_timeout_seconds=float(os.getenv("MARKET_DATA_TIMEOUT_SECONDS", "10")),
            redis_url=os.getenv("REDIS_URL", ""),
            cache_ttl_seconds=int(os.getenv("MARKET_DATA_CACHE_TTL_SECONDS", "60")),
        )


class MockMarketSnapshotProvider:
    def __init__(self):
        self.observations: list[dict] = []

    def _record(self, symbol: str, value: float):
        self.observations.append({
            "source": "mock", "symbol": symbol.upper(), "value": value,
            "exchange_timestamp": None,
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "quality": "synthetic",
        })
        self.observations = self.observations[-50:]
        return value

    def _rng(self, key: str) -> Random:
        return Random(hash(key) & 0xFFFFFFFF)

    def get_index_level(self, symbol: str) -> float:
        base = 22.0 if symbol.upper() == "VIX" else 100.0
        return self._record(symbol, base + self._rng(symbol).uniform(0, 10))

    def get_latest_price(self, symbol: str) -> float:
        return self.get_index_level(symbol)

    def get_iv_rank(self, symbol: str) -> float:
        return self._rng(f"iv:{symbol}").uniform(10, 55)

    def get_ema_crossover(self, symbol: str, fast_period: int | None = None, slow_period: int | None = None) -> bool:
        return self._rng(f"ema:{symbol}").randint(0, 1) == 1

    def get_rsi(self, symbol: str, period: int | None = None) -> float:
        return self._rng(f"rsi:{symbol}").uniform(5, 45)

    def get_intraday_roc(self, symbol: str) -> float:
        return self._rng(f"roc:{symbol}").uniform(-2.5, 2.5)

    def get_atr(self, symbol: str, period: int = 14) -> float:
        return self._rng(f"atr:{symbol}").uniform(1.0, 5.0)

    def get_opening_range(self, symbol: str, session_date: str, range_minutes: int = 15) -> dict:
        price = self.get_index_level(symbol)
        return {
            "open": price * 0.99, "high": price * 0.995, "low": price * 0.985,
            "last": price, "vwap": price * 0.992, "volume": 100000,
            "bar_count": range_minutes + 1, "last_timestamp": datetime.now(timezone.utc).isoformat(),
        }

    def get_option_contracts(self, symbol: str) -> list[dict]:
        import datetime
        exp_date_obj = datetime.date.today() + datetime.timedelta(days=35)
        exp_date = exp_date_obj.strftime("%Y-%m-%d")
        exp_date_occ = exp_date_obj.strftime("%y%m%d")
        return [
            {
                "symbol": f"{symbol}{exp_date_occ}P00250000",
                "strike_price": "250",
                "expiration_date": exp_date,
                "type": "put"
            },
            {
                "symbol": f"{symbol}{exp_date_occ}C00350000",
                "strike_price": "350",
                "expiration_date": exp_date,
                "type": "call"
            }
        ]

    def get_option_chain_snapshots(self, symbol: str) -> dict[str, dict]:
        import datetime
        exp_date_obj = datetime.date.today() + datetime.timedelta(days=35)
        exp_date_occ = exp_date_obj.strftime("%y%m%d")
        return {
            f"{symbol}{exp_date_occ}P00250000": {
                "latestQuote": {"bp": 5.50, "ap": 6.00},
                "latestTrade": {"price": 5.75},
                "greeks": {"delta": -0.25, "vega": 0.15}
            },
            f"{symbol}{exp_date_occ}C00350000": {
                "latestQuote": {"bp": 4.50, "ap": 5.00},
                "latestTrade": {"price": 4.75},
                "greeks": {"delta": 0.25, "vega": 0.12}
            }
        }


class FredMarketDataProvider:
    def __init__(
        self,
        api_key: str,
        base_url: str,
        series_map: dict[str, str],
        session: requests.Session | None = None,
        timeout_seconds: float = 10.0,
    ):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.series_map = {key.upper(): value for key, value in series_map.items()}
        self.session = session or requests.Session()
        self.timeout_seconds = timeout_seconds

    def get_index_level(self, symbol: str) -> float:
        series_id = self.series_map.get(symbol.upper())
        if not series_id:
            raise DataFeedUnavailable(f"No FRED mapping configured for {symbol}")

        params = {
            "series_id": series_id,
            "file_type": "json",
            "sort_order": "desc",
            "limit": 10,
        }
        if self.api_key:
            params["api_key"] = self.api_key

        response = self.session.get(
            f"{self.base_url}/series/observations",
            params=params,
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        observations = response.json().get("observations", [])
        for observation in observations:
            value = str(observation.get("value", "")).strip()
            if value and value != ".":
                return float(value)
        raise DataFeedUnavailable(f"FRED returned no numeric observation for {symbol}")

    def get_iv_rank(self, symbol: str) -> float:
        raise DataFeedUnavailable(f"FRED does not provide IV Rank for {symbol}")

    def get_ema_crossover(self, symbol: str, fast_period: int | None = None, slow_period: int | None = None) -> bool:
        raise DataFeedUnavailable(f"FRED does not provide EMA data for {symbol}")

    def get_rsi(self, symbol: str, period: int | None = None) -> float:
        raise DataFeedUnavailable(f"FRED does not provide RSI data for {symbol}")

    def get_intraday_roc(self, symbol: str) -> float:
        raise DataFeedUnavailable(f"FRED does not provide intraday ROC for {symbol}")

    def get_atr(self, symbol: str, period: int = 14) -> float:
        raise DataFeedUnavailable(f"FRED does not provide ATR data for {symbol}")


class AlpacaMarketDataProvider:
    def __init__(
        self,
        api_key: str,
        secret_key: str,
        base_url: str,
        indicator_settings: IndicatorSettings,
        trading_api_url: str = "",
        data_feed: str = "iex",
        session: requests.Session | None = None,
        timeout_seconds: float = 10.0,
        redis_url: str = "",
        cache_ttl_seconds: int = 60,
        options_feed: str = "indicative",
        options_min_dte: int = 7,
        options_max_dte: int = 21,
    ):
        self.api_key = api_key
        self.secret_key = secret_key
        self.base_url = base_url.rstrip("/")
        self.trading_api_url = trading_api_url.rstrip("/")
        self.data_feed = data_feed
        self.indicator_settings = indicator_settings
        self.session = session or requests.Session()
        self.timeout_seconds = timeout_seconds
        self.cache_ttl_seconds = cache_ttl_seconds
        if options_feed not in {"indicative", "opra"}:
            raise ValueError("unsupported options market data feed")
        if options_min_dte <= 0 or options_max_dte < options_min_dte:
            raise ValueError("invalid options DTE window")
        self.options_feed = options_feed
        self.options_min_dte = options_min_dte
        self.options_max_dte = options_max_dte
        self.observations: list[dict] = []
        
        self.redis_client = None
        if redis_url:
            try:
                self.redis_client = redis.Redis.from_url(redis_url, decode_responses=True)
                self.redis_client.ping()
                LOGGER.info("AlpacaMarketDataProvider connected to Redis at %s", redis_url)
            except Exception as e:
                LOGGER.warning("AlpacaMarketDataProvider could not connect to Redis: %s. Caching disabled.", e)
                self.redis_client = None

    def get_option_contracts(self, symbol: str) -> list[dict]:
        import datetime
        today = datetime.date.today()
        gte_date = (today + datetime.timedelta(days=self.options_min_dte)).strftime("%Y-%m-%d")
        lte_date = (today + datetime.timedelta(days=self.options_max_dte)).strftime("%Y-%m-%d")
        try:
            response = self.session.get(
                f"{self.trading_api_url}/v2/options/contracts",
                headers={
                    "APCA-API-KEY-ID": self.api_key,
                    "APCA-API-SECRET-KEY": self.secret_key,
                },
                params={
                    "underlying_symbols": symbol,
                    "status": "active",
                    "expiration_date_gte": gte_date,
                    "expiration_date_lte": lte_date,
                    "limit": 1000,
                },
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            data = response.json()
            contracts = data.get("option_contracts", [])
            if not contracts and isinstance(data, list):
                contracts = data
            return contracts
        except requests.RequestException as exc:
            raise DataFeedUnavailable(f"Alpaca option contracts fetch failed: {exc}") from exc

    def get_option_chain_snapshots(self, symbol: str) -> dict[str, dict]:
        try:
            contracts = self.get_option_contracts(symbol)
            option_symbols = [c.get("symbol") for c in contracts if c.get("symbol")]
            if not option_symbols:
                return {}

            snapshots = {}
            for i in range(0, len(option_symbols), 100):
                chunk = option_symbols[i : i + 100]
                response = self.session.get(
                    f"{self.base_url}/v1beta1/options/snapshots",
                    headers=self._headers(),
                    params={
                        "feed": self.options_feed,
                        "symbols": ",".join(chunk),
                    },
                    timeout=self.timeout_seconds,
                )
                response.raise_for_status()
                chunk_snaps = response.json().get("snapshots", {})
                snapshots.update(chunk_snaps)
            return snapshots
        except requests.RequestException as exc:
            raise DataFeedUnavailable(f"Alpaca option snapshots fetch failed: {exc}") from exc

    def _headers(self) -> dict[str, str]:
        if not self.api_key or not self.secret_key:
            raise DataFeedUnavailable("Alpaca API credentials are not configured")
        return {
            "APCA-API-KEY-ID": self.api_key,
            "APCA-API-SECRET-KEY": self.secret_key,
        }

    def _get_bars(self, symbol: str, timeframe: str, limit: int) -> list[dict]:
        cache_key = f"cache:bars:{symbol.upper()}:{timeframe}"
        if self.redis_client:
            try:
                cached_data = self.redis_client.get(cache_key)
                if cached_data:
                    cache_payload = json.loads(cached_data)
                    bars = cache_payload.get("bars", [])
                    full_history = cache_payload.get("full_history", False)
                    if len(bars) >= limit or full_history:
                        self._record_observation(symbol, bars[-1], "alpaca_cache")
                        return bars[-limit:]
                    else:
                        LOGGER.debug("Cache hit for %s but length %d is less than requested limit %d", cache_key, len(bars), limit)
            except Exception as e:
                LOGGER.warning("Failed to retrieve or parse cache for key %s: %s", cache_key, e)

        # Cache miss or insufficient elements: query Alpaca API
        fetch_limit = max(limit, 100)
        params = {
            "timeframe": timeframe,
            "limit": fetch_limit,
            "feed": self.data_feed,
            "adjustment": "raw",
            # Alpaca applies limit after sorting. Ascending order returned the
            # oldest bars in the lookback window, not the latest market bars.
            "sort": "desc",
        }

        # Calculate start time based on timeframe to ensure we retrieve enough data points
        if "Day" in timeframe:
            # 1.6 factor to account for weekends and holidays
            days_to_lookback = int(fetch_limit * 1.6) + 7
            start_dt = datetime.now(timezone.utc) - timedelta(days=days_to_lookback)
            params["start"] = start_dt.strftime("%Y-%m-%d")
        elif "Min" in timeframe:
            # 1 trading day has 390 minutes. Let's convert limit to trading days and look back.
            minutes_per_bar = int("".join(filter(str.isdigit, timeframe)) or "1")
            total_minutes = fetch_limit * minutes_per_bar
            trading_days = (total_minutes // 390) + 1
            calendar_days = int(trading_days * 1.6) + 4
            start_dt = datetime.now(timezone.utc) - timedelta(days=calendar_days)
            params["start"] = start_dt.isoformat()

        response = self.session.get(
            f"{self.base_url}/v2/stocks/{symbol}/bars",
            headers={
                "APCA-API-KEY-ID": self.api_key,
                "APCA-API-SECRET-KEY": self.secret_key,
            },
            params=params,
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        bars = response.json().get("bars", [])
        if not bars:
            raise DataFeedUnavailable(f"Alpaca returned no bars for {symbol} timeframe={timeframe}")
        bars = sorted(bars, key=lambda bar: str(bar.get("t") or ""))
        self._record_observation(symbol, bars[-1], "alpaca")

        if self.redis_client:
            try:
                full_history = len(bars) < fetch_limit
                cache_payload = {
                    "bars": bars,
                    "full_history": full_history
                }
                self.redis_client.setex(
                    cache_key,
                    self.cache_ttl_seconds,
                    json.dumps(cache_payload)
                )
            except Exception as e:
                LOGGER.warning("Failed to write to cache for key %s: %s", cache_key, e)

        return bars[-limit:]

    def _record_observation(self, symbol: str, bar: dict, source: str):
        self.observations.append({
            "source": source,
            "symbol": symbol.upper(),
            "value": float(bar.get("c") or 0.0),
            "exchange_timestamp": bar.get("t"),
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "quality": "verified" if bar.get("c") is not None else "incomplete",
        })
        self.observations = self.observations[-50:]

    def _get_bar_closes(self, symbol: str, timeframe: str, limit: int) -> list[float]:
        bars = self._get_bars(symbol=symbol, timeframe=timeframe, limit=limit)
        closes = [float(bar["c"]) for bar in bars if bar.get("c") is not None]
        if not closes:
            raise DataFeedUnavailable(f"Alpaca returned no closes for {symbol} timeframe={timeframe}")
        return closes

    def get_index_level(self, symbol: str) -> float:
        closes = self._get_bar_closes(symbol=symbol, timeframe="1Day", limit=1)
        return closes[-1]

    def get_latest_price(self, symbol: str) -> float:
        response = self.session.get(
            f"{self.base_url}/v2/stocks/{symbol}/trades/latest",
            headers=self._headers(), params={"feed": self.data_feed}, timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        trade = response.json().get("trade", {})
        price = float(trade.get("p") or 0.0)
        if price <= 0 or not trade.get("t"):
            raise DataFeedUnavailable(f"Alpaca latest trade is incomplete for {symbol}")
        self._record_observation(symbol, {"c": price, "t": trade["t"]}, "alpaca")
        return price

    def get_iv_rank(self, symbol: str) -> float:
        try:
            # 1. Fetch 282 daily closes to calculate 252 days of 30-day historical volatility
            # (252 lookback + 30 window = 282 bars)
            closes = self._get_bar_closes(symbol=symbol, timeframe="1Day", limit=282)
            hv_series = _calculate_historical_volatility(closes, window=30)
            current_hv = hv_series[-1]
        except Exception as exc:
            raise DataFeedUnavailable(f"Failed to calculate historical volatility for {symbol}: {exc}") from exc

        # 2. Try to fetch option chain and ATM IV
        current_iv = None
        try:
            contracts = self.get_option_contracts(symbol)
            if contracts:
                snapshots = self.get_option_chain_snapshots(symbol)
                
                import datetime
                today = datetime.date.today()
                
                valid_contracts = []
                for contract in contracts:
                    c_symbol = contract.get("symbol", "")
                    c_type = contract.get("type", "").lower()
                    if not c_symbol or c_type not in ("call", "put"):
                        continue
                    
                    try:
                        exp_str = contract.get("expiration_date")
                        if exp_str:
                            expiration = datetime.date.fromisoformat(exp_str)
                            dte = (expiration - today).days
                        else:
                            continue
                    except Exception:
                        continue
                    
                    if 30 <= dte <= 45:
                        valid_contracts.append(contract)
                
                if valid_contracts:
                    underlying_price = closes[-1]
                    
                    closest_call_contract = None
                    closest_put_contract = None
                    min_call_diff = float("inf")
                    min_put_diff = float("inf")
                    
                    for contract in valid_contracts:
                        c_symbol = contract.get("symbol", "")
                        c_type = contract.get("type", "").lower()
                        strike = float(contract.get("strike_price") or 0.0)
                        if strike <= 0:
                            continue
                            
                        diff = abs(strike - underlying_price)
                        if c_type == "call":
                            if diff < min_call_diff:
                                min_call_diff = diff
                                closest_call_contract = c_symbol
                        elif c_type == "put":
                            if diff < min_put_diff:
                                min_put_diff = diff
                                closest_put_contract = c_symbol
                    
                    iv_values = []
                    for c_symbol in (closest_call_contract, closest_put_contract):
                        if not c_symbol:
                            continue
                        snap = snapshots.get(c_symbol)
                        if snap:
                            iv = snap.get("impliedVolatility")
                            if iv is not None and isinstance(iv, (int, float)) and iv > 0:
                                iv_values.append(iv)
                    
                    if iv_values:
                        current_iv = sum(iv_values) / len(iv_values)
        except Exception as exc:
            LOGGER.warning("Could not compute live ATM implied volatility for %s: %s. Falling back to HV Rank.", symbol, exc)
            current_iv = None

        # 3. Fallback to HV Rank if option IV is unavailable
        if current_iv is None or current_iv <= 0:
            LOGGER.info("No valid option implied volatility found for %s, calculating HV Rank instead.", symbol)
            current_iv = current_hv
            scale = 1.0
        else:
            scale = current_iv / current_hv if current_hv > 0 else 1.0

        # 4. Scale the daily HV series to estimate historical IV
        historical_iv = [hv * scale for hv in hv_series]
        
        # 5. Compute IV Rank
        min_iv = min(historical_iv)
        max_iv = max(historical_iv)
        
        if max_iv > min_iv:
            iv_rank = ((current_iv - min_iv) / (max_iv - min_iv)) * 100
        else:
            iv_rank = 50.0
            
        return iv_rank

    def get_ema_crossover(self, symbol: str, fast_period: int | None = None, slow_period: int | None = None) -> bool:
        slow = slow_period or self.indicator_settings.slow_ema_period
        fast = fast_period or self.indicator_settings.fast_ema_period
        lookback = max(slow + 5, fast + 5)
        closes = self._get_bar_closes(symbol=symbol, timeframe="1Day", limit=lookback)
        fast_ema = _calculate_ema(closes, fast)
        slow_ema = _calculate_ema(closes, slow)
        return fast_ema > slow_ema

    def get_rsi(self, symbol: str, period: int | None = None) -> float:
        p = period or self.indicator_settings.rsi_period
        closes = self._get_bar_closes(symbol=symbol, timeframe="1Day", limit=p + 20)
        return _calculate_rsi(closes, p)

    def get_intraday_roc(self, symbol: str) -> float:
        lookback = max(self.indicator_settings.intraday_roc_minutes, 2)
        closes = self._get_bar_closes(symbol=symbol, timeframe="1Min", limit=lookback)
        opening_price = closes[0]
        if opening_price == 0:
            raise DataFeedUnavailable(f"Cannot compute ROC with zero opening price for {symbol}")
        return ((closes[-1] - opening_price) / opening_price) * 100

    def get_atr(self, symbol: str, period: int = 14) -> float:
        bars = self._get_bars(symbol=symbol, timeframe="1Day", limit=period + 10)
        return _calculate_atr(bars, period)

    def get_opening_range(self, symbol: str, session_date: str, range_minutes: int = 15) -> dict:
        """Return regular-session ORB/VWAP metrics using candidate-level feed data."""
        from zoneinfo import ZoneInfo

        session = datetime.fromisoformat(session_date).replace(tzinfo=ZoneInfo("America/New_York"))
        start = session.replace(hour=9, minute=30, second=0, microsecond=0)
        now = datetime.now(ZoneInfo("America/New_York"))
        if now <= start + timedelta(minutes=range_minutes):
            raise DataFeedUnavailable("Opening range is not complete")
        response = self.session.get(
            f"{self.base_url}/v2/stocks/{symbol}/bars",
            headers=self._headers(),
            params={
                "timeframe": "1Min", "start": start.astimezone(timezone.utc).isoformat(),
                "end": now.astimezone(timezone.utc).isoformat(), "limit": 1000,
                "feed": self.data_feed, "adjustment": "raw", "sort": "asc",
            },
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        bars = response.json().get("bars", [])
        if len(bars) < range_minutes:
            raise DataFeedUnavailable(f"Incomplete opening range for {symbol}")
        opening = bars[:range_minutes]
        last = bars[-1]
        total_volume = sum(float(bar.get("v") or 0) for bar in bars)
        if total_volume <= 0:
            raise DataFeedUnavailable(f"Opening bars have no volume for {symbol}")
        vwap = sum(
            ((float(bar["h"]) + float(bar["l"]) + float(bar["c"])) / 3) * float(bar.get("v") or 0)
            for bar in bars
        ) / total_volume
        self._record_observation(symbol, last, "alpaca")
        return {
            "open": float(opening[0]["o"]), "high": max(float(bar["h"]) for bar in opening),
            "low": min(float(bar["l"]) for bar in opening), "last": float(last["c"]),
            "vwap": vwap, "volume": total_volume, "bar_count": len(bars),
            "last_timestamp": last.get("t"),
        }


class HybridMarketSnapshotProvider:
    def __init__(
        self,
        alpaca: AlpacaMarketDataProvider | None = None,
        fred: FredMarketDataProvider | None = None,
        fallback: MockMarketSnapshotProvider | None = None,
        allow_mock_fallback: bool = True,
        allow_mock_iv_rank: bool = True,
    ):
        self.alpaca = alpaca
        self.fred = fred
        self.fallback = fallback
        self.allow_mock_fallback = allow_mock_fallback
        self.allow_mock_iv_rank = allow_mock_iv_rank
        self.observations: list[dict] = []

    def get_option_contracts(self, symbol: str) -> list[dict]:
        if self.alpaca:
            try:
                return self.alpaca.get_option_contracts(symbol)
            except DataFeedUnavailable as exc:
                if self.allow_mock_fallback and self.fallback:
                    return self.fallback.get_option_contracts(symbol)
                raise exc
        if self.allow_mock_fallback and self.fallback:
            return self.fallback.get_option_contracts(symbol)
        raise DataFeedUnavailable("No provider configured for options contracts")

    def get_option_chain_snapshots(self, symbol: str) -> dict[str, dict]:
        if self.alpaca:
            try:
                return self.alpaca.get_option_chain_snapshots(symbol)
            except DataFeedUnavailable as exc:
                if self.allow_mock_fallback and self.fallback:
                    return self.fallback.get_option_chain_snapshots(symbol)
                raise exc
        if self.allow_mock_fallback and self.fallback:
            return self.fallback.get_option_chain_snapshots(symbol)
        raise DataFeedUnavailable("No provider configured for options snapshots")

    def _call_with_fallback(
        self,
        method_name: str,
        *args,
        providers: list[object],
        allow_fallback: bool,
    ):
        last_error: Exception | None = None
        for provider in providers:
            if provider is None:
                continue
            try:
                result = getattr(provider, method_name)(*args)
                self.observations.extend(getattr(provider, "observations", [])[-1:])
                self.observations = self.observations[-50:]
                return result
            except DataFeedUnavailable as exc:
                last_error = exc
                LOGGER.info("%s unavailable from %s: %s", method_name, provider.__class__.__name__, exc)
            except requests.RequestException as exc:
                last_error = exc
                LOGGER.warning("%s request failed via %s: %s", method_name, provider.__class__.__name__, exc)

        if allow_fallback and self.fallback is not None:
            LOGGER.info("Falling back to mock provider for %s", method_name)
            result = getattr(self.fallback, method_name)(*args)
            self.observations.extend(getattr(self.fallback, "observations", [])[-1:])
            self.observations = self.observations[-50:]
            return result

        if last_error:
            raise DataFeedUnavailable(str(last_error)) from last_error
        raise DataFeedUnavailable(f"No provider configured for {method_name}")

    def get_index_level(self, symbol: str) -> float:
        providers: list[object] = []
        if self.fred and symbol.upper() in self.fred.series_map:
            providers.append(self.fred)
        providers.append(self.alpaca)
        return self._call_with_fallback(
            "get_index_level",
            symbol,
            providers=providers,
            allow_fallback=self.allow_mock_fallback,
        )

    def get_latest_price(self, symbol: str) -> float:
        return self._call_with_fallback(
            "get_latest_price", symbol, providers=[self.alpaca], allow_fallback=False,
        )

    def get_iv_rank(self, symbol: str) -> float:
        return self._call_with_fallback(
            "get_iv_rank",
            symbol,
            providers=[self.alpaca, self.fred],
            allow_fallback=self.allow_mock_iv_rank,
        )

    def get_ema_crossover(self, symbol: str, fast_period: int | None = None, slow_period: int | None = None) -> bool:
        return self._call_with_fallback(
            "get_ema_crossover",
            symbol,
            fast_period,
            slow_period,
            providers=[self.alpaca],
            allow_fallback=self.allow_mock_fallback,
        )

    def get_rsi(self, symbol: str, period: int | None = None) -> float:
        return self._call_with_fallback(
            "get_rsi",
            symbol,
            period,
            providers=[self.alpaca],
            allow_fallback=self.allow_mock_fallback,
        )

    def get_intraday_roc(self, symbol: str) -> float:
        return self._call_with_fallback(
            "get_intraday_roc",
            symbol,
            providers=[self.alpaca],
            allow_fallback=self.allow_mock_fallback,
        )

    def get_atr(self, symbol: str, period: int = 14) -> float:
        return self._call_with_fallback(
            "get_atr",
            symbol,
            period,
            providers=[self.alpaca],
            allow_fallback=self.allow_mock_fallback,
        )

    def get_opening_range(self, symbol: str, session_date: str, range_minutes: int = 15) -> dict:
        return self._call_with_fallback(
            "get_opening_range", symbol, session_date, range_minutes,
            providers=[self.alpaca], allow_fallback=False,
        )


def build_market_snapshot_provider() -> MarketSnapshotProvider:
    settings = DataFeedSettings.from_env()
    indicators = IndicatorSettings.from_env()
    mock_provider = MockMarketSnapshotProvider()

    if settings.provider == "mock":
        return mock_provider

    alpaca_provider = AlpacaMarketDataProvider(
        api_key=settings.alpaca_api_key,
        secret_key=settings.alpaca_secret_key,
        base_url=settings.alpaca_data_api_url,
        trading_api_url=settings.alpaca_trading_api_url,
        data_feed=settings.alpaca_data_feed,
        indicator_settings=indicators,
        timeout_seconds=settings.request_timeout_seconds,
        redis_url=settings.redis_url,
        cache_ttl_seconds=settings.cache_ttl_seconds,
        options_feed=settings.options_market_data_feed,
        options_min_dte=settings.options_min_dte,
        options_max_dte=settings.options_max_dte,
    )
    fred_provider = FredMarketDataProvider(
        api_key=settings.fred_api_key,
        base_url=settings.fred_api_url,
        series_map=settings.fred_series_map,
        timeout_seconds=settings.request_timeout_seconds,
    )

    if settings.provider == "alpaca":
        return HybridMarketSnapshotProvider(
            alpaca=alpaca_provider,
            fallback=mock_provider if settings.allow_mock_fallback else None,
            allow_mock_fallback=settings.allow_mock_fallback,
            allow_mock_iv_rank=settings.allow_mock_iv_rank,
        )

    if settings.provider == "fred":
        return HybridMarketSnapshotProvider(
            fred=fred_provider,
            fallback=mock_provider if settings.allow_mock_fallback else None,
            allow_mock_fallback=settings.allow_mock_fallback,
            allow_mock_iv_rank=settings.allow_mock_iv_rank,
        )

    return HybridMarketSnapshotProvider(
        alpaca=alpaca_provider,
        fred=fred_provider,
        fallback=mock_provider if settings.allow_mock_fallback else None,
        allow_mock_fallback=settings.allow_mock_fallback,
        allow_mock_iv_rank=settings.allow_mock_iv_rank,
    )
