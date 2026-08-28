"""Persisted strategy-owned exit plans and deterministic exit assessment."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN
import hashlib
import json
from pathlib import Path
import threading

from agent_contracts import OptionsProposal, canonical_json

from .models import DefinedRiskOptionsConfig, ExitDecision, ExitPlan, ExitPlanState


CENT = Decimal("0.01")


class ExitPlanFactory:
    def __init__(self, config: DefinedRiskOptionsConfig | None = None) -> None:
        self.config = config or DefinedRiskOptionsConfig()

    def for_filled_proposal(
        self,
        proposal: OptionsProposal,
        *,
        filled_quantity: int,
        entry_debit: Decimal,
        opened_at: datetime,
        thesis_evidence_ids: tuple[str, ...],
    ) -> ExitPlan:
        if filled_quantity <= 0 or filled_quantity > proposal.contract_quantity:
            raise ValueError("filled quantity is outside proposal bounds")
        if not proposal.legs or len({leg.expiration for leg in proposal.legs}) != 1:
            raise ValueError("exit plan requires one shared option expiration")
        maximum_loss = entry_debit * Decimal("100") * filled_quantity
        identity = f"{proposal.record_id}:{opened_at.isoformat()}:{filled_quantity}:{entry_debit}"
        return ExitPlan(
            plan_id=f"exit-plan.{hashlib.sha256(identity.encode()).hexdigest()[:24]}",
            proposal_id=proposal.record_id,
            underlying=proposal.underlying,
            legs=proposal.legs,
            quantity=filled_quantity,
            entry_debit=entry_debit,
            maximum_loss=maximum_loss,
            opened_at=opened_at,
            expiration=proposal.legs[0].expiration,
            profit_target_pct=self.config.profit_target_pct,
            loss_limit_pct=self.config.loss_limit_pct,
            max_holding_days=self.config.max_holding_days,
            exit_before_expiration_days=self.config.exit_before_expiration_days,
            thesis_evidence_ids=thesis_evidence_ids,
        )


class ExitDecisionEngine:
    def assess(
        self,
        plan: ExitPlan,
        *,
        current_mark: Decimal,
        now: datetime,
        thesis_valid: bool,
    ) -> ExitDecision:
        if plan.state is ExitPlanState.CLOSED:
            return ExitDecision(False, (), Decimal("0"))
        if current_mark < 0:
            raise ValueError("current option mark cannot be negative")
        if now.tzinfo is None or now.utcoffset() != timezone.utc.utcoffset(now):
            raise ValueError("exit assessment time must be UTC")
        reasons = []
        if current_mark >= plan.entry_debit * (Decimal("1") + plan.profit_target_pct):
            reasons.append("profit_target")
        if current_mark <= plan.entry_debit * (Decimal("1") - plan.loss_limit_pct):
            reasons.append("loss_limit")
        if now >= plan.opened_at + timedelta(days=plan.max_holding_days):
            reasons.append("holding_time")
        if (plan.expiration - now.date()).days <= plan.exit_before_expiration_days:
            reasons.append("expiration_control")
        if not thesis_valid:
            reasons.append("thesis_invalidation")
        limit_credit = current_mark.quantize(CENT, rounding=ROUND_DOWN) if reasons else Decimal("0")
        return ExitDecision(bool(reasons), tuple(reasons), limit_credit)


class JsonlExitPlanStore:
    """Append-only plan journal; latest record per plan ID is authoritative on reload."""

    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def save(self, plan: ExitPlan) -> None:
        payload = canonical_json(plan)
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(payload + "\n")

    def mark_state(self, plan: ExitPlan, state: ExitPlanState) -> ExitPlan:
        updated = replace(plan, state=state)
        self.save(updated)
        return updated

    def load_latest(self) -> tuple[ExitPlan, ...]:
        if not self.path.exists():
            return ()
        latest = {}
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                plan = _decode_plan(json.loads(line))
                latest[plan.plan_id] = plan
        return tuple(sorted(latest.values(), key=lambda plan: plan.plan_id))


def _decode_plan(payload: dict) -> ExitPlan:
    from agent_contracts import LegSide, OptionLeg, OptionRight

    legs = tuple(
        OptionLeg(
            option_symbol=item["option_symbol"],
            side=LegSide(item["side"]),
            right=OptionRight(item["right"]),
            quantity=int(item["quantity"]),
            strike=Decimal(item["strike"]),
            expiration=date.fromisoformat(item["expiration"]),
        )
        for item in payload["legs"]
    )
    opened_at = datetime.fromisoformat(payload["opened_at"].replace("Z", "+00:00")).astimezone(timezone.utc)
    return ExitPlan(
        plan_id=payload["plan_id"],
        proposal_id=payload["proposal_id"],
        underlying=payload["underlying"],
        legs=legs,
        quantity=int(payload["quantity"]),
        entry_debit=Decimal(payload["entry_debit"]),
        maximum_loss=Decimal(payload["maximum_loss"]),
        opened_at=opened_at,
        expiration=date.fromisoformat(payload["expiration"]),
        profit_target_pct=Decimal(payload["profit_target_pct"]),
        loss_limit_pct=Decimal(payload["loss_limit_pct"]),
        max_holding_days=int(payload["max_holding_days"]),
        exit_before_expiration_days=int(payload["exit_before_expiration_days"]),
        thesis_evidence_ids=tuple(payload["thesis_evidence_ids"]),
        state=ExitPlanState(payload["state"]),
    )
