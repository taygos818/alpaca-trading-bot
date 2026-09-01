from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import sys

import pytest
import requests


ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "services" / "strategy-engine"
sys.path.insert(0, str(ENGINE))

from agent_contracts import ContractValidationError  # noqa: E402
from external_data import (  # noqa: E402
    DataQualityEngine,
    DataQualityPolicy,
    FinnhubAdapter,
    FinnhubSettings,
    MemoryEvidenceCache,
    ProviderDisabled,
    ProviderRateLimited,
    ProviderUnavailable,
    YFinanceAdapter,
    YFinanceSettings,
)
from external_data.common import make_evidence  # noqa: E402
from multi_agent import DataQualityAgent  # noqa: E402


NOW = datetime(2026, 8, 28, 16, 0, tzinfo=timezone.utc)


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def get(self, url, headers=None, params=None, timeout=None):
        self.calls.append({"url": url, "headers": headers or {}, "params": params or {}, "timeout": timeout})
        response = self.routes[url]
        if isinstance(response, Exception):
            raise response
        return response


def test_finnhub_news_is_cached_source_attributed_and_never_leaks_key():
    event_epoch = int((NOW - timedelta(hours=1)).timestamp())
    session = FakeSession(
        {
            "https://finnhub.io/api/v1/company-news": FakeResponse(
                [{
                    "category": "company",
                    "datetime": event_epoch,
                    "headline": "Company schedules product event",
                    "id": 123,
                    "related": "AAPL",
                    "source": "Example Wire",
                    "summary": "Factual summary only.",
                    "url": "https://example.test/news/123",
                }]
            )
        }
    )
    adapter = FinnhubAdapter(
        FinnhubSettings(enabled=True, api_key="test-finnhub-key", entitlement="contest"),
        session=session,
    )
    kwargs = dict(symbol="AAPL", start=date(2026, 8, 27), end=date(2026, 8, 28), trace_id="trace.001", received_at=NOW)
    first = adapter.company_news(**kwargs)
    second = adapter.company_news(**kwargs)
    assert first == second
    assert len(session.calls) == 1
    assert session.calls[0]["headers"] == {"X-Finnhub-Token": "test-finnhub-key"}
    assert "test-finnhub-key" not in first[0].source_uri
    assert first[0].authority == "licensed_research"
    assert first[0].transformation_version == "finnhub-company-news-v1"
    assert first[0].is_fresh is True


def test_cached_evidence_is_rebound_to_new_trace_and_expires():
    clock = [100.0]
    cache = MemoryEvidenceCache(clock=lambda: clock[0])
    event_epoch = int((NOW - timedelta(minutes=5)).timestamp())
    session = FakeSession(
        {"https://finnhub.io/api/v1/company-news": FakeResponse([{"datetime": event_epoch, "headline": "Event"}])}
    )
    adapter = FinnhubAdapter(
        FinnhubSettings(enabled=True, api_key="key", cache_ttl_seconds=30),
        session=session,
        cache=cache,
    )
    query = ("AAPL", date(2026, 8, 28), date(2026, 8, 28))
    first = adapter.company_news(*query, trace_id="trace.001", received_at=NOW)
    second = adapter.company_news(*query, trace_id="trace.002", received_at=NOW + timedelta(seconds=5))
    assert len(session.calls) == 1
    assert first[0].trace_id == "trace.001"
    assert second[0].trace_id == "trace.002"
    assert first[0].record_id != second[0].record_id
    assert first[0].raw_sha256 == second[0].raw_sha256
    assert first[0].received_at == second[0].received_at == NOW

    clock[0] = 131.0
    adapter.company_news(*query, trace_id="trace.003", received_at=NOW + timedelta(seconds=31))
    assert len(session.calls) == 2


def test_finnhub_rate_limit_and_outage_fail_explicitly():
    rate_limited = FinnhubAdapter(
        FinnhubSettings(enabled=True, api_key="key"),
        session=FakeSession({"https://finnhub.io/api/v1/company-news": FakeResponse({}, 429)}),
    )
    with pytest.raises(ProviderRateLimited):
        rate_limited.company_news("AAPL", date(2026, 8, 28), date(2026, 8, 28), trace_id="trace.001", received_at=NOW)

    outage = FinnhubAdapter(
        FinnhubSettings(enabled=True, api_key="key"),
        session=FakeSession({"https://finnhub.io/api/v1/company-news": requests.ConnectionError("offline")}),
    )
    with pytest.raises(ProviderUnavailable, match="ConnectionError"):
        outage.company_news("AAPL", date(2026, 8, 28), date(2026, 8, 28), trace_id="trace.001", received_at=NOW)


def test_finnhub_stale_news_is_visible_for_quality_veto():
    session = FakeSession(
        {
            "https://finnhub.io/api/v1/company-news": FakeResponse(
                [{"datetime": int((NOW - timedelta(days=10)).timestamp()), "headline": "Old event"}]
            )
        }
    )
    adapter = FinnhubAdapter(FinnhubSettings(enabled=True, api_key="key", max_news_age_hours=24), session=session)
    item = adapter.company_news("AAPL", date(2026, 8, 1), date(2026, 8, 28), trace_id="trace.001", received_at=NOW)[0]
    assert item.is_fresh is False
    assert DataQualityEngine().evaluate((item,), NOW).veto is True


def test_finnhub_future_earnings_is_scheduled_evidence():
    session = FakeSession(
        {
            "https://finnhub.io/api/v1/calendar/earnings": FakeResponse(
                {"earningsCalendar": [{"date": "2026-09-01", "symbol": "AAPL", "hour": "amc"}]}
            )
        }
    )
    adapter = FinnhubAdapter(FinnhubSettings(enabled=True, api_key="key"), session=session)
    item = adapter.earnings_calendar(
        "AAPL", date(2026, 8, 28), date(2026, 9, 2), trace_id="trace.001", received_at=NOW
    )[0]
    assert item.temporal_kind == "scheduled"
    assert item.event_time > item.received_at
    assert item.is_fresh is True


class FakeFrame:
    def __init__(self, rows):
        self.rows = rows

    def iterrows(self):
        return iter(self.rows)


class FakeTicker:
    def __init__(self, frame=None, calendar=None, error=None):
        self.frame = frame
        self._calendar = calendar
        self.error = error
        self.history_calls = 0

    def history(self, **kwargs):
        self.history_calls += 1
        if self.error:
            raise self.error
        return self.frame

    @property
    def calendar(self):
        if self.error:
            raise self.error
        return self._calendar


def test_yfinance_history_is_unofficial_cached_research_only():
    ticker = FakeTicker(
        FakeFrame(
            [
                (NOW - timedelta(days=1), {"Open": 100, "High": 102, "Low": 99, "Close": 101.5, "Volume": 1000}),
                (NOW, {"Open": 101, "High": 103, "Low": 100, "Close": 102.25, "Volume": 1200}),
            ]
        )
    )
    adapter = YFinanceAdapter(YFinanceSettings(enabled=True), ticker_factory=lambda symbol: ticker)
    first = adapter.history("AAPL", trace_id="trace.001", received_at=NOW)
    second = adapter.history("AAPL", trace_id="trace.001", received_at=NOW)
    assert first == second
    assert ticker.history_calls == 1
    assert all(item.authority == "unofficial_research" for item in first)
    assert all(item.entitlement == "unofficial-personal-research" for item in first)
    assert first[-1].numeric_value == Decimal("102.25")


def test_yfinance_calendar_and_failure_are_isolated():
    ticker = FakeTicker(calendar={"Earnings Date": [NOW + timedelta(days=5)]})
    item = YFinanceAdapter(YFinanceSettings(enabled=True), ticker_factory=lambda symbol: ticker).calendar(
        "AAPL", trace_id="trace.001", received_at=NOW
    )[0]
    assert item.temporal_kind == "scheduled"
    assert item.authority == "unofficial_research"

    failing = YFinanceAdapter(
        YFinanceSettings(enabled=True),
        ticker_factory=lambda symbol: FakeTicker(error=RuntimeError("upstream changed")),
    )
    with pytest.raises(ProviderUnavailable, match="RuntimeError"):
        failing.history("AAPL", trace_id="trace.002", received_at=NOW)


def test_each_external_provider_flag_fails_closed_independently():
    with pytest.raises(ProviderDisabled):
        FinnhubAdapter(FinnhubSettings(enabled=False)).company_news(
            "AAPL", date(2026, 8, 28), date(2026, 8, 28), trace_id="trace.001", received_at=NOW
        )
    with pytest.raises(ProviderDisabled):
        YFinanceAdapter(YFinanceSettings(enabled=False)).history("AAPL", trace_id="trace.001", received_at=NOW)


def numeric_evidence(provider, value, *, fresh=True, authority="licensed_research"):
    return make_evidence(
        provider=provider,
        trace_id="trace.quality",
        instrument="AAPL",
        event_time=NOW - timedelta(minutes=1),
        received_at=NOW,
        value_name="completed_bar_close",
        payload={"close": str(value)},
        source_uri=f"https://{provider}.test",
        entitlement="test",
        is_fresh=fresh,
        authority=authority,
        session="regular",
        temporal_kind="observed",
        transformation_version="test-v1",
        numeric_value=Decimal(str(value)),
    )


def test_quality_engine_exposes_disagreement_without_averaging():
    alpaca = numeric_evidence("alpaca", "100", authority="broker_truth")
    secondary = numeric_evidence("yfinance", "106", authority="unofficial_research")
    report = DataQualityEngine(DataQualityPolicy(max_relative_disagreement=Decimal("0.02"))).evaluate(
        (alpaca, secondary), NOW
    )
    assert report.veto is True
    assert len(report.disagreements) == 1
    assert {item.numeric_value for item in report.accepted} == {Decimal("100"), Decimal("106")}
    assert report.disagreements[0].relative_difference > Decimal("0.02")


def test_quality_engine_accepts_current_values_within_tolerance_and_rejects_old_receipts():
    alpaca = numeric_evidence("alpaca", "100", authority="broker_truth")
    secondary = numeric_evidence("yfinance", "101", authority="unofficial_research")
    report = DataQualityEngine(DataQualityPolicy(max_relative_disagreement=Decimal("0.02"))).evaluate(
        (alpaca, secondary), NOW
    )
    assert report.veto is False
    assert report.rejected_ids == ()
    assert report.disagreements == ()

    old = replace(
        alpaca,
        event_time=NOW - timedelta(minutes=21),
        received_at=NOW - timedelta(minutes=20),
        created_at=NOW - timedelta(minutes=20),
    )
    rejected = DataQualityEngine(DataQualityPolicy(max_receipt_age_seconds=900)).evaluate((old,), NOW)
    assert rejected.veto is True
    assert rejected.rejected_ids == (old.record_id,)


def test_quality_agent_refuses_stale_or_disagreeing_evidence_before_freeze():
    quality_agent = DataQualityAgent(DataQualityEngine())
    with pytest.raises(ContractValidationError, match="data-quality veto"):
        quality_agent.freeze(
            "trace.quality",
            (numeric_evidence("alpaca", "100", authority="broker_truth"), numeric_evidence("yfinance", "110")),
            NOW,
        )
    with pytest.raises(ContractValidationError, match="data-quality veto"):
        quality_agent.freeze("trace.quality", (numeric_evidence("alpaca", "100", fresh=False),), NOW)
