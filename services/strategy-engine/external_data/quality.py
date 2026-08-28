"""Central freshness, provenance, and cross-source disagreement checks."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from itertools import combinations

from agent_contracts import ContractValidationError, EvidenceItem


@dataclass(frozen=True, slots=True)
class DataQualityPolicy:
    max_receipt_age_seconds: int = 900
    max_relative_disagreement: Decimal = Decimal("0.02")

    def __post_init__(self) -> None:
        if self.max_receipt_age_seconds <= 0:
            raise ValueError("max receipt age must be positive")
        if self.max_relative_disagreement < 0:
            raise ValueError("disagreement tolerance cannot be negative")


@dataclass(frozen=True, slots=True)
class EvidenceDisagreement:
    instrument: str
    value_name: str
    event_date: str
    left_provider: str
    left_value: Decimal
    right_provider: str
    right_value: Decimal
    relative_difference: Decimal


@dataclass(frozen=True, slots=True)
class DataQualityReport:
    accepted: tuple[EvidenceItem, ...]
    rejected_ids: tuple[str, ...]
    disagreements: tuple[EvidenceDisagreement, ...]
    veto: bool


class DataQualityEngine:
    def __init__(self, policy: DataQualityPolicy | None = None) -> None:
        self.policy = policy or DataQualityPolicy()

    def evaluate(self, evidence: tuple[EvidenceItem, ...], now: datetime) -> DataQualityReport:
        if not evidence:
            raise ContractValidationError("data quality requires evidence")
        trace_ids = {item.trace_id for item in evidence}
        if len(trace_ids) != 1:
            raise ContractValidationError("data quality cannot fuse multiple trace IDs")
        rejected = []
        accepted = []
        for item in evidence:
            receipt_age = now - item.received_at
            receipt_is_current = timedelta(0) <= receipt_age <= timedelta(seconds=self.policy.max_receipt_age_seconds)
            if not item.is_fresh or not receipt_is_current:
                rejected.append(item.record_id)
            else:
                accepted.append(item)

        groups: dict[tuple[str, str, str], list[EvidenceItem]] = {}
        for item in accepted:
            if item.numeric_value is None:
                continue
            key = (item.instrument, item.value_name, item.event_time.date().isoformat())
            groups.setdefault(key, []).append(item)

        disagreements = []
        for (instrument, value_name, event_date), items in sorted(groups.items()):
            for left, right in combinations(sorted(items, key=lambda item: item.provider), 2):
                if left.provider == right.provider:
                    continue
                denominator = max(abs(left.numeric_value), abs(right.numeric_value), Decimal("0.00000001"))
                relative = abs(left.numeric_value - right.numeric_value) / denominator
                if relative > self.policy.max_relative_disagreement:
                    disagreements.append(
                        EvidenceDisagreement(
                            instrument=instrument,
                            value_name=value_name,
                            event_date=event_date,
                            left_provider=left.provider,
                            left_value=left.numeric_value,
                            right_provider=right.provider,
                            right_value=right.numeric_value,
                            relative_difference=relative,
                        )
                    )
        return DataQualityReport(
            accepted=tuple(sorted(accepted, key=lambda item: item.record_id)),
            rejected_ids=tuple(sorted(rejected)),
            disagreements=tuple(disagreements),
            veto=bool(rejected or disagreements),
        )
