"""Immutable contracts at every trust boundary in the agent workflow.

The contracts deliberately use only the Python standard library. They serialize
to canonical JSON, reject unknown schema versions, and require an unbroken trace
from frozen evidence through position assessment. They do not contain broker
credentials or methods capable of placing orders.
"""

from __future__ import annotations

from dataclasses import dataclass, fields, is_dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
import hashlib
import json
import math
import os
import re
from typing import Any


CONTRACT_SCHEMA_VERSION = "1.0"
_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SHA256_PATTERN = re.compile(r"^[a-f0-9]{64}$")


class ContractValidationError(ValueError):
    """Raised when agent output cannot safely cross a trust boundary."""


class Direction(str, Enum):
    BULLISH = "bullish"
    BEARISH = "bearish"
    NEUTRAL = "neutral"


class AnalysisDisposition(str, Enum):
    ANALYZE = "analyze"
    ABSTAIN = "abstain"


class LegSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


class OptionRight(str, Enum):
    CALL = "call"
    PUT = "put"


class ProposalDecision(str, Enum):
    PROPOSE = "propose"
    ABSTAIN = "abstain"


class RiskDecision(str, Enum):
    APPROVE = "approve"
    REJECT = "reject"
    REDUCE = "reduce"


class ExecutionAction(str, Enum):
    SUBMIT = "submit"
    CANCEL = "cancel"
    REPLACE = "replace"


class OrderStatus(str, Enum):
    REQUESTED = "requested"
    ACCEPTED = "accepted"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELED = "canceled"
    REJECTED = "rejected"
    EXPIRED = "expired"


class PositionState(str, Enum):
    OPENING = "opening"
    OPEN = "open"
    EXIT_DUE = "exit_due"
    CLOSING = "closing"
    CLOSED = "closed"


def _require_id(name: str, value: str) -> None:
    if not isinstance(value, str) or not _ID_PATTERN.fullmatch(value):
        raise ContractValidationError(f"{name} must be a stable identifier")


def _require_text(name: str, value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ContractValidationError(f"{name} must be non-empty")


def _require_utc(name: str, value: datetime) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timezone.utc.utcoffset(value):
        raise ContractValidationError(f"{name} must be timezone-aware UTC")


def _require_decimal(
    name: str,
    value: Decimal,
    *,
    minimum: Decimal | None = Decimal("0"),
    positive: bool = False,
) -> None:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ContractValidationError(f"{name} must be a finite Decimal")
    if positive and minimum is not None and value <= minimum:
        raise ContractValidationError(f"{name} must be greater than {minimum}")
    if not positive and minimum is not None and value < minimum:
        raise ContractValidationError(f"{name} must be at least {minimum}")


def _validate_common(schema_version: str, trace_id: str, record_id: str, created_at: datetime) -> None:
    if schema_version != CONTRACT_SCHEMA_VERSION:
        raise ContractValidationError(f"unsupported schema_version: {schema_version}")
    _require_id("trace_id", trace_id)
    _require_id("record_id", record_id)
    _require_utc("created_at", created_at)


def _validate_ids(name: str, values: tuple[str, ...], *, required: bool = True) -> None:
    if not isinstance(values, tuple):
        raise ContractValidationError(f"{name} must be an immutable tuple")
    if required and not values:
        raise ContractValidationError(f"{name} must not be empty")
    if len(values) != len(set(values)):
        raise ContractValidationError(f"{name} must not contain duplicates")
    for value in values:
        _require_id(name, value)


def _require_enum(name: str, value: Any, enum_type: type[Enum]) -> None:
    if not isinstance(value, enum_type):
        raise ContractValidationError(f"{name} must be a {enum_type.__name__}")


def _require_tuple(name: str, value: Any) -> None:
    if not isinstance(value, tuple):
        raise ContractValidationError(f"{name} must be an immutable tuple")


def _enum_value(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime):
        _require_utc("datetime", value)
        return value.isoformat(timespec="microseconds").replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if is_dataclass(value):
        return {field.name: _enum_value(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, tuple):
        return [_enum_value(item) for item in value]
    if isinstance(value, list):
        return [_enum_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _enum_value(item) for key, item in value.items()}
    if isinstance(value, float) and not math.isfinite(value):
        raise ContractValidationError("canonical payload cannot contain non-finite numbers")
    return value


def canonical_json(value: Any) -> str:
    """Return a byte-stable JSON representation suitable for replay hashing."""

    return json.dumps(_enum_value(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def contract_fingerprint(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def coordinator_contracts_enabled(environment: dict[str, str] | None = None) -> bool:
    source = os.environ if environment is None else environment
    return source.get("AGENT_COORDINATOR_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True, slots=True)
class EvidenceItem:
    record_id: str
    trace_id: str
    provider: str
    instrument: str
    event_time: datetime
    received_at: datetime
    raw_sha256: str
    value_name: str
    value: str
    created_at: datetime
    source_uri: str = ""
    entitlement: str = "unknown"
    is_fresh: bool = False
    authority: str = "research"
    session: str = "unknown"
    temporal_kind: str = "observed"
    transformation_version: str = "raw-v1"
    numeric_value: Decimal | None = None
    vintage: str = ""
    schema_version: str = CONTRACT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _validate_common(self.schema_version, self.trace_id, self.record_id, self.created_at)
        for name in ("provider", "instrument", "value_name", "value", "entitlement"):
            _require_text(name, getattr(self, name))
        _require_utc("event_time", self.event_time)
        _require_utc("received_at", self.received_at)
        if self.temporal_kind == "observed" and self.received_at < self.event_time:
            raise ContractValidationError("received_at cannot precede event_time")
        if self.created_at < self.received_at:
            raise ContractValidationError("created_at cannot precede received_at")
        if not _SHA256_PATTERN.fullmatch(self.raw_sha256):
            raise ContractValidationError("raw_sha256 must be a lowercase SHA-256 digest")
        if not isinstance(self.is_fresh, bool):
            raise ContractValidationError("is_fresh must be a boolean")
        if self.authority not in {"broker_truth", "licensed_research", "unofficial_research", "macro_research", "research"}:
            raise ContractValidationError("unsupported evidence authority")
        if self.session not in {"premarket", "regular", "postmarket", "overnight", "daily", "event", "unknown"}:
            raise ContractValidationError("unsupported evidence session")
        if self.temporal_kind not in {"observed", "scheduled", "historical", "release"}:
            raise ContractValidationError("unsupported temporal_kind")
        _require_text("transformation_version", self.transformation_version)
        if self.numeric_value is not None:
            _require_decimal("numeric_value", self.numeric_value, minimum=None)


@dataclass(frozen=True, slots=True)
class EvidenceBundle:
    record_id: str
    trace_id: str
    evidence_ids: tuple[str, ...]
    evidence_fingerprint: str
    frozen_at: datetime
    created_at: datetime
    schema_version: str = CONTRACT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _validate_common(self.schema_version, self.trace_id, self.record_id, self.created_at)
        _validate_ids("evidence_ids", self.evidence_ids)
        if not _SHA256_PATTERN.fullmatch(self.evidence_fingerprint):
            raise ContractValidationError("evidence_fingerprint must be a lowercase SHA-256 digest")
        _require_utc("frozen_at", self.frozen_at)
        if self.created_at < self.frozen_at:
            raise ContractValidationError("created_at cannot precede frozen_at")


@dataclass(frozen=True, slots=True)
class AgentAnalysis:
    record_id: str
    trace_id: str
    agent_name: str
    evidence_bundle_id: str
    evidence_fingerprint: str
    cited_evidence_ids: tuple[str, ...]
    direction: Direction
    confidence: Decimal
    thesis: str
    contradictions: tuple[str, ...]
    created_at: datetime
    disposition: AnalysisDisposition = AnalysisDisposition.ANALYZE
    schema_version: str = CONTRACT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _validate_common(self.schema_version, self.trace_id, self.record_id, self.created_at)
        _require_text("agent_name", self.agent_name)
        _require_id("evidence_bundle_id", self.evidence_bundle_id)
        if not _SHA256_PATTERN.fullmatch(self.evidence_fingerprint):
            raise ContractValidationError("evidence_fingerprint must be a lowercase SHA-256 digest")
        _validate_ids("cited_evidence_ids", self.cited_evidence_ids)
        _require_enum("direction", self.direction, Direction)
        _require_enum("disposition", self.disposition, AnalysisDisposition)
        _require_decimal("confidence", self.confidence)
        if self.confidence > Decimal("1"):
            raise ContractValidationError("confidence must be no greater than 1")
        _require_text("thesis", self.thesis)
        _require_tuple("contradictions", self.contradictions)
        if self.disposition is AnalysisDisposition.ABSTAIN:
            if self.direction is not Direction.NEUTRAL or self.confidence != Decimal("0"):
                raise ContractValidationError("abstention must be neutral with zero confidence")


@dataclass(frozen=True, slots=True)
class OptionLeg:
    option_symbol: str
    side: LegSide
    right: OptionRight
    quantity: int
    strike: Decimal
    expiration: date

    def __post_init__(self) -> None:
        _require_text("option_symbol", self.option_symbol)
        _require_enum("side", self.side, LegSide)
        _require_enum("right", self.right, OptionRight)
        if not isinstance(self.quantity, int) or isinstance(self.quantity, bool) or self.quantity <= 0:
            raise ContractValidationError("option leg quantity must be a positive whole number")
        _require_decimal("strike", self.strike, positive=True)
        if not isinstance(self.expiration, date) or isinstance(self.expiration, datetime):
            raise ContractValidationError("expiration must be a date")


@dataclass(frozen=True, slots=True)
class OptionsProposal:
    record_id: str
    trace_id: str
    evidence_bundle_id: str
    analysis_ids: tuple[str, ...]
    underlying: str
    decision: ProposalDecision
    direction: Direction
    strategy_name: str
    legs: tuple[OptionLeg, ...]
    contract_quantity: int
    limit_debit: Decimal
    maximum_loss: Decimal
    rationale: str
    created_at: datetime
    schema_version: str = CONTRACT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _validate_common(self.schema_version, self.trace_id, self.record_id, self.created_at)
        _require_id("evidence_bundle_id", self.evidence_bundle_id)
        _validate_ids("analysis_ids", self.analysis_ids)
        _require_text("underlying", self.underlying)
        _require_text("strategy_name", self.strategy_name)
        _require_text("rationale", self.rationale)
        _require_enum("decision", self.decision, ProposalDecision)
        _require_enum("direction", self.direction, Direction)
        _require_tuple("legs", self.legs)
        if self.decision is ProposalDecision.ABSTAIN:
            if self.legs or self.contract_quantity != 0 or self.limit_debit != Decimal("0") or self.maximum_loss != Decimal("0"):
                raise ContractValidationError("abstention cannot carry executable terms")
            return
        if self.direction is Direction.NEUTRAL:
            raise ContractValidationError("executable proposal must be directional")
        if not self.legs:
            raise ContractValidationError("executable proposal requires option legs")
        if not isinstance(self.contract_quantity, int) or isinstance(self.contract_quantity, bool) or self.contract_quantity <= 0:
            raise ContractValidationError("contract_quantity must be a positive whole number")
        _require_decimal("limit_debit", self.limit_debit, positive=True)
        _require_decimal("maximum_loss", self.maximum_loss, positive=True)


@dataclass(frozen=True, slots=True)
class AdversarialObjection:
    record_id: str
    trace_id: str
    proposal_id: str
    cited_evidence_ids: tuple[str, ...]
    severity: str
    objection: str
    blocking: bool
    created_at: datetime
    schema_version: str = CONTRACT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _validate_common(self.schema_version, self.trace_id, self.record_id, self.created_at)
        _require_id("proposal_id", self.proposal_id)
        _validate_ids("cited_evidence_ids", self.cited_evidence_ids)
        if self.severity not in {"low", "medium", "high", "critical"}:
            raise ContractValidationError("severity must be low, medium, high, or critical")
        _require_text("objection", self.objection)
        if not isinstance(self.blocking, bool):
            raise ContractValidationError("blocking must be a boolean")


@dataclass(frozen=True, slots=True)
class RiskAuthorization:
    record_id: str
    trace_id: str
    proposal_id: str
    proposal_fingerprint: str
    objection_ids: tuple[str, ...]
    decision: RiskDecision
    authorized_quantity: int
    authorized_maximum_loss: Decimal
    reason: str
    expires_at: datetime
    created_at: datetime
    schema_version: str = CONTRACT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _validate_common(self.schema_version, self.trace_id, self.record_id, self.created_at)
        _require_id("proposal_id", self.proposal_id)
        if not _SHA256_PATTERN.fullmatch(self.proposal_fingerprint):
            raise ContractValidationError("proposal_fingerprint must be a lowercase SHA-256 digest")
        _validate_ids("objection_ids", self.objection_ids, required=False)
        _require_enum("decision", self.decision, RiskDecision)
        _require_text("reason", self.reason)
        _require_utc("expires_at", self.expires_at)
        if self.decision is RiskDecision.REJECT:
            if self.authorized_quantity != 0 or self.authorized_maximum_loss != Decimal("0"):
                raise ContractValidationError("rejected authorization cannot authorize exposure")
        else:
            if not isinstance(self.authorized_quantity, int) or isinstance(self.authorized_quantity, bool) or self.authorized_quantity <= 0:
                raise ContractValidationError("approved quantity must be a positive whole number")
            _require_decimal("authorized_maximum_loss", self.authorized_maximum_loss, positive=True)
            if self.expires_at <= self.created_at:
                raise ContractValidationError("authorization must expire after creation")


@dataclass(frozen=True, slots=True)
class ExecutionCommand:
    record_id: str
    trace_id: str
    authorization_id: str
    authorization_fingerprint: str
    proposal_id: str
    action: ExecutionAction
    client_order_id: str
    legs: tuple[OptionLeg, ...]
    quantity: int
    limit_price: Decimal
    created_at: datetime
    schema_version: str = CONTRACT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _validate_common(self.schema_version, self.trace_id, self.record_id, self.created_at)
        for name in ("authorization_id", "proposal_id", "client_order_id"):
            _require_id(name, getattr(self, name))
        _require_enum("action", self.action, ExecutionAction)
        _require_tuple("legs", self.legs)
        if not _SHA256_PATTERN.fullmatch(self.authorization_fingerprint):
            raise ContractValidationError("authorization_fingerprint must be a lowercase SHA-256 digest")
        if not isinstance(self.quantity, int) or isinstance(self.quantity, bool) or self.quantity < 0:
            raise ContractValidationError("execution quantity must be a non-negative whole number")
        _require_decimal("limit_price", self.limit_price)
        if self.action is ExecutionAction.SUBMIT:
            if not self.legs:
                raise ContractValidationError("submit command requires option legs")
            if not isinstance(self.quantity, int) or isinstance(self.quantity, bool) or self.quantity <= 0:
                raise ContractValidationError("execution quantity must be a positive whole number")
            _require_decimal("limit_price", self.limit_price, positive=True)


@dataclass(frozen=True, slots=True)
class AuthorizedExecution:
    """Self-validating envelope accepted by the credentialed gateway."""

    proposal: OptionsProposal
    authorization: RiskAuthorization
    command: ExecutionCommand

    def __post_init__(self) -> None:
        if len({self.proposal.trace_id, self.authorization.trace_id, self.command.trace_id}) != 1:
            raise ContractValidationError("authorized execution trace_id mismatch")
        if self.authorization.proposal_id != self.proposal.record_id:
            raise ContractValidationError("authorization does not reference the proposal")
        if self.authorization.proposal_fingerprint != contract_fingerprint(self.proposal):
            raise ContractValidationError("authorization proposal fingerprint mismatch")
        if self.authorization.decision is RiskDecision.REJECT:
            raise ContractValidationError("rejected authorization cannot reach execution")
        if self.authorization.authorized_quantity > self.proposal.contract_quantity:
            raise ContractValidationError("authorization exceeds proposal quantity")
        if self.authorization.authorized_maximum_loss > self.proposal.maximum_loss:
            raise ContractValidationError("authorization exceeds proposal maximum loss")
        if self.command.authorization_id != self.authorization.record_id:
            raise ContractValidationError("command does not reference the authorization")
        if self.command.authorization_fingerprint != contract_fingerprint(self.authorization):
            raise ContractValidationError("command authorization fingerprint mismatch")
        if self.command.proposal_id != self.proposal.record_id:
            raise ContractValidationError("command does not reference the proposal")
        if not self.authorization.created_at <= self.command.created_at < self.authorization.expires_at:
            raise ContractValidationError("command falls outside the authorization window")
        if self.command.quantity > self.authorization.authorized_quantity:
            raise ContractValidationError("command exceeds authorized quantity")
        if self.command.legs != self.proposal.legs:
            raise ContractValidationError("command legs do not match proposal")
        if self.command.limit_price > self.proposal.limit_debit:
            raise ContractValidationError("command exceeds proposal limit debit")
        minimum_proposal_loss = self.proposal.limit_debit * Decimal("100") * self.proposal.contract_quantity
        if self.proposal.maximum_loss < minimum_proposal_loss:
            raise ContractValidationError("proposal understates premium at risk")
        minimum_authorized_loss = self.command.limit_price * Decimal("100") * self.command.quantity
        if self.authorization.authorized_maximum_loss < minimum_authorized_loss:
            raise ContractValidationError("authorization understates premium at risk")


@dataclass(frozen=True, slots=True)
class OrderEvent:
    record_id: str
    trace_id: str
    command_id: str
    broker_order_id: str
    status: OrderStatus
    filled_quantity: int
    average_fill_price: Decimal
    broker_timestamp: datetime
    created_at: datetime
    schema_version: str = CONTRACT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _validate_common(self.schema_version, self.trace_id, self.record_id, self.created_at)
        _require_id("command_id", self.command_id)
        _require_id("broker_order_id", self.broker_order_id)
        _require_enum("status", self.status, OrderStatus)
        if not isinstance(self.filled_quantity, int) or isinstance(self.filled_quantity, bool) or self.filled_quantity < 0:
            raise ContractValidationError("filled_quantity must be a non-negative whole number")
        _require_decimal("average_fill_price", self.average_fill_price)
        if self.filled_quantity == 0 and self.average_fill_price != Decimal("0"):
            raise ContractValidationError("unfilled event must have zero average fill price")
        _require_utc("broker_timestamp", self.broker_timestamp)


@dataclass(frozen=True, slots=True)
class PositionAssessment:
    record_id: str
    trace_id: str
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
    created_at: datetime
    schema_version: str = CONTRACT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _validate_common(self.schema_version, self.trace_id, self.record_id, self.created_at)
        for name in ("proposal_id", "authorization_id", "position_key"):
            _require_id(name, getattr(self, name))
        _validate_ids("order_event_ids", self.order_event_ids)
        _require_enum("state", self.state, PositionState)
        _require_tuple("exit_reasons", self.exit_reasons)
        if not isinstance(self.quantity, int) or isinstance(self.quantity, bool) or self.quantity < 0:
            raise ContractValidationError("position quantity must be a non-negative whole number")
        _require_decimal("mark_value", self.mark_value)
        _require_decimal("unrealized_pnl", self.unrealized_pnl, minimum=None)
        _require_utc("assessed_at", self.assessed_at)
        if self.state is PositionState.EXIT_DUE and not self.exit_reasons:
            raise ContractValidationError("exit_due assessment requires an exit reason")


@dataclass(frozen=True, slots=True)
class DecisionTrace:
    """A complete, validated evidence-to-position replay unit."""

    evidence: tuple[EvidenceItem, ...]
    bundle: EvidenceBundle
    analyses: tuple[AgentAnalysis, ...]
    proposals: tuple[OptionsProposal, ...]
    objections: tuple[AdversarialObjection, ...]
    authorizations: tuple[RiskAuthorization, ...]
    commands: tuple[ExecutionCommand, ...]
    order_events: tuple[OrderEvent, ...]
    assessments: tuple[PositionAssessment, ...]

    def __post_init__(self) -> None:
        for name in (
            "evidence",
            "analyses",
            "proposals",
            "objections",
            "authorizations",
            "commands",
            "order_events",
            "assessments",
        ):
            _require_tuple(name, getattr(self, name))
        records: tuple[Any, ...] = (
            *self.evidence,
            self.bundle,
            *self.analyses,
            *self.proposals,
            *self.objections,
            *self.authorizations,
            *self.commands,
            *self.order_events,
            *self.assessments,
        )
        if not records:
            raise ContractValidationError("decision trace cannot be empty")
        trace_ids = {record.trace_id for record in records}
        if len(trace_ids) != 1:
            raise ContractValidationError("all records must share one trace_id")
        record_ids = [record.record_id for record in records]
        if len(record_ids) != len(set(record_ids)):
            raise ContractValidationError("record_id must be unique across the trace")

        evidence_by_id = {item.record_id: item for item in self.evidence}
        if set(self.bundle.evidence_ids) != set(evidence_by_id):
            raise ContractValidationError("evidence bundle references must exactly match supplied evidence")
        frozen_evidence = tuple(sorted(self.evidence, key=lambda item: item.record_id))
        if self.bundle.evidence_fingerprint != contract_fingerprint(frozen_evidence):
            raise ContractValidationError("evidence bundle fingerprint does not match supplied evidence")
        if self.bundle.frozen_at < max(item.created_at for item in self.evidence):
            raise ContractValidationError("evidence bundle was frozen before evidence collection completed")
        bundle_fingerprint = contract_fingerprint(self.bundle)
        analysis_by_id = {item.record_id: item for item in self.analyses}
        for item in self.analyses:
            if item.evidence_bundle_id != self.bundle.record_id or item.evidence_fingerprint != bundle_fingerprint:
                raise ContractValidationError("analysis does not reference the frozen evidence bundle")
            if not set(item.cited_evidence_ids).issubset(evidence_by_id):
                raise ContractValidationError("analysis cites unknown evidence")
            if item.created_at < self.bundle.created_at:
                raise ContractValidationError("analysis predates its evidence bundle")

        proposal_by_id = {item.record_id: item for item in self.proposals}
        for item in self.proposals:
            if item.evidence_bundle_id != self.bundle.record_id or set(item.analysis_ids) != set(analysis_by_id):
                raise ContractValidationError("proposal has an untraceable analysis or evidence reference")
            if any(analysis_by_id[analysis_id].disposition is AnalysisDisposition.ABSTAIN for analysis_id in item.analysis_ids):
                raise ContractValidationError("proposal cannot cross an analysis abstention")
            if item.created_at < max(analysis_by_id[analysis_id].created_at for analysis_id in item.analysis_ids):
                raise ContractValidationError("proposal predates one of its analyses")

        objection_by_id = {item.record_id: item for item in self.objections}
        for item in self.objections:
            if item.proposal_id not in proposal_by_id or not set(item.cited_evidence_ids).issubset(evidence_by_id):
                raise ContractValidationError("objection has an untraceable proposal or evidence reference")
            if item.created_at < proposal_by_id[item.proposal_id].created_at:
                raise ContractValidationError("objection predates its proposal")

        authorization_by_id = {item.record_id: item for item in self.authorizations}
        for item in self.authorizations:
            proposal = proposal_by_id.get(item.proposal_id)
            if proposal is None or item.proposal_fingerprint != contract_fingerprint(proposal):
                raise ContractValidationError("authorization does not match its proposal")
            if not set(item.objection_ids).issubset(objection_by_id):
                raise ContractValidationError("authorization references an unknown objection")
            if item.created_at < proposal.created_at:
                raise ContractValidationError("authorization predates its proposal")
            if item.decision is not RiskDecision.REJECT:
                if proposal.decision is ProposalDecision.ABSTAIN:
                    raise ContractValidationError("risk cannot authorize an abstention")
                if item.authorized_quantity > proposal.contract_quantity:
                    raise ContractValidationError("risk authorization cannot increase proposal quantity")
                if item.authorized_maximum_loss > proposal.maximum_loss:
                    raise ContractValidationError("risk authorization cannot increase maximum loss")
                if item.decision is RiskDecision.REDUCE and (
                    item.authorized_quantity == proposal.contract_quantity
                    and item.authorized_maximum_loss == proposal.maximum_loss
                ):
                    raise ContractValidationError("reduce decision must reduce quantity or maximum loss")

        command_by_id = {item.record_id: item for item in self.commands}
        for item in self.commands:
            authorization = authorization_by_id.get(item.authorization_id)
            if authorization is None or authorization.decision is RiskDecision.REJECT:
                raise ContractValidationError("execution command requires a non-rejected authorization")
            if item.authorization_fingerprint != contract_fingerprint(authorization) or item.proposal_id != authorization.proposal_id:
                raise ContractValidationError("execution command does not match its authorization")
            proposal = proposal_by_id[item.proposal_id]
            if item.created_at < authorization.created_at or item.created_at >= authorization.expires_at:
                raise ContractValidationError("execution command falls outside its authorization window")
            if item.action is ExecutionAction.SUBMIT:
                if item.quantity > authorization.authorized_quantity:
                    raise ContractValidationError("execution command exceeds authorized quantity")
                if item.legs != proposal.legs:
                    raise ContractValidationError("execution command legs do not match the proposal")
                if item.limit_price > proposal.limit_debit:
                    raise ContractValidationError("execution command exceeds proposal limit debit")

        event_by_id = {item.record_id: item for item in self.order_events}
        for item in self.order_events:
            if item.command_id not in command_by_id:
                raise ContractValidationError("order event references an unknown command")
            if item.created_at < command_by_id[item.command_id].created_at:
                raise ContractValidationError("order event predates its command")

        for item in self.assessments:
            if item.proposal_id not in proposal_by_id or item.authorization_id not in authorization_by_id:
                raise ContractValidationError("position assessment has an unknown proposal or authorization")
            if authorization_by_id[item.authorization_id].proposal_id != item.proposal_id:
                raise ContractValidationError("position assessment proposal and authorization do not match")
            if not set(item.order_event_ids).issubset(event_by_id):
                raise ContractValidationError("position assessment references an unknown order event")
            if item.created_at < max(event_by_id[event_id].created_at for event_id in item.order_event_ids):
                raise ContractValidationError("position assessment predates an order event")

    @property
    def replay_fingerprint(self) -> str:
        """Fingerprint independent of concurrent agent completion order."""

        payload = {
            "evidence": tuple(sorted(self.evidence, key=lambda item: item.record_id)),
            "bundle": self.bundle,
            "analyses": tuple(sorted(self.analyses, key=lambda item: item.record_id)),
            "proposals": tuple(sorted(self.proposals, key=lambda item: item.record_id)),
            "objections": tuple(sorted(self.objections, key=lambda item: item.record_id)),
            "authorizations": tuple(sorted(self.authorizations, key=lambda item: item.record_id)),
            "commands": tuple(sorted(self.commands, key=lambda item: item.record_id)),
            "order_events": tuple(sorted(self.order_events, key=lambda item: item.record_id)),
            "assessments": tuple(sorted(self.assessments, key=lambda item: item.record_id)),
        }
        return contract_fingerprint(payload)
