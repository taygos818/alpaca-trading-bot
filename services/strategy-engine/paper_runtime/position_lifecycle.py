"""Restart-safe entry reconciliation and deterministic option exits."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
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
            "submitted_at": snapshot.broker_timestamp.isoformat(),
        }
        self._apply_new_fill(row, plans)
        self.append(execution.command.client_order_id, row)

    def reconcile(self, gateway, plans: JsonlExitPlanStore) -> tuple[dict, ...]:
        changes = []
        now = datetime.now(timezone.utc)
        ttl_seconds = max(30, int(os.getenv("PAPER_ENTRY_ORDER_TTL_SECONDS", "90")))
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
            submitted_at = _utc(row.get("submitted_at") or row["broker_timestamp"])
            if (
                current.status in {"new", "accepted", "pending_new", "partially_filled"}
                and now - submitted_at >= timedelta(seconds=ttl_seconds)
                and not row.get("cancel_requested_at")
            ):
                gateway.cancel_order(current.broker_order_id)
                row["status"] = "pending_cancel"
                row["cancel_reason"] = "entry_signal_ttl"
                row["cancel_requested_at"] = now.isoformat()
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

    def manage_exits(
        self,
        positions: list[dict],
        now: datetime,
        *,
        quote_provider=None,
        thesis_provider=None,
    ) -> tuple[dict, ...]:
        events = list(self._reconcile_exit_orders(now))
        quote_cache = {}
        active_exit_plan_ids = {
            row["plan_id"]
            for row in self.exit_orders.latest().values()
            if row["status"] not in {"filled", "canceled", "cancelled", "expired", "rejected"}
        }
        for plan in self.plans.load_latest():
            if plan.state is ExitPlanState.CLOSED or plan.plan_id in active_exit_plan_ids:
                continue
            if any(leg.option_symbol not in {str(item.get("symbol")) for item in positions} for leg in plan.legs):
                continue
            try:
                if quote_provider is not None and plan.underlying not in quote_cache:
                    quote_cache[plan.underlying] = quote_provider(plan.underlying)
                snapshots = quote_cache.get(plan.underlying) if quote_provider is not None else None
            except RuntimeError:
                continue
            mark = self._executable_credit(plan, snapshots, now) if snapshots is not None else self._mark(plan, positions)
            if mark is None:
                continue
            try:
                thesis_valid = thesis_provider(plan, now) if thesis_provider is not None else True
            except RuntimeError:
                thesis_valid = True
            decision = self.decisions.assess(plan, current_mark=mark, now=now, thesis_valid=thesis_valid)
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
                        "broker_order_id": result.snapshot.broker_order_id,
                        "submitted_at": now.isoformat(),
                        "limit_credit": format(command.limit_credit, "f"),
                        "reasons": list(decision.reasons),
                    },
                )
                if result.snapshot.status == "filled":
                    self.plans.mark_state(plan, ExitPlanState.CLOSED)
                events.append({"plan_id": plan.plan_id, "status": result.snapshot.status, "reasons": decision.reasons})
        return tuple(events)

    def _reconcile_exit_orders(self, now: datetime) -> tuple[dict, ...]:
        events = []
        reprice_seconds = max(15, int(os.getenv("PAPER_EXIT_REPRICE_SECONDS", "30")))
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
                broker_order_id=snapshot.broker_order_id,
            )
            submitted_at = _utc(row.get("submitted_at") or row["broker_timestamp"])
            if (
                snapshot.status in {"new", "accepted", "pending_new", "partially_filled"}
                and now - submitted_at >= timedelta(seconds=reprice_seconds)
                and not row.get("cancel_requested_at")
            ):
                self.launcher.gateway.cancel_order(snapshot.broker_order_id)
                row["status"] = "pending_cancel"
                row["cancel_reason"] = "exit_reprice_ttl"
                row["cancel_requested_at"] = now.isoformat()
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

    @staticmethod
    def _executable_credit(plan, snapshots: dict[str, dict], now: datetime) -> Decimal | None:
        if not isinstance(snapshots, dict):
            return None
        credit = Decimal("0")
        maximum_age = timedelta(seconds=max(5, int(os.getenv("OPTIONS_MAX_QUOTE_AGE_SECONDS", "30"))))
        for leg in plan.legs:
            snapshot = snapshots.get(leg.option_symbol)
            if not isinstance(snapshot, dict):
                return None
            quote = snapshot.get("latestQuote") or snapshot.get("latest_quote") or {}
            try:
                bid = Decimal(str(quote.get("bp") if quote.get("bp") is not None else quote["bid_price"]))
                ask = Decimal(str(quote.get("ap") if quote.get("ap") is not None else quote["ask_price"]))
                observed_at = _utc(str(quote.get("t") or quote.get("timestamp")))
            except (KeyError, TypeError, ValueError):
                return None
            if bid <= 0 or ask < bid or not timedelta(0) <= now - observed_at <= maximum_age:
                return None
            credit += bid if leg.side is LegSide.BUY else -ask
        return max(Decimal("0"), credit)
