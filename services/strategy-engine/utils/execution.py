import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import requests
import pyotp
import robin_stocks.robinhood as r
from utils.alpaca_credentials import alpaca_credentials


if TYPE_CHECKING:
    from strategies.base import TradeIntent


SUPPORTED_STOCK_ACTIONS = {"buy", "sell", "sell_short", "buy_to_cover"}
SUPPORTED_OPTIONS_ACTIONS = {"sell_to_open", "sell_to_close", "buy_to_open", "buy_to_close"}


class OrderExecutionError(RuntimeError):
    pass


class OrderPreflightError(OrderExecutionError):
    """Definitive rejection raised before any broker submission is attempted."""


@dataclass(frozen=True)
class ExecutionSettings:
    api_key: str
    secret_key: str
    trading_api_url: str
    paper_trade: bool
    dry_run: bool
    timeout_seconds: float
    data_api_url: str = "https://data.alpaca.markets"
    max_quote_deviation_pct: float = 0.0
    max_quote_age_seconds: float = 0.0

    @classmethod
    def from_env(cls):
        paper_trade = os.getenv("ALPACA_PAPER_TRADE", "true").strip().lower() == "true"
        api_key, secret_key = alpaca_credentials(paper_trade)
        default_url = "https://paper-api.alpaca.markets"
        trading_api_url = os.getenv("ALPACA_TRADING_API_URL", default_url).rstrip("/")
        if trading_api_url != default_url:
            raise ValueError("alpaca-trading-bot accepts only https://paper-api.alpaca.markets")
        return cls(
            api_key=api_key,
            secret_key=secret_key,
            trading_api_url=trading_api_url,
            paper_trade=paper_trade,
            dry_run=os.getenv("ALPACA_ORDER_DRY_RUN", "true").strip().lower() == "true",
            timeout_seconds=float(os.getenv("ALPACA_ORDER_TIMEOUT_SECONDS", "10")),
            data_api_url=os.getenv("ALPACA_DATA_API_URL", "https://data.alpaca.markets").rstrip("/"),
            max_quote_deviation_pct=float(os.getenv("MAX_QUOTE_DEVIATION_PCT", "0.01")),
            max_quote_age_seconds=float(os.getenv("MAX_QUOTE_AGE_SECONDS", "15")),
        )


@dataclass(frozen=True)
class ExecutionResult:
    intent_id: str
    timestamp: str
    strategy: str
    symbol: str
    action: str
    quantity: float
    requested_order_type: str
    requested_time_in_force: str
    dry_run: bool
    status: str
    broker: str
    broker_order_id: str = ""
    request_payload: dict | None = None
    response_payload: dict | None = None
    error_message: str = ""

    def to_record(self) -> dict:
        return {
            "intent_id": self.intent_id,
            "timestamp": self.timestamp,
            "strategy": self.strategy,
            "symbol": self.symbol,
            "action": self.action,
            "quantity": self.quantity,
            "requested_order_type": self.requested_order_type,
            "requested_time_in_force": self.requested_time_in_force,
            "dry_run": self.dry_run,
            "status": self.status,
            "broker": self.broker,
            "broker_order_id": self.broker_order_id,
            "request_payload": self.request_payload,
            "response_payload": self.response_payload,
            "error_message": self.error_message,
        }


class AlpacaOrderExecutor:
    def __init__(self, settings: ExecutionSettings, session: requests.Session | None = None):
        self.settings = settings
        self.session = session or requests.Session()

    @classmethod
    def from_env(cls):
        return cls(ExecutionSettings.from_env())

    def execute(self, intent: "TradeIntent", intent_id: str | None = None) -> ExecutionResult:
        resolved_intent_id = intent_id or str(uuid.uuid4())
        timestamp = datetime.now(timezone.utc).isoformat()

        if intent.action not in SUPPORTED_STOCK_ACTIONS and intent.action not in SUPPORTED_OPTIONS_ACTIONS:
            return ExecutionResult(
                intent_id=resolved_intent_id,
                timestamp=timestamp,
                strategy=intent.strategy,
                symbol=intent.symbol,
                action=intent.action,
                quantity=intent.quantity,
                requested_order_type="market",
                requested_time_in_force="day",
                dry_run=self.settings.dry_run,
                status="unsupported_action",
                broker="alpaca",
                request_payload=None,
                error_message=f"Action {intent.action} is not yet mapped to an Alpaca order",
            )

        payload = self._build_order_payload(intent, resolved_intent_id)

        if self.settings.dry_run:
            return ExecutionResult(
                intent_id=resolved_intent_id,
                timestamp=timestamp,
                strategy=intent.strategy,
                symbol=intent.symbol,
                action=intent.action,
                quantity=intent.quantity,
                requested_order_type=str(payload["type"]),
                requested_time_in_force=str(payload["time_in_force"]),
                dry_run=True,
                status="dry_run",
                broker="alpaca",
                request_payload=payload,
            )

        if intent.action == "sell_short":
            self._validate_short_eligibility(intent.symbol)
        self._validate_latest_price(intent)
        response_payload = self._submit_order(payload)
        return ExecutionResult(
            intent_id=resolved_intent_id,
            timestamp=timestamp,
            strategy=intent.strategy,
            symbol=intent.symbol,
            action=intent.action,
            quantity=intent.quantity,
            requested_order_type=str(payload["type"]),
            requested_time_in_force=str(payload["time_in_force"]),
            dry_run=False,
            status="submitted",
            broker="alpaca",
            broker_order_id=str(response_payload.get("id", "")),
            request_payload=payload,
            response_payload=response_payload,
        )

    def _build_order_payload(self, intent: "TradeIntent", intent_id: str) -> dict:
        if intent.quantity <= 0 and (getattr(intent, "order_value", 0.0) <= 0):
            raise OrderExecutionError("Trade quantity or notional order_value must be positive")

        if intent.action in SUPPORTED_STOCK_ACTIONS:
            side = "buy" if intent.action in {"buy", "buy_to_cover"} else "sell"
            payload = {
                "symbol": intent.symbol,
                "side": side,
                "type": "market",
                "time_in_force": "day",
                "client_order_id": self._client_order_id(intent, intent_id),
            }

            if intent.quantity > 0:
                payload["qty"] = str(intent.quantity)
            elif getattr(intent, "order_value", 0.0) > 0:
                payload["notional"] = f"{intent.order_value:.2f}"

            if intent.action in {"buy", "sell_short"} and getattr(intent, "stop_loss_price", None) is not None and getattr(intent, "take_profit_price", None) is not None:
                if float(intent.quantity).is_integer():
                    payload["order_class"] = "bracket"
                    # Protective exits must survive the closing bell. Alpaca equity
                    # brackets support GTC; DAY caused overnight positions to lose
                    # their broker-side protection.
                    payload["time_in_force"] = "gtc"
                    payload["take_profit"] = {
                        "limit_price": f"{intent.take_profit_price:.2f}"
                    }
                    payload["stop_loss"] = {
                        "stop_price": f"{intent.stop_loss_price:.2f}"
                    }
            return payload
        elif intent.action in SUPPORTED_OPTIONS_ACTIONS:
            side = "buy" if intent.action in {"buy_to_open", "buy_to_close"} else "sell"
            payload = {
                "symbol": intent.symbol,
                "side": side,
                "type": "market",
                "time_in_force": "day",
                "position_intent": intent.action,
                "client_order_id": self._client_order_id(intent, intent_id),
            }
            if intent.quantity > 0:
                payload["qty"] = str(intent.quantity)
            elif getattr(intent, "order_value", 0.0) > 0:
                payload["notional"] = f"{intent.order_value:.2f}"
            return payload
        else:
            raise OrderExecutionError(f"Unsupported intent action: {intent.action}")

    @staticmethod
    def _client_order_id(intent: "TradeIntent", intent_id: str) -> str:
        prefix = f"{intent.strategy}-"
        return (intent_id if intent_id.startswith(prefix) else f"{prefix}{intent_id}")[:48]

    def get_order_by_client_id(self, client_order_id: str) -> dict | None:
        if not self.settings.api_key or not self.settings.secret_key:
            raise OrderExecutionError("Alpaca API credentials are not configured")
        try:
            response = self.session.get(
                f"{self.settings.trading_api_url}/v2/orders:by_client_order_id",
                headers={
                    "APCA-API-KEY-ID": self.settings.api_key,
                    "APCA-API-SECRET-KEY": self.settings.secret_key,
                },
                params={"client_order_id": client_order_id, "nested": "true"},
                timeout=self.settings.timeout_seconds,
            )
            if response.status_code == 404:
                return None
            response.raise_for_status()
            return response.json()
        except OrderExecutionError:
            raise
        except Exception as exc:
            raise OrderExecutionError(f"Alpaca order reconciliation failed: {exc}") from exc

    def get_order(self, order_id: str) -> dict | None:
        try:
            response = self.session.get(
                f"{self.settings.trading_api_url}/v2/orders/{order_id}",
                headers=self._headers(), params={"nested": "true"},
                timeout=self.settings.timeout_seconds,
            )
            if response.status_code == 404:
                return None
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            raise OrderExecutionError(f"Alpaca order lookup failed: {type(exc).__name__}") from exc

    def list_open_orders(self, symbol: str = "") -> list[dict]:
        params = {"status": "open", "nested": "true", "limit": 100}
        if symbol:
            params["symbols"] = symbol.upper()
        try:
            response = self.session.get(
                f"{self.settings.trading_api_url}/v2/orders",
                headers=self._headers(), params=params, timeout=self.settings.timeout_seconds,
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, list):
                raise ValueError("unexpected orders payload")
            return payload
        except Exception as exc:
            raise OrderExecutionError(f"Alpaca open-order lookup failed: {type(exc).__name__}") from exc

    def cancel_order(self, order_id: str):
        try:
            response = self.session.delete(
                f"{self.settings.trading_api_url}/v2/orders/{order_id}",
                headers=self._headers(), timeout=self.settings.timeout_seconds,
            )
            if response.status_code not in {204, 404}:
                response.raise_for_status()
        except Exception as exc:
            raise OrderExecutionError(f"Alpaca order cancellation failed: {type(exc).__name__}") from exc

    def submit_fractional_stop(self, symbol: str, quantity: float, stop_price: float, client_order_id: str) -> dict:
        payload = {
            "symbol": symbol.upper(), "qty": f"{quantity:.4f}", "side": "sell",
            "type": "stop", "time_in_force": "day", "stop_price": f"{stop_price:.2f}",
            "client_order_id": client_order_id[:48],
        }
        try:
            response = self.session.post(
                f"{self.settings.trading_api_url}/v2/orders",
                headers={**self._headers(), "Content-Type": "application/json"},
                data=json.dumps(payload), timeout=self.settings.timeout_seconds,
            )
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            raise OrderExecutionError(f"Alpaca fractional stop submission failed: {type(exc).__name__}") from exc

    def get_position(self, symbol: str) -> dict | None:
        if not self.settings.api_key or not self.settings.secret_key:
            raise OrderExecutionError("Alpaca API credentials are not configured")
        try:
            response = self.session.get(
                f"{self.settings.trading_api_url}/v2/positions/{symbol}",
                headers=self._headers(),
                timeout=self.settings.timeout_seconds,
            )
            if response.status_code == 404:
                return None
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            raise OrderExecutionError(f"Alpaca position reconciliation failed: {type(exc).__name__}") from exc

    def result_from_reconciliation(self, intent: "TradeIntent", intent_id: str, payload: dict) -> ExecutionResult:
        request_payload = self._build_order_payload(intent, intent_id)
        return ExecutionResult(
            intent_id=intent_id,
            timestamp=datetime.now(timezone.utc).isoformat(),
            strategy=intent.strategy,
            symbol=intent.symbol,
            action=intent.action,
            quantity=intent.quantity,
            requested_order_type=str(request_payload["type"]),
            requested_time_in_force=str(request_payload["time_in_force"]),
            dry_run=False,
            status=str(payload.get("status", "submitted")),
            broker="alpaca",
            broker_order_id=str(payload.get("id", "")),
            request_payload=request_payload,
            response_payload=payload,
        )

    def _validate_latest_price(self, intent: "TradeIntent"):
        if intent.action not in SUPPORTED_STOCK_ACTIONS or self.settings.max_quote_deviation_pct <= 0:
            return
        reference_price = float(getattr(intent, "reference_price", 0.0) or 0.0)
        if reference_price <= 0 and intent.quantity > 0:
            reference_price = float(intent.order_value) / float(intent.quantity)
        if reference_price <= 0:
            raise OrderPreflightError("A positive strategy reference price is required before submission")
        latest_price = 0.0
        timestamp = None
        try:
            response = self.session.get(
                f"{self.settings.data_api_url}/v2/stocks/{intent.symbol}/trades/latest",
                headers=self._headers(),
                params={"feed": os.getenv("ALPACA_DATA_FEED", "iex")},
                timeout=self.settings.timeout_seconds,
            )
            response.raise_for_status()
            trade = response.json().get("trade", {})
            latest_price = float(trade.get("p") or 0.0)
            timestamp = datetime.fromisoformat(str(trade.get("t", "")).replace("Z", "+00:00"))
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=timezone.utc)
        except Exception:
            pass

        now = datetime.now(timezone.utc)
        max_future_skew = float(os.getenv("MAX_QUOTE_FUTURE_SKEW_SECONDS", "5"))
        trade_age = (
            (now - timestamp.astimezone(timezone.utc)).total_seconds()
            if timestamp is not None else float("inf")
        )
        trade_is_fresh = (
            latest_price > 0
            and trade_age >= -max_future_skew
            and trade_age <= self.settings.max_quote_age_seconds
        )
        if not trade_is_fresh:
            try:
                response = self.session.get(
                    f"{self.settings.data_api_url}/v2/stocks/{intent.symbol}/quotes/latest",
                    headers=self._headers(),
                    params={"feed": os.getenv("ALPACA_DATA_FEED", "iex")},
                    timeout=self.settings.timeout_seconds,
                )
                response.raise_for_status()
                quote = response.json().get("quote", {})
                bid = float(quote.get("bp") or 0.0)
                ask = float(quote.get("ap") or 0.0)
                quote_timestamp = datetime.fromisoformat(str(quote.get("t", "")).replace("Z", "+00:00"))
                if quote_timestamp.tzinfo is None:
                    quote_timestamp = quote_timestamp.replace(tzinfo=timezone.utc)
                quote_age = (now - quote_timestamp.astimezone(timezone.utc)).total_seconds()
                spread_pct = (ask - bid) / ((ask + bid) / 2) if ask >= bid > 0 else float("inf")
                max_spread_pct = float(os.getenv("MAX_PREFLIGHT_SPREAD_PCT", "0.01"))
                if (
                    quote_age < -max_future_skew
                    or quote_age > self.settings.max_quote_age_seconds
                    or spread_pct > max_spread_pct
                ):
                    raise ValueError("quote is stale, crossed, or too wide")
                latest_price = ask if intent.action in {"buy", "buy_to_cover"} else bid
            except Exception as exc:
                raise OrderPreflightError(
                    "Latest Alpaca trade and executable quote are missing or stale"
                ) from exc
        deviation = abs(latest_price - reference_price) / reference_price
        if deviation > self.settings.max_quote_deviation_pct:
            raise OrderPreflightError(
                f"Quote deviation {deviation:.4f} exceeds limit {self.settings.max_quote_deviation_pct:.4f}"
            )

    def _headers(self) -> dict[str, str]:
        return {
            "APCA-API-KEY-ID": self.settings.api_key,
            "APCA-API-SECRET-KEY": self.settings.secret_key,
        }

    def _validate_short_eligibility(self, symbol: str):
        try:
            response = self.session.get(
                f"{self.settings.trading_api_url}/v2/assets/{symbol}",
                headers=self._headers(), timeout=self.settings.timeout_seconds,
            )
            response.raise_for_status()
            asset = response.json()
        except Exception as exc:
            raise OrderPreflightError(f"Short eligibility validation failed: {type(exc).__name__}") from exc
        if not asset.get("tradable") or not asset.get("shortable") or not asset.get("easy_to_borrow"):
            raise OrderPreflightError(f"{symbol} is not currently tradable, shortable, and easy-to-borrow")

    def _submit_order(self, payload: dict) -> dict:
        if not self.settings.api_key or not self.settings.secret_key:
            raise OrderExecutionError("Alpaca API credentials are not configured")

        # If a custom session (e.g. FakeSession for unit testing) was provided, use it
        if type(self.session).__name__ != "Session":
            try:
                response = self.session.post(
                    f"{self.settings.trading_api_url}/v2/orders",
                    headers={
                        "APCA-API-KEY-ID": self.settings.api_key,
                        "APCA-API-SECRET-KEY": self.settings.secret_key,
                        "Content-Type": "application/json",
                    },
                    data=json.dumps(payload),
                    timeout=self.settings.timeout_seconds,
                )
                response.raise_for_status()
                return response.json()
            except Exception as exc:
                raise OrderExecutionError(f"Alpaca order submission failed: {exc}") from exc

        try:
            from alpaca.trading.client import TradingClient
            from alpaca.trading.requests import (
                MarketOrderRequest,
                TakeProfitRequest,
                StopLossRequest,
            )
            from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass

            client = TradingClient(
                api_key=self.settings.api_key,
                secret_key=self.settings.secret_key,
                paper=self.settings.paper_trade,
            )

            req_kwargs = {
                "symbol": payload["symbol"],
                "side": OrderSide.BUY if payload["side"] == "buy" else OrderSide.SELL,
                "time_in_force": (
                    TimeInForce.GTC
                    if payload.get("time_in_force") == "gtc"
                    else TimeInForce.DAY
                ),
                "client_order_id": payload.get("client_order_id"),
            }
            if "qty" in payload:
                req_kwargs["qty"] = float(payload["qty"])
            elif "notional" in payload:
                req_kwargs["notional"] = float(payload["notional"])

            if payload.get("order_class") == "bracket":
                req_kwargs["order_class"] = OrderClass.BRACKET
                tp_val = float(payload["take_profit"]["limit_price"])
                sl_val = float(payload["stop_loss"]["stop_price"])
                req_kwargs["take_profit"] = TakeProfitRequest(limit_price=tp_val)
                req_kwargs["stop_loss"] = StopLossRequest(stop_price=sl_val)

            order_req = MarketOrderRequest(**req_kwargs)
            created_order = client.submit_order(order_req)
            return json.loads(created_order.model_dump_json() if hasattr(created_order, "model_dump_json") else json.dumps(created_order.__dict__, default=str))

        except Exception as exc:
            try:
                response = self.session.post(
                    f"{self.settings.trading_api_url}/v2/orders",
                    headers={
                        "APCA-API-KEY-ID": self.settings.api_key,
                        "APCA-API-SECRET-KEY": self.settings.secret_key,
                        "Content-Type": "application/json",
                    },
                    data=json.dumps(payload),
                    timeout=self.settings.timeout_seconds,
                )
                response.raise_for_status()
                return response.json()
            except Exception as inner_exc:
                raise OrderExecutionError(f"Alpaca order submission failed: {exc}") from exc


class RobinhoodOrderExecutor:
    def __init__(self, username: str, password: str, totp_secret: str = "", dry_run: bool = True):
        self.username = username
        self.password = password
        self.totp_secret = totp_secret
        self.dry_run = dry_run

    @classmethod
    def from_env(cls):
        dry_run = os.getenv("ROBINHOOD_ORDER_DRY_RUN", os.getenv("ALPACA_ORDER_DRY_RUN", "true")).strip().lower() == "true"
        return cls(
            username=os.getenv("ROBINHOOD_USERNAME", "").strip(),
            password=os.getenv("ROBINHOOD_PASSWORD", "").strip(),
            totp_secret=os.getenv("ROBINHOOD_TOTP_SECRET", "").strip(),
            dry_run=dry_run,
        )

    def execute(self, intent: "TradeIntent", intent_id: str | None = None) -> ExecutionResult:
        resolved_intent_id = intent_id or str(uuid.uuid4())
        timestamp = datetime.now(timezone.utc).isoformat()

        if intent.action not in {"buy", "sell"}:
            return ExecutionResult(
                intent_id=resolved_intent_id,
                timestamp=timestamp,
                strategy=intent.strategy,
                symbol=intent.symbol,
                action=intent.action,
                quantity=intent.quantity,
                requested_order_type="market",
                requested_time_in_force="day",
                dry_run=self.dry_run,
                status="unsupported_action",
                broker="robinhood",
                request_payload=None,
                error_message=f"Action {intent.action} is not supported for Robinhood execution",
            )

        payload = {
            "symbol": intent.symbol,
            "qty": intent.quantity,
            "side": intent.action,
            "type": "market",
            "time_in_force": "day",
        }

        if self.dry_run:
            return ExecutionResult(
                intent_id=resolved_intent_id,
                timestamp=timestamp,
                strategy=intent.strategy,
                symbol=intent.symbol,
                action=intent.action,
                quantity=intent.quantity,
                requested_order_type="market",
                requested_time_in_force="day",
                dry_run=True,
                status="dry_run",
                broker="robinhood",
                request_payload=payload,
            )

        if not self.username or not self.password:
            raise OrderExecutionError("Robinhood username and password must be configured")

        try:
            if self.totp_secret:
                totp = pyotp.TOTP(self.totp_secret)
                totp_code = totp.now()
                r.login(username=self.username, password=self.password, store_session=False, mfa_code=totp_code)
            else:
                r.login(username=self.username, password=self.password, store_session=False)

            if intent.action == "buy":
                order_response = r.orders.order_buy_market(symbol=intent.symbol, quantity=intent.quantity)
            else:
                order_response = r.orders.order_sell_market(symbol=intent.symbol, quantity=intent.quantity)

            # Logout
            r.logout()

            if order_response is None or (isinstance(order_response, dict) and "id" not in order_response and "detail" in order_response):
                detail = order_response.get("detail", "Unknown error") if order_response else "No response"
                raise OrderExecutionError(f"Robinhood order failed: {detail}")

            broker_order_id = ""
            if isinstance(order_response, dict):
                broker_order_id = str(order_response.get("id", ""))

            return ExecutionResult(
                intent_id=resolved_intent_id,
                timestamp=timestamp,
                strategy=intent.strategy,
                symbol=intent.symbol,
                action=intent.action,
                quantity=intent.quantity,
                requested_order_type="market",
                requested_time_in_force="day",
                dry_run=False,
                status="submitted",
                broker="robinhood",
                broker_order_id=broker_order_id,
                request_payload=payload,
                response_payload=order_response if isinstance(order_response, dict) else {"response": str(order_response)},
            )

        except Exception as exc:
            raise OrderExecutionError(f"Robinhood order execution failed: {exc}") from exc
