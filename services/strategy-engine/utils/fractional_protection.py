import time
from datetime import datetime
from zoneinfo import ZoneInfo

from utils.execution import OrderExecutionError


class FractionalProtectionError(RuntimeError):
    pass


def is_fractional(quantity: float) -> bool:
    value = abs(float(quantity))
    return value > 0 and not value.is_integer()


class FractionalProtectionCoordinator:
    """Durable DAY-stop lifecycle for fractional Alpaca swing positions."""

    def __init__(self, store, executor):
        self.store = store
        self.executor = executor

    def reserve_entry(self, intent, intent_id: str):
        self.store.upsert_fractional_protection({
            "symbol": intent.symbol.upper(),
            "entry_intent_id": intent_id,
            "quantity": float(intent.quantity),
            "stop_price": float(intent.stop_loss_price),
            "take_profit_price": float(intent.take_profit_price),
            "entry_order_id": "",
            "stop_order_id": "",
            "status": "entry_pending",
            "session_date": None,
            "last_error": "",
        })

    def protect_entry_fill(self, intent, intent_id: str, entry_order_id: str, timeout_seconds: float = 60):
        deadline = time.time() + timeout_seconds
        order = None
        while time.time() < deadline:
            order = self.executor.get_order(entry_order_id)
            status = str((order or {}).get("status", "")).lower()
            if status == "filled":
                break
            if status in {"canceled", "expired", "rejected"}:
                self.store.update_fractional_protection(intent.symbol, status="entry_failed", last_error=status)
                raise FractionalProtectionError(f"Fractional entry ended with status={status}")
            time.sleep(1)
        else:
            self.store.update_fractional_protection(
                intent.symbol, entry_order_id=entry_order_id,
                status="entry_fill_unresolved", last_error="entry fill timeout",
            )
            raise FractionalProtectionError("Fractional entry fill was not confirmed before timeout")

        filled_qty = float(order.get("filled_qty") or intent.quantity)
        self.store.update_fractional_protection(
            intent.symbol, quantity=filled_qty, entry_order_id=entry_order_id,
            status="filled_unprotected", last_error="",
        )
        return self._submit_stop(intent.symbol, filled_qty, float(intent.stop_loss_price), intent_id)

    def _submit_stop(self, symbol: str, quantity: float, stop_price: float, entry_intent_id: str):
        session_date = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
        renewal_nonce = str(time.time_ns())[-8:]
        client_id = (
            f"frac-stop-{symbol.lower()}-{session_date.replace('-', '')}-"
            f"{entry_intent_id[-8:]}-{renewal_nonce}"
        )
        try:
            response = self.executor.submit_fractional_stop(
                symbol, quantity, stop_price, client_id,
            )
        except OrderExecutionError as exc:
            self.store.update_fractional_protection(
                symbol, status="unprotected", last_error=str(exc),
            )
            raise FractionalProtectionError(f"Could not establish fractional stop for {symbol}") from exc
        order_id = str(response.get("id", ""))
        if not order_id:
            raise FractionalProtectionError(f"Fractional stop for {symbol} returned no order id")
        self.store.update_fractional_protection(
            symbol, stop_order_id=order_id, status="protected",
            session_date=session_date, last_error="",
        )
        return response

    def reconcile(self, broker_state) -> list[str]:
        records = {row["symbol"].upper(): row for row in self.store.list_fractional_protections()}
        fractional_positions = {
            symbol: position for symbol, position in broker_state.positions.items()
            if is_fractional(position.qty)
        }
        failures = []
        for symbol in fractional_positions:
            if symbol not in records:
                failures.append(f"{symbol} fractional position has no durable protection plan")
        for symbol, record in records.items():
            if symbol not in broker_state.positions:
                self.store.update_fractional_protection(symbol, status="closed", stop_order_id="")

        protected_positions = {
            symbol: broker_state.positions[symbol]
            for symbol in records if symbol in broker_state.positions
        }
        for symbol, position in protected_positions.items():
            record = records[symbol]
            open_stops = [
                order for order in self.executor.list_open_orders(symbol)
                if str(order.get("side", "")).lower() == "sell"
                and str(order.get("type", "")).lower() in {"stop", "stop_limit"}
            ]
            if open_stops:
                stop = open_stops[0]
                protected_qty = float(stop.get("qty") or 0)
                if abs(protected_qty - abs(position.qty)) > 0.0001:
                    failures.append(f"{symbol} fractional stop quantity does not match the position")
                    continue
                self.store.update_fractional_protection(
                    symbol, quantity=abs(position.qty), stop_order_id=str(stop.get("id", "")),
                    status="protected", session_date=datetime.now(ZoneInfo("America/New_York")).date().isoformat(),
                    last_error="",
                )
                continue
            if self._stop_submission_window():
                try:
                    self._submit_stop(
                        symbol, abs(position.qty), float(record["stop_price"]),
                        str(record["entry_intent_id"]),
                    )
                except FractionalProtectionError as exc:
                    failures.append(str(exc))
            else:
                self.store.update_fractional_protection(symbol, status="awaiting_renewal", stop_order_id="")
        return failures

    def cancel_stop_before_close(self, symbol: str):
        records = {row["symbol"].upper(): row for row in self.store.list_fractional_protections()}
        record = records.get(symbol.upper())
        if not record:
            raise FractionalProtectionError(f"No fractional protection record exists for {symbol}")
        for order in self.executor.list_open_orders(symbol):
            if str(order.get("side", "")).lower() == "sell" and str(order.get("type", "")).lower() in {"stop", "stop_limit"}:
                self.executor.cancel_order(str(order["id"]))
        deadline = time.time() + 15
        while time.time() < deadline:
            remaining = [
                order for order in self.executor.list_open_orders(symbol)
                if str(order.get("side", "")).lower() == "sell"
                and str(order.get("type", "")).lower() in {"stop", "stop_limit"}
            ]
            if not remaining:
                break
            time.sleep(0.5)
        else:
            raise FractionalProtectionError(f"Fractional stop for {symbol} did not cancel before close")
        self.store.update_fractional_protection(symbol, status="closing", stop_order_id="")

    @staticmethod
    def _stop_submission_window(now: datetime | None = None) -> bool:
        current = now or datetime.now(ZoneInfo("America/New_York"))
        current = current.astimezone(ZoneInfo("America/New_York"))
        minutes = current.hour * 60 + current.minute
        return current.weekday() < 5 and (9 * 60 + 25) <= minutes < 16 * 60
