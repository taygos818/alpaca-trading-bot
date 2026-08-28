import importlib.util
import sys
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace


try:
    import requests
except ModuleNotFoundError:
    requests = types.ModuleType("requests")

    class RequestException(Exception):
        pass

    class HTTPError(RequestException):
        pass

    class Session:
        pass

    requests.HTTPError = HTTPError
    requests.RequestException = RequestException
    requests.Session = Session
    sys.modules["requests"] = requests


SERVICE_DIR = Path(__file__).resolve().parents[1] / "services" / "strategy-engine"
sys.path.insert(0, str(SERVICE_DIR))
MODULE_PATH = SERVICE_DIR / "utils" / "execution.py"
SPEC = importlib.util.spec_from_file_location("execution", MODULE_PATH)
execution = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(execution)

AlpacaOrderExecutor = execution.AlpacaOrderExecutor
ExecutionResult = execution.ExecutionResult
ExecutionSettings = execution.ExecutionSettings
OrderExecutionError = execution.OrderExecutionError


class FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200):
        self.payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"status={self.status_code}")

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, payload: dict | None = None, status_code: int = 200):
        self.payload = payload or {"id": "alpaca-order-1", "status": "accepted"}
        self.status_code = status_code
        self.calls: list[dict] = []

    def post(self, url, headers=None, data=None, timeout=None):
        self.calls.append(
            {
                "url": url,
                "headers": headers,
                "data": data,
                "timeout": timeout,
            }
        )
        return FakeResponse(self.payload, status_code=self.status_code)

    def get(self, url, headers=None, params=None, timeout=None):
        self.calls.append({"url": url, "headers": headers, "params": params, "timeout": timeout})
        return FakeResponse(self.payload, status_code=self.status_code)


class ExecutionTests(unittest.TestCase):
    def make_intent(self, **overrides):
        payload = {
            "strategy": "tier2_swing",
            "symbol": "SPY",
            "action": "buy",
            "quantity": 3,
            "order_value": 1000.0,
            "account_nav": 100000.0,
        }
        payload.update(overrides)
        return SimpleNamespace(**payload)

    def test_dry_run_persists_payload_without_submission(self):
        session = FakeSession()
        executor = AlpacaOrderExecutor(
            settings=ExecutionSettings(
                api_key="key",
                secret_key="secret",
                trading_api_url="https://paper-api.alpaca.markets",
                paper_trade=True,
                dry_run=True,
                timeout_seconds=10,
            ),
            session=session,
        )

        result = executor.execute(self.make_intent(), intent_id="intent-1")

        self.assertEqual(result.status, "dry_run")
        self.assertTrue(result.dry_run)
        self.assertEqual(result.request_payload["client_order_id"], "tier2_swing-intent-1")
        self.assertEqual(session.calls, [])

    def test_live_submission_posts_to_alpaca(self):
        session = FakeSession(payload={"id": "alpaca-123", "status": "accepted"})
        executor = AlpacaOrderExecutor(
            settings=ExecutionSettings(
                api_key="key",
                secret_key="secret",
                trading_api_url="https://paper-api.alpaca.markets",
                paper_trade=True,
                dry_run=False,
                timeout_seconds=10,
            ),
            session=session,
        )

        result = executor.execute(self.make_intent(), intent_id="intent-2")

        self.assertEqual(result.status, "submitted")
        self.assertEqual(result.broker_order_id, "alpaca-123")
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(session.calls[0]["url"], "https://paper-api.alpaca.markets/v2/orders")

    def test_reconciliation_reads_order_by_client_id(self):
        session = FakeSession(payload={"id": "alpaca-123", "status": "accepted"})
        executor = AlpacaOrderExecutor(
            ExecutionSettings("key", "secret", "https://paper-api.alpaca.markets", True, False, 10),
            session=session,
        )
        result = executor.get_order_by_client_id("tier2_swing-abc")
        self.assertEqual(result["id"], "alpaca-123")
        self.assertEqual(
            session.calls[0]["params"],
            {"client_order_id": "tier2_swing-abc", "nested": "true"},
        )

    def test_live_submission_blocks_excessive_quote_deviation(self):
        class QuoteSession(FakeSession):
            def get(self, url, headers=None, params=None, timeout=None):
                return FakeResponse({"trade": {"p": 120.0, "t": datetime.now(timezone.utc).isoformat()}})

        executor = AlpacaOrderExecutor(
            ExecutionSettings(
                "key", "secret", "https://paper-api.alpaca.markets", True, False, 10,
                max_quote_deviation_pct=0.01, max_quote_age_seconds=15,
            ),
            session=QuoteSession(),
        )
        with self.assertRaisesRegex(OrderExecutionError, "Quote deviation"):
            executor.execute(self.make_intent(order_value=300.0), intent_id="intent-deviation")

    def test_stale_trade_uses_fresh_executable_quote(self):
        class QuoteFallbackSession(FakeSession):
            def get(self, url, headers=None, params=None, timeout=None):
                if url.endswith("/trades/latest"):
                    return FakeResponse({"trade": {"p": 100.0, "t": "2026-08-26T13:00:00Z"}})
                if url.endswith("/quotes/latest"):
                    now = datetime.now(timezone.utc).isoformat()
                    return FakeResponse({"quote": {"bp": 99.95, "ap": 100.05, "t": now}})
                return super().get(url, headers=headers, params=params, timeout=timeout)

        session = QuoteFallbackSession(payload={"id": "alpaca-quote-fallback", "status": "accepted"})
        executor = AlpacaOrderExecutor(
            ExecutionSettings(
                "key", "secret", "https://paper-api.alpaca.markets", True, False, 10,
                max_quote_deviation_pct=0.01, max_quote_age_seconds=15,
            ),
            session=session,
        )
        result = executor.execute(
            self.make_intent(order_value=300.0, reference_price=100.0),
            intent_id="intent-quote-fallback",
        )
        self.assertEqual(result.status, "submitted")

    def test_stale_trade_does_not_accept_stale_quote(self):
        class StaleSession(FakeSession):
            def get(self, url, headers=None, params=None, timeout=None):
                if url.endswith("/trades/latest"):
                    return FakeResponse({"trade": {"p": 100.0, "t": "2026-08-26T13:00:00Z"}})
                return FakeResponse({"quote": {"bp": 99.95, "ap": 100.05, "t": "2026-08-26T13:00:00Z"}})

        executor = AlpacaOrderExecutor(
            ExecutionSettings(
                "key", "secret", "https://paper-api.alpaca.markets", True, False, 10,
                max_quote_deviation_pct=0.01, max_quote_age_seconds=15,
            ),
            session=StaleSession(),
        )
        with self.assertRaisesRegex(OrderExecutionError, "trade and executable quote"):
            executor.execute(
                self.make_intent(order_value=300.0, reference_price=100.0),
                intent_id="intent-stale-all",
            )

    def test_unsupported_action_is_marked_unsupported(self):
        executor = AlpacaOrderExecutor(
            settings=ExecutionSettings(
                api_key="key",
                secret_key="secret",
                trading_api_url="https://paper-api.alpaca.markets",
                paper_trade=True,
                dry_run=True,
                timeout_seconds=10,
            ),
            session=FakeSession(),
        )

        result = executor.execute(self.make_intent(action="exercise"), intent_id="intent-3")

        self.assertEqual(result.status, "unsupported_action")
        self.assertIn("not yet mapped", result.error_message)

    def test_options_style_action_is_supported_in_dry_run(self):
        executor = AlpacaOrderExecutor(
            settings=ExecutionSettings(
                api_key="key",
                secret_key="secret",
                trading_api_url="https://paper-api.alpaca.markets",
                paper_trade=True,
                dry_run=True,
                timeout_seconds=10,
            ),
            session=FakeSession(),
        )

        result = executor.execute(self.make_intent(action="sell_to_open"), intent_id="intent-6")

        self.assertEqual(result.status, "dry_run")
        self.assertTrue(result.dry_run)
        self.assertEqual(result.request_payload["position_intent"], "sell_to_open")

    def test_short_payload_is_sell_bracket_in_dry_run(self):
        executor = AlpacaOrderExecutor(
            ExecutionSettings("key", "secret", "https://paper-api.alpaca.markets", True, True, 10),
            session=FakeSession(),
        )
        intent = self.make_intent(action="sell_short", stop_loss_price=105.0, take_profit_price=90.0)
        result = executor.execute(intent, intent_id="short-1")
        self.assertEqual(result.request_payload["side"], "sell")
        self.assertEqual(result.request_payload["order_class"], "bracket")

    def test_short_submission_requires_current_easy_to_borrow_asset(self):
        class ShortSession(FakeSession):
            def get(self, url, headers=None, params=None, timeout=None):
                if "/v2/assets/" in url:
                    return FakeResponse({"tradable": True, "shortable": False, "easy_to_borrow": False})
                return FakeResponse({"trade": {"p": 100, "t": datetime.now(timezone.utc).isoformat()}})

        executor = AlpacaOrderExecutor(
            ExecutionSettings("key", "secret", "https://paper-api.alpaca.markets", True, False, 10,
                              max_quote_deviation_pct=0.01, max_quote_age_seconds=15),
            session=ShortSession(),
        )
        with self.assertRaisesRegex(OrderExecutionError, "not currently tradable"):
            executor.execute(self.make_intent(action="sell_short"), intent_id="short-2")

    def test_zero_quantity_raises(self):
        executor = AlpacaOrderExecutor(
            settings=ExecutionSettings(
                api_key="key",
                secret_key="secret",
                trading_api_url="https://paper-api.alpaca.markets",
                paper_trade=True,
                dry_run=True,
                timeout_seconds=10,
            ),
            session=FakeSession(),
        )

        with self.assertRaises(OrderExecutionError):
            executor.execute(self.make_intent(quantity=0, order_value=0.0), intent_id="intent-4")

    def test_submission_http_error_is_wrapped(self):
        executor = AlpacaOrderExecutor(
            settings=ExecutionSettings(
                api_key="key",
                secret_key="secret",
                trading_api_url="https://paper-api.alpaca.markets",
                paper_trade=True,
                dry_run=False,
                timeout_seconds=10,
            ),
            session=FakeSession(status_code=403),
        )

        with self.assertRaises(OrderExecutionError):
            executor.execute(self.make_intent(), intent_id="intent-5")

    def test_bracket_order_payload_building(self):
        session = FakeSession()
        executor = AlpacaOrderExecutor(
            settings=ExecutionSettings(
                api_key="key",
                secret_key="secret",
                trading_api_url="https://paper-api.alpaca.markets",
                paper_trade=True,
                dry_run=True,
                timeout_seconds=10,
            ),
            session=session,
        )

        intent = self.make_intent(
            action="buy",
            stop_loss_price=95.5,
            take_profit_price=105.5
        )

        result = executor.execute(intent, intent_id="intent-bracket-1")

        self.assertEqual(result.status, "dry_run")
        payload = result.request_payload
        self.assertEqual(payload["order_class"], "bracket")
        self.assertEqual(payload["time_in_force"], "gtc")
        self.assertEqual(payload["take_profit"]["limit_price"], "105.50")
        self.assertEqual(payload["stop_loss"]["stop_price"], "95.50")

    def test_fractional_entry_is_submitted_as_simple_day_order(self):
        session = FakeSession()
        executor = AlpacaOrderExecutor(
            settings=ExecutionSettings(
                api_key="key", secret_key="secret",
                trading_api_url="https://paper-api.alpaca.markets",
                paper_trade=True, dry_run=True, timeout_seconds=10,
            ),
            session=session,
        )
        intent = self.make_intent(
            action="buy", quantity=0.25, order_value=75.0,
            stop_loss_price=285.0, take_profit_price=330.0,
        )

        result = executor.execute(intent, intent_id="fractional-entry")

        self.assertEqual(result.request_payload["qty"], "0.25")
        self.assertEqual(result.request_payload["time_in_force"], "day")
        self.assertNotIn("order_class", result.request_payload)
        self.assertEqual(session.calls, [])

    def test_live_bracket_order_submission(self):
        session = FakeSession(payload={"id": "alpaca-bracket-999", "status": "accepted"})
        executor = AlpacaOrderExecutor(
            settings=ExecutionSettings(
                api_key="key",
                secret_key="secret",
                trading_api_url="https://paper-api.alpaca.markets",
                paper_trade=True,
                dry_run=False,
                timeout_seconds=10,
            ),
            session=session,
        )

        intent = self.make_intent(
            action="buy",
            stop_loss_price=98.123,
            take_profit_price=102.456
        )

        result = executor.execute(intent, intent_id="intent-bracket-2")

        self.assertEqual(result.status, "submitted")
        self.assertEqual(result.broker_order_id, "alpaca-bracket-999")
        self.assertEqual(len(session.calls), 1)
        
        import json
        payload = json.loads(session.calls[0]["data"])
        self.assertEqual(payload["order_class"], "bracket")
        self.assertEqual(payload["time_in_force"], "gtc")
        self.assertEqual(payload["take_profit"]["limit_price"], "102.46") # Rounding check
        self.assertEqual(payload["stop_loss"]["stop_price"], "98.12")


from unittest.mock import patch

class RobinhoodExecutionTests(unittest.TestCase):
    def make_intent(self, **overrides):
        payload = {
            "strategy": "tier2_swing",
            "symbol": "SPY",
            "action": "buy",
            "quantity": 3,
            "order_value": 1000.0,
            "account_nav": 100000.0,
        }
        payload.update(overrides)
        return SimpleNamespace(**payload)

    @patch.object(execution, "r")
    @patch.object(execution, "pyotp")
    def test_dry_run_persists_payload_without_submission(self, mock_pyotp, mock_robin):
        RobinhoodOrderExecutor = execution.RobinhoodOrderExecutor
        executor = RobinhoodOrderExecutor(
            username="user",
            password="pwd",
            totp_secret="TOTP_123",
            dry_run=True,
        )

        result = executor.execute(self.make_intent(), intent_id="intent-1")

        self.assertEqual(result.status, "dry_run")
        self.assertTrue(result.dry_run)
        self.assertEqual(result.broker, "robinhood")
        self.assertEqual(result.request_payload["symbol"], "SPY")
        mock_robin.login.assert_not_called()

    @patch.object(execution, "r")
    @patch.object(execution, "pyotp")
    def test_live_submission_posts_to_robinhood(self, mock_pyotp, mock_robin):
        mock_robin.orders.order_buy_market.return_value = {"id": "rh-123", "status": "submitted"}
        mock_totp_instance = mock_pyotp.TOTP.return_value
        mock_totp_instance.now.return_value = "123456"

        RobinhoodOrderExecutor = execution.RobinhoodOrderExecutor
        executor = RobinhoodOrderExecutor(
            username="user",
            password="pwd",
            totp_secret="TOTP_123",
            dry_run=False,
        )

        result = executor.execute(self.make_intent(), intent_id="intent-2")

        self.assertEqual(result.status, "submitted")
        self.assertEqual(result.broker_order_id, "rh-123")
        mock_robin.login.assert_called_once_with(
            username="user", password="pwd", store_session=False, mfa_code="123456"
        )
        mock_robin.orders.order_buy_market.assert_called_once_with(symbol="SPY", quantity=3)
        mock_robin.logout.assert_called_once()

    def test_unsupported_action_is_marked_unsupported(self):
        RobinhoodOrderExecutor = execution.RobinhoodOrderExecutor
        executor = RobinhoodOrderExecutor(
            username="user",
            password="pwd",
            dry_run=True,
        )

        result = executor.execute(self.make_intent(action="sell_to_open"), intent_id="intent-3")

        self.assertEqual(result.status, "unsupported_action")
        self.assertIn("not supported for Robinhood", result.error_message)


if __name__ == "__main__":
    unittest.main()
