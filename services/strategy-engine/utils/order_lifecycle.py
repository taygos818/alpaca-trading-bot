import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone

from utils.execution import ExecutionResult, OrderExecutionError, OrderPreflightError


class OrderReconciliationRequired(RuntimeError):
    pass


def _signal_timestamp(intent) -> datetime:
    raw = getattr(intent, "signal_timestamp", None)
    if isinstance(raw, datetime):
        value = raw
    elif raw:
        value = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    else:
        value = datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def deterministic_intent_id(intent, window_seconds: int | None = None) -> str:
    window = window_seconds or int(os.getenv("SIGNAL_IDEMPOTENCY_WINDOW_SECONDS", "60"))
    timestamp = _signal_timestamp(intent)
    bucket = int(timestamp.timestamp()) // window
    canonical = {
        "strategy": intent.strategy,
        "symbol": intent.symbol.upper(),
        "action": intent.action,
        "quantity": float(intent.quantity),
        "window": bucket,
        "config_version": str(getattr(intent, "config_version", "unversioned")),
    }
    digest = hashlib.sha256(json.dumps(canonical, sort_keys=True).encode()).hexdigest()[:24]
    return f"{intent.strategy}-{digest}"[:48]


@dataclass(frozen=True)
class LifecycleResult:
    status: str
    execution_result: ExecutionResult | None = None
    reason: str = ""


class OrderLifecycleCoordinator:
    def __init__(self, store, executor):
        self.store = store
        self.executor = executor

    def reserve(self, intent, intent_id: str) -> bool:
        now = datetime.now(timezone.utc).isoformat()
        signal_timestamp = _signal_timestamp(intent).isoformat()
        return self.store.reserve_order_intent(
            {
                "intent_id": intent_id,
                "idempotency_key": intent_id,
                "created_at": now,
                "updated_at": now,
                "strategy": intent.strategy,
                "symbol": intent.symbol,
                "action": intent.action,
                "quantity": float(intent.quantity),
                "order_value": float(intent.order_value),
                "signal_timestamp": signal_timestamp,
                "config_version": str(getattr(intent, "config_version", "unversioned")),
                "status": "reserved",
            }
        )

    def execute(self, intent, intent_id: str) -> LifecycleResult:
        if not self.reserve(intent, intent_id):
            return LifecycleResult(status="duplicate_blocked", reason="duplicate signal intent already reserved")

        self.store.update_order_intent(intent_id, "submitting")
        try:
            result = self.executor.execute(intent, intent_id=intent_id)
        except OrderPreflightError as exc:
            self.store.update_order_intent(
                intent_id, "rejected_preflight", error_message=str(exc),
            )
            raise
        except OrderExecutionError as exc:
            try:
                recovered = self.executor.get_order_by_client_id(intent_id)
            except OrderExecutionError as reconciliation_exc:
                self.store.update_order_intent(
                    intent_id,
                    "unknown_requires_reconciliation",
                    error_message=f"submission={type(exc).__name__}; reconciliation={type(reconciliation_exc).__name__}",
                )
                raise OrderReconciliationRequired(f"Order outcome is ambiguous for {intent_id}") from reconciliation_exc
            if recovered:
                broker_order_id = str(recovered.get("id", ""))
                recovered_result = self.executor.result_from_reconciliation(intent, intent_id, recovered)
                recovered_status = str(recovered.get("status", "submitted")).lower()
                self.store.update_order_intent(
                    intent_id,
                    recovered_status,
                    broker_order_id=broker_order_id,
                    request_payload=recovered_result.request_payload,
                    response_payload=recovered,
                    error_message="recovered after ambiguous submission failure",
                )
                return LifecycleResult(
                    status="recovered_submitted",
                    execution_result=recovered_result,
                )
            self.store.update_order_intent(
                intent_id,
                "unknown_requires_reconciliation",
                error_message=type(exc).__name__,
            )
            raise OrderReconciliationRequired(f"Order outcome is ambiguous for {intent_id}") from exc

        self.store.update_order_intent(
            intent_id,
            result.status,
            broker_order_id=result.broker_order_id,
            request_payload=result.request_payload,
            response_payload=result.response_payload,
            error_message=result.error_message,
        )
        return LifecycleResult(status=result.status, execution_result=result)

    @staticmethod
    def _reconciled_status(record: dict, recovered: dict) -> tuple[str, str]:
        status = str(recovered.get("status", "submitted")).lower()
        request_payload = record.get("request_payload") or {}
        if status == "filled" and request_payload.get("order_class") == "bracket":
            legs = recovered.get("legs") or []
            if any(str(leg.get("status", "")).lower() == "filled" for leg in legs):
                return "closed", ""
            active_statuses = {"new", "accepted", "pending_new", "partially_filled", "held"}
            active_legs = [leg for leg in legs if str(leg.get("status", "")).lower() in active_statuses]
            stop_is_active = any(
                str(leg.get("type", "")).lower() in {"stop", "stop_limit"}
                for leg in active_legs
            )
            if not stop_is_active:
                return "unprotected_filled", "filled bracket has no active broker-side stop leg"
            return "filled_protected", ""
        return status, ""

    def reconcile_once(self):
        unresolved = []
        updates = []
        for record in self.store.list_unfinished_order_intents():
            intent_id = record["intent_id"]
            try:
                recovered = self.executor.get_order_by_client_id(intent_id)
            except OrderExecutionError:
                unresolved.append(intent_id)
                continue
            if recovered:
                status, error_message = self._reconciled_status(record, recovered)
                if status == "unprotected_filled":
                    position = self.executor.get_position(record["symbol"])
                    if position is None:
                        status = "closed_reconciled"
                        error_message = ""
                self.store.update_order_intent(
                    intent_id,
                    status,
                    broker_order_id=str(recovered.get("id", "")),
                    response_payload=recovered,
                    error_message=error_message,
                )
                updates.append((record, status, recovered, error_message))
                if status == "unprotected_filled":
                    unresolved.append(intent_id)
            else:
                unresolved.append(intent_id)
        if unresolved:
            raise OrderReconciliationRequired(
                "Unresolved or unprotected order intents block trading: " + ", ".join(unresolved)
            )
        return updates

    def reconcile_startup(self):
        return self.reconcile_once()
