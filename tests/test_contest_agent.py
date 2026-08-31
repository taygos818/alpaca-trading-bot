from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
import sys


ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "services" / "strategy-engine"
sys.path.insert(0, str(ENGINE))

from agent_contracts import Direction  # noqa: E402
from contest_agent import ContestPaperAgent, _bounded_fresh_news, _direction_and_rank, _list_payload  # noqa: E402
from execution_gateway import CliResponse  # noqa: E402
from external_data import ProviderUnavailable  # noqa: E402


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
