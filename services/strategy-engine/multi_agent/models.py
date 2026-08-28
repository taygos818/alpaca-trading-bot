"""Credential-free inputs used by deterministic Milestone 2 agents."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from agent_contracts import AnalysisDisposition, Direction, OptionLeg, PositionState, ProposalDecision


@dataclass(frozen=True, slots=True)
class AnalysisDraft:
    direction: Direction
    confidence: Decimal
    thesis: str
    cited_evidence_ids: tuple[str, ...]
    contradictions: tuple[str, ...] = ()
    disposition: AnalysisDisposition = AnalysisDisposition.ANALYZE


@dataclass(frozen=True, slots=True)
class ProposalDraft:
    proposal_key: str
    underlying: str
    decision: ProposalDecision
    direction: Direction
    strategy_name: str
    legs: tuple[OptionLeg, ...]
    contract_quantity: int
    limit_debit: Decimal
    maximum_loss: Decimal
    rationale: str


@dataclass(frozen=True, slots=True)
class ObjectionDraft:
    severity: str
    objection: str
    cited_evidence_ids: tuple[str, ...]
    blocking: bool


@dataclass(frozen=True, slots=True)
class AllocationLimits:
    max_open_positions: int
    max_total_maximum_loss: Decimal
    max_underlying_maximum_loss: Decimal
    authorization_ttl_seconds: int = 120

    def __post_init__(self) -> None:
        if self.max_open_positions < 0:
            raise ValueError("max_open_positions cannot be negative")
        if self.max_total_maximum_loss < 0 or self.max_underlying_maximum_loss < 0:
            raise ValueError("maximum-loss limits cannot be negative")
        if self.authorization_ttl_seconds <= 0:
            raise ValueError("authorization TTL must be positive")


@dataclass(frozen=True, slots=True)
class PortfolioSnapshot:
    open_underlyings: tuple[str, ...]
    open_position_count: int
    reserved_maximum_loss: Decimal
    underlying_maximum_loss: tuple[tuple[str, Decimal], ...] = ()

    def __post_init__(self) -> None:
        if self.open_position_count < 0 or self.reserved_maximum_loss < 0:
            raise ValueError("portfolio counters cannot be negative")
        if len(self.open_underlyings) != len(set(self.open_underlyings)):
            raise ValueError("open_underlyings cannot contain duplicates")
        names = [symbol for symbol, _ in self.underlying_maximum_loss]
        if len(names) != len(set(names)) or any(value < 0 for _, value in self.underlying_maximum_loss):
            raise ValueError("underlying risk reservations must be unique and non-negative")


@dataclass(frozen=True, slots=True)
class PositionDraft:
    proposal_id: str
    authorization_id: str
    order_event_ids: tuple[str, ...]
    position_key: str
    state: PositionState
    quantity: int
    mark_value: Decimal
    unrealized_pnl: Decimal
    exit_reasons: tuple[str, ...]
    assessed_at: datetime
