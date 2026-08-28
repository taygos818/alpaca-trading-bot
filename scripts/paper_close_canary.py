#!/usr/bin/env python3
"""Close explicitly named paper positions through the durable order lifecycle.

This is an operator canary, not a scheduled strategy. It refuses live credentials,
requires an acknowledgement, cancels broker-held exits for the named symbol, and
records the replacement market close in the paper order-intent database.
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import requests


ROOT = Path(__file__).resolve().parents[1]
SERVICE_DIR = ROOT / "services" / "strategy-engine"
sys.path.insert(0, str(SERVICE_DIR if SERVICE_DIR.exists() else ROOT))

from utils.execution import AlpacaOrderExecutor, OrderExecutionError  # noqa: E402
from utils.order_lifecycle import OrderLifecycleCoordinator, deterministic_intent_id  # noqa: E402
from utils.storage import TradeStore  # noqa: E402


ACK = "CLOSE_NAMED_PAPER_POSITIONS"
TERMINAL = {"filled", "canceled", "expired", "rejected"}


def _request(executor, method: str, path: str, **kwargs):
    response = getattr(executor.session, method)(
        f"{executor.settings.trading_api_url}{path}",
        headers=executor._headers(),
        timeout=executor.settings.timeout_seconds,
        **kwargs,
    )
    response.raise_for_status()
    return response.json() if response.content else None


def _cancel_symbol_orders(executor, symbol: str):
    orders = _request(
        executor,
        "get",
        "/v2/orders",
        params={"status": "open", "symbols": symbol, "nested": "true", "limit": 100},
    ) or []
    for order in orders:
        response = executor.session.delete(
            f"{executor.settings.trading_api_url}/v2/orders/{order['id']}",
            headers=executor._headers(),
            timeout=executor.settings.timeout_seconds,
        )
        if response.status_code not in {204, 404}:
            response.raise_for_status()
    deadline = time.time() + 30
    while time.time() < deadline:
        remaining = _request(
            executor,
            "get",
            "/v2/orders",
            params={"status": "open", "symbols": symbol, "limit": 100},
        ) or []
        if not remaining:
            return
        time.sleep(1)
    raise RuntimeError(f"Open orders for {symbol} did not cancel within 30 seconds")


def close_symbol(executor, store, symbol: str):
    max_chunk = max(1, int(os.getenv("MAX_ORDER_QUANTITY", "20")))
    _cancel_symbol_orders(executor, symbol)
    while True:
        position = executor.get_position(symbol)
        if position is None:
            print(f"{symbol}: flat", flush=True)
            return
        remaining_qty = float(position.get("qty") or 0)
        if remaining_qty <= 0 or not remaining_qty.is_integer():
            raise RuntimeError(f"{symbol}: controlled close requires a positive whole-share long position")
        current_price = float(position.get("current_price") or 0)
        if current_price <= 0:
            raise RuntimeError(f"{symbol}: broker position has no valid current price")
        qty = min(int(remaining_qty), max_chunk)
        intent = SimpleNamespace(
        strategy="paper_lifecycle_canary",
        symbol=symbol,
        action="sell",
        quantity=qty,
        order_value=qty * current_price,
        account_nav=0.0,
        estimated_risk_value=0.0,
        current_position_value=qty * current_price,
        stop_loss_price=None,
        take_profit_price=None,
        reference_price=current_price,
        signal_timestamp=datetime.now(timezone.utc),
        config_version="paper-close-canary-v1",
    )
        intent_id = deterministic_intent_id(intent)
        lifecycle = OrderLifecycleCoordinator(store, executor)
        result = lifecycle.execute(intent, intent_id)
        if result.execution_result is None:
            raise RuntimeError(f"{symbol}: close was not submitted ({result.status})")
        store.log_order_event(result.execution_result.to_record())

        deadline = time.time() + 120
        last_status = result.execution_result.status
        while time.time() < deadline:
            recovered = executor.get_order_by_client_id(intent_id)
            if recovered is None:
                raise RuntimeError(f"{symbol}: submitted close cannot be found at broker")
            status = str(recovered.get("status", "unknown")).lower()
            if status != last_status:
                store.update_order_intent(
                    intent_id,
                    status,
                    broker_order_id=str(recovered.get("id", "")),
                    response_payload=recovered,
                )
                reconciled = executor.result_from_reconciliation(intent, intent_id, recovered)
                store.log_order_event(reconciled.to_record())
                last_status = status
            if status in TERMINAL:
                if status != "filled":
                    raise RuntimeError(f"{symbol}: close ended with status={status}")
                print(f"{symbol}: {qty}-share close filled and reconciled", flush=True)
                break
            time.sleep(2)
        else:
            raise RuntimeError(f"{symbol}: close did not reach a terminal state within 120 seconds")


def wait_for_market_open(executor, timeout_seconds: float):
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            clock = _request(executor, "get", "/v2/clock")
            if clock and clock.get("is_open") is True:
                print("Alpaca paper market is verified open", flush=True)
                return
        except requests.RequestException as exc:
            print(
                f"Transient Alpaca clock failure ({type(exc).__name__}); retrying fail-closed",
                flush=True,
            )
        time.sleep(15)
    raise RuntimeError("Market did not open before the controlled canary timeout")


def validate_gtc_bracket(executor, store, symbol: str) -> dict:
    if executor.get_position(symbol) is not None:
        raise RuntimeError(f"{symbol}: GTC canary requires no existing paper position")
    response = executor.session.get(
        f"{executor.settings.data_api_url}/v2/stocks/{symbol}/trades/latest",
        headers=executor._headers(),
        params={"feed": os.getenv("ALPACA_DATA_FEED", "sip")},
        timeout=executor.settings.timeout_seconds,
    )
    response.raise_for_status()
    price = float((response.json().get("trade") or {}).get("p") or 0)
    if price <= 0:
        raise RuntimeError(f"{symbol}: latest SIP trade is unavailable")
    intent = SimpleNamespace(
        strategy="paper_gtc_canary",
        symbol=symbol,
        action="buy",
        quantity=1,
        order_value=price,
        account_nav=0.0,
        estimated_risk_value=price * 0.05,
        current_position_value=0.0,
        stop_loss_price=round(price * 0.95, 2),
        take_profit_price=round(price * 1.05, 2),
        reference_price=price,
        signal_timestamp=datetime.now(timezone.utc),
        config_version="paper-gtc-canary-v1",
    )
    intent_id = deterministic_intent_id(intent)
    result = OrderLifecycleCoordinator(store, executor).execute(intent, intent_id)
    if result.execution_result is None:
        raise RuntimeError(f"{symbol}: GTC bracket was not submitted ({result.status})")
    store.log_order_event(result.execution_result.to_record())
    if result.execution_result.requested_time_in_force != "gtc":
        raise RuntimeError(f"{symbol}: bracket did not request GTC")

    deadline = time.time() + 180
    while time.time() < deadline:
        recovered = executor.get_order_by_client_id(intent_id)
        if recovered is None:
            raise RuntimeError(f"{symbol}: GTC bracket cannot be found at broker")
        if str(recovered.get("status", "")).lower() == "filled":
            legs = recovered.get("legs") or []
            active = {"new", "accepted", "pending_new", "partially_filled", "held"}
            active_stop = any(
                str(leg.get("type", "")).lower() in {"stop", "stop_limit"}
                and str(leg.get("status", "")).lower() in active
                for leg in legs
            )
            if not active_stop:
                raise RuntimeError(f"{symbol}: filled bracket lacks an active stop leg")
            store.update_order_intent(
                intent_id,
                "filled_protected",
                broker_order_id=str(recovered.get("id", "")),
                response_payload=recovered,
            )
            evidence = {
                "symbol": symbol,
                "intent_id": intent_id,
                "broker_order_id": recovered.get("id"),
                "status": "filled_protected",
                "time_in_force": recovered.get("time_in_force"),
                "verified_at": datetime.now(timezone.utc).isoformat(),
            }
            print(json.dumps({"gtc_canary": evidence}), flush=True)
            return evidence
        time.sleep(2)
    raise RuntimeError(f"{symbol}: GTC bracket did not fill within 180 seconds")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("symbols", nargs="+", help="Exact paper symbols to close")
    parser.add_argument(
        "--wait-for-open",
        action="store_true",
        help="Wait up to PAPER_CLOSE_WAIT_HOURS for Alpaca's verified market open",
    )
    parser.add_argument("--gtc-canary-symbol", default="")
    args = parser.parse_args()
    if os.getenv("PAPER_CLOSE_CANARY_ACKNOWLEDGEMENT") != ACK:
        raise SystemExit(f"Set PAPER_CLOSE_CANARY_ACKNOWLEDGEMENT={ACK}")

    executor = AlpacaOrderExecutor.from_env()
    if not executor.settings.paper_trade or "paper-api.alpaca.markets" not in executor.settings.trading_api_url:
        raise SystemExit("Refusing to run outside Alpaca paper trading")
    if executor.settings.dry_run:
        raise SystemExit("Paper close canary requires ALPACA_ORDER_DRY_RUN=false")

    store = TradeStore.from_env()
    if args.wait_for_open:
        wait_for_market_open(
            executor,
            float(os.getenv("PAPER_CLOSE_WAIT_HOURS", "12")) * 3600,
        )
    for symbol in [value.strip().upper() for value in args.symbols]:
        try:
            close_symbol(executor, store, symbol)
        except (OrderExecutionError, RuntimeError) as exc:
            raise SystemExit(f"{symbol}: {exc}") from exc
    if args.gtc_canary_symbol:
        try:
            evidence = validate_gtc_bracket(executor, store, args.gtc_canary_symbol.upper())
            evidence_path = Path(os.getenv("PAPER_READINESS_EVIDENCE_PATH", "/app/paper-data/final-readiness.json"))
            evidence_path.parent.mkdir(parents=True, exist_ok=True)
            evidence_path.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
        except (OrderExecutionError, RuntimeError, requests.RequestException) as exc:
            raise SystemExit(f"GTC canary failed: {exc}") from exc


if __name__ == "__main__":
    main()
