"""Restart-safe entry reconciliation and deterministic option exits."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import threading

from agent_contracts import (
    Direction,
    LegSide,
    OptionLeg,
    OptionRight,
    OptionsProposal,
    ProposalDecision,
    canonical_json,
)
from defined_risk_options import (
    ExitCommandFactory,
    ExitDecision,
    ExitDecisionEngine,
    ExitPlanFactory,
    ExitPlanState,
    JsonlExitPlanStore,
    DefinedRiskOptionsConfig,
)

from .lifecycle import BoundedPaperLauncher, BrokerOrderSnapshot


def _utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _proposal(payload: dict) -> OptionsProposal:
    return OptionsProposal(
        record_id=payload["record_id"],
        trace_id=payload["trace_id"],
        evidence_bundle_id=payload["evidence_bundle_id"],
        analysis_ids=tuple(payload["analysis_ids"]),
        underlying=payload["underlying"],
        decision=ProposalDecision(payload["decision"]),
        direction=Direction(payload["direction"]),
        strategy_name=payload["strategy_name"],
        legs=tuple(
            OptionLeg(
                item["option_symbol"],
                LegSide(item["side"]),
                OptionRight(item["right"]),
                int(item["quantity"]),
                Decimal(item["strike"]),
                date.fromisoformat(item["expiration"]),
            )
            for item in payload["legs"]
        ),
        contract_quantity=int(payload["contract_quantity"]),
        limit_debit=Decimal(payload["limit_debit"]),
        maximum_loss=Decimal(payload["maximum_loss"]),
        rationale=payload["rationale"],
        created_at=_utc(payload["created_at"]),
        schema_version=payload["schema_version"],
    )


class _JsonlLatestStore:
    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def append(self, key: str, payload: dict) -> None:
        row = {"key": key, **payload}
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n")

    def latest(self) -> dict[str, dict]:
        if not self.path.exists():
            return {}
        rows = {}
        with self._lock, self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    row = json.loads(line)
                    rows[row["key"]] = row
        return rows


class PendingEntryStore(_JsonlLatestStore):
    def track(
        self,
        execution,
        evidence_ids: tuple[str, ...],
        snapshot: BrokerOrderSnapshot,
        plans: JsonlExitPlanStore,
    ) -> None:
        if execution.command.client_order_id in self.latest():
            return
        row = {
            "client_order_id": execution.command.client_order_id,
            "command_id": execution.command.record_id,
            "trace_id": execution.command.trace_id,
            "proposal": json.loads(canonical_json(execution.proposal)),
            "evidence_ids": list(evidence_ids),
            "status": snapshot.status,
            "filled_quantity": snapshot.filled_quantity,
            "planned_quantity": 0,
            "average_fill_price": format(snapshot.average_fill_price, "f"),
            "broker_timestamp": snapshot.broker_timestamp.isoformat(),
        }
        self._apply_new_fill(row, plans)
        self.append(execution.command.client_order_id, row)

    def reconcile(self, gateway, plans: JsonlExitPlanStore) -> tuple[dict, ...]:
        changes = []
        for client_id, row in self.latest().items():
            if row["status"] in {"filled", "canceled", "cancelled", "expired", "rejected"}:
                continue
            snapshot = gateway.order_by_client_id(client_id)
            from .lifecycle import normalize_broker_order

            current = normalize_broker_order(snapshot.payload)
            row.update(
                status=current.status,
                filled_quantity=current.filled_quantity,
                average_fill_price=format(current.average_fill_price, "f"),
                broker_timestamp=current.broker_timestamp.isoformat(),
            )
            self._apply_new_fill(row, plans)
            self.append(client_id, row)
            changes.append(row)
        return tuple(changes)

    @staticmethod
    def _apply_new_fill(row: dict, plans: JsonlExitPlanStore) -> None:
        delta = int(row["filled_quantity"]) - int(row.get("planned_quantity", 0))
        if delta <= 0:
            return
        proposal = _proposal(row["proposal"])
        plan = ExitPlanFactory(DefinedRiskOptionsConfig.from_env()).for_filled_proposal(
            proposal,
            filled_quantity=delta,
            entry_debit=Decimal(row["average_fill_price"]),
            opened_at=_utc(row["broker_timestamp"]),
            thesis_evidence_ids=tuple(row["evidence_ids"]),
        )
        plans.save(plan)
        row["planned_quantity"] = int(row["filled_quantity"])


class ExitOrderStore(_JsonlLatestStore):
    pass


class CompetitionPositionLifecycle:
    def __init__(
        self,
        launcher: BoundedPaperLauncher,
        plans: JsonlExitPlanStore,
        pending_entries: PendingEntryStore,
        exit_orders: ExitOrderStore,
    ) -> None:
        self.launcher = launcher
        self.plans = plans
        self.pending_entries = pending_entries
        self.exit_orders = exit_orders
        self.decisions = ExitDecisionEngine()
        self.commands = ExitCommandFactory()

    def reconcile_entries(self) -> tuple[dict, ...]:
        return self.pending_entries.reconcile(self.launcher.gateway, self.plans)

    def manage_exits(self, positions: list[dict], now: datetime) -> tuple[dict, ...]:
        events = list(self._reconcile_exit_orders())
        active_exit_plan_ids = {
            row["plan_id"]
            for row in self.exit_orders.latest().values()
            if row["status"] not in {"filled", "canceled", "cancelled", "expired", "rejected"}
        }
        for plan in self.plans.load_latest():
            if plan.state is ExitPlanState.CLOSED or plan.plan_id in active_exit_plan_ids:
                continue
            mark = self._mark(plan, positions)
            if mark is None:
                continue
            decision = self.decisions.assess(plan, current_mark=mark, now=now, thesis_valid=True)
            force_at = os.getenv("COMPETITION_FORCE_FLATTEN_AT", "").strip()
            if force_at and now >= _utc(force_at):
                decision = ExitDecision(
                    True,
                    tuple(sorted(set((*decision.reasons, "competition_close")))),
                    max(mark, Decimal("0.01")),
                )
            if not decision.should_exit:
                continue
            command = self.commands.for_due_plan(plan, decision, now)
            result = self.launcher.launch_exit(command)
            if result.mode in {"submitted", "duplicate"} and result.snapshot is not None:
                self.plans.mark_state(plan, ExitPlanState.EXIT_DUE)
                self.exit_orders.append(
                    command.client_order_id,
                    {
                        "plan_id": plan.plan_id,
                        "status": result.snapshot.status,
                        "filled_quantity": result.snapshot.filled_quantity,
                        "broker_timestamp": result.snapshot.broker_timestamp.isoformat(),
                    },
                )
                if result.snapshot.status == "filled":
                    self.plans.mark_state(plan, ExitPlanState.CLOSED)
                events.append({"plan_id": plan.plan_id, "status": result.snapshot.status, "reasons": decision.reasons})
        return tuple(events)

    def _reconcile_exit_orders(self) -> tuple[dict, ...]:
        events = []
        plans = {plan.plan_id: plan for plan in self.plans.load_latest()}
        for client_id, row in self.exit_orders.latest().items():
            if row["status"] in {"filled", "canceled", "cancelled", "expired", "rejected"}:
                continue
            from .lifecycle import normalize_broker_order

            snapshot = normalize_broker_order(self.launcher.gateway.order_by_client_id(client_id).payload)
            row.update(
                status=snapshot.status,
                filled_quantity=snapshot.filled_quantity,
                broker_timestamp=snapshot.broker_timestamp.isoformat(),
            )
            self.exit_orders.append(client_id, row)
            plan = plans.get(row["plan_id"])
            if plan is not None and snapshot.status == "filled":
                self.plans.mark_state(plan, ExitPlanState.CLOSED)
            elif plan is not None and snapshot.status in {"canceled", "cancelled", "expired", "rejected"}:
                remaining = plan.quantity - snapshot.filled_quantity
                if remaining > 0:
                    self.plans.save(replace(plan, quantity=remaining, state=ExitPlanState.ACTIVE))
                else:
                    self.plans.mark_state(plan, ExitPlanState.CLOSED)
            events.append({"plan_id": row["plan_id"], "status": snapshot.status})
        return tuple(events)

    @staticmethod
    def _mark(plan, positions: list[dict]) -> Decimal | None:
        prices = {str(item.get("symbol")): Decimal(str(item.get("current_price") or 0)) for item in positions}
        if any(leg.option_symbol not in prices for leg in plan.legs):
            return None
        return max(
            Decimal("0"),
            sum((prices[leg.option_symbol] if leg.side is LegSide.BUY else -prices[leg.option_symbol]) for leg in plan.legs),
        )
