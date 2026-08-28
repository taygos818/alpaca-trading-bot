"""Deterministic catalyst-confirmed debit-spread and long-option selection."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal, ROUND_UP
import hashlib

from agent_contracts import AgentAnalysis, AnalysisDisposition, Direction, LegSide, OptionLeg, OptionRight, ProposalDecision
from multi_agent import ProposalDraft

from .models import (
    DefinedRiskOptionsConfig,
    DiscoveryCandidate,
    OptionSnapshot,
    OptionsRiskState,
    StrategyDisabled,
    StrategySelection,
)


HUNDRED = Decimal("100")
CENT = Decimal("0.01")


class DefinedRiskOptionsStrategy:
    def __init__(self, config: DefinedRiskOptionsConfig | None = None) -> None:
        self.config = config or DefinedRiskOptionsConfig()

    def evaluate(
        self,
        candidate: DiscoveryCandidate,
        chain: tuple[OptionSnapshot, ...],
        analyses: tuple[AgentAnalysis, ...],
        risk: OptionsRiskState,
        now: datetime,
    ) -> StrategySelection:
        if not self.config.enabled:
            raise StrategyDisabled("defined-risk options strategy is disabled")
        gate = self._gate(candidate, analyses, risk)
        if gate:
            return StrategySelection(None, gate)
        eligible = tuple(item for item in chain if self._eligible_contract(item, candidate, now))
        if not eligible:
            return StrategySelection(None, "no liquid contracts in the configured DTE and quote window")
        if risk.options_trading_level >= 3:
            proposal = self._select_spread(candidate, eligible, risk)
        elif risk.options_trading_level == 2:
            proposal = self._select_long(candidate, eligible, risk)
        else:
            return StrategySelection(None, "account lacks Level 2 options trading")
        if proposal is None:
            return StrategySelection(None, "no structure passed price, reward-risk, and whole-contract limits")
        return StrategySelection(proposal, "defined-risk structure selected", tuple(leg.option_symbol for leg in proposal.legs))

    def _gate(
        self,
        candidate: DiscoveryCandidate,
        analyses: tuple[AgentAnalysis, ...],
        risk: OptionsRiskState,
    ) -> str:
        if risk.equity <= 0:
            return "account equity is unavailable"
        if risk.daily_pnl <= -(risk.equity * self.config.daily_loss_limit_pct):
            return "daily loss limit reached"
        if risk.open_position_count >= self.config.max_open_positions:
            return "maximum open positions reached"
        if risk.pending_order_count >= self.config.max_pending_orders:
            return "maximum pending orders reached"
        if risk.reserved_maximum_loss >= risk.equity * self.config.max_total_risk_pct:
            return "total premium-at-risk limit reached"
        if risk.underlying_maximum_loss >= risk.equity * self.config.max_underlying_risk_pct:
            return "underlying concentration limit reached"
        if risk.correlation_maximum_loss >= risk.equity * self.config.max_correlation_risk_pct:
            return "correlation-group risk limit reached"
        if not analyses:
            return "independent analyses are missing"
        if any(item.disposition is AnalysisDisposition.ABSTAIN for item in analyses):
            return "an independent analysis abstained"
        by_name = {item.agent_name: item for item in analyses}
        for required in ("technical", "catalyst"):
            item = by_name.get(required)
            if item is None:
                return f"{required} analysis is missing"
            if item.direction is not candidate.direction or item.confidence < self.config.min_analysis_confidence:
                return f"{required} analysis does not confirm the candidate"
        macro = by_name.get("macro")
        if macro is not None and macro.direction not in {Direction.NEUTRAL, candidate.direction}:
            return "macro analysis opposes the candidate"
        cited = {citation for item in analyses for citation in item.cited_evidence_ids}
        if not set(candidate.catalyst_evidence_ids).intersection(cited):
            return "catalyst evidence is not cited"
        if not set(candidate.completed_bar_evidence_ids).intersection(cited):
            return "completed-bar evidence is not cited"
        return ""

    def _eligible_contract(self, item: OptionSnapshot, candidate: DiscoveryCandidate, now: datetime) -> bool:
        expected_right = OptionRight.CALL if candidate.direction is Direction.BULLISH else OptionRight.PUT
        dte = (item.expiration - now.date()).days
        quote_age = now - item.quote_time
        absolute_delta = abs(item.delta)
        return (
            item.underlying == candidate.symbol
            and item.right is expected_right
            and self.config.min_dte <= dte <= self.config.max_dte
            and timedelta(0) <= quote_age <= timedelta(seconds=self.config.max_quote_age_seconds)
            and item.feed == self.config.required_feed
            and item.bid > 0
            and item.ask > item.bid
            and item.bid_size >= self.config.min_quote_size
            and item.ask_size >= self.config.min_quote_size
            and item.volume >= self.config.min_volume
            and item.open_interest >= self.config.min_open_interest
            and item.spread_pct <= self.config.max_leg_spread_pct
            and self.config.min_short_delta <= absolute_delta <= self.config.max_long_delta
        )

    def _select_spread(
        self,
        candidate: DiscoveryCandidate,
        chain: tuple[OptionSnapshot, ...],
        risk: OptionsRiskState,
    ) -> ProposalDraft | None:
        pairs = []
        for long_leg in chain:
            long_delta = abs(long_leg.delta)
            if not self.config.min_long_delta <= long_delta <= self.config.max_long_delta:
                continue
            for short_leg in chain:
                short_delta = abs(short_leg.delta)
                if long_leg.expiration != short_leg.expiration:
                    continue
                if not self.config.min_short_delta <= short_delta <= self.config.max_short_delta:
                    continue
                if candidate.direction is Direction.BULLISH and long_leg.strike >= short_leg.strike:
                    continue
                if candidate.direction is Direction.BEARISH and long_leg.strike <= short_leg.strike:
                    continue
                width = abs(short_leg.strike - long_leg.strike)
                debit = _round_up(long_leg.ask - short_leg.bid)
                if debit <= 0 or debit >= width:
                    continue
                reward_risk = (width - debit) / debit
                if reward_risk < self.config.min_reward_risk:
                    continue
                score = (
                    abs(long_delta - self.config.target_long_delta)
                    + abs(short_delta - self.config.target_short_delta)
                    + long_leg.spread_pct
                    + short_leg.spread_pct
                    + debit / width
                )
                pairs.append((score, debit, long_leg, short_leg))
        if not pairs:
            return None
        _, debit, long_leg, short_leg = min(
            pairs,
            key=lambda row: (row[0], row[1], row[2].expiration, row[2].strike, row[3].strike),
        )
        quantity = self._quantity(debit * HUNDRED, risk)
        if quantity <= 0:
            return None
        right_name = "call" if candidate.direction is Direction.BULLISH else "put"
        key = _proposal_key(candidate.symbol, right_name, long_leg.option_symbol, short_leg.option_symbol)
        return ProposalDraft(
            proposal_key=key,
            underlying=candidate.symbol,
            decision=ProposalDecision.PROPOSE,
            direction=candidate.direction,
            strategy_name=f"{right_name}_debit_spread",
            legs=(
                OptionLeg(long_leg.option_symbol, LegSide.BUY, long_leg.right, 1, long_leg.strike, long_leg.expiration),
                OptionLeg(short_leg.option_symbol, LegSide.SELL, short_leg.right, 1, short_leg.strike, short_leg.expiration),
            ),
            contract_quantity=quantity,
            limit_debit=debit,
            maximum_loss=debit * HUNDRED * quantity,
            rationale=(
                "Catalyst and completed-bar analyses agree; liquid same-expiration debit spread has known premium risk; "
                f"option pricing feed={long_leg.feed}."
            ),
        )

    def _select_long(
        self,
        candidate: DiscoveryCandidate,
        chain: tuple[OptionSnapshot, ...],
        risk: OptionsRiskState,
    ) -> ProposalDraft | None:
        longs = [
            item
            for item in chain
            if self.config.min_long_delta <= abs(item.delta) <= self.config.max_long_delta
        ]
        if not longs:
            return None
        contract = min(
            longs,
            key=lambda item: (
                abs(abs(item.delta) - self.config.target_long_delta),
                item.ask,
                item.expiration,
                item.strike,
            ),
        )
        debit = _round_up(contract.ask)
        quantity = self._quantity(debit * HUNDRED, risk)
        if quantity <= 0:
            return None
        right_name = "call" if candidate.direction is Direction.BULLISH else "put"
        return ProposalDraft(
            proposal_key=_proposal_key(candidate.symbol, f"long-{right_name}", contract.option_symbol),
            underlying=candidate.symbol,
            decision=ProposalDecision.PROPOSE,
            direction=candidate.direction,
            strategy_name=f"long_{right_name}_level2_fallback",
            legs=(OptionLeg(contract.option_symbol, LegSide.BUY, contract.right, 1, contract.strike, contract.expiration),),
            contract_quantity=quantity,
            limit_debit=debit,
            maximum_loss=debit * HUNDRED * quantity,
            rationale=(
                "Level 2 fallback uses a long option with premium as the known maximum loss; "
                f"option pricing feed={contract.feed}."
            ),
        )

    def _quantity(self, per_contract_risk: Decimal, risk: OptionsRiskState) -> int:
        if per_contract_risk <= 0:
            return 0
        cash_after_buffer = max(Decimal("0"), risk.cash - risk.equity * self.config.min_cash_buffer_pct)
        budgets = (
            risk.equity * self.config.max_trade_risk_pct,
            risk.equity * self.config.max_total_risk_pct - risk.reserved_maximum_loss,
            risk.equity * self.config.max_underlying_risk_pct - risk.underlying_maximum_loss,
            risk.equity * self.config.max_correlation_risk_pct - risk.correlation_maximum_loss,
            risk.options_buying_power,
            cash_after_buffer,
        )
        available = max(Decimal("0"), min(budgets))
        return min(self.config.max_contracts, int(available / per_contract_risk))


def _round_up(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_UP)


def _proposal_key(*parts: str) -> str:
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:24]
    return f"defined-risk.{digest}"
