from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "services" / "strategy-engine"
sys.path.insert(0, str(ENGINE))

from agent_contracts import (  # noqa: E402
    AgentAnalysis,
    AnalysisDisposition,
    AuthorizedExecution,
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
    contract_fingerprint,
)
from execution_gateway import AlpacaCliError, CliResponse  # noqa: E402
from paper_runtime import (  # noqa: E402
    BoundedPaperLauncher,
    BrokerStateUnresolved,
    DecisionTraceJournal,
    DeterministicReplayRunner,
    PaperLaunchPolicy,
    PaperAgentCycleRunner,
    ReplayScenario,
)
from defined_risk_options import JsonlExitPlanStore  # noqa: E402


NOW = datetime(2026, 8, 28, 16, 0, tzinfo=timezone.utc)


def execution_and_trace(*, include_fill=False):
    evidence = EvidenceItem(
        record_id="evidence.bar",
        trace_id="trace.m6",
        provider="alpaca_sip",
        instrument="ANF",
        event_time=NOW - timedelta(minutes=2),
        received_at=NOW - timedelta(minutes=1),
        raw_sha256="a" * 64,
        value_name="completed_bar_close",
        value="101.25",
        created_at=NOW - timedelta(minutes=1),
        entitlement="sip",
        is_fresh=True,
    )
    bundle = EvidenceBundle(
        record_id="bundle.m6",
        trace_id="trace.m6",
        evidence_ids=(evidence.record_id,),
        evidence_fingerprint=contract_fingerprint((evidence,)),
        frozen_at=NOW,
        created_at=NOW,
    )
    analysis = AgentAnalysis(
        record_id="analysis.m6",
        trace_id="trace.m6",
        agent_name="technical",
        evidence_bundle_id=bundle.record_id,
        evidence_fingerprint=contract_fingerprint(bundle),
        cited_evidence_ids=(evidence.record_id,),
        direction=Direction.BULLISH,
        confidence=Decimal("0.70"),
        thesis="Completed bar confirms the replay fixture.",
        contradictions=(),
        created_at=NOW,
        disposition=AnalysisDisposition.ANALYZE,
    )
    legs = (
        OptionLeg("ANF260911C00100000", LegSide.BUY, OptionRight.CALL, 1, Decimal("100"), date(2026, 9, 11)),
        OptionLeg("ANF260911C00105000", LegSide.SELL, OptionRight.CALL, 1, Decimal("105"), date(2026, 9, 11)),
    )
    proposal = OptionsProposal(
        record_id="proposal.m6",
        trace_id="trace.m6",
        evidence_bundle_id=bundle.record_id,
        analysis_ids=(analysis.record_id,),
        underlying="ANF",
        decision=ProposalDecision.PROPOSE,
        direction=Direction.BULLISH,
        strategy_name="call_debit_spread",
        legs=legs,
        contract_quantity=1,
        limit_debit=Decimal("1.00"),
        maximum_loss=Decimal("100"),
        rationale="Replay fixture with defined maximum loss.",
        created_at=NOW,
    )
    authorization = RiskAuthorization(
        record_id="authorization.m6",
        trace_id="trace.m6",
        proposal_id=proposal.record_id,
        proposal_fingerprint=contract_fingerprint(proposal),
        objection_ids=(),
        decision=RiskDecision.APPROVE,
        authorized_quantity=1,
        authorized_maximum_loss=Decimal("100"),
        reason="Within bounded paper limits.",
        expires_at=NOW + timedelta(minutes=2),
        created_at=NOW,
    )
    command = ExecutionCommand(
        record_id="command.m6",
        trace_id="trace.m6",
        authorization_id=authorization.record_id,
        authorization_fingerprint=contract_fingerprint(authorization),
        proposal_id=proposal.record_id,
        action=ExecutionAction.SUBMIT,
        client_order_id="agent.trace.m6",
        legs=legs,
        quantity=1,
        limit_price=Decimal("1.00"),
        created_at=NOW,
    )
    execution = AuthorizedExecution(proposal, authorization, command)
    events = ()
    assessments = ()
    if include_fill:
        event = OrderEvent(
            record_id="event.m6.filled",
            trace_id="trace.m6",
            command_id=command.record_id,
            broker_order_id="paper-order.m6",
            status=OrderStatus.FILLED,
            filled_quantity=1,
            average_fill_price=Decimal("0.98"),
            broker_timestamp=NOW + timedelta(seconds=30),
            created_at=NOW + timedelta(seconds=30),
        )
        assessment = PositionAssessment(
            record_id="assessment.m6",
            trace_id="trace.m6",
            proposal_id=proposal.record_id,
            authorization_id=authorization.record_id,
            order_event_ids=(event.record_id,),
            position_key="position.ANF.m6",
            state=PositionState.OPEN,
            quantity=1,
            mark_value=Decimal("98"),
            unrealized_pnl=Decimal("0"),
            exit_reasons=(),
            assessed_at=NOW + timedelta(seconds=30),
            created_at=NOW + timedelta(seconds=30),
        )
        events = (event,)
        assessments = (assessment,)
    trace = DecisionTrace(
        evidence=(evidence,),
        bundle=bundle,
        analyses=(analysis,),
        proposals=(proposal,),
        objections=(),
        authorizations=(authorization,),
        commands=(command,),
        order_events=events,
        assessments=assessments,
    )
    return execution, trace


def broker_order(status="new", *, filled_qty="0", client_id="agent.trace.m6"):
    return {
        "id": "paper-order.m6",
        "client_order_id": client_id,
        "status": status,
        "filled_qty": filled_qty,
        "filled_avg_price": "0.98" if Decimal(filled_qty) else None,
        "updated_at": (NOW + timedelta(seconds=30)).isoformat(),
    }


class FakeGateway:
    def __init__(self, *, open_orders=None, submitted=None, reconciled=None):
        self.open_payload = [] if open_orders is None else open_orders
        self.submitted_payload = submitted or broker_order()
        self.reconciled_payload = reconciled
        self.calls = []

    def open_orders(self):
        self.calls.append("open_orders")
        return CliResponse("order.list", self.open_payload, 0)

    def preview(self, execution):
        self.calls.append("preview")
        return CliResponse("order.preview", {"dry_run": True}, 0)

    def submit(self, execution):
        self.calls.append("submit")
        return CliResponse("order.submit", self.submitted_payload, 0)

    def order_by_client_id(self, client_order_id):
        self.calls.append("lookup")
        if self.reconciled_payload is None:
            raise AlpacaCliError("structured CLI error status=404 code=40410000")
        return CliResponse("order.get_by_client_id", self.reconciled_payload, 0)

    def cancel_order(self, order_id):
        self.calls.append("cancel")
        return CliResponse("order.cancel", {"accepted": True}, 0)


def test_paper_launch_policy_is_default_dry_run_and_submission_needs_ack():
    assert PaperLaunchPolicy().dry_run is True
    assert PaperLaunchPolicy().submission_enabled is False
    with pytest.raises(ValueError, match="acknowledgement"):
        PaperLaunchPolicy(submission_enabled=True, dry_run=False)


def test_dry_run_previews_after_duplicate_and_open_order_checks():
    execution, _ = execution_and_trace()
    gateway = FakeGateway()
    result = BoundedPaperLauncher(gateway, PaperLaunchPolicy()).launch(execution)
    assert result.mode == "dry_run"
    assert gateway.calls == ["lookup", "open_orders", "preview"]


def test_duplicate_client_order_id_is_idempotent_and_never_repreviewed():
    execution, _ = execution_and_trace()
    gateway = FakeGateway(reconciled=broker_order("filled", filled_qty="1"))
    result = BoundedPaperLauncher(gateway, PaperLaunchPolicy()).launch(execution)
    assert result.duplicate is True
    assert gateway.calls == ["lookup"]


def test_bounded_submission_allows_one_order_and_rejects_second_or_excess_risk():
    execution, _ = execution_and_trace()
    policy = PaperLaunchPolicy(
        submission_enabled=True,
        dry_run=False,
        bounded_ack="paper-contest",
        max_authorized_loss_usd=Decimal("100"),
    )
    gateway = FakeGateway()
    launcher = BoundedPaperLauncher(gateway, policy)
    assert launcher.launch(execution).mode == "submitted"
    with pytest.raises(BrokerStateUnresolved, match="submission count"):
        launcher.launch(execution)

    too_small = replace(policy, max_authorized_loss_usd=Decimal("99"))
    with pytest.raises(BrokerStateUnresolved, match="loss limit"):
        BoundedPaperLauncher(FakeGateway(), too_small).launch(execution)


def test_partial_fill_reconciliation_is_typed_and_restart_safe():
    execution, _ = execution_and_trace()
    gateway = FakeGateway(reconciled=broker_order("partially_filled", filled_qty="1"))
    snapshot, event = BoundedPaperLauncher(gateway).reconcile(execution)
    assert snapshot.status == "partially_filled"
    assert event.status is OrderStatus.PARTIALLY_FILLED
    assert event.filled_quantity == 1
    assert event.average_fill_price == Decimal("0.98")


def test_provider_or_broker_shape_outage_fails_closed():
    execution, _ = execution_and_trace()
    gateway = FakeGateway(open_orders={"unexpected": "shape"})
    with pytest.raises(BrokerStateUnresolved, match="open-order payload"):
        BoundedPaperLauncher(gateway).launch(execution)


def test_cancel_is_single_order_bound_and_refuses_unrelated_or_final_order():
    execution, _ = execution_and_trace()
    launcher = BoundedPaperLauncher(FakeGateway(reconciled=broker_order()))
    open_snapshot = launcher.reconcile(execution)[0]
    assert launcher.cancel(execution, open_snapshot).payload == {"accepted": True}
    with pytest.raises(BrokerStateUnresolved, match="unrelated"):
        launcher.cancel(execution, replace(open_snapshot, client_order_id="agent.other"))
    with pytest.raises(BrokerStateUnresolved, match="final"):
        launcher.cancel(execution, replace(open_snapshot, status="filled"))


def test_decision_journal_persists_latest_complete_trace_without_secrets(tmp_path):
    _, initial = execution_and_trace()
    _, filled = execution_and_trace(include_fill=True)
    journal = DecisionTraceJournal(str(tmp_path / "decision-traces.jsonl"))
    journal.append(initial, phase="preview", outcome="dry_run", metadata={"rank": "1"}, recorded_at=NOW)
    journal.append(filled, phase="position", outcome="open", metadata={"provenance": "indicative"}, recorded_at=NOW + timedelta(minutes=1))
    records = journal.load_latest()
    assert len(records) == 1
    assert records[0].phase == "position"
    assert records[0].trace["assessments"][0]["state"] == "open"
    assert "secret" not in journal.path.read_text(encoding="utf-8").lower()
    with pytest.raises(ValueError, match="sensitive"):
        journal.append(initial, phase="preview", outcome="blocked", metadata={"alpaca_api_key": "never"})


def test_cycle_runner_persists_coordinator_to_preview_trace(tmp_path):
    execution, base_trace = execution_and_trace()
    coordinator_result = SimpleNamespace(
        bundle=base_trace.bundle,
        analyses=base_trace.analyses,
        proposals=base_trace.proposals,
        objections=base_trace.objections,
        authorizations=base_trace.authorizations,
        commands=base_trace.commands,
        authorized_executions=(execution,),
        duplicate_proposal_ids=(),
    )
    coordinator = SimpleNamespace(run_shadow_cycle=lambda **kwargs: coordinator_result)
    journal = DecisionTraceJournal(str(tmp_path / "cycles.jsonl"))
    result = PaperAgentCycleRunner(
        coordinator,
        BoundedPaperLauncher(FakeGateway(), PaperLaunchPolicy()),
        journal,
    ).run_cycle(
        trace_id="trace.m6",
        evidence=base_trace.evidence,
        portfolio=SimpleNamespace(),
        now=NOW,
        environment={"AGENT_COORDINATOR_ENABLED": "true"},
    )
    assert result.launches[0].mode == "dry_run"
    assert result.trace.replay_fingerprint == base_trace.replay_fingerprint
    assert journal.load_latest()[0].outcome == "dry_run"


def test_cycle_runner_persists_exit_ownership_on_reconciled_fill(tmp_path):
    execution, base_trace = execution_and_trace()
    coordinator_result = SimpleNamespace(
        bundle=base_trace.bundle,
        analyses=base_trace.analyses,
        proposals=base_trace.proposals,
        objections=(),
        authorizations=base_trace.authorizations,
        commands=base_trace.commands,
        authorized_executions=(execution,),
        duplicate_proposal_ids=(),
    )
    coordinator = SimpleNamespace(run_shadow_cycle=lambda **kwargs: coordinator_result)
    snapshot_gateway = FakeGateway(reconciled=broker_order("filled", filled_qty="1"))
    reconciler = BoundedPaperLauncher(snapshot_gateway)
    snapshot, event = reconciler.reconcile(execution)
    launcher = SimpleNamespace(
        launch=lambda item: SimpleNamespace(mode="submitted"),
        reconcile=lambda item: (snapshot, event),
    )
    plans = JsonlExitPlanStore(str(tmp_path / "exit-plans.jsonl"))
    result = PaperAgentCycleRunner(
        coordinator,
        launcher,
        DecisionTraceJournal(str(tmp_path / "cycles.jsonl")),
        exit_plan_store=plans,
    ).run_cycle(
        trace_id="trace.m6",
        evidence=base_trace.evidence,
        portfolio=SimpleNamespace(),
        now=NOW,
        environment={"AGENT_COORDINATOR_ENABLED": "true"},
    )
    assert result.trace.order_events[0].status is OrderStatus.FILLED
    assert result.trace.assessments[0].state is PositionState.OPEN
    assert plans.load_latest()[0].proposal_id == execution.proposal.record_id


def test_completed_bar_replay_is_deterministic_and_rejects_lookahead():
    _, trace = execution_and_trace(include_fill=True)
    scenario = ReplayScenario("bullish-fill", NOW, NOW + timedelta(minutes=1))
    result = DeterministicReplayRunner(lambda item: trace).run((scenario,))
    assert result[0].deterministic is True
    assert result[0].fingerprint == trace.replay_fingerprint
    with pytest.raises(ValueError, match="after the signal"):
        ReplayScenario("lookahead", NOW, NOW)


def test_replay_detects_changed_second_result():
    _, initial = execution_and_trace()
    _, filled = execution_and_trace(include_fill=True)
    traces = iter((initial, filled))
    with pytest.raises(RuntimeError, match="replay drift"):
        DeterministicReplayRunner(lambda item: next(traces)).run(
            (ReplayScenario("drift", NOW, NOW + timedelta(minutes=1)),)
        )
