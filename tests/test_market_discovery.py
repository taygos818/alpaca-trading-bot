import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest


SERVICE_DIR = Path(__file__).resolve().parents[1] / "services" / "strategy-engine"
sys.path.insert(0, str(SERVICE_DIR))

from utils.market_discovery import (
    AlpacaMarketDiscovery,
    DiscoverySettings,
    MarketDiscoveryError,
    load_current_shortlist,
    write_discovery_failure,
)


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, assets, snapshots):
        self.assets = assets
        self.snapshots = snapshots

    def get(self, url, headers=None, params=None, timeout=None):
        updated = next(iter(self.snapshots.values()), {}).get("latestTrade", {}).get("t", "2026-08-10T15:00:00+00:00")
        if url.endswith("/v2/assets"):
            return FakeResponse(self.assets)
        if "/screener/stocks/most-actives" in url:
            return FakeResponse({"last_updated": updated, "most_actives": [
                {
                    "symbol": symbol,
                    "volume": (value.get("dailyBar") or {}).get("v", 100_000),
                    "trade_count": 10_000,
                }
                for symbol, value in self.snapshots.items()
            ]})
        if "/screener/stocks/movers" in url:
            movers = []
            for symbol, value in self.snapshots.items():
                price = float((value.get("latestTrade") or {}).get("p") or 0)
                previous = float((value.get("prevDailyBar") or {}).get("c") or price or 1)
                movers.append({
                    "symbol": symbol,
                    "price": price,
                    "percent_change": ((price / previous) - 1) * 100,
                    "volume": (value.get("dailyBar") or {}).get("v", 0),
                })
            return FakeResponse({"last_updated": updated, "gainers": movers, "losers": []})
        requested = (params or {}).get("symbols", "").split(",")
        return FakeResponse({"snapshots": {symbol: self.snapshots[symbol] for symbol in requested if symbol in self.snapshots}})


def settings(tmp_path):
    return DiscoverySettings(
        trading_api_url="https://paper-api.alpaca.markets",
        data_api_url="https://data.alpaca.markets",
        data_feed="iex",
        paper_trade=True,
        api_key="key",
        secret_key="secret",
        output_path=str(tmp_path / "shortlist.json"),
        minimum_daily_dollar_volume=1_000_000,
        shortlist_per_lane=2,
        maximum_shortlist=3,
        minimum_qualified_symbols=1,
    )


def asset(symbol, **overrides):
    value = {"symbol": symbol, "name": f"{symbol} Corporation", "status": "active", "tradable": True, "fractionable": True, "exchange": "NASDAQ"}
    value.update(overrides)
    return value


def snapshot(price, previous=100, volume=100_000, previous_volume=100_000, bid=None, ask=None):
    bid = price - 0.01 if bid is None else bid
    ask = price + 0.01 if ask is None else ask
    return {
        "latestTrade": {"p": price, "t": "2026-08-10T15:00:00+00:00"},
        "latestQuote": {"bp": bid, "ap": ask, "t": "2026-08-10T15:00:00+00:00"},
        "dailyBar": {"c": price, "h": price * 1.01, "l": price * 0.99, "v": volume},
        "prevDailyBar": {"c": previous, "v": previous_volume},
    }


def test_discovery_filters_and_deterministically_ranks_lanes(tmp_path):
    configured = settings(tmp_path)
    session = FakeSession(
        [asset("UP"), asset("DOWN"), asset("ACTV"), asset("WIDE"), asset("OTC", exchange="OTC")],
        {
            "UP": snapshot(110),
            "DOWN": snapshot(95),
            "ACTV": snapshot(101, volume=410_000),
            "WIDE": snapshot(100, bid=90, ask=110),
        },
    )
    result = AlpacaMarketDiscovery(configured, session=session).run(
        now=datetime(2026, 8, 10, 15, 0, tzinfo=timezone.utc)
    )
    assert result["lanes"]["momentum"][0]["symbol"] == "UP"
    assert result["lanes"]["pullback"][0]["symbol"] == "DOWN"
    assert result["lanes"]["activity"][0]["symbol"] == "ACTV"
    # Candidate discovery no longer treats sparse IEX quotes as whole-market
    # eligibility. Quote/spread freshness is enforced immediately before entry.
    assert "WIDE" in result["symbols"]
    assert len(result["symbols"]) == 4
    persisted = json.loads(Path(configured.output_path).read_text())
    assert persisted["config_hash"] == configured.config_hash()
    history = list((Path(configured.output_path).parent / "market-shortlist-history").glob("*.json"))
    assert len(history) == 1


def test_shortlist_rejects_stale_failed_and_wrong_configuration(tmp_path):
    configured = settings(tmp_path)
    write_discovery_failure(configured, RuntimeError("secret must not be persisted"), now=datetime(2026, 8, 10, tzinfo=timezone.utc))
    with pytest.raises(MarketDiscoveryError, match="failed or stale"):
        load_current_shortlist(configured.output_path, now=datetime(2026, 8, 10, 12, tzinfo=timezone.utc))
    payload = {"status": "passed", "session_date": "2026-08-10", "config_hash": "wrong", "symbols": ["SPY"]}
    Path(configured.output_path).write_text(json.dumps(payload))
    with pytest.raises(MarketDiscoveryError, match="configuration"):
        load_current_shortlist(
            configured.output_path,
            now=datetime(2026, 8, 10, 12, tzinfo=timezone.utc),
            expected_config_hash=configured.config_hash(),
        )


def test_current_passing_shortlist_can_be_detected_for_session_freeze(tmp_path):
    configured = settings(tmp_path)
    payload = {
        "status": "passed",
        "session_date": "2026-08-10",
        "config_hash": configured.config_hash(),
        "symbols": ["SPY"],
    }
    Path(configured.output_path).write_text(json.dumps(payload))
    loaded = load_current_shortlist(
        configured.output_path,
        now=datetime(2026, 8, 10, 15, 0, tzinfo=timezone.utc),
        expected_config_hash=configured.config_hash(),
    )
    assert loaded["symbols"] == ["SPY"]


def test_discovery_fails_when_every_snapshot_is_rejected(tmp_path):
    configured = settings(tmp_path)
    session = FakeSession([asset("BAD")], {"BAD": snapshot(1.0)})
    with pytest.raises(MarketDiscoveryError, match="below the configured minimum"):
        AlpacaMarketDiscovery(configured, session=session).run(now=datetime(2026, 8, 10, 15, 0, tzinfo=timezone.utc))


def test_discovery_uses_screener_activity_without_requiring_opening_snapshot_volume(tmp_path):
    configured = settings(tmp_path)
    # Five minutes after the open, today's partial volume is small even though the
    # symbol cleared the liquidity floor in the completed prior session.
    early_snapshot = snapshot(
        100,
        previous=100,
        volume=100,
        previous_volume=100_000,
    )
    early_snapshot["latestTrade"]["t"] = "2026-08-10T13:35:00+00:00"
    early_snapshot["latestQuote"]["t"] = "2026-08-10T13:35:00+00:00"
    result = AlpacaMarketDiscovery(
        configured,
        session=FakeSession([asset("EARLY")], {"EARLY": early_snapshot}),
    ).run(now=datetime(2026, 8, 10, 13, 35, tzinfo=timezone.utc))
    assert result["qualified_count"] == 1
    assert result["lanes"]["pullback"][0]["dollar_volume"] == 10_000


def test_screener_allows_small_bounded_future_clock_skew(tmp_path):
    configured = settings(tmp_path)
    value = snapshot(100)
    value["latestTrade"]["t"] = "2026-08-10T15:00:00.400000+00:00"
    result = AlpacaMarketDiscovery(
        configured, session=FakeSession([asset("SKEW")], {"SKEW": value})
    ).run(now=datetime(2026, 8, 10, 15, 0, tzinfo=timezone.utc))
    assert result["status"] == "passed"


def test_screener_rejects_timestamp_beyond_future_clock_tolerance(tmp_path):
    configured = settings(tmp_path)
    value = snapshot(100)
    value["latestTrade"]["t"] = "2026-08-10T15:00:06+00:00"
    with pytest.raises(MarketDiscoveryError, match="too far in the future"):
        AlpacaMarketDiscovery(
            configured, session=FakeSession([asset("FUTURE")], {"FUTURE": value})
        ).run(now=datetime(2026, 8, 10, 15, 0, tzinfo=timezone.utc))


def test_premarket_discovery_ranks_long_and_short_gaps(tmp_path):
    configured = settings(tmp_path)
    now = datetime(2026, 8, 10, 13, 20, tzinfo=timezone.utc)
    values = {
        "UP": snapshot(105, previous=100),
        "DOWN": snapshot(94, previous=100),
        "FLAT": snapshot(101, previous=100),
        "UP2": snapshot(104, previous=100),
    }
    for value in values.values():
        value["latestTrade"]["t"] = now.isoformat()
        value["latestQuote"]["t"] = now.isoformat()
    with patch.dict(os.environ, {"PREMARKET_MINIMUM_CANDIDATES": "3"}):
        result = AlpacaMarketDiscovery(
            configured,
            session=FakeSession([asset(x) for x in values], values),
        ).run_premarket(str(tmp_path / "premarket.json"), now=now)
    assert [x["symbol"] for x in result["lanes"]["long"]] == ["UP", "UP2"]
    assert [x["symbol"] for x in result["lanes"]["short_research_only"]] == ["DOWN"]
    assert "FLAT" not in result["symbols"]


def test_failure_artifact_keeps_safe_diagnostics_but_redacts_external_errors(tmp_path):
    configured = settings(tmp_path)
    write_discovery_failure(
        configured,
        MarketDiscoveryError("qualified=0 rejected=stale=10"),
        now=datetime(2026, 8, 10, tzinfo=timezone.utc),
    )
    payload = json.loads(Path(configured.output_path).read_text())
    assert payload["error_reason"] == "qualified=0 rejected=stale=10"

    write_discovery_failure(
        configured,
        RuntimeError("secret must not be persisted"),
        now=datetime(2026, 8, 10, tzinfo=timezone.utc),
    )
    payload = json.loads(Path(configured.output_path).read_text())
    assert payload["error_reason"] == "external_dependency_failure"
    assert "secret" not in json.dumps(payload)


def test_discovery_excludes_fund_products_by_audited_name_terms(tmp_path):
    configured = settings(tmp_path)
    fund = asset("FUND", name="Example Daily ETF")
    assert AlpacaMarketDiscovery(configured, session=FakeSession([], {}))._eligible_asset(fund) is False


def test_semantic_config_hash_ignores_environment_specific_endpoint_and_path(tmp_path):
    paper = settings(tmp_path)
    live_values = {**paper.__dict__}
    live_values.update(
        trading_api_url="https://api.alpaca.markets",
        paper_trade=False,
        api_key="different-live-key",
        secret_key="different-live-secret",
        output_path=str(tmp_path / "live-shortlist.json"),
    )
    live = DiscoverySettings(**live_values)
    assert live.config_hash() == paper.config_hash()
