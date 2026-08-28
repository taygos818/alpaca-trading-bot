from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import threading
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "services" / "strategy-engine"
sys.path.insert(0, str(ENGINE))

from agent_contracts import (  # noqa: E402
    ContractValidationError,
    Direction,
    EvidenceItem,
    LegSide,
    OptionLeg,
    OptionRight,
    OrderEvent,
    OrderStatus,
    PositionState,
    ProposalDecision,
    RiskDecision,
)
from multi_agent import (  # noqa: E402
    AdversarialReviewAgent,
    AllocationLimits,
    AnalysisDraft,
    CatalystAgent,
    CoordinatorDisabled,
    CoordinatorPolicy,
    DataQualityAgent,
    DeterministicAllocator,
    ExecutionAgent,
    IdempotencyRegistry,
    MacroAgent,
    MultiAgentCoordinator,
    ObjectionDraft,
    OptionsStructureAgent,
    PortfolioRiskAgent,
    PortfolioSnapshot,
    PositionAnalysisAgent,
    PositionDraft,
    ProposalDraft,
    TechnicalAgent,
)


NOW = datetime(2026, 8, 28, 16, 0, tzinfo=timezone.utc)


def evidence(trace_id="trace.001"):
    return (
        EvidenceItem(
            record_id="evidence.completed-bar",
            trace_id=trace_id,
            provider="alpaca_sip",
            instrument="AAPL",
            event_time=NOW - timedelta(minutes=1),
            received_at=NOW - timedelta(seconds=30),
            raw_sha256="a" * 64,
            value_name="completed_bar_close",
            value="228.41",
            created_at=NOW,
            entitlement="sip",
            is_fresh=True,
        ),
        EvidenceItem(
            record_id="evidence.catalyst",
            trace_id=trace_id,
            provider="finnhub",
            instrument="AAPL",
            event_time=NOW - timedelta(minutes=5),
            received_at=NOW - timedelta(minutes=4),
            raw_sha256="b" * 64,
            value_name="news_catalyst",
            value="product-event",
            created_at=NOW,
            entitlement="contest",
            is_fresh=True,
        ),
    )


def proposal_draft(key="primary", quantity=1, maximum_loss=Decimal("125")):
    return ProposalDraft(
        proposal_key=key,
        underlying="AAPL",
        decision=ProposalDecision.PROPOSE,
        direction=Direction.BULLISH,
        strategy_name="call_debit_spread",
        legs=(
            OptionLeg("AAPL260904C00230000", LegSide.BUY, OptionRight.CALL, 1, Decimal("230"), date(2026, 9, 4)),
            OptionLeg("AAPL260904C00235000", LegSide.SELL, OptionRight.CALL, 1, Decimal("235"), date(2026, 9, 4)),
        ),
        contract_quantity=quantity,
        limit_debit=Decimal("1.25"),
        maximum_loss=maximum_loss,
        rationale="Independent agents agree on a bounded options structure.",
    )


def make_coordinator(*, registry=None, structure_agents=None, reviewer=None, limits=None):
    def analysis(direction, confidence, citation):
        return lambda bundle, items: AnalysisDraft(
            direction=direction,
            confidence=confidence,
            thesis=f"{citation} supports the assessment.",
            cited_evidence_ids=(citation,),
        )

    analysis_agents = (
        TechnicalAgent(analysis(Direction.BULLISH, Decimal("0.70"), "evidence.completed-bar")),
        CatalystAgent(analysis(Direction.BULLISH, Decimal("0.65"), "evidence.catalyst")),
        MacroAgent(analysis(Direction.NEUTRAL, Decimal("0.55"), "evidence.completed-bar")),
    )
    if structure_agents is None:
        structure_agents = (
            OptionsStructureAgent("directional", lambda bundle, items, analyses: (proposal_draft(),)),
        )
    class RecordingPreviewPort:
        def __init__(self):
            self.entries = []

        def preview(self, execution):
            payload = {"client_order_id": execution.command.client_order_id, "dry_run": True}
            self.entries.append(payload)
            return payload

    preview_port = RecordingPreviewPort()

    coordinator = MultiAgentCoordinator(
        data_agent=DataQualityAgent(),
        analysis_agents=analysis_agents,
        structure_agents=structure_agents,
        adversarial_agent=AdversarialReviewAgent(reviewer or (lambda proposal, bundle, items: ())),
        risk_agent=PortfolioRiskAgent(
            DeterministicAllocator(
                limits
                or AllocationLimits(
                    max_open_positions=4,
                    max_total_maximum_loss=Decimal("500"),
                    max_underlying_maximum_loss=Decimal("250"),
                )
            )
        ),
        execution_agent=ExecutionAgent(),
        preview_port=preview_port,
        registry=registry,
    )
    return coordinator, preview_port.entries


def empty_portfolio():
    return PortfolioSnapshot(open_underlyings=(), open_position_count=0, reserved_maximum_loss=Decimal("0"))


def test_coordinator_is_default_off():
    coordinator, _ = make_coordinator()
    with pytest.raises(CoordinatorDisabled):
        coordinator.run_shadow_cycle(
            trace_id="trace.001",
            evidence=evidence(),
            portfolio=empty_portfolio(),
            now=NOW,
            environment={},
        )


def test_environment_cannot_disable_shadow_barrier():
    coordinator, _ = make_coordinator()
    with pytest.raises(CoordinatorDisabled, match="must remain true"):
        coordinator.run_shadow_cycle(
            trace_id="trace.001",
            evidence=evidence(),
            portfolio=empty_portfolio(),
            now=NOW,
            environment={
                "AGENT_COORDINATOR_ENABLED": "true",
                "AGENT_COORDINATOR_SHADOW_MODE": "false",
            },
        )


def test_shadow_cycle_produces_deterministic_authorized_preview():
    coordinator, previews = make_coordinator()
    result = coordinator.run_shadow_cycle(
        trace_id="trace.001",
        evidence=evidence(),
        portfolio=empty_portfolio(),
        now=NOW,
        environment={"AGENT_COORDINATOR_ENABLED": "true"},
    )
    assert [item.record_id for item in result.analyses] == sorted(item.record_id for item in result.analyses)
    assert {item.agent_name for item in result.analyses} == {"technical", "catalyst", "macro"}
    assert len(result.proposals) == 1
    assert result.authorizations[0].decision is RiskDecision.APPROVE
    assert len(result.commands) == 1
    assert len(previews) == 1
    assert '"dry_run":true' in result.previews[0]


def test_analysis_agents_really_execute_independently_in_parallel():
    barrier = threading.Barrier(3, timeout=2)

    def evaluator(name):
        def run(bundle, items):
            barrier.wait()
            return AnalysisDraft(
                direction=Direction.NEUTRAL,
                confidence=Decimal("0.5"),
                thesis=f"{name} completed independently.",
                cited_evidence_ids=("evidence.completed-bar",),
            )
        return run

    coordinator = MultiAgentCoordinator(
        data_agent=DataQualityAgent(),
        analysis_agents=(TechnicalAgent(evaluator("t")), CatalystAgent(evaluator("c")), MacroAgent(evaluator("m"))),
        structure_agents=(OptionsStructureAgent("directional", lambda bundle, items, analyses: (proposal_draft(),)),),
        adversarial_agent=AdversarialReviewAgent(lambda proposal, bundle, items: ()),
        risk_agent=PortfolioRiskAgent(DeterministicAllocator(AllocationLimits(4, Decimal("500"), Decimal("250")))),
        execution_agent=ExecutionAgent(),
        preview_port=type("PreviewPort", (), {"preview": lambda self, execution: {"dry_run": True}})(),
    )
    result = coordinator.run_shadow_cycle(
        trace_id="trace.001",
        evidence=evidence(),
        portfolio=empty_portfolio(),
        now=NOW,
        environment={"AGENT_COORDINATOR_ENABLED": "true"},
    )
    assert len(result.analyses) == 3


def test_options_structure_agents_propose_concurrently():
    barrier = threading.Barrier(2, timeout=2)

    def builder(key):
        def run(bundle, items, analyses):
            barrier.wait()
            return (proposal_draft(key=key),)
        return run

    structures = (
        OptionsStructureAgent("structure_alpha", builder("alpha")),
        OptionsStructureAgent("structure_beta", builder("beta")),
    )
    coordinator, previews = make_coordinator(structure_agents=structures)
    result = coordinator.run_shadow_cycle(
        trace_id="trace.001", evidence=evidence(), portfolio=empty_portfolio(), now=NOW,
        environment={"AGENT_COORDINATOR_ENABLED": "true"},
    )
    assert len(result.proposals) == 2
    assert len(result.commands) == 2
    assert len(previews) == 2


def test_allocator_rejects_opposing_direction_in_same_batch():
    structures = (
        OptionsStructureAgent("bullish", lambda bundle, items, analyses: (proposal_draft(key="bull"),)),
        OptionsStructureAgent(
            "bearish",
            lambda bundle, items, analyses: (
                replace(proposal_draft(key="bear"), direction=Direction.BEARISH),
            ),
        ),
    )
    coordinator, _ = make_coordinator(structure_agents=structures)
    result = coordinator.run_shadow_cycle(
        trace_id="trace.001", evidence=evidence(), portfolio=empty_portfolio(), now=NOW,
        environment={"AGENT_COORDINATOR_ENABLED": "true"},
    )
    assert sorted([item.decision for item in result.authorizations], key=lambda item: item.value) == [
        RiskDecision.APPROVE,
        RiskDecision.REJECT,
    ]
    assert any("opposing directional exposure" in item.reason for item in result.authorizations)


def test_repeat_cycle_is_idempotent_and_emits_no_second_command():
    registry = IdempotencyRegistry()
    coordinator, previews = make_coordinator(registry=registry)
    kwargs = dict(
        trace_id="trace.001",
        evidence=evidence(),
        portfolio=empty_portfolio(),
        now=NOW,
        environment={"AGENT_COORDINATOR_ENABLED": "true"},
    )
    first = coordinator.run_shadow_cycle(**kwargs)
    second = coordinator.run_shadow_cycle(**kwargs)
    assert len(first.commands) == 1
    assert second.commands == ()
    assert second.proposals == ()
    assert len(second.duplicate_proposal_ids) == 1
    assert len(previews) == 1


def test_same_stable_proposal_id_with_changed_content_fails_closed():
    registry = IdempotencyRegistry()
    first, _ = make_coordinator(registry=registry)
    first.run_shadow_cycle(
        trace_id="trace.001", evidence=evidence(), portfolio=empty_portfolio(), now=NOW,
        environment={"AGENT_COORDINATOR_ENABLED": "true"},
    )
    changed_agents = (
        OptionsStructureAgent("directional", lambda bundle, items, analyses: (proposal_draft(maximum_loss=Decimal("100")),)),
    )
    changed, _ = make_coordinator(registry=registry, structure_agents=changed_agents)
    with pytest.raises(ContractValidationError, match="reused with different content"):
        changed.run_shadow_cycle(
            trace_id="trace.001", evidence=evidence(), portfolio=empty_portfolio(), now=NOW,
            environment={"AGENT_COORDINATOR_ENABLED": "true"},
        )


def test_adversarial_agent_has_veto_authority():
    def veto(proposal, bundle, items):
        return (
            ObjectionDraft(
                severity="critical",
                objection="Required source contradicts the proposed direction.",
                cited_evidence_ids=("evidence.catalyst",),
                blocking=True,
            ),
        )

    coordinator, previews = make_coordinator(reviewer=veto)
    result = coordinator.run_shadow_cycle(
        trace_id="trace.001", evidence=evidence(), portfolio=empty_portfolio(), now=NOW,
        environment={"AGENT_COORDINATOR_ENABLED": "true"},
    )
    assert result.authorizations[0].decision is RiskDecision.REJECT
    assert result.commands == ()
    assert previews == []


def test_allocator_reduces_quantity_to_maximum_loss_budget():
    structures = (
        OptionsStructureAgent(
            "directional",
            lambda bundle, items, analyses: (proposal_draft(quantity=3, maximum_loss=Decimal("375")),),
        ),
    )
    coordinator, _ = make_coordinator(
        structure_agents=structures,
        limits=AllocationLimits(4, Decimal("250"), Decimal("250")),
    )
    result = coordinator.run_shadow_cycle(
        trace_id="trace.001", evidence=evidence(), portfolio=empty_portfolio(), now=NOW,
        environment={"AGENT_COORDINATOR_ENABLED": "true"},
    )
    authorization = result.authorizations[0]
    assert authorization.decision is RiskDecision.REDUCE
    assert authorization.authorized_quantity == 2
    assert authorization.authorized_maximum_loss == Decimal("250")


def test_stale_evidence_fails_before_any_agent_or_preview():
    coordinator, previews = make_coordinator()
    stale = (evidence()[0],)
    stale = (stale[0].__class__(
        **{field: getattr(stale[0], field) for field in stale[0].__dataclass_fields__ if field != "is_fresh"},
        is_fresh=False,
    ),)
    with pytest.raises(ContractValidationError, match="stale evidence"):
        coordinator.run_shadow_cycle(
            trace_id="trace.001", evidence=stale, portfolio=empty_portfolio(), now=NOW,
            environment={"AGENT_COORDINATOR_ENABLED": "true"},
        )
    assert previews == []


def test_position_agent_only_assesses_reconciled_order_events():
    event = OrderEvent(
        record_id="event.001",
        trace_id="trace.001",
        command_id="command.001",
        broker_order_id="paper-order.001",
        status=OrderStatus.FILLED,
        filled_quantity=1,
        average_fill_price=Decimal("1.22"),
        broker_timestamp=NOW,
        created_at=NOW,
    )
    draft = PositionDraft(
        proposal_id="proposal.001",
        authorization_id="authorization.001",
        order_event_ids=(event.record_id,),
        position_key="position.AAPL.001",
        state=PositionState.OPEN,
        quantity=1,
        mark_value=Decimal("122"),
        unrealized_pnl=Decimal("0"),
        exit_reasons=(),
        assessed_at=NOW,
    )
    assessment = PositionAnalysisAgent().assess("trace.001", draft, (event,), NOW)
    assert assessment.order_event_ids == (event.record_id,)
    with pytest.raises(ContractValidationError, match="unknown order event"):
        PositionAnalysisAgent().assess("trace.001", replace(draft, order_event_ids=("event.missing",)), (event,), NOW)


def test_milestone_two_agents_have_no_credential_or_subprocess_access():
    source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (ENGINE / "multi_agent").glob("*.py")
    )
    assert "ALPACA_API_KEY" not in source
    assert "ALPACA_SECRET_KEY" not in source
    assert "subprocess" not in source
    assert "execution_gateway" not in source


def test_non_shadow_coordinator_policy_is_impossible():
    with pytest.raises(ValueError, match="shadow-only"):
        CoordinatorPolicy(shadow_mode=False)
