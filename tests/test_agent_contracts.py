from dataclasses import FrozenInstanceError, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import sys

import pytest


ENGINE = Path(__file__).resolve().parents[1] / "services" / "strategy-engine"
sys.path.insert(0, str(ENGINE))

from agent_contracts import (  # noqa: E402
    AnalysisDisposition,
    AdversarialObjection,
    AgentAnalysis,
    ContractValidationError,
    DecisionTrace,
    Direction,
    EvidenceBundle,
    EvidenceItem,
    ExecutionAction,
    ExecutionCommand,
    LegSide,
    OptionLeg,
    OptionRight,
    OptionsProposal,
    OrderEvent,
    OrderStatus,
    PositionAssessment,
    PositionState,
    ProposalDecision,
    RiskAuthorization,
    RiskDecision,
    canonical_json,
    contract_fingerprint,
    coordinator_contracts_enabled,
)


NOW = datetime(2026, 8, 28, 16, 0, tzinfo=timezone.utc)
RAW_HASH = "a" * 64


def build_trace(*, reverse_independent_records: bool = False) -> DecisionTrace:
    evidence = (
        EvidenceItem(
            record_id="evidence.quote",
            trace_id="trace.001",
            provider="alpaca_sip",
            instrument="AAPL",
            event_time=NOW - timedelta(seconds=2),
            received_at=NOW - timedelta(seconds=1),
            raw_sha256=RAW_HASH,
            value_name="completed_bar_close",
            value="228.41",
            created_at=NOW,
            entitlement="sip",
            is_fresh=True,
        ),
        EvidenceItem(
            record_id="evidence.news",
            trace_id="trace.001",
            provider="finnhub",
            instrument="AAPL",
            event_time=NOW - timedelta(minutes=5),
            received_at=NOW - timedelta(minutes=4),
            raw_sha256="b" * 64,
            value_name="catalyst",
            value="product-event",
            created_at=NOW,
            entitlement="contest",
            is_fresh=True,
        ),
    )
    bundle = EvidenceBundle(
        record_id="bundle.001",
        trace_id="trace.001",
        evidence_ids=("evidence.news", "evidence.quote"),
        evidence_fingerprint=contract_fingerprint(tuple(sorted(evidence, key=lambda item: item.record_id))),
        frozen_at=NOW,
        created_at=NOW,
    )
    analyses = (
        AgentAnalysis(
            record_id="analysis.technical",
            trace_id="trace.001",
            agent_name="technical",
            evidence_bundle_id=bundle.record_id,
            evidence_fingerprint=contract_fingerprint(bundle),
            cited_evidence_ids=("evidence.quote",),
            direction=Direction.BULLISH,
            confidence=Decimal("0.72"),
            thesis="Completed bars show confirmed momentum.",
            contradictions=(),
            created_at=NOW,
        ),
        AgentAnalysis(
            record_id="analysis.catalyst",
            trace_id="trace.001",
            agent_name="catalyst",
            evidence_bundle_id=bundle.record_id,
            evidence_fingerprint=contract_fingerprint(bundle),
            cited_evidence_ids=("evidence.news",),
            direction=Direction.BULLISH,
            confidence=Decimal("0.64"),
            thesis="A source-attributed catalyst is present.",
            contradictions=("Event impact is uncertain.",),
            created_at=NOW,
        ),
    )
    legs = (
        OptionLeg("AAPL260904C00230000", LegSide.BUY, OptionRight.CALL, 1, Decimal("230"), date(2026, 9, 4)),
        OptionLeg("AAPL260904C00235000", LegSide.SELL, OptionRight.CALL, 1, Decimal("235"), date(2026, 9, 4)),
    )
    proposal = OptionsProposal(
        record_id="proposal.001",
        trace_id="trace.001",
        evidence_bundle_id=bundle.record_id,
        analysis_ids=("analysis.catalyst", "analysis.technical"),
        underlying="AAPL",
        decision=ProposalDecision.PROPOSE,
        direction=Direction.BULLISH,
        strategy_name="call_debit_spread",
        legs=legs,
        contract_quantity=1,
        limit_debit=Decimal("1.25"),
        maximum_loss=Decimal("125.00"),
        rationale="Catalyst and completed-bar confirmation agree.",
        created_at=NOW,
    )
    objection = AdversarialObjection(
        record_id="objection.001",
        trace_id="trace.001",
        proposal_id=proposal.record_id,
        cited_evidence_ids=("evidence.news",),
        severity="medium",
        objection="Catalyst direction could reverse.",
        blocking=False,
        created_at=NOW,
    )
    authorization = RiskAuthorization(
        record_id="authorization.001",
        trace_id="trace.001",
        proposal_id=proposal.record_id,
        proposal_fingerprint=contract_fingerprint(proposal),
        objection_ids=(objection.record_id,),
        decision=RiskDecision.APPROVE,
        authorized_quantity=1,
        authorized_maximum_loss=Decimal("125.00"),
        reason="Within deterministic paper risk limits.",
        expires_at=NOW + timedelta(minutes=2),
        created_at=NOW,
    )
    command = ExecutionCommand(
        record_id="command.001",
        trace_id="trace.001",
        authorization_id=authorization.record_id,
        authorization_fingerprint=contract_fingerprint(authorization),
        proposal_id=proposal.record_id,
        action=ExecutionAction.SUBMIT,
        client_order_id="trace.001.entry",
        legs=legs,
        quantity=1,
        limit_price=Decimal("1.25"),
        created_at=NOW,
    )
    event = OrderEvent(
        record_id="event.001",
        trace_id="trace.001",
        command_id=command.record_id,
        broker_order_id="paper-order.001",
        status=OrderStatus.FILLED,
        filled_quantity=1,
        average_fill_price=Decimal("1.22"),
        broker_timestamp=NOW + timedelta(seconds=3),
        created_at=NOW + timedelta(seconds=3),
    )
    assessment = PositionAssessment(
        record_id="assessment.001",
        trace_id="trace.001",
        proposal_id=proposal.record_id,
        authorization_id=authorization.record_id,
        order_event_ids=(event.record_id,),
        position_key="position.AAPL.001",
        state=PositionState.OPEN,
        quantity=1,
        mark_value=Decimal("130.00"),
        unrealized_pnl=Decimal("8.00"),
        exit_reasons=(),
        assessed_at=NOW + timedelta(minutes=1),
        created_at=NOW + timedelta(minutes=1),
    )
    if reverse_independent_records:
        evidence = tuple(reversed(evidence))
        analyses = tuple(reversed(analyses))
    return DecisionTrace(
        evidence=evidence,
        bundle=bundle,
        analyses=analyses,
        proposals=(proposal,),
        objections=(objection,),
        authorizations=(authorization,),
        commands=(command,),
        order_events=(event,),
        assessments=(assessment,),
    )


def test_contracts_are_immutable_and_canonical():
    trace = build_trace()
    with pytest.raises(FrozenInstanceError):
        trace.bundle.record_id = "changed"
    encoded = canonical_json(trace.proposals[0])
    assert '"limit_debit":"1.25"' in encoded
    assert encoded == canonical_json(trace.proposals[0])


def test_replay_fingerprint_is_deterministic_across_agent_completion_order():
    assert build_trace().replay_fingerprint == build_trace(reverse_independent_records=True).replay_fingerprint


def test_decision_trace_rejects_proposal_crossing_analysis_abstention():
    trace = build_trace()
    abstaining = replace(
        trace.analyses[0],
        direction=Direction.NEUTRAL,
        confidence=Decimal("0"),
        disposition=AnalysisDisposition.ABSTAIN,
    )
    with pytest.raises(ContractValidationError, match="analysis abstention"):
        replace(trace, analyses=(abstaining, trace.analyses[1]))


def test_untraceable_or_tampered_output_fails_closed():
    trace = build_trace()
    tampered = replace(trace.authorizations[0], proposal_fingerprint="0" * 64)
    with pytest.raises(ContractValidationError, match="does not match"):
        replace(trace, authorizations=(tampered,))


def test_tampered_evidence_breaks_the_frozen_bundle():
    trace = build_trace()
    changed = replace(trace.evidence[0], value="999.99")
    with pytest.raises(ContractValidationError, match="evidence bundle fingerprint"):
        replace(trace, evidence=(changed, trace.evidence[1]))


def test_invalid_schema_timestamp_and_mutable_collections_are_rejected():
    trace = build_trace()
    with pytest.raises(ContractValidationError, match="unsupported schema"):
        replace(trace.bundle, schema_version="2.0")
    with pytest.raises(ContractValidationError, match="timezone-aware UTC"):
        replace(trace.bundle, created_at=NOW.replace(tzinfo=None))
    with pytest.raises(ContractValidationError, match="immutable tuple"):
        replace(trace.bundle, evidence_ids=["evidence.quote"])


def test_rejected_risk_record_cannot_authorize_exposure():
    trace = build_trace()
    with pytest.raises(ContractValidationError, match="cannot authorize exposure"):
        replace(trace.authorizations[0], decision=RiskDecision.REJECT)


def test_execution_cannot_reference_rejected_authorization():
    trace = build_trace()
    rejected = replace(
        trace.authorizations[0],
        decision=RiskDecision.REJECT,
        authorized_quantity=0,
        authorized_maximum_loss=Decimal("0"),
    )
    command = replace(
        trace.commands[0],
        authorization_fingerprint=contract_fingerprint(rejected),
    )
    with pytest.raises(ContractValidationError, match="non-rejected authorization"):
        replace(trace, authorizations=(rejected,), commands=(command,))


def test_risk_authorization_cannot_widen_the_proposal():
    trace = build_trace()
    widened = replace(trace.authorizations[0], authorized_quantity=2)
    command = replace(trace.commands[0], authorization_fingerprint=contract_fingerprint(widened))
    with pytest.raises(ContractValidationError, match="cannot increase proposal quantity"):
        replace(trace, authorizations=(widened,), commands=(command,))


def test_execution_command_cannot_substitute_option_legs():
    trace = build_trace()
    changed_leg = replace(trace.proposals[0].legs[0], strike=Decimal("231"))
    tampered_command = replace(trace.commands[0], legs=(changed_leg, trace.commands[0].legs[1]))
    with pytest.raises(ContractValidationError, match="legs do not match"):
        replace(trace, commands=(tampered_command,))


def test_execution_command_requires_unexpired_authorization():
    trace = build_trace()
    late_command = replace(trace.commands[0], created_at=trace.authorizations[0].expires_at)
    with pytest.raises(ContractValidationError, match="authorization window"):
        replace(trace, commands=(late_command,))


def test_proposal_contract_rejects_uncovered_or_wrong_direction_options():
    trace = build_trace()
    with pytest.raises(ContractValidationError, match="single-leg proposal must be long-only"):
        replace(trace.proposals[0], legs=(trace.proposals[0].legs[1],))
    wrong_geometry = (
        trace.proposals[0].legs[1],
        trace.proposals[0].legs[0],
    )
    with pytest.raises(ContractValidationError, match="defined-risk debit spread"):
        replace(
            trace.proposals[0],
            legs=(replace(wrong_geometry[0], side=LegSide.BUY), replace(wrong_geometry[1], side=LegSide.SELL)),
        )
    put_leg = replace(trace.proposals[0].legs[0], right=OptionRight.PUT)
    with pytest.raises(ContractValidationError, match="bullish proposal must use calls"):
        replace(trace.proposals[0], legs=(put_leg,))


def test_coordinator_contract_gate_defaults_off():
    assert coordinator_contracts_enabled({}) is False
    assert coordinator_contracts_enabled({"AGENT_COORDINATOR_ENABLED": "true"}) is True
