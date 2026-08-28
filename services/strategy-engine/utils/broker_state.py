import os
from dataclasses import dataclass
from datetime import datetime, timezone

import requests
import pyotp
import robin_stocks.robinhood as r
from utils.alpaca_credentials import alpaca_credentials



class BrokerStateError(RuntimeError):
    pass


@dataclass(frozen=True)
class BrokerStateSettings:
    api_key: str
    secret_key: str
    trading_api_url: str
    timeout_seconds: float
    require_for_trades: bool

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
            timeout_seconds=float(os.getenv("ALPACA_BROKER_STATE_TIMEOUT_SECONDS", "10")),
            require_for_trades=os.getenv("REQUIRE_BROKER_STATE_FOR_TRADES", "true").strip().lower() == "true",
        )


@dataclass(frozen=True)
class BrokerPosition:
    symbol: str
    qty: float
    market_value: float
    avg_entry_price: float = 0.0
    current_price: float = 0.0


@dataclass(frozen=True)
class BrokerState:
    timestamp: str
    account_id: str
    account_nav: float
    buying_power: float
    trading_blocked: bool
    positions: dict[str, BrokerPosition]
    cash: float = 0.0
    last_equity: float = 0.0
    open_order_exposure: float = 0.0
    open_order_count: int = 0

    def get_position_market_value(self, symbol: str) -> float:
        position = self.positions.get(symbol.upper())
        return position.market_value if position is not None else 0.0

    def get_position_quantity(self, symbol: str) -> float:
        position = self.positions.get(symbol.upper())
        return position.qty if position is not None else 0.0

    @property
    def gross_exposure(self) -> float:
        return sum(abs(position.market_value) for position in self.positions.values())


class AlpacaBrokerStateClient:
    def __init__(self, settings: BrokerStateSettings, session: requests.Session | None = None):
        self.settings = settings
        self.session = session or requests.Session()

    @classmethod
    def from_env(cls):
        return cls(BrokerStateSettings.from_env())

    def fetch(self) -> BrokerState:
        if not self.settings.api_key or not self.settings.secret_key:
            raise BrokerStateError("Alpaca API credentials are not configured")

        headers = {
            "APCA-API-KEY-ID": self.settings.api_key,
            "APCA-API-SECRET-KEY": self.settings.secret_key,
            "accept": "application/json",
        }
        account_payload = self._get_json("/v2/account", headers)
        positions_payload = self._get_json("/v2/positions", headers)
        orders_payload = self._get_json("/v2/orders?status=open&nested=true", headers)

        equity_raw = account_payload.get("equity") or account_payload.get("portfolio_value")
        if equity_raw in (None, ""):
            raise BrokerStateError("Account response did not include equity or portfolio_value")

        positions: dict[str, BrokerPosition] = {}
        for item in positions_payload:
            symbol = str(item.get("symbol", "")).upper()
            if not symbol:
                continue
            qty = float(item.get("qty") or 0.0)
            market_value = float(item.get("market_value") or 0.0)
            avg_entry_price = float(item.get("avg_entry_price") or 0.0)
            current_price = float(item.get("current_price") or 0.0)
            positions[symbol] = BrokerPosition(
                symbol=symbol,
                qty=qty,
                market_value=market_value,
                avg_entry_price=avg_entry_price,
                current_price=current_price
            )

        open_order_exposure = 0.0
        for order in orders_payload:
            notional = float(order.get("notional") or 0.0)
            if notional > 0:
                open_order_exposure += notional
                continue
            remaining_qty = max(float(order.get("qty") or 0.0) - float(order.get("filled_qty") or 0.0), 0.0)
            if remaining_qty == 0:
                continue
            price = float(order.get("limit_price") or order.get("stop_price") or 0.0)
            if price <= 0:
                position = positions.get(str(order.get("symbol", "")).upper())
                price = position.current_price if position is not None else 0.0
            if price <= 0:
                raise BrokerStateError("Open order exposure cannot be valued from the broker snapshot")
            open_order_exposure += remaining_qty * price

        return BrokerState(
            timestamp=datetime.now(timezone.utc).isoformat(),
            account_id=str(account_payload.get("id", "")),
            account_nav=float(equity_raw),
            buying_power=float(account_payload.get("buying_power") or 0.0),
            trading_blocked=bool(account_payload.get("trading_blocked", False)),
            positions=positions,
            cash=float(account_payload.get("cash") or 0.0),
            last_equity=float(account_payload.get("last_equity") or equity_raw),
            open_order_exposure=open_order_exposure,
            open_order_count=len(orders_payload),
        )

    def _get_json(self, path: str, headers: dict[str, str]):
        try:
            response = self.session.get(
                f"{self.settings.trading_api_url}{path}",
                headers=headers,
                timeout=self.settings.timeout_seconds,
            )
            response.raise_for_status()
            payload = response.json()
        except requests.RequestException as exc:
            raise BrokerStateError(f"Alpaca broker state request failed for {path}: {exc}") from exc
        if not isinstance(payload, (list, dict)):
            raise BrokerStateError(f"Unexpected payload type for {path}")
        return payload


class RobinhoodBrokerStateClient:
    def __init__(self, username: str, password: str, totp_secret: str = ""):
        self.username = username
        self.password = password
        self.totp_secret = totp_secret

    @classmethod
    def from_env(cls):
        return cls(
            username=os.getenv("ROBINHOOD_USERNAME", "").strip(),
            password=os.getenv("ROBINHOOD_PASSWORD", "").strip(),
            totp_secret=os.getenv("ROBINHOOD_TOTP_SECRET", "").strip(),
        )

    def fetch(self) -> BrokerState:
        if not self.username or not self.password:
            raise BrokerStateError("Robinhood username and password must be configured")

        try:
            if self.totp_secret:
                totp = pyotp.TOTP(self.totp_secret)
                totp_code = totp.now()
                r.login(username=self.username, password=self.password, store_session=False, mfa_code=totp_code)
            else:
                r.login(username=self.username, password=self.password, store_session=False)

            portfolio_profile = r.profiles.load_portfolio_profile()
            account_profile = r.profiles.load_account_profile()

            equity = float(portfolio_profile.get("equity") or portfolio_profile.get("portfolio_value") or 0.0)
            cash = float(account_profile.get("cash") or 0.0)
            buying_power = float(account_profile.get("buying_power") or account_profile.get("margin_limit") or 0.0)
            trading_blocked = False

            holdings = r.account.build_holdings()
            positions: dict[str, BrokerPosition] = {}
            for symbol, details in holdings.items():
                qty = float(details.get("quantity") or 0.0)
                market_value = float(details.get("equity") or 0.0)
                avg_entry_price = float(details.get("average_buy_price") or 0.0)
                current_price = float(details.get("price") or 0.0)

                positions[symbol.upper()] = BrokerPosition(
                    symbol=symbol.upper(),
                    qty=qty,
                    market_value=market_value,
                    avg_entry_price=avg_entry_price,
                    current_price=current_price
                )

            r.logout()

            return BrokerState(
                timestamp=datetime.now(timezone.utc).isoformat(),
                account_id=self.username,
                account_nav=equity,
                buying_power=buying_power,
                trading_blocked=trading_blocked,
                positions=positions,
                cash=cash,
                last_equity=float(portfolio_profile.get("equity_previous_close") or equity),
            )

        except Exception as exc:
            raise BrokerStateError(f"Robinhood broker state fetch failed: {exc}") from exc
