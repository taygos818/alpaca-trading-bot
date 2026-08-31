from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "services" / "strategy-engine"
sys.path.insert(0, str(ENGINE))

from agent_contracts import (  # noqa: E402
    AgentAnalysis,
    AnalysisDisposition,
    Direction,
    EvidenceItem,
    OptionRight,
    OptionsProposal,
    contract_fingerprint,
)
from defined_risk_options import (  # noqa: E402
    DefinedRiskOptionsConfig,
    DefinedRiskOptionsStrategy,
    DiscoveryCandidate,
    DynamicOptionsProposalBuilder,
    ExitCommandFactory,
    ExitDecisionEngine,
    ExitPlanFactory,
    ExitPlanState,
    JsonlExitPlanStore,
    OptionSnapshot,
    OptionsRiskState,
    StrategyDisabled,
    normalize_alpaca_chain,
)
from multi_agent import DataQualityAgent  # noqa: E402


NOW = datetime(2026, 8, 28, 16, 0, tzinfo=timezone.utc)
EXPIRATION = date(2026, 9, 11)


def frozen_context(direction=Direction.BULLISH):
    evidence = (
        EvidenceItem(
            record_id="evidence.bar",
            trace_id="trace.options",
            provider="alpaca_sip",
            instrument="ANF",
            event_time=NOW - timedelta(minutes=1),
            received_at=NOW - timedelta(seconds=30),
            raw_sha256="a" * 64,
            value_name="completed_bar_signal",
            value="confirmed",
            created_at=NOW,
            source_uri="https://data.alpaca.markets",
            entitlement="sip",
            is_fresh=True,
            authority="broker_truth",
            session="regular",
        ),
        EvidenceItem(
            record_id="evidence.catalyst",
            trace_id="trace.options",
            provider="finnhub",
            instrument="ANF",
            event_time=NOW - timedelta(minutes=10),
            received_at=NOW - timedelta(minutes=9),
            raw_sha256="b" * 64,
            value_name="company_news_event",
            value="source-attributed catalyst",
            created_at=NOW,
            source_uri="https://example.test/news",
            entitlement="free-tier",
            is_fresh=True,
            authority="licensed_research",
            session="event",
        ),
    )
    bundle = DataQualityAgent().freeze("trace.options", evidence, NOW)
    fingerprint = contract_fingerprint(bundle)
    analyses = (
        AgentAnalysis(
            record_id="analysis.technical",
            trace_id="trace.options",
            agent_name="technical",
            evidence_bundle_id=bundle.record_id,
            evidence_fingerprint=fingerprint,
            cited_evidence_ids=("evidence.bar",),
            direction=direction,
            confidence=Decimal("0.70"),
            thesis="Completed bar confirms direction.",
            contradictions=(),
            created_at=NOW,
        ),
        AgentAnalysis(
            record_id="analysis.catalyst",
            trace_id="trace.options",
            agent_name="catalyst",
            evidence_bundle_id=bundle.record_id,
            evidence_fingerprint=fingerprint,
            cited_evidence_ids=("evidence.catalyst",),
            direction=direction,
            confidence=Decimal("0.65"),
            thesis="Catalyst confirms direction.",
            contradictions=(),
            created_at=NOW,
        ),
        AgentAnalysis(
            record_id="analysis.macro",
            trace_id="trace.options",
            agent_name="macro",
            evidence_bundle_id=bundle.record_id,
            evidence_fingerprint=fingerprint,
            cited_evidence_ids=("evidence.bar",),
            direction=Direction.NEUTRAL,
            confidence=Decimal("0.60"),
            thesis="Macro does not oppose the trade.",
            contradictions=(),
            created_at=NOW,
        ),
    )
    return bundle, evidence, analyses


def candidate(direction=Direction.BULLISH, symbol="ANF", rank="90"):
    return DiscoveryCandidate(
        symbol=symbol,
        direction=direction,
        catalyst_evidence_ids=("evidence.catalyst",),
        completed_bar_evidence_ids=("evidence.bar",),
        correlation_group="consumer-discretionary",
        rank=Decimal(rank),
    )


def snapshot(
    symbol,
    strike,
    delta,
    *,
    right=OptionRight.CALL,
    bid="4.40",
    ask="4.50",
    expiration=EXPIRATION,
    quote_time=NOW - timedelta(seconds=2),
    feed="indicative",
    volume=100,
    open_interest=500,
):
    return OptionSnapshot(
        option_symbol=symbol,
        underlying="ANF",
        right=right,
        strike=Decimal(str(strike)),
        expiration=expiration,
        bid=Decimal(bid),
        ask=Decimal(ask),
        bid_size=10,
        ask_size=12,
        volume=volume,
        open_interest=open_interest,
        delta=Decimal(str(delta)),
        quote_time=quote_time,
        feed=feed,
    )


def call_chain():
    return (
        snapshot("ANF260911C00200000", 200, "0.55"),
        snapshot("ANF260911C00205000", 205, "0.30", bid="2.00", ask="2.05"),
        snapshot("ANF260911C00210000", 210, "0.20", bid="0.80", ask="0.85"),
    )


def put_chain():
    return (
        snapshot("ANF260911P00200000", 200, "-0.30", right=OptionRight.PUT, bid="2.00", ask="2.05"),
        snapshot("ANF260911P00205000", 205, "-0.55", right=OptionRight.PUT),
    )


def risk(level=3, **overrides):
    values = dict(
        options_trading_level=level,
        equity=Decimal("100000"),
        cash=Decimal("100000"),
        options_buying_power=Decimal("100000"),
        daily_pnl=Decimal("0"),
        open_position_count=0,
        pending_order_count=0,
        reserved_maximum_loss=Decimal("0"),
        underlying_maximum_loss=Decimal("0"),
        correlation_maximum_loss=Decimal("0"),
    )
    values.update(overrides)
    return OptionsRiskState(**values)


def strategy(**config):
    return DefinedRiskOptionsStrategy(DefinedRiskOptionsConfig(enabled=True, **config))


def test_strategy_is_default_off():
    _, _, analyses = frozen_context()
    with pytest.raises(StrategyDisabled):
        DefinedRiskOptionsStrategy().evaluate(candidate(), call_chain(), analyses, risk(), NOW)


def test_competition_liquidity_thresholds_load_from_environment(monkeypatch):
    monkeypatch.setenv("OPTIONS_MAX_QUOTE_AGE_SECONDS", "30")
    monkeypatch.setenv("OPTIONS_MIN_OPEN_INTEREST", "0")
    monkeypatch.setenv("OPTIONS_MIN_VOLUME", "5")
    monkeypatch.setenv("OPTIONS_MAX_LEG_SPREAD_PCT", "0.25")
    monkeypatch.setenv("OPTIONS_MIN_REWARD_RISK", "0.40")

    config = DefinedRiskOptionsConfig.from_env()

    assert config.max_quote_age_seconds == 30
    assert config.min_open_interest == 0
    assert config.min_volume == 5
    assert config.max_leg_spread_pct == Decimal("0.25")
    assert config.min_reward_risk == Decimal("0.40")


def test_level3_selects_defined_risk_call_spread_with_whole_contract_sizing():
    _, _, analyses = frozen_context()
    selection = strategy().evaluate(candidate(), call_chain(), analyses, risk(), NOW)
    proposal = selection.proposal
    assert proposal is not None
    assert proposal.strategy_name == "call_debit_spread"
    assert tuple(leg.option_symbol for leg in proposal.legs) == (
        "ANF260911C00200000",
        "ANF260911C00205000",
    )
    assert proposal.limit_debit == Decimal("2.50")
    assert proposal.contract_quantity == 4
    assert proposal.maximum_loss == Decimal("1000.00")
    assert proposal.legs[0].strike < proposal.legs[1].strike


def test_bearish_candidate_selects_put_debit_geometry():
    _, _, analyses = frozen_context(Direction.BEARISH)
    proposal = strategy().evaluate(
        candidate(Direction.BEARISH), put_chain(), analyses, risk(), NOW
    ).proposal
    assert proposal is not None
    assert proposal.strategy_name == "put_debit_spread"
    assert proposal.legs[0].strike > proposal.legs[1].strike


def test_level2_fallback_is_long_only_and_premium_bounded():
    _, _, analyses = frozen_context()
    proposal = strategy().evaluate(candidate(), call_chain(), analyses, risk(level=2), NOW).proposal
    assert proposal is not None
    assert proposal.strategy_name == "long_call_level2_fallback"
    assert len(proposal.legs) == 1
    assert proposal.legs[0].side.value == "buy"
    assert proposal.contract_quantity == 2
    assert proposal.maximum_loss == Decimal("900.00")


def test_level1_and_unaffordable_contracts_abstain():
    _, _, analyses = frozen_context()
    assert strategy().evaluate(candidate(), call_chain(), analyses, risk(level=1), NOW).proposal is None
    poor = risk(level=2, cash=Decimal("100"), options_buying_power=Decimal("100"))
    assert strategy().evaluate(candidate(), call_chain(), analyses, poor, NOW).proposal is None


@pytest.mark.parametrize(
    "changed",
    [
        {"feed": "opra"},
        {"quote_time": NOW - timedelta(minutes=5)},
        {"open_interest": 5},
        {"volume": 0},
        {"expiration": date(2026, 8, 31)},
    ],
)
def test_wrong_feed_stale_illiquid_or_expiring_chain_is_ineligible(changed):
    _, _, analyses = frozen_context()
    chain = tuple(replace(item, **changed) for item in call_chain())
    selection = strategy().evaluate(candidate(), chain, analyses, risk(), NOW)
    assert selection.proposal is None


@pytest.mark.parametrize(
    ("risk_change", "reason"),
    [
        ({"daily_pnl": Decimal("-3000")}, "daily loss"),
        ({"open_position_count": 4}, "open positions"),
        ({"pending_order_count": 4}, "pending orders"),
        ({"reserved_maximum_loss": Decimal("10000")}, "total premium"),
        ({"underlying_maximum_loss": Decimal("3000")}, "underlying concentration"),
        ({"correlation_maximum_loss": Decimal("5000")}, "correlation-group"),
    ],
)
def test_portfolio_gates_reject_before_structure_selection(risk_change, reason):
    _, _, analyses = frozen_context()
    selection = strategy().evaluate(candidate(), call_chain(), analyses, risk(**risk_change), NOW)
    assert selection.proposal is None
    assert reason in selection.reason


def test_analysis_abstention_opposition_and_missing_citation_block_trade():
    _, _, analyses = frozen_context()
    abstain = replace(
        analyses[2],
        disposition=AnalysisDisposition.ABSTAIN,
        direction=Direction.NEUTRAL,
        confidence=Decimal("0"),
    )
    assert strategy().evaluate(candidate(), call_chain(), (*analyses[:2], abstain), risk(), NOW).proposal is None
    opposed = replace(analyses[0], direction=Direction.BEARISH)
    assert strategy().evaluate(candidate(), call_chain(), (opposed, *analyses[1:]), risk(), NOW).proposal is None
    uncited = replace(analyses[1], cited_evidence_ids=("evidence.bar",))
    assert strategy().evaluate(candidate(), call_chain(), (analyses[0], uncited, analyses[2]), risk(), NOW).proposal is None


def test_dynamic_builder_has_no_symbol_allowlist_and_preserves_rank_order():
    bundle, evidence, analyses = frozen_context()
    symbols_seen = []

    def chains(symbol, now):
        symbols_seen.append(symbol)
        return tuple(replace(item, underlying=symbol, option_symbol=item.option_symbol.replace("ANF", symbol)) for item in call_chain())

    builder = DynamicOptionsProposalBuilder(
        strategy(),
        candidate_provider=lambda frozen, rows, agent_results: (
            candidate(symbol="XYZ", rank="80"),
            candidate(symbol="ANF", rank="90"),
        ),
        chain_provider=chains,
        risk_provider=lambda discovered: risk(),
        clock=lambda: NOW,
    )
    proposals = builder(bundle, evidence, analyses)
    assert symbols_seen == ["ANF", "XYZ"]
    assert {proposal.underlying for proposal in proposals} == {"ANF", "XYZ"}


def test_dynamic_builder_reserves_batch_correlation_risk_between_candidates():
    bundle, evidence, analyses = frozen_context()

    def chains(symbol, now):
        return tuple(replace(item, underlying=symbol, option_symbol=item.option_symbol.replace("ANF", symbol)) for item in call_chain())

    limited = DefinedRiskOptionsStrategy(
        DefinedRiskOptionsConfig(
            enabled=True,
            max_trade_risk_pct=Decimal("0.10"),
            max_correlation_risk_pct=Decimal("0.03"),
        )
    )
    builder = DynamicOptionsProposalBuilder(
        limited,
        candidate_provider=lambda frozen, rows, agent_results: (
            candidate(symbol="ANF", rank="90"),
            candidate(symbol="XYZ", rank="80"),
        ),
        chain_provider=chains,
        risk_provider=lambda discovered: risk(equity=Decimal("10000"), cash=Decimal("10000")),
        clock=lambda: NOW,
    )
    proposals = builder(bundle, evidence, analyses)
    assert len(proposals) == 1
    assert proposals[0].underlying == "ANF"


def test_normalizes_only_complete_active_alpaca_contract_snapshots():
    contracts = [
        {
            "symbol": "ANF260911C00200000",
            "type": "call",
            "strike_price": "200",
            "expiration_date": "2026-09-11",
            "status": "active",
            "tradable": True,
        },
        {
            "symbol": "ANF260911C00205000",
            "type": "call",
            "strike_price": "205",
            "expiration_date": "2026-09-11",
            "status": "inactive",
        },
    ]
    snapshots = {
        "ANF260911C00200000": {
            "latestQuote": {"bp": 4.4, "ap": 4.5, "bs": 10, "as": 12, "t": NOW.isoformat()},
            "greeks": {"delta": 0.55},
            "dailyBar": {"v": 100},
            "openInterest": 500,
        },
        "ANF260911C00205000": {},
    }
    normalized = normalize_alpaca_chain("ANF", contracts, snapshots, feed="indicative")
    assert len(normalized) == 1
    assert normalized[0].delta == Decimal("0.55")
    assert normalized[0].feed == "indicative"


def options_proposal_from_draft(draft):
    return OptionsProposal(
        record_id="proposal.exit-test",
        trace_id="trace.options",
        evidence_bundle_id="bundle.exit-test",
        analysis_ids=("analysis.technical",),
        underlying=draft.underlying,
        decision=draft.decision,
        direction=draft.direction,
        strategy_name=draft.strategy_name,
        legs=draft.legs,
        contract_quantity=draft.contract_quantity,
        limit_debit=draft.limit_debit,
        maximum_loss=draft.maximum_loss,
        rationale=draft.rationale,
        created_at=NOW,
    )


def test_exit_plan_persists_and_profit_loss_time_expiration_thesis_rules(tmp_path):
    _, _, analyses = frozen_context()
    draft = strategy().evaluate(candidate(), call_chain(), analyses, risk(), NOW).proposal
    proposal = options_proposal_from_draft(draft)
    plan = ExitPlanFactory(DefinedRiskOptionsConfig(enabled=True)).for_filled_proposal(
        proposal,
        filled_quantity=2,
        entry_debit=Decimal("2.40"),
        opened_at=NOW,
        thesis_evidence_ids=("evidence.bar", "evidence.catalyst"),
    )
    assert plan.maximum_loss == Decimal("480.00")
    engine = ExitDecisionEngine()
    profit = engine.assess(plan, current_mark=Decimal("3.60"), now=NOW, thesis_valid=True)
    assert profit.reasons == ("profit_target",)
    close = ExitCommandFactory().for_due_plan(plan, profit, NOW)
    assert close.quantity == 2
    assert [leg.side.value for leg in close.closing_legs] == ["sell", "buy"]
    assert close.limit_credit == Decimal("3.60")
    assert engine.assess(plan, current_mark=Decimal("1.40"), now=NOW, thesis_valid=True).reasons == ("loss_limit",)
    assert "holding_time" in engine.assess(
        plan, current_mark=Decimal("2.40"), now=NOW + timedelta(days=3), thesis_valid=True
    ).reasons
    assert "expiration_control" in engine.assess(
        plan, current_mark=Decimal("2.40"), now=datetime(2026, 9, 9, 16, tzinfo=timezone.utc), thesis_valid=True
    ).reasons
    assert engine.assess(plan, current_mark=Decimal("2.40"), now=NOW, thesis_valid=False).reasons == (
        "thesis_invalidation",
    )

    store = JsonlExitPlanStore(str(tmp_path / "exit-plans.jsonl"))
    store.save(plan)
    due = store.mark_state(plan, ExitPlanState.EXIT_DUE)
    loaded = store.load_latest()
    assert loaded == (due,)


def test_invalid_expiration_window_is_rejected():
    with pytest.raises(ValueError, match="expiration window"):
        DefinedRiskOptionsConfig(min_dte=2, exit_before_expiration_days=2)
