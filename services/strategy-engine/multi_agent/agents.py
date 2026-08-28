"""Independent agents that transform frozen data into typed contracts.

These objects receive evidence and configuration only. Broker credentials and
CLI access are intentionally absent from every constructor and method.
"""

from __future__ import annotations

from datetime import datetime
import hashlib
from typing import Callable

from agent_contracts import (
    AnalysisDisposition,
    AdversarialObjection,
    AgentAnalysis,
    ContractValidationError,
    Direction,
    EvidenceBundle,
    EvidenceItem,
    ExecutionAction,
    ExecutionCommand,
    OptionsProposal,
    OrderEvent,
    PositionAssessment,
    RiskAuthorization,
    RiskDecision,
    contract_fingerprint,
)

from .models import AnalysisDraft, ObjectionDraft, PositionDraft, ProposalDraft


def stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:24]
    return f"{prefix}.{digest}"


class DataQualityAgent:
    name = "data_quality"

    def __init__(self, quality_engine=None) -> None:
        self._quality_engine = quality_engine

    def freeze(self, trace_id: str, evidence: tuple[EvidenceItem, ...], now: datetime) -> EvidenceBundle:
        if not evidence:
            raise ContractValidationError("data agent requires evidence")
        if any(item.trace_id != trace_id for item in evidence):
            raise ContractValidationError("evidence trace_id mismatch")
        if self._quality_engine is not None:
            report = self._quality_engine.evaluate(evidence, now)
            if report.veto:
                details = [*report.rejected_ids]
                details.extend(
                    f"{item.instrument}:{item.value_name}:{item.left_provider}!={item.right_provider}"
                    for item in report.disagreements
                )
                raise ContractValidationError(f"data-quality veto: {','.join(details)}")
            ordered = report.accepted
        else:
            stale = sorted(item.record_id for item in evidence if not item.is_fresh)
            if stale:
                raise ContractValidationError(f"stale evidence cannot be frozen: {','.join(stale)}")
            ordered = tuple(sorted(evidence, key=lambda item: item.record_id))
        evidence_fingerprint = contract_fingerprint(ordered)
        return EvidenceBundle(
            record_id=stable_id("bundle", trace_id, evidence_fingerprint),
            trace_id=trace_id,
            evidence_ids=tuple(item.record_id for item in ordered),
            evidence_fingerprint=evidence_fingerprint,
            frozen_at=now,
            created_at=now,
        )


AnalysisEvaluator = Callable[[EvidenceBundle, tuple[EvidenceItem, ...]], AnalysisDraft]


class EvidenceAnalysisAgent:
    def __init__(self, name: str, evaluator: AnalysisEvaluator) -> None:
        if not name:
            raise ValueError("analysis agent requires a name")
        self.name = name
        self._evaluator = evaluator

    def analyze(
        self,
        bundle: EvidenceBundle,
        evidence: tuple[EvidenceItem, ...],
        now: datetime,
    ) -> AgentAnalysis:
        draft = self._evaluator(bundle, evidence)
        draft_fingerprint = contract_fingerprint(draft)
        return AgentAnalysis(
            record_id=stable_id("analysis", self.name, bundle.record_id, draft_fingerprint),
            trace_id=bundle.trace_id,
            agent_name=self.name,
            evidence_bundle_id=bundle.record_id,
            evidence_fingerprint=contract_fingerprint(bundle),
            cited_evidence_ids=tuple(sorted(draft.cited_evidence_ids)),
            direction=draft.direction,
            confidence=draft.confidence,
            thesis=draft.thesis,
            contradictions=draft.contradictions,
            created_at=now,
            disposition=draft.disposition,
        )


class TechnicalAgent(EvidenceAnalysisAgent):
    def __init__(self, evaluator: AnalysisEvaluator) -> None:
        super().__init__("technical", evaluator)


class CatalystAgent(EvidenceAnalysisAgent):
    def __init__(self, evaluator: AnalysisEvaluator) -> None:
        super().__init__("catalyst", evaluator)


class MacroAgent(EvidenceAnalysisAgent):
    def __init__(self, evaluator: AnalysisEvaluator) -> None:
        super().__init__("macro", evaluator)


ProposalBuilder = Callable[[EvidenceBundle, tuple[EvidenceItem, ...], tuple[AgentAnalysis, ...]], tuple[ProposalDraft, ...]]


class OptionsStructureAgent:
    def __init__(self, name: str, builder: ProposalBuilder) -> None:
        if not name:
            raise ValueError("options-structure agent requires a name")
        self.name = name
        self._builder = builder

    def propose(
        self,
        bundle: EvidenceBundle,
        evidence: tuple[EvidenceItem, ...],
        analyses: tuple[AgentAnalysis, ...],
        now: datetime,
    ) -> tuple[OptionsProposal, ...]:
        if any(item.disposition is AnalysisDisposition.ABSTAIN for item in analyses):
            return ()
        analysis_ids = tuple(sorted(item.record_id for item in analyses))
        proposals = []
        for draft in self._builder(bundle, evidence, analyses):
            proposals.append(
                OptionsProposal(
                    record_id=stable_id("proposal", bundle.trace_id, self.name, draft.proposal_key),
                    trace_id=bundle.trace_id,
                    evidence_bundle_id=bundle.record_id,
                    analysis_ids=analysis_ids,
                    underlying=draft.underlying,
                    decision=draft.decision,
                    direction=draft.direction,
                    strategy_name=draft.strategy_name,
                    legs=draft.legs,
                    contract_quantity=draft.contract_quantity,
                    limit_debit=draft.limit_debit,
                    maximum_loss=draft.maximum_loss,
                    rationale=draft.rationale,
                    created_at=now,
                )
            )
        return tuple(proposals)


ObjectionReviewer = Callable[[OptionsProposal, EvidenceBundle, tuple[EvidenceItem, ...]], tuple[ObjectionDraft, ...]]


class AdversarialReviewAgent:
    name = "adversarial"

    def __init__(self, reviewer: ObjectionReviewer) -> None:
        self._reviewer = reviewer

    def review(
        self,
        proposal: OptionsProposal,
        bundle: EvidenceBundle,
        evidence: tuple[EvidenceItem, ...],
        now: datetime,
    ) -> tuple[AdversarialObjection, ...]:
        objections = []
        for index, draft in enumerate(self._reviewer(proposal, bundle, evidence)):
            objections.append(
                AdversarialObjection(
                    record_id=stable_id("objection", proposal.record_id, str(index), contract_fingerprint(draft)),
                    trace_id=proposal.trace_id,
                    proposal_id=proposal.record_id,
                    cited_evidence_ids=tuple(sorted(draft.cited_evidence_ids)),
                    severity=draft.severity,
                    objection=draft.objection,
                    blocking=draft.blocking,
                    created_at=now,
                )
            )
        return tuple(objections)


class ExecutionAgent:
    """Creates an immutable command; it cannot invoke a broker or CLI."""

    name = "execution"

    def command(
        self,
        proposal: OptionsProposal,
        authorization: RiskAuthorization,
        now: datetime,
    ) -> ExecutionCommand:
        if authorization.proposal_id != proposal.record_id:
            raise ContractValidationError("execution authorization does not match proposal")
        if authorization.decision is RiskDecision.REJECT:
            raise ContractValidationError("execution agent cannot command a rejected proposal")
        authorization_fingerprint = contract_fingerprint(authorization)
        client_order_id = stable_id("agent", proposal.trace_id, proposal.record_id, authorization_fingerprint)
        return ExecutionCommand(
            record_id=stable_id("command", client_order_id),
            trace_id=proposal.trace_id,
            authorization_id=authorization.record_id,
            authorization_fingerprint=authorization_fingerprint,
            proposal_id=proposal.record_id,
            action=ExecutionAction.SUBMIT,
            client_order_id=client_order_id,
            legs=proposal.legs,
            quantity=authorization.authorized_quantity,
            limit_price=proposal.limit_debit,
            created_at=now,
        )


class PositionAnalysisAgent:
    name = "position_analysis"

    def assess(
        self,
        trace_id: str,
        draft: PositionDraft,
        order_events: tuple[OrderEvent, ...],
        now: datetime,
    ) -> PositionAssessment:
        known = {event.record_id for event in order_events}
        if not set(draft.order_event_ids).issubset(known):
            raise ContractValidationError("position draft references an unknown order event")
        return PositionAssessment(
            record_id=stable_id("assessment", trace_id, draft.position_key, now.isoformat()),
            trace_id=trace_id,
            proposal_id=draft.proposal_id,
            authorization_id=draft.authorization_id,
            order_event_ids=draft.order_event_ids,
            position_key=draft.position_key,
            state=draft.state,
            quantity=draft.quantity,
            mark_value=draft.mark_value,
            unrealized_pnl=draft.unrealized_pnl,
            exit_reasons=draft.exit_reasons,
            assessed_at=draft.assessed_at,
            created_at=now,
        )
