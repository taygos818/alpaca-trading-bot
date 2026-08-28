"""Single deterministic allocator with veto authority over all proposals."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from agent_contracts import (
    AdversarialObjection,
    Direction,
    OptionsProposal,
    ProposalDecision,
    RiskAuthorization,
    RiskDecision,
    contract_fingerprint,
)

from .agents import stable_id
from .models import AllocationLimits, PortfolioSnapshot


class DeterministicAllocator:
    def __init__(self, limits: AllocationLimits) -> None:
        self.limits = limits

    def allocate(
        self,
        proposals: tuple[OptionsProposal, ...],
        objections: tuple[AdversarialObjection, ...],
        portfolio: PortfolioSnapshot,
        now: datetime,
    ) -> tuple[RiskAuthorization, ...]:
        objections_by_proposal: dict[str, list[AdversarialObjection]] = {}
        for objection in objections:
            objections_by_proposal.setdefault(objection.proposal_id, []).append(objection)

        remaining_total = max(
            Decimal("0"),
            self.limits.max_total_maximum_loss - portfolio.reserved_maximum_loss,
        )
        underlying_risk = dict(portfolio.underlying_maximum_loss)
        allocated_directions = {}
        new_underlyings: set[str] = set()
        authorizations = []

        ordered = sorted(
            proposals,
            key=lambda item: (item.maximum_loss, item.underlying, item.record_id),
        )
        for proposal in ordered:
            related = tuple(sorted(objections_by_proposal.get(proposal.record_id, ()), key=lambda item: item.record_id))
            decision, quantity, maximum_loss, reason = self._decide(
                proposal,
                related,
                portfolio,
                remaining_total,
                underlying_risk,
                new_underlyings,
                allocated_directions,
            )
            authorization = RiskAuthorization(
                record_id=stable_id("authorization", proposal.record_id, decision.value, str(quantity), format(maximum_loss, "f")),
                trace_id=proposal.trace_id,
                proposal_id=proposal.record_id,
                proposal_fingerprint=contract_fingerprint(proposal),
                objection_ids=tuple(item.record_id for item in related),
                decision=decision,
                authorized_quantity=quantity,
                authorized_maximum_loss=maximum_loss,
                reason=reason,
                expires_at=now + timedelta(seconds=self.limits.authorization_ttl_seconds),
                created_at=now,
            )
            authorizations.append(authorization)
            if decision is not RiskDecision.REJECT:
                remaining_total -= maximum_loss
                underlying_risk[proposal.underlying] = underlying_risk.get(proposal.underlying, Decimal("0")) + maximum_loss
                if proposal.underlying not in portfolio.open_underlyings:
                    new_underlyings.add(proposal.underlying)
                allocated_directions[proposal.underlying] = proposal.direction
        return tuple(authorizations)

    def _decide(
        self,
        proposal: OptionsProposal,
        objections: tuple[AdversarialObjection, ...],
        portfolio: PortfolioSnapshot,
        remaining_total: Decimal,
        underlying_risk: dict[str, Decimal],
        new_underlyings: set[str],
        allocated_directions: dict[str, Direction],
    ) -> tuple[RiskDecision, int, Decimal, str]:
        if proposal.decision is ProposalDecision.ABSTAIN:
            return RiskDecision.REJECT, 0, Decimal("0"), "proposal abstained"
        if any(item.blocking or item.severity == "critical" for item in objections):
            return RiskDecision.REJECT, 0, Decimal("0"), "adversarial veto"
        allocated_direction = allocated_directions.get(proposal.underlying)
        if allocated_direction is not None and allocated_direction is not proposal.direction:
            return RiskDecision.REJECT, 0, Decimal("0"), "opposing directional exposure in allocation batch"
        is_new = proposal.underlying not in portfolio.open_underlyings and proposal.underlying not in new_underlyings
        available_slots = self.limits.max_open_positions - portfolio.open_position_count - len(new_underlyings)
        if is_new and available_slots <= 0:
            return RiskDecision.REJECT, 0, Decimal("0"), "maximum position count reached"

        current_underlying = underlying_risk.get(proposal.underlying, Decimal("0"))
        remaining_underlying = max(
            Decimal("0"),
            self.limits.max_underlying_maximum_loss - current_underlying,
        )
        per_contract = proposal.maximum_loss / proposal.contract_quantity
        affordable = min(
            proposal.contract_quantity,
            int(remaining_total / per_contract),
            int(remaining_underlying / per_contract),
        )
        if affordable <= 0:
            return RiskDecision.REJECT, 0, Decimal("0"), "maximum-loss budget exhausted"
        approved_loss = per_contract * affordable
        if affordable < proposal.contract_quantity:
            return RiskDecision.REDUCE, affordable, approved_loss, "reduced to deterministic maximum-loss limits"
        return RiskDecision.APPROVE, affordable, approved_loss, "within deterministic portfolio limits"


class PortfolioRiskAgent:
    """Credential-free risk agent; the allocator is its only authority."""

    name = "portfolio_risk"

    def __init__(self, allocator: DeterministicAllocator) -> None:
        self._allocator = allocator

    def authorize(
        self,
        proposals: tuple[OptionsProposal, ...],
        objections: tuple[AdversarialObjection, ...],
        portfolio: PortfolioSnapshot,
        now: datetime,
    ) -> tuple[RiskAuthorization, ...]:
        return self._allocator.allocate(proposals, objections, portfolio, now)
