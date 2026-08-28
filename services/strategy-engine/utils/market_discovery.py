import hashlib
import json
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from utils.alpaca_credentials import alpaca_credentials


class MarketDiscoveryError(RuntimeError):
    pass


@dataclass(frozen=True)
class DiscoverySettings:
    trading_api_url: str
    data_api_url: str
    data_feed: str
    paper_trade: bool
    api_key: str
    secret_key: str
    output_path: str
    minimum_price: float = 2.0
    maximum_price: float = 1000.0
    minimum_daily_dollar_volume: float = 5_000_000.0
    maximum_spread_pct: float = 0.0075
    shortlist_per_lane: int = 20
    maximum_shortlist: int = 50
    batch_size: int = 200
    timeout_seconds: float = 15.0
    require_fractionable: bool = False
    maximum_snapshot_age_seconds: float = 120.0
    minimum_snapshot_coverage: float = 0.50
    minimum_qualified_symbols: int = 20
    screener_top: int = 50
    maximum_screener_future_skew_seconds: float = 5.0
    excluded_name_terms: tuple[str, ...] = (" ETF", " ETN", " FUND", "PROSHARES", "DIREXION", "GRANITESHARES", " SPDR ", "PIMCO ")

    @classmethod
    def from_env(cls):
        paper = os.getenv("ALPACA_PAPER_TRADE", "true").lower() == "true"
        api_key, secret_key = alpaca_credentials(paper)
        trading_api_url = os.getenv(
            "ALPACA_TRADING_API_URL", "https://paper-api.alpaca.markets"
        ).rstrip("/")
        if trading_api_url != "https://paper-api.alpaca.markets":
            raise ValueError("alpaca-trading-bot accepts only the Alpaca paper endpoint")
        return cls(
            trading_api_url=trading_api_url,
            data_api_url=os.getenv("ALPACA_DATA_API_URL", "https://data.alpaca.markets").rstrip("/"),
            data_feed=os.getenv("ALPACA_DATA_FEED", "iex"),
            paper_trade=paper,
            api_key=api_key,
            secret_key=secret_key,
            output_path=os.getenv("DISCOVERY_OUTPUT_PATH", "/app/data/market-shortlist.json"),
            minimum_price=float(os.getenv("DISCOVERY_MIN_PRICE", "2")),
            maximum_price=float(os.getenv("DISCOVERY_MAX_PRICE", "1000")),
            minimum_daily_dollar_volume=float(os.getenv("DISCOVERY_MIN_DOLLAR_VOLUME", "5000000")),
            maximum_spread_pct=float(os.getenv("DISCOVERY_MAX_SPREAD_PCT", "0.0075")),
            shortlist_per_lane=int(os.getenv("DISCOVERY_SHORTLIST_PER_LANE", "20")),
            maximum_shortlist=int(os.getenv("DISCOVERY_MAX_SHORTLIST", "50")),
            batch_size=int(os.getenv("DISCOVERY_BATCH_SIZE", "200")),
            timeout_seconds=float(os.getenv("DISCOVERY_TIMEOUT_SECONDS", "15")),
            require_fractionable=os.getenv("DISCOVERY_REQUIRE_FRACTIONABLE", "false").lower() == "true",
            maximum_snapshot_age_seconds=float(os.getenv("DISCOVERY_MAX_SNAPSHOT_AGE_SECONDS", "120")),
            minimum_snapshot_coverage=float(os.getenv("DISCOVERY_MIN_SNAPSHOT_COVERAGE", "0.50")),
            minimum_qualified_symbols=int(os.getenv("DISCOVERY_MIN_QUALIFIED_SYMBOLS", "20")),
            screener_top=int(os.getenv("DISCOVERY_SCREENER_TOP", "50")),
            maximum_screener_future_skew_seconds=float(
                os.getenv("DISCOVERY_MAX_SCREENER_FUTURE_SKEW_SECONDS", "5")
            ),
            excluded_name_terms=tuple(
                term.strip().upper()
                for term in os.getenv(
                    "DISCOVERY_EXCLUDED_NAME_TERMS",
                    "ETF,ETN,FUND,PROSHARES,DIREXION,GRANITESHARES,SPDR,PIMCO",
                ).split(",")
                if term.strip()
            ),
        )

    def config_hash(self) -> str:
        safe = asdict(self)
        safe.pop("api_key")
        safe.pop("secret_key")
        safe.pop("output_path")
        safe.pop("trading_api_url")
        safe.pop("paper_trade")
        return hashlib.sha256(json.dumps(safe, sort_keys=True).encode()).hexdigest()[:16]


class AlpacaMarketDiscovery:
    def __init__(self, settings: DiscoverySettings, session: requests.Session | None = None):
        self.settings = settings
        self.session = session or requests.Session()

    def run(self, now: datetime | None = None) -> dict:
        timestamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        assets = self._assets()
        eligible_by_symbol = {
            str(asset["symbol"]).upper(): asset for asset in assets if self._eligible_asset(asset)
        }
        screened = self._screener_records(timestamp)
        symbols = sorted(symbol for symbol in screened if symbol in eligible_by_symbol)
        if len(symbols) < self.settings.minimum_qualified_symbols:
            raise MarketDiscoveryError(
                "SIP screener candidate universe is below the configured minimum "
                f"(screened={len(screened)} eligible={len(symbols)} minimum={self.settings.minimum_qualified_symbols})"
            )
        candidates = []
        rejected = {"asset_ineligible": len(screened) - len(symbols), "price": 0}
        for symbol in symbols:
            record = screened[symbol]
            price = float(record.get("price") or 0.0)
            if price > 0 and not self.settings.minimum_price <= price <= self.settings.maximum_price:
                rejected["price"] += 1
                continue
            candidates.append(record)
        coverage = 1.0
        if len(candidates) < self.settings.minimum_qualified_symbols:
            rejected_summary = ",".join(f"{key}={value}" for key, value in sorted(rejected.items()))
            raise MarketDiscoveryError(
                "Qualified discovery universe is below the configured minimum "
                f"(eligible={len(symbols)} qualified={len(candidates)} "
                f"minimum={self.settings.minimum_qualified_symbols} rejected={rejected_summary})"
            )
        lanes = {
            "pullback": self._rank(candidates, "pullback_score"),
            "momentum": self._rank(candidates, "momentum_score"),
            "activity": self._rank(candidates, "activity_score"),
        }
        ordered = []
        for rank in range(self.settings.shortlist_per_lane):
            for lane in ("pullback", "momentum", "activity"):
                values = lanes[lane]
                if rank < len(values) and values[rank]["symbol"] not in ordered:
                    ordered.append(values[rank]["symbol"])
                if len(ordered) >= self.settings.maximum_shortlist:
                    break
        result = {
            "status": "passed" if ordered else "failed",
            "generated_at": timestamp.isoformat(),
            "session_date": timestamp.astimezone(ZoneInfo("America/New_York")).date().isoformat(),
            "config_hash": self.settings.config_hash(),
            "source": "alpaca_sip_screeners_and_candidate_snapshots",
            "universe_count": len(assets),
            "eligible_asset_count": len(eligible_by_symbol),
            "screened_candidate_count": len(symbols),
            "snapshot_count": 0,
            "snapshot_coverage": round(coverage, 6),
            "qualified_count": len(candidates),
            "rejected": rejected,
            "lanes": lanes,
            "symbols": ordered,
        }
        if not ordered:
            raise MarketDiscoveryError("Discovery produced no qualified symbols")
        self._write_atomic(result)
        return result

    def _screener_records(self, now: datetime) -> dict[str, dict]:
        base = f"{self.settings.data_api_url}/v1beta1/screener/stocks"
        records: dict[str, dict] = {}
        requests_to_make = (
            (f"{base}/most-actives", {"top": self.settings.screener_top, "by": "volume"}),
            (f"{base}/most-actives", {"top": self.settings.screener_top, "by": "trades"}),
            (f"{base}/movers", {"top": self.settings.screener_top}),
        )
        for url, params in requests_to_make:
            response = self.session.get(url, headers=self._headers(), params=params, timeout=self.settings.timeout_seconds)
            response.raise_for_status()
            payload = response.json()
            try:
                updated = datetime.fromisoformat(str(payload["last_updated"]).replace("Z", "+00:00"))
                age = (now - updated.astimezone(timezone.utc)).total_seconds()
            except (KeyError, TypeError, ValueError):
                raise MarketDiscoveryError("Alpaca screener timestamp is missing or invalid")
            if age < -self.settings.maximum_screener_future_skew_seconds:
                raise MarketDiscoveryError(f"Alpaca screener timestamp is too far in the future (age_seconds={age:.1f})")
            if age > float(os.getenv("DISCOVERY_MAX_SCREENER_AGE_SECONDS", "120")):
                raise MarketDiscoveryError(f"Alpaca screener data is stale (age_seconds={age:.1f})")
            for key in ("most_actives", "gainers", "losers"):
                values = payload.get(key) or []
                if not isinstance(values, list):
                    raise MarketDiscoveryError("Unexpected Alpaca screener response")
                for item in values:
                    symbol = str(item.get("symbol", "")).upper()
                    if not symbol:
                        continue
                    record = records.setdefault(symbol, {
                        "symbol": symbol, "price": 0.0, "return_pct": 0.0,
                        "relative_volume": 0.0, "spread_pct": 0.0, "dollar_volume": 0.0,
                        "pullback_score": 0.0, "momentum_score": 0.0, "activity_score": 0.0,
                        "screener_updated_at": updated.astimezone(timezone.utc).isoformat(),
                    })
                    price = float(item.get("price") or record["price"] or 0.0)
                    percent = float(item.get("percent_change") or 0.0) / 100.0
                    volume = float(item.get("volume") or 0.0)
                    trades = float(item.get("trade_count") or 0.0)
                    record["price"] = round(price, 4)
                    if item.get("percent_change") is not None:
                        record["return_pct"] = round(percent, 6)
                    record["dollar_volume"] = round(max(record["dollar_volume"], volume * price), 2)
                    record["activity_score"] = round(max(record["activity_score"], volume + trades), 6)
        for record in records.values():
            record["pullback_score"] = round(-record["return_pct"] * 100, 6)
            record["momentum_score"] = round(record["return_pct"] * 100, 6)
        return records

    def _screened_symbols(self) -> set[str]:
        return set(self._screener_records(datetime.now(timezone.utc)))

    def run_premarket(self, output_path: str, now: datetime | None = None) -> dict:
        timestamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        assets = self._assets()
        eligible = {str(asset["symbol"]).upper() for asset in assets if self._eligible_asset(asset)}
        screened = self._screener_records(timestamp)
        symbols = sorted(set(screened) & eligible)
        candidates = []
        rejected = {"asset_ineligible": len(screened) - len(symbols), "price": 0, "gap": 0}
        minimum_gap = float(os.getenv("PREMARKET_MIN_ABS_GAP_PCT", "0.02"))
        for symbol in symbols:
            record = screened[symbol]
            price = float(record["price"] or 0.0)
            gap_pct = float(record["return_pct"] or 0.0)
            if not self.settings.minimum_price <= price <= self.settings.maximum_price:
                rejected["price"] += 1
                continue
            if abs(gap_pct) < minimum_gap:
                rejected["gap"] += 1
                continue
            candidates.append({
                "symbol": symbol,
                "price": round(price, 4),
                "gap_pct": round(gap_pct, 6),
                "screener_updated_at": record["screener_updated_at"],
            })
        long_lane = sorted((x for x in candidates if x["gap_pct"] > 0), key=lambda x: (-x["gap_pct"], x["symbol"]))[:10]
        short_lane = sorted((x for x in candidates if x["gap_pct"] < 0), key=lambda x: (x["gap_pct"], x["symbol"]))[:10]
        minimum_candidates = int(os.getenv("PREMARKET_MINIMUM_CANDIDATES", "4"))
        if len(long_lane) + len(short_lane) < minimum_candidates:
            summary = ",".join(f"{k}={v}" for k, v in sorted(rejected.items()))
            raise MarketDiscoveryError(
                f"Premarket candidate universe is below minimum (candidates={len(long_lane) + len(short_lane)} "
                f"minimum={minimum_candidates} rejected={summary})"
            )
        result = {
            "status": "passed",
            "kind": "opening_research",
            "generated_at": timestamp.isoformat(),
            "session_date": timestamp.astimezone(ZoneInfo("America/New_York")).date().isoformat(),
            "config_hash": self.settings.config_hash(),
            "source": "alpaca_sip_screeners_and_candidate_snapshots",
            "universe_count": len(assets),
            "eligible_asset_count": len(symbols),
            "snapshot_count": 0,
            "qualified_count": len(candidates),
            "rejected": rejected,
            "lanes": {"long": long_lane, "short_research_only": short_lane},
            "symbols": [x["symbol"] for x in long_lane + short_lane],
        }
        original = self.settings.output_path
        object.__setattr__(self.settings, "output_path", output_path)
        try:
            self._write_atomic(result)
        finally:
            object.__setattr__(self.settings, "output_path", original)
        return result

    def _headers(self):
        if not self.settings.api_key or not self.settings.secret_key:
            raise MarketDiscoveryError("Alpaca credentials are not configured")
        return {
            "APCA-API-KEY-ID": self.settings.api_key,
            "APCA-API-SECRET-KEY": self.settings.secret_key,
        }

    def _assets(self) -> list[dict]:
        response = self.session.get(
            f"{self.settings.trading_api_url}/v2/assets",
            headers=self._headers(),
            params={"status": "active", "asset_class": "us_equity"},
            timeout=self.settings.timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list):
            raise MarketDiscoveryError("Unexpected Alpaca assets response")
        return payload

    def _eligible_asset(self, asset: dict) -> bool:
        symbol = str(asset.get("symbol", ""))
        name = str(asset.get("name", "")).upper()
        return bool(
            symbol
            and asset.get("status") == "active"
            and asset.get("tradable") is True
            and asset.get("exchange") in {"NYSE", "NASDAQ", "ARCA", "AMEX", "BATS"}
            and "/" not in symbol
            and len(symbol) <= 5
            and not any(term in name for term in self.settings.excluded_name_terms)
            and (not self.settings.require_fractionable or asset.get("fractionable") is True)
        )

    def _snapshots(self, symbols: list[str]) -> dict[str, dict]:
        snapshots: dict[str, dict] = {}
        for start in range(0, len(symbols), self.settings.batch_size):
            batch = symbols[start : start + self.settings.batch_size]
            response = self.session.get(
                f"{self.settings.data_api_url}/v2/stocks/snapshots",
                headers=self._headers(),
                params={"symbols": ",".join(batch), "feed": self.settings.data_feed},
                timeout=self.settings.timeout_seconds,
            )
            response.raise_for_status()
            payload = response.json()
            values = payload.get("snapshots", payload)
            if not isinstance(values, dict):
                raise MarketDiscoveryError("Unexpected Alpaca snapshots response")
            snapshots.update(values)
        return snapshots

    def _score_snapshots(self, snapshots: dict[str, dict], now: datetime):
        accepted = []
        rejected = {"missing": 0, "stale": 0, "price": 0, "liquidity": 0, "quote": 0}
        for symbol, snapshot in snapshots.items():
            trade = snapshot.get("latestTrade") or {}
            quote = snapshot.get("latestQuote") or {}
            daily = snapshot.get("dailyBar") or {}
            previous = snapshot.get("prevDailyBar") or {}
            price = float(trade.get("p") or daily.get("c") or 0.0)
            volume = float(daily.get("v") or 0.0)
            previous_close = float(previous.get("c") or 0.0)
            previous_volume = float(previous.get("v") or 0.0)
            bid = float(quote.get("bp") or 0.0)
            ask = float(quote.get("ap") or 0.0)
            high = float(daily.get("h") or price)
            low = float(daily.get("l") or price)
            try:
                observed_at = datetime.fromisoformat(str(quote.get("t") or trade.get("t")).replace("Z", "+00:00"))
                if observed_at.tzinfo is None:
                    observed_at = observed_at.replace(tzinfo=timezone.utc)
                age = (now - observed_at.astimezone(timezone.utc)).total_seconds()
            except (TypeError, ValueError):
                age = float("inf")
            if age < 0 or age > self.settings.maximum_snapshot_age_seconds:
                rejected["stale"] += 1
                continue
            if min(price, volume, previous_close, previous_volume) <= 0:
                rejected["missing"] += 1
                continue
            if not self.settings.minimum_price <= price <= self.settings.maximum_price:
                rejected["price"] += 1
                continue
            # Discovery runs shortly after the open, when the current daily bar is
            # necessarily incomplete. Use the completed prior session to measure
            # baseline liquidity so otherwise liquid symbols are not rejected just
            # because only a few minutes of today's volume have accumulated.
            dollar_volume = previous_volume * previous_close
            if dollar_volume < self.settings.minimum_daily_dollar_volume:
                rejected["liquidity"] += 1
                continue
            if bid <= 0 or ask <= 0 or ask < bid:
                rejected["quote"] += 1
                continue
            spread_pct = (ask - bid) / ((ask + bid) / 2)
            if spread_pct > self.settings.maximum_spread_pct:
                rejected["quote"] += 1
                continue
            return_pct = (price / previous_close) - 1
            relative_volume = volume / previous_volume
            range_pct = max(high - low, 0.0) / previous_close
            accepted.append(
                {
                    "symbol": symbol,
                    "price": round(price, 4),
                    "return_pct": round(return_pct, 6),
                    "relative_volume": round(relative_volume, 4),
                    "spread_pct": round(spread_pct, 6),
                    "dollar_volume": round(dollar_volume, 2),
                    "pullback_score": round((-return_pct * 100) + relative_volume - (spread_pct * 100), 6),
                    "momentum_score": round((return_pct * 100) + (relative_volume * 2) + range_pct, 6),
                    "activity_score": round((abs(return_pct) * 100) + (relative_volume * 3) + range_pct, 6),
                }
            )
        return accepted, rejected

    def _rank(self, candidates: list[dict], field: str) -> list[dict]:
        if field == "pullback_score":
            candidates = [item for item in candidates if -0.08 <= item["return_pct"] <= 0.01]
        elif field == "momentum_score":
            candidates = [item for item in candidates if item["return_pct"] >= 0.01]
        elif field == "activity_score":
            candidates = [item for item in candidates if item["activity_score"] > 0]
        ranked = sorted(candidates, key=lambda item: (-item[field], item["symbol"]))
        return ranked[: self.settings.shortlist_per_lane]

    def _write_atomic(self, result: dict):
        path = Path(self.settings.output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._write_json_atomic(path, result)
        history_name = result["generated_at"].replace(":", "-") + f"-{result['config_hash']}.json"
        self._write_json_atomic(path.parent / "market-shortlist-history" / history_name, result)

    @staticmethod
    def _write_json_atomic(path: Path, result: dict):
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False, encoding="utf-8") as handle:
            json.dump(result, handle, indent=2, sort_keys=True)
            handle.write("\n")
            temporary = Path(handle.name)
        temporary.replace(path)


def load_current_shortlist(path: str, now: datetime | None = None, expected_config_hash: str = "") -> dict:
    resolved = Path(path)
    if not resolved.exists():
        raise MarketDiscoveryError("Current-session discovery shortlist is missing")
    try:
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MarketDiscoveryError("Discovery shortlist is unreadable or malformed") from exc
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    session_date = current.astimezone(ZoneInfo("America/New_York")).date().isoformat()
    if payload.get("status") != "passed" or payload.get("session_date") != session_date:
        raise MarketDiscoveryError("Discovery shortlist is failed or stale")
    if expected_config_hash and payload.get("config_hash") != expected_config_hash:
        raise MarketDiscoveryError("Discovery shortlist configuration does not match runtime")
    symbols = payload.get("symbols")
    if not isinstance(symbols, list) or not symbols:
        raise MarketDiscoveryError("Discovery shortlist contains no symbols")
    return payload


def write_discovery_failure(settings: DiscoverySettings, error: Exception, now: datetime | None = None):
    timestamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    payload = {
        "status": "failed",
        "generated_at": timestamp.isoformat(),
        "session_date": timestamp.astimezone(ZoneInfo("America/New_York")).date().isoformat(),
        "config_hash": settings.config_hash(),
        "source": "alpaca_assets_and_snapshots",
        "error_type": type(error).__name__,
        # Persist only messages produced by our bounded discovery exception type.
        # Arbitrary request exceptions can contain URLs or other sensitive context.
        "error_reason": str(error)[:1000] if isinstance(error, MarketDiscoveryError) else "external_dependency_failure",
        "symbols": [],
    }
    path = Path(settings.output_path)
    AlpacaMarketDiscovery._write_json_atomic(path, payload)
    history_name = payload["generated_at"].replace(":", "-") + f"-{payload['config_hash']}-failed.json"
    AlpacaMarketDiscovery._write_json_atomic(path.parent / "market-shortlist-history" / history_name, payload)
