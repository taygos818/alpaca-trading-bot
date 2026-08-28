"""Adapter from dynamic discovery and frozen analyses to proposal drafts."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from typing import Callable

from agent_contracts import AgentAnalysis, EvidenceBundle, EvidenceItem

from .models import DiscoveryCandidate, OptionSnapshot, OptionsRiskState
from .strategy import DefinedRiskOptionsStrategy


CandidateProvider = Callable[[EvidenceBundle, tuple[EvidenceItem, ...], tuple[AgentAnalysis, ...]], tuple[DiscoveryCandidate, ...]]
ChainProvider = Callable[[str, datetime], tuple[OptionSnapshot, ...]]
RiskProvider = Callable[[DiscoveryCandidate], OptionsRiskState]


class DynamicOptionsProposalBuilder:
    """No symbol allowlist: evaluates every ranked candidate supplied by discovery."""

    def __init__(
        self,
        strategy: DefinedRiskOptionsStrategy,
        candidate_provider: CandidateProvider,
        chain_provider: ChainProvider,
        risk_provider: RiskProvider,
        clock: Callable[[], datetime],
    ) -> None:
        self.strategy = strategy
        self.candidate_provider = candidate_provider
        self.chain_provider = chain_provider
        self.risk_provider = risk_provider
        self.clock = clock

    def __call__(
        self,
        bundle: EvidenceBundle,
        evidence: tuple[EvidenceItem, ...],
        analyses: tuple[AgentAnalysis, ...],
    ):
        now = self.clock()
        candidates = sorted(
            self.candidate_provider(bundle, evidence, analyses),
            key=lambda item: (-item.rank, item.symbol),
        )
        proposals = []
        added_total = Decimal("0")
        added_underlying: dict[str, Decimal] = {}
        added_correlation: dict[str, Decimal] = {}
        added_positions = 0
        for candidate in candidates:
            base_risk = self.risk_provider(candidate)
            adjusted_risk = replace(
                base_risk,
                open_position_count=base_risk.open_position_count + added_positions,
                reserved_maximum_loss=base_risk.reserved_maximum_loss + added_total,
                underlying_maximum_loss=(
                    base_risk.underlying_maximum_loss + added_underlying.get(candidate.symbol, Decimal("0"))
                ),
                correlation_maximum_loss=(
                    base_risk.correlation_maximum_loss
                    + added_correlation.get(candidate.correlation_group, Decimal("0"))
                ),
            )
            selection = self.strategy.evaluate(
                candidate,
                self.chain_provider(candidate.symbol, now),
                analyses,
                adjusted_risk,
                now,
            )
            if selection.proposal is not None:
                proposal = selection.proposal
                proposals.append(proposal)
                added_total += proposal.maximum_loss
                added_underlying[candidate.symbol] = (
                    added_underlying.get(candidate.symbol, Decimal("0")) + proposal.maximum_loss
                )
                added_correlation[candidate.correlation_group] = (
                    added_correlation.get(candidate.correlation_group, Decimal("0")) + proposal.maximum_loss
                )
                added_positions += 1
        return tuple(proposals)
