"""End-to-end coordinator promotion with a complete persisted trace."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from agent_contracts import DecisionTrace, OrderStatus, PositionState
from multi_agent import MultiAgentCoordinator, PortfolioSnapshot, PositionAnalysisAgent, PositionDraft
from defined_risk_options import ExitPlanFactory, JsonlExitPlanStore

from .audit import DecisionTraceJournal
from .lifecycle import BoundedPaperLauncher, LaunchResult


@dataclass(frozen=True, slots=True)
class PaperCycleResult:
    coordinator: object
    launches: tuple[LaunchResult, ...]
    trace: DecisionTrace


class PaperAgentCycleRunner:
    def __init__(
        self,
        coordinator: MultiAgentCoordinator,
        launcher: BoundedPaperLauncher,
        journal: DecisionTraceJournal,
        exit_plan_store: JsonlExitPlanStore | None = None,
    ) -> None:
        self.coordinator = coordinator
        self.launcher = launcher
        self.journal = journal
        self.exit_plan_store = exit_plan_store
        self.exit_plan_factory = ExitPlanFactory()
        self.position_agent = PositionAnalysisAgent()

    def run_cycle(
        self,
        *,
        trace_id,
        evidence,
        portfolio: PortfolioSnapshot,
        now: datetime,
        environment=None,
        display_metadata=None,
    ):
        result = self.coordinator.run_shadow_cycle(
            trace_id=trace_id,
            evidence=evidence,
            portfolio=portfolio,
            now=now,
            environment=environment,
        )
        launches = []
        events = []
        assessments = []
        for execution in result.authorized_executions:
            launch = self.launcher.launch(execution)
            launches.append(launch)
            if launch.mode != "submitted":
                continue
            snapshot, event = self.launcher.reconcile(execution)
            events.append(event)
            if event.filled_quantity:
                if self.exit_plan_store is not None:
                    self.exit_plan_store.save(
                        self.exit_plan_factory.for_filled_proposal(
                            execution.proposal,
                            filled_quantity=event.filled_quantity,
                            entry_debit=event.average_fill_price,
                            opened_at=snapshot.broker_timestamp,
                            thesis_evidence_ids=tuple(item.record_id for item in evidence),
                        )
                    )
                state = PositionState.OPEN if event.status is OrderStatus.FILLED else PositionState.OPENING
                draft = PositionDraft(
                    proposal_id=execution.proposal.record_id,
                    authorization_id=execution.authorization.record_id,
                    order_event_ids=(event.record_id,),
                    position_key=f"position.{execution.proposal.underlying}.{execution.proposal.record_id}",
                    state=state,
                    quantity=event.filled_quantity,
                    mark_value=event.average_fill_price * 100 * event.filled_quantity,
                    unrealized_pnl=event.average_fill_price * 0,
                    exit_reasons=(),
                    assessed_at=snapshot.broker_timestamp,
                )
                assessments.append(self.position_agent.assess(trace_id, draft, (event,), snapshot.broker_timestamp))
        trace = DecisionTrace(
            evidence=evidence,
            bundle=result.bundle,
            analyses=result.analyses,
            proposals=result.proposals,
            objections=result.objections,
            authorizations=result.authorizations,
            commands=result.commands,
            order_events=tuple(events),
            assessments=tuple(assessments),
        )
        submitted = sum(1 for item in launches if item.mode == "submitted")
        dry_runs = sum(1 for item in launches if item.mode == "dry_run")
        phase = "broker_reconciled" if submitted else "preview"
        outcome = "submitted" if submitted else ("dry_run" if dry_runs else "no_authorized_trade")
        metadata = {
            "proposal_count": len(result.proposals),
            "rejection_reasons": [
                item.reason for item in result.authorizations if item.decision.value == "reject"
            ],
            "duplicate_proposal_ids": list(result.duplicate_proposal_ids),
        }
        metadata.update(display_metadata or {})
        self.journal.append(
            trace,
            phase=phase,
            outcome=outcome,
            metadata=metadata,
            recorded_at=now,
        )
        return PaperCycleResult(result, tuple(launches), trace)
