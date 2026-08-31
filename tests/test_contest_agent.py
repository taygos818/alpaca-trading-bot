from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
import inspect
import sys


ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "services" / "strategy-engine"
sys.path.insert(0, str(ENGINE))

from agent_contracts import Direction  # noqa: E402
from contest_agent import (  # noqa: E402
    ContestPaperAgent,
    _bounded_fresh_news,
    _direction_and_rank,
    _list_payload,
    _option_underlying,
    _order_underlyings,
    _reconcile_candidate_direction,
)
from agent_contracts import AnalysisDisposition  # noqa: E402
from execution_gateway import CliResponse  # noqa: E402
from external_data import ProviderUnavailable  # noqa: E402
from defined_risk_options import DiscoveryCandidate  # noqa: E402


def test_dynamic_shortlist_direction_and_rank_has_no_symbol_allowlist():
    shortlist = {
        "lanes": {
            "activity": [
                {"symbol": "XYZ", "return_pct": "-0.03", "activity_score": "8.2"},
                {"symbol": "ANF", "return_pct": "0.04", "activity_score": "7.1"},
            ],
            "momentum": [{"symbol": "ANF", "return_pct": "0.04", "momentum_score": "9.5"}],
        }
    }
    assert _direction_and_rank(shortlist, "XYZ") == (Direction.BEARISH, Decimal("8.2"))
    assert _direction_and_rank(shortlist, "ANF") == (Direction.BULLISH, Decimal("9.5"))


def test_payload_normalization_is_fail_closed_for_unknown_shapes():
    assert _list_payload([{"id": "1"}], "orders") == [{"id": "1"}]
    assert _list_payload({"orders": [{"id": "1"}]}, "orders") == [{"id": "1"}]
    assert _list_payload({"unexpected": [{"id": "1"}]}, "orders") == []


def test_fresh_news_is_bounded_to_most_recent_items(monkeypatch):
    monkeypatch.setenv("M6_MAX_NEWS_ITEMS_PER_CANDIDATE", "2")
    news = tuple(SimpleNamespace(is_fresh=True, record_id=f"news.{index}") for index in range(4))

    bounded = _bounded_fresh_news(news)

    assert [item.record_id for item in bounded] == ["news.2", "news.3"]


def test_direction_reconciles_to_unanimous_technical_and_catalyst_consensus():
    discovered = DiscoveryCandidate(
        symbol="AMD",
        direction=Direction.BULLISH,
        catalyst_evidence_ids=("evidence.catalyst",),
        completed_bar_evidence_ids=("evidence.bar",),
        correlation_group="broad_equity",
        rank=Decimal("10"),
    )
    analyses = (
        SimpleNamespace(agent_name="technical", direction=Direction.BEARISH, confidence=Decimal("0.80"), disposition=AnalysisDisposition.ANALYZE),
        SimpleNamespace(agent_name="catalyst", direction=Direction.BEARISH, confidence=Decimal("0.75"), disposition=AnalysisDisposition.ANALYZE),
        SimpleNamespace(agent_name="macro", direction=Direction.BEARISH, confidence=Decimal("0.70"), disposition=AnalysisDisposition.ANALYZE),
    )

    reconciled = _reconcile_candidate_direction(discovered, analyses, Decimal("0.50"))

    assert reconciled.direction is Direction.BEARISH


def test_option_selection_clock_is_not_frozen_to_cycle_start():
    source = inspect.getsource(ContestPaperAgent.run_once)
    assert "clock=lambda: datetime.now(timezone.utc)" in source
    assert "clock=lambda current=timestamp" not in source


def test_option_positions_and_multileg_orders_resolve_to_underlying():
    assert _option_underlying("AAL260911P00011500") == "AAL"
    assert _option_underlying("SPY") == "SPY"
    assert _order_underlyings({
        "legs": [
            {"symbol": "AAL260911P00011500"},
            {"symbol": "AAL260911P00010500"},
        ]
    }) == {"AAL"}


def test_portfolio_groups_option_legs_and_tracks_underlying_risk():
    agent = ContestPaperAgent.__new__(ContestPaperAgent)
    account = {
        "equity": "100000",
        "last_equity": "100000",
        "cash": "99000",
        "options_buying_power": "98000",
        "options_trading_level": 3,
    }
    positions = [
        {"symbol": "AAL260911P00011500", "cost_basis": "1000"},
        {"symbol": "AAL260911P00010500", "cost_basis": "-200"},
    ]

    state, portfolio = agent._portfolio(account, positions, [])

    assert portfolio.open_underlyings == ("AAL",)
    assert portfolio.open_position_count == 1
    assert portfolio.underlying_maximum_loss == (("AAL", Decimal("1200")),)
    assert state.open_position_count == 1
    assert state.reserved_maximum_loss == Decimal("1200")


def test_market_closed_cycle_never_calls_discovery_data_ai_or_submission():
    agent = ContestPaperAgent.__new__(ContestPaperAgent)
    agent.config = SimpleNamespace(enabled=True)
    agent.gateway = SimpleNamespace(clock=lambda: CliResponse("clock", {"is_open": False}, 0))
    assert agent.run_once(datetime(2026, 8, 28, 20, 0, tzinfo=timezone.utc)) == ()


def test_compose_runs_contest_entrypoint_with_default_off_submission():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    engine = compose.split("  strategy-engine:", 1)[1].split("  monitor-dash:", 1)[0]
    assert 'BOT_STRATEGY: defined_risk_options' in engine
    assert 'command: ["python", "contest_agent.py"]' in engine
    assert "PAPER_ORDER_SUBMISSION_ENABLED: ${PAPER_ORDER_SUBMISSION_ENABLED:-false}" in engine
    assert "DEFINED_RISK_OPTIONS_ENABLED: ${DEFINED_RISK_OPTIONS_ENABLED:-false}" in engine


def test_optional_research_failure_is_recorded_without_blocking_candidate(monkeypatch):
    monkeypatch.setenv("YFINANCE_ENABLED", "true")
    monkeypatch.setenv("FRED_ENABLED", "true")
    agent = ContestPaperAgent.__new__(ContestPaperAgent)
    agent.yfinance = SimpleNamespace(
        history=lambda *args, **kwargs: (_ for _ in ()).throw(ProviderUnavailable("missing optional module"))
    )
    fred_item = SimpleNamespace(provider="fred")
    agent.fred = SimpleNamespace(fetch=lambda *args, **kwargs: (fred_item,))

    research, failures = agent._optional_research("AAL", "trace.optional", datetime.now(timezone.utc))

    assert research == [fred_item]
    assert failures == [{
        "provider": "yfinance",
        "error_type": "ProviderUnavailable",
        "reason": "optional secondary research unavailable",
    }]
