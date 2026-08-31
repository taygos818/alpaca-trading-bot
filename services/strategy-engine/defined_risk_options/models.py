"""Immutable inputs and outputs for the defined-risk options strategy."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
import os

from agent_contracts import Direction, OptionLeg, OptionRight
from multi_agent import ProposalDraft


class StrategyDisabled(RuntimeError):
    pass


class ExitPlanState(str, Enum):
    ACTIVE = "active"
    EXIT_DUE = "exit_due"
    CLOSED = "closed"


def _decimal(name: str, value: Decimal, *, minimum: Decimal = Decimal("0"), positive: bool = False) -> None:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError(f"{name} must be a finite Decimal")
    if positive and value <= minimum:
        raise ValueError(f"{name} must be greater than {minimum}")
    if not positive and value < minimum:
        raise ValueError(f"{name} must be at least {minimum}")


def _utc(name: str, value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() != timezone.utc.utcoffset(value):
        raise ValueError(f"{name} must be UTC")


@dataclass(frozen=True, slots=True)
class OptionSnapshot:
    option_symbol: str
    underlying: str
    right: OptionRight
    strike: Decimal
    expiration: date
    bid: Decimal
    ask: Decimal
    bid_size: int
    ask_size: int
    volume: int
    open_interest: int
    delta: Decimal
    quote_time: datetime
    feed: str

    def __post_init__(self) -> None:
        if not self.option_symbol or not self.underlying:
            raise ValueError("option symbol and underlying are required")
        _decimal("strike", self.strike, positive=True)
        _decimal("bid", self.bid)
        _decimal("ask", self.ask)
        if self.ask < self.bid:
            raise ValueError("option ask cannot be below bid")
        for name in ("bid_size", "ask_size", "volume", "open_interest"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        _decimal("delta", self.delta, minimum=Decimal("-1"))
        if self.delta > 1:
            raise ValueError("delta must not exceed one")
        _utc("quote_time", self.quote_time)
        if not self.feed:
            raise ValueError("option feed is required")

    @property
    def midpoint(self) -> Decimal:
        return (self.bid + self.ask) / Decimal("2")

    @property
    def spread_pct(self) -> Decimal:
        midpoint = self.midpoint
        if midpoint <= 0:
            return Decimal("Infinity")
        return (self.ask - self.bid) / midpoint


@dataclass(frozen=True, slots=True)
class DiscoveryCandidate:
    symbol: str
    direction: Direction
    catalyst_evidence_ids: tuple[str, ...]
    completed_bar_evidence_ids: tuple[str, ...]
    correlation_group: str
    rank: Decimal

    def __post_init__(self) -> None:
        if not self.symbol or self.direction is Direction.NEUTRAL:
            raise ValueError("candidate must be directional")
        if not self.catalyst_evidence_ids or not self.completed_bar_evidence_ids:
            raise ValueError("candidate requires catalyst and completed-bar evidence")
        if not self.correlation_group:
            raise ValueError("candidate correlation group is required")
        _decimal("rank", self.rank)


@dataclass(frozen=True, slots=True)
class OptionsRiskState:
    options_trading_level: int
    equity: Decimal
    cash: Decimal
    options_buying_power: Decimal
    daily_pnl: Decimal
    open_position_count: int
    pending_order_count: int
    reserved_maximum_loss: Decimal
    underlying_maximum_loss: Decimal
    correlation_maximum_loss: Decimal

    def __post_init__(self) -> None:
        if self.options_trading_level not in {0, 1, 2, 3}:
            raise ValueError("options trading level must be 0 through 3")
        for name in (
            "equity",
            "cash",
            "options_buying_power",
            "reserved_maximum_loss",
            "underlying_maximum_loss",
            "correlation_maximum_loss",
        ):
            _decimal(name, getattr(self, name))
        _decimal("daily_pnl", self.daily_pnl, minimum=Decimal("-Infinity"))
        if self.open_position_count < 0 or self.pending_order_count < 0:
            raise ValueError("position and order counts cannot be negative")


@dataclass(frozen=True, slots=True)
class DefinedRiskOptionsConfig:
    enabled: bool = False
    min_dte: int = 7
    max_dte: int = 21
    exit_before_expiration_days: int = 2
    max_quote_age_seconds: int = 15
    required_feed: str = "indicative"
    min_open_interest: int = 100
    min_volume: int = 10
    min_quote_size: int = 1
    max_leg_spread_pct: Decimal = Decimal("0.15")
    target_long_delta: Decimal = Decimal("0.55")
    min_long_delta: Decimal = Decimal("0.40")
    max_long_delta: Decimal = Decimal("0.70")
    min_short_delta: Decimal = Decimal("0.15")
    target_short_delta: Decimal = Decimal("0.30")
    max_short_delta: Decimal = Decimal("0.40")
    min_reward_risk: Decimal = Decimal("0.75")
    max_contracts: int = 4
    max_trade_risk_pct: Decimal = Decimal("0.01")
    liquid_trade_risk_pct: Decimal = Decimal("0.03")
    expensive_trade_risk_pct: Decimal = Decimal("0.025")
    illiquid_trade_risk_pct: Decimal = Decimal("0.02")
    expensive_contract_equity_pct: Decimal = Decimal("0.01")
    max_total_risk_pct: Decimal = Decimal("0.10")
    max_underlying_risk_pct: Decimal = Decimal("0.03")
    max_correlation_risk_pct: Decimal = Decimal("0.05")
    min_cash_buffer_pct: Decimal = Decimal("0.20")
    daily_loss_limit_pct: Decimal = Decimal("0.03")
    max_open_positions: int = 4
    max_pending_orders: int = 4
    min_analysis_confidence: Decimal = Decimal("0.55")
    profit_target_pct: Decimal = Decimal("0.50")
    loss_limit_pct: Decimal = Decimal("0.40")
    max_holding_days: int = 3

    def __post_init__(self) -> None:
        if self.min_dte <= self.exit_before_expiration_days or self.max_dte < self.min_dte:
            raise ValueError("invalid options expiration window")
        if self.max_quote_age_seconds <= 0 or self.required_feed not in {"opra", "indicative"}:
            raise ValueError("invalid quote policy")
        if min(self.min_open_interest, self.min_volume, self.min_quote_size) < 0:
            raise ValueError("liquidity thresholds cannot be negative")
        for name in (
            "max_leg_spread_pct",
            "target_long_delta",
            "min_long_delta",
            "max_long_delta",
            "min_short_delta",
            "target_short_delta",
            "max_short_delta",
            "min_reward_risk",
            "max_trade_risk_pct",
            "liquid_trade_risk_pct",
            "expensive_trade_risk_pct",
            "illiquid_trade_risk_pct",
            "expensive_contract_equity_pct",
            "max_total_risk_pct",
            "max_underlying_risk_pct",
            "max_correlation_risk_pct",
            "min_cash_buffer_pct",
            "daily_loss_limit_pct",
            "min_analysis_confidence",
            "profit_target_pct",
            "loss_limit_pct",
        ):
            _decimal(name, getattr(self, name))
        if not (self.min_long_delta <= self.target_long_delta <= self.max_long_delta <= 1):
            raise ValueError("invalid long delta window")
        if not (Decimal("0") <= self.min_short_delta <= self.target_short_delta <= self.max_short_delta <= 1):
            raise ValueError("invalid short delta window")
        if self.max_contracts <= 0 or self.max_open_positions <= 0 or self.max_pending_orders < 0:
            raise ValueError("invalid portfolio count limit")
        if self.max_holding_days <= 0:
            raise ValueError("holding period must be positive")

    @classmethod
    def from_env(cls) -> "DefinedRiskOptionsConfig":
        return cls(
            enabled=os.getenv("DEFINED_RISK_OPTIONS_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"},
            required_feed=os.getenv("OPTIONS_MARKET_DATA_FEED", "indicative").strip().lower(),
            min_dte=int(os.getenv("OPTIONS_MIN_DTE", "7")),
            max_dte=int(os.getenv("OPTIONS_MAX_DTE", "21")),
            exit_before_expiration_days=int(os.getenv("OPTIONS_EXIT_BEFORE_EXPIRATION_DAYS", "2")),
            max_quote_age_seconds=int(os.getenv("OPTIONS_MAX_QUOTE_AGE_SECONDS", "15")),
            min_open_interest=int(os.getenv("OPTIONS_MIN_OPEN_INTEREST", "100")),
            min_volume=int(os.getenv("OPTIONS_MIN_VOLUME", "10")),
            min_quote_size=int(os.getenv("OPTIONS_MIN_QUOTE_SIZE", "1")),
            max_leg_spread_pct=Decimal(os.getenv("OPTIONS_MAX_LEG_SPREAD_PCT", "0.15")),
            target_long_delta=Decimal(os.getenv("OPTIONS_TARGET_LONG_DELTA", "0.55")),
            min_long_delta=Decimal(os.getenv("OPTIONS_MIN_LONG_DELTA", "0.40")),
            max_long_delta=Decimal(os.getenv("OPTIONS_MAX_LONG_DELTA", "0.70")),
            min_short_delta=Decimal(os.getenv("OPTIONS_MIN_SHORT_DELTA", "0.15")),
            target_short_delta=Decimal(os.getenv("OPTIONS_TARGET_SHORT_DELTA", "0.30")),
            max_short_delta=Decimal(os.getenv("OPTIONS_MAX_SHORT_DELTA", "0.40")),
            min_reward_risk=Decimal(os.getenv("OPTIONS_MIN_REWARD_RISK", "0.75")),
            max_contracts=int(os.getenv("OPTIONS_MAX_CONTRACTS", "10")),
            max_trade_risk_pct=Decimal(os.getenv("OPTIONS_MAX_TRADE_RISK_PCT", "0.05")),
            liquid_trade_risk_pct=Decimal(os.getenv("OPTIONS_LIQUID_TRADE_RISK_PCT", "0.03")),
            expensive_trade_risk_pct=Decimal(os.getenv("OPTIONS_EXPENSIVE_TRADE_RISK_PCT", "0.025")),
            illiquid_trade_risk_pct=Decimal(os.getenv("OPTIONS_ILLIQUID_TRADE_RISK_PCT", "0.02")),
            expensive_contract_equity_pct=Decimal(os.getenv("OPTIONS_EXPENSIVE_CONTRACT_EQUITY_PCT", "0.01")),
            max_total_risk_pct=Decimal(os.getenv("OPTIONS_MAX_TOTAL_RISK_PCT", "0.50")),
            max_underlying_risk_pct=Decimal(os.getenv("OPTIONS_MAX_UNDERLYING_RISK_PCT", "0.15")),
            max_correlation_risk_pct=Decimal(os.getenv("OPTIONS_MAX_CORRELATION_RISK_PCT", "0.40")),
            min_cash_buffer_pct=Decimal(os.getenv("OPTIONS_MIN_CASH_BUFFER_PCT", "0.05")),
            daily_loss_limit_pct=Decimal(os.getenv("OPTIONS_DAILY_LOSS_LIMIT_PCT", "0.12")),
            max_open_positions=int(os.getenv("OPTIONS_MAX_OPEN_POSITIONS", "10")),
            max_pending_orders=int(os.getenv("OPTIONS_MAX_PENDING_ORDERS", "8")),
            min_analysis_confidence=Decimal(os.getenv("OPTIONS_MIN_ANALYSIS_CONFIDENCE", "0.55")),
            profit_target_pct=Decimal(os.getenv("OPTIONS_PROFIT_TARGET_PCT", "0.25")),
            loss_limit_pct=Decimal(os.getenv("OPTIONS_LOSS_LIMIT_PCT", "0.30")),
            max_holding_days=int(os.getenv("OPTIONS_MAX_HOLDING_DAYS", "1")),
        )


@dataclass(frozen=True, slots=True)
class StrategySelection:
    proposal: ProposalDraft | None
    reason: str
    selected_symbols: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ExitPlan:
    plan_id: str
    proposal_id: str
    underlying: str
    legs: tuple[OptionLeg, ...]
    quantity: int
    entry_debit: Decimal
    maximum_loss: Decimal
    opened_at: datetime
    expiration: date
    profit_target_pct: Decimal
    loss_limit_pct: Decimal
    max_holding_days: int
    exit_before_expiration_days: int
    thesis_evidence_ids: tuple[str, ...]
    state: ExitPlanState = ExitPlanState.ACTIVE

    def __post_init__(self) -> None:
        if not self.plan_id or not self.proposal_id or not self.underlying or not self.legs:
            raise ValueError("exit plan identity and legs are required")
        if self.quantity <= 0:
            raise ValueError("exit plan quantity must be positive")
        _decimal("entry_debit", self.entry_debit, positive=True)
        _decimal("maximum_loss", self.maximum_loss, positive=True)
        _utc("opened_at", self.opened_at)
        if not self.thesis_evidence_ids:
            raise ValueError("exit plan requires thesis evidence")


@dataclass(frozen=True, slots=True)
class ExitDecision:
    should_exit: bool
    reasons: tuple[str, ...]
    limit_credit: Decimal
