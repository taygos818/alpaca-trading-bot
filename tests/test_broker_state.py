import importlib.util
import sys
import types
import unittest
from pathlib import Path


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
MODULE_PATH = SERVICE_DIR / "utils" / "broker_state.py"
SPEC = importlib.util.spec_from_file_location("broker_state", MODULE_PATH)
broker_state = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(broker_state)

AlpacaBrokerStateClient = broker_state.AlpacaBrokerStateClient
BrokerStateError = broker_state.BrokerStateError
BrokerStateSettings = broker_state.BrokerStateSettings


class FakeResponse:
    def __init__(self, payload, status_code: int = 200):
        self.payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"status={self.status_code}")

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, responses: dict[str, object], status_codes: dict[str, int] | None = None):
        self.responses = responses
        self.status_codes = status_codes or {}
        self.calls: list[str] = []

    def get(self, url, headers=None, timeout=None):
        self.calls.append(url)
        return FakeResponse(self.responses[url], status_code=self.status_codes.get(url, 200))


class BrokerStateTests(unittest.TestCase):
    def make_client(self, responses):
        return AlpacaBrokerStateClient(
            settings=BrokerStateSettings(
                api_key="key",
                secret_key="secret",
                trading_api_url="https://paper-api.alpaca.markets",
                timeout_seconds=10,
                require_for_trades=True,
            ),
            session=FakeSession(responses),
        )

    def test_fetch_reads_account_and_positions(self):
        client = self.make_client(
            {
                "https://paper-api.alpaca.markets/v2/account": {
                    "id": "acct-1",
                    "equity": "102345.67",
                    "buying_power": "50000",
                    "trading_blocked": False,
                },
                "https://paper-api.alpaca.markets/v2/positions": [
                    {"symbol": "SPY", "qty": "5", "market_value": "2750.50", "current_price": "550.10"},
                    {"symbol": "QQQ", "qty": "2", "market_value": "950.25"},
                ],
                "https://paper-api.alpaca.markets/v2/orders?status=open&nested=true": [
                    {"symbol": "SPY", "qty": "2", "filled_qty": "1", "limit_price": "551"}
                ],
            }
        )

        state = client.fetch()

        self.assertEqual(state.account_id, "acct-1")
        self.assertAlmostEqual(state.account_nav, 102345.67)
        self.assertAlmostEqual(state.buying_power, 50000.0)
        self.assertFalse(state.trading_blocked)
        self.assertAlmostEqual(state.get_position_market_value("SPY"), 2750.50)
        self.assertAlmostEqual(state.get_position_market_value("AAPL"), 0.0)
        self.assertAlmostEqual(state.open_order_exposure, 551.0)

    def test_fetch_accepts_portfolio_value_fallback(self):
        client = self.make_client(
            {
                "https://paper-api.alpaca.markets/v2/account": {
                    "id": "acct-2",
                    "portfolio_value": "99999.99",
                    "buying_power": "12345",
                    "trading_blocked": True,
                },
                "https://paper-api.alpaca.markets/v2/positions": [],
                "https://paper-api.alpaca.markets/v2/orders?status=open&nested=true": [],
            }
        )

        state = client.fetch()

        self.assertAlmostEqual(state.account_nav, 99999.99)
        self.assertTrue(state.trading_blocked)

    def test_fetch_raises_without_credentials(self):
        client = AlpacaBrokerStateClient(
            settings=BrokerStateSettings(
                api_key="",
                secret_key="",
                trading_api_url="https://paper-api.alpaca.markets",
                timeout_seconds=10,
                require_for_trades=True,
            ),
            session=FakeSession({}),
        )

        with self.assertRaises(BrokerStateError):
            client.fetch()

    def test_fetch_wraps_http_errors(self):
        url = "https://paper-api.alpaca.markets/v2/account"
        client = AlpacaBrokerStateClient(
            settings=BrokerStateSettings(
                api_key="key",
                secret_key="secret",
                trading_api_url="https://paper-api.alpaca.markets",
                timeout_seconds=10,
                require_for_trades=True,
            ),
            session=FakeSession(
                responses={
                    url: {"message": "forbidden"},
                    "https://paper-api.alpaca.markets/v2/positions": [],
                },
                status_codes={url: 403},
            ),
        )

        with self.assertRaises(BrokerStateError):
            client.fetch()


from unittest.mock import patch

class RobinhoodBrokerStateTests(unittest.TestCase):
    @patch.object(broker_state, "r")
    @patch.object(broker_state, "pyotp")
    def test_fetch_reads_portfolio_and_holdings(self, mock_pyotp, mock_robin):
        mock_robin.profiles.load_portfolio_profile.return_value = {
            "equity": "50000.00"
        }
        mock_robin.profiles.load_account_profile.return_value = {
            "cash": "10000.00",
            "buying_power": "40000.00",
            "account_number": "12345"
        }
        mock_robin.account.build_holdings.return_value = {
            "AAPL": {
                "quantity": "10.0000",
                "equity": "1800.00",
                "average_buy_price": "170.00",
                "price": "180.00"
            }
        }
        
        # Specifying mock totp key behavior
        mock_totp_instance = mock_pyotp.TOTP.return_value
        mock_totp_instance.now.return_value = "123456"

        RobinhoodBrokerStateClient = broker_state.RobinhoodBrokerStateClient
        client = RobinhoodBrokerStateClient(username="test_user", password="test_password", totp_secret="TOTP_123")
        
        state = client.fetch()
        
        mock_robin.login.assert_called_once_with(
            username="test_user", password="test_password", store_session=False, mfa_code="123456"
        )
        self.assertEqual(state.account_id, "test_user")
        self.assertAlmostEqual(state.account_nav, 50000.0)
        self.assertAlmostEqual(state.buying_power, 40000.0)
        self.assertAlmostEqual(state.get_position_market_value("AAPL"), 1800.0)
        mock_robin.logout.assert_called_once()

    def test_fetch_raises_without_credentials(self):
        RobinhoodBrokerStateClient = broker_state.RobinhoodBrokerStateClient
        client = RobinhoodBrokerStateClient(username="", password="")
        with self.assertRaises(BrokerStateError):
            client.fetch()


if __name__ == "__main__":
    unittest.main()
